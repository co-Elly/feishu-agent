"""Keep every pytest write outside production databases and meeting files."""

import atexit
import os
import shutil
import tempfile


TEST_DATA_ROOT = tempfile.mkdtemp(prefix="feishu-agent-tests-")
atexit.register(shutil.rmtree, TEST_DATA_ROOT, True)

os.environ["FEISHU_TESTING"] = "1"
os.environ["FEISHU_DB_PATH"] = os.path.join(TEST_DATA_ROOT, "conversations.db")
os.environ["FEISHU_ROUNDTABLE_ROOT"] = os.path.join(TEST_DATA_ROOT, "roundtable")
os.environ["FEISHU_ROUNDTABLE_DB"] = os.path.join(TEST_DATA_ROOT, "roundtable", "roundtable.db")
# WSL 下 config.json 里的 Windows 路径（E:\...）abspath 后会落在项目内，触发越界检查。
# 测试环境统一指向 TEST_DATA_ROOT，保证两个平台都能收集测试。
os.environ.setdefault("FEISHU_ANTIGRAVITY_PROFILE", os.path.join(TEST_DATA_ROOT, "agy-profile"))
os.environ.setdefault("FEISHU_CODEX_COMMAND", "codex")

_workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _path in (os.environ["FEISHU_DB_PATH"], os.environ["FEISHU_ROUNDTABLE_DB"]):
    try:
        _inside_workspace = os.path.commonpath((_workspace, os.path.abspath(_path))) == _workspace
    except ValueError:  # Different Windows drives are necessarily isolated.
        _inside_workspace = False
    if _inside_workspace:
        raise RuntimeError(f"测试数据库不得位于生产工作区：{_path}")
