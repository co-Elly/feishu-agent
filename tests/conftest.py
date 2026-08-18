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

_workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _path in (os.environ["FEISHU_DB_PATH"], os.environ["FEISHU_ROUNDTABLE_DB"]):
    try:
        _inside_workspace = os.path.commonpath((_workspace, os.path.abspath(_path))) == _workspace
    except ValueError:  # Different Windows drives are necessarily isolated.
        _inside_workspace = False
    if _inside_workspace:
        raise RuntimeError(f"测试数据库不得位于生产工作区：{_path}")
