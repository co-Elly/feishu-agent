"""Exclusive ownership guard for the Feishu event ingress."""

import atexit
import os
import shutil
import subprocess


class CompetingGatewayError(RuntimeError):
    pass


class IngressAlreadyOwnedError(RuntimeError):
    pass


def _active_hermes_gateway_app_id(timeout=5):
    """Return the active local Hermes gateway App ID without logging it."""
    if os.name != "nt" or shutil.which("wsl.exe") is None:
        return None
    active = subprocess.run(
        ["wsl.exe", "-e", "bash", "-lc",
         "systemctl --user is-active --quiet hermes-gateway.service || "
         "pgrep -f '[h]ermes_cli.main gateway run' >/dev/null"],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if active.returncode != 0:
        return None
    result = subprocess.run(
        ["wsl.exe", "-e", "bash", "-lc",
         "set -a; source /root/.hermes/.env >/dev/null 2>&1; printf '%s' \"$FEISHU_APP_ID\""],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def assert_no_competing_hermes_gateway(app_id):
    competing_app_id = _active_hermes_gateway_app_id()
    if competing_app_id and competing_app_id == app_id:
        raise CompetingGatewayError(
            "Hermes gateway 正在使用同一飞书 App ID；长连接是随机集群消费，"
            "请先停止 systemd 服务及所有残留的 gateway run 进程"
        )


class IngressLease:
    def __init__(self, handle):
        self.handle = handle

    def close(self):
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def acquire_ingress_lease(workspace_dir, app_id):
    """Fail closed when another local consumer could receive this app's events."""
    assert_no_competing_hermes_gateway(app_id)
    os.makedirs(workspace_dir, exist_ok=True)
    path = os.path.join(workspace_dir, "feishu_ingress.lock")
    handle = open(path, "a+b")
    if os.path.getsize(path) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as exc:
        handle.close()
        raise IngressAlreadyOwnedError(
            "已有另一个 bot.py 实例持有飞书入站连接"
        ) from exc
    lease = IngressLease(handle)
    atexit.register(lease.close)
    return lease
