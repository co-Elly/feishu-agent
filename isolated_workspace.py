"""Task-scoped staging workspace with validated merge and rollback."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time


EXCLUDED_DIRS = {".git", ".tools", ".venv", "venv", "workspace", "roundtable",
                 "__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".log", ".db", ".db-wal", ".db-shm", ".pyc"}
EXCLUDED_FILES = {"config.json", "chat_log.jsonl", "chat_history.json",
                  "persistent_memory.json"}
SOURCE_GUARD_EXCLUDED_FILES = EXCLUDED_FILES - {"config.json"}
EXCLUDED_RELATIVE_PATHS = {"_guardian.ps1", "scripts/restart_bot.ps1"}


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path):
    normalized = str(path).replace("\\", "/").lstrip("./")
    if not normalized or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise ValueError(f"不安全的相对路径: {path}")
    return normalized


class IsolatedWorkspace:
    def __init__(self, source_root, execution_root, task_id, python_executable):
        self.source_root = os.path.abspath(source_root)
        execution_root = os.path.abspath(execution_root)
        if os.path.commonpath([self.source_root, execution_root]) == self.source_root:
            raise ValueError("隔离执行目录不得位于主项目目录内部")
        self.root = os.path.abspath(os.path.join(execution_root, str(task_id), "stage"))
        self.python_executable = os.path.abspath(python_executable)
        self._rollback = {}
        self._merged = []
        self.execution_dir = os.path.dirname(self.root)
        self.journal_path = os.path.join(self.execution_dir, "merge-journal.json")
        self.backup_dir = os.path.join(self.execution_dir, "merge-backup")

    def prepare(self):
        if os.path.exists(self.root):
            shutil.rmtree(self.root, onerror=self._remove_readonly)
        os.makedirs(self.root, exist_ok=False)
        for current, dirs, files in os.walk(self.source_root):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
            relative_dir = os.path.relpath(current, self.source_root)
            target_dir = self.root if relative_dir == "." else os.path.join(self.root, relative_dir)
            os.makedirs(target_dir, exist_ok=True)
            for name in files:
                if name in EXCLUDED_FILES or any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
                    continue
                source_path = os.path.join(current, name)
                relative = os.path.relpath(source_path, self.source_root).replace("\\", "/")
                if relative in EXCLUDED_RELATIVE_PATHS:
                    continue
                shutil.copy2(source_path, os.path.join(target_dir, name))
        example = os.path.join(self.root, "config.example.json")
        if os.path.isfile(example):
            shutil.copy2(example, os.path.join(self.root, "config.json"))
        return self

    @staticmethod
    def _remove_readonly(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    def snapshot(self):
        result = {}
        for current, dirs, files in os.walk(self.root):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS and name != ".pytest-temp"]
            for name in files:
                if name == "config.json" or any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
                    continue
                path = os.path.join(current, name)
                relative = os.path.relpath(path, self.root).replace("\\", "/")
                result[relative] = _hash_file(path)
        return result

    @staticmethod
    def changed(before, after):
        return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))

    def test_command(self):
        return [self.python_executable, "-m", "pytest", "-q", os.path.join(self.root, "tests"),
                "--basetemp", os.path.join(self.root, ".pytest-temp")]

    def run_tests(self, timeout=900):
        command = self.test_command()
        proc = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, shell=False)
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        return {"command": subprocess.list2cmdline(command), "exit_code": proc.returncode,
                "passed": proc.returncode == 0, "blocked": False, "output_tail": output[-2000:]}

    def merge(self, changed_paths):
        """Apply staged paths to source while retaining exact bytes for rollback."""
        self._rollback, self._merged = {}, []
        os.makedirs(self.backup_dir, exist_ok=True)
        manifest = {"status": "merging", "source_root": self.source_root, "entries": []}
        for raw in changed_paths:
            relative = _safe_relative(raw)
            staged = os.path.realpath(os.path.join(self.root, relative))
            target = os.path.realpath(os.path.join(self.source_root, relative))
            if os.path.commonpath([self.root, staged]) != self.root:
                raise ValueError(f"staging 路径越界: {relative}")
            if os.path.commonpath([self.source_root, target]) != self.source_root:
                raise ValueError(f"目标路径越界: {relative}")
            self._rollback[relative] = open(target, "rb").read() if os.path.isfile(target) else None
            backup_name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".bak"
            if self._rollback[relative] is not None:
                with open(os.path.join(self.backup_dir, backup_name), "wb") as handle:
                    handle.write(self._rollback[relative])
            manifest["entries"].append({"path": relative,
                                        "existed": self._rollback[relative] is not None,
                                        "backup": backup_name})
        self._write_journal(manifest)
        for entry in manifest["entries"]:
            relative = entry["path"]
            staged = os.path.realpath(os.path.join(self.root, relative))
            target = os.path.realpath(os.path.join(self.source_root, relative))
            if os.path.isfile(staged):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                temp = target + ".merge-tmp"
                shutil.copy2(staged, temp)
                os.replace(temp, target)
            elif os.path.exists(target):
                os.remove(target)
            self._merged.append(relative)
        return list(self._merged)

    def _write_journal(self, data):
        temp = self.journal_path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp, self.journal_path)

    def commit(self):
        if os.path.isfile(self.journal_path):
            with open(self.journal_path, encoding="utf-8") as handle:
                journal = json.load(handle)
            journal["status"] = "committed"
            self._write_journal(journal)

    def rollback(self):
        for relative in reversed(self._merged):
            target = os.path.join(self.source_root, relative.replace("/", os.sep))
            original = self._rollback.get(relative)
            if original is None:
                if os.path.isfile(target):
                    os.remove(target)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                temp = target + ".rollback-tmp"
                with open(temp, "wb") as handle:
                    handle.write(original)
                os.replace(temp, target)
        self._merged = []
        if os.path.isfile(self.journal_path):
            os.remove(self.journal_path)

    def cleanup(self, attempts=7, initial_delay=0.05):
        """Best-effort cleanup that tolerates short-lived Windows handle ownership.

        Some desktop agent launchers return after their foreground command exits while a
        descendant still has the staging directory as its current working directory.
        Windows rejects directory removal in that interval.  Cleanup is hygiene, not an
        execution verdict, so retry briefly and leave the directory for startup recovery
        instead of masking an otherwise valid result with ``WinError 32``.
        """
        task_dir = os.path.dirname(self.root)
        if not os.path.isdir(task_dir):
            return True
        delay = max(0.0, float(initial_delay))
        for attempt in range(max(1, int(attempts))):
            try:
                shutil.rmtree(task_dir, onerror=self._remove_readonly)
                return True
            except OSError:
                if attempt + 1 >= max(1, int(attempts)):
                    return False
                time.sleep(delay)
                delay = min(max(delay * 2, 0.05), 1.0)
        return False

    def __enter__(self):
        return self.prepare()

    def __exit__(self, *_exc):
        self.cleanup()


class SourceTreeGuard:
    """Detect and restore code-tree mutations made outside the staging directory."""
    def __init__(self, source_root):
        self.source_root = os.path.abspath(source_root)
        self.before = {}

    def _files(self):
        for current, dirs, files in os.walk(self.source_root):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
            for name in files:
                if (name in SOURCE_GUARD_EXCLUDED_FILES or
                        any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)):
                    continue
                path = os.path.join(current, name)
                relative = os.path.relpath(path, self.source_root).replace("\\", "/")
                yield relative, path

    def capture(self):
        self.before = {relative: open(path, "rb").read() for relative, path in self._files()}
        return self

    def restore_if_changed(self):
        current = {relative: open(path, "rb").read() for relative, path in self._files()}
        changed = sorted(path for path in set(self.before) | set(current)
                         if self.before.get(path) != current.get(path))
        for relative in changed:
            target = os.path.join(self.source_root, relative.replace("/", os.sep))
            original = self.before.get(relative)
            if original is None:
                if os.path.isfile(target):
                    os.remove(target)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                temp = target + ".guard-restore"
                with open(temp, "wb") as handle:
                    handle.write(original)
                os.replace(temp, target)
        return changed


def recover_abandoned_merges(source_root, execution_root):
    """Rollback journals left by a process crash before merge commit."""
    recovered = []
    if not os.path.isdir(execution_root):
        return recovered
    source_root = os.path.abspath(source_root)
    for task_name in os.listdir(execution_root):
        task_dir = os.path.join(execution_root, task_name)
        journal_path = os.path.join(task_dir, "merge-journal.json")
        if not os.path.isfile(journal_path):
            continue
        try:
            with open(journal_path, encoding="utf-8") as handle:
                journal = json.load(handle)
            if journal.get("status") == "committed":
                shutil.rmtree(task_dir, onerror=IsolatedWorkspace._remove_readonly)
                continue
            for entry in reversed(journal.get("entries") or []):
                relative = _safe_relative(entry["path"])
                target = os.path.realpath(os.path.join(source_root, relative))
                if os.path.commonpath([source_root, target]) != source_root:
                    raise ValueError("恢复日志包含越界路径")
                if entry.get("existed"):
                    backup = os.path.join(task_dir, "merge-backup", entry["backup"])
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(backup, target)
                elif os.path.isfile(target):
                    os.remove(target)
            recovered.append(task_name)
            shutil.rmtree(task_dir, onerror=IsolatedWorkspace._remove_readonly)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return recovered
