import os

import conversation_store
import roundtable_engine


def test_pytest_uses_disposable_data_paths():
    assert os.environ["FEISHU_TESTING"] == "1"
    assert conversation_store.DB_PATH == os.environ["FEISHU_DB_PATH"]
    assert roundtable_engine.DB_PATH == os.environ["FEISHU_ROUNDTABLE_DB"]
    assert "feishu-agent-tests-" in conversation_store.DB_PATH
    assert "feishu-agent-tests-" in roundtable_engine.DB_PATH
