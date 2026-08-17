import importlib
import json


def _store(tmp_path, monkeypatch):
    import conversation_store
    monkeypatch.setattr(conversation_store, "DB_PATH", str(tmp_path / "conversations.db"))
    monkeypatch.setattr(conversation_store, "LEGACY_PATH", str(tmp_path / "chat_history.json"))
    return conversation_store


def test_history_roundtrip_and_clear(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.add_exchange("chat:codex", "你好", "收到")
    assert store.get_history("chat:codex") == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "收到"},
    ]
    store.clear_history("chat:codex")
    assert store.get_history("chat:codex") == []


def test_event_claim_is_idempotent(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    assert store.claim_event("message-1") is True
    assert store.claim_event("message-1") is False


def test_legacy_json_migrates_once(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    legacy = {"u:hermes": [{"role": "user", "content": "旧消息"}]}
    (tmp_path / "chat_history.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    assert store.migrate_legacy_json() == 1
    assert store.migrate_legacy_json() == 0
    assert store.get_history("u:hermes") == [{"role": "user", "content": "旧消息"}]
