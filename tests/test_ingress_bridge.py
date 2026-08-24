import json
import urllib.error
import urllib.request

from ingress_bridge import IngressBridge, event_from_payload


def _payload():
    return {
        "message_id": "om_123", "chat_id": "oc_123", "chat_type": "p2p",
        "message_type": "text", "content": json.dumps({"text": "开会 测试"}),
        "user_id": "ou_123", "open_id": "ou_123",
    }


def test_event_from_payload_matches_bot_surface():
    data = event_from_payload(_payload())
    assert data.event.message.message_id == "om_123"
    assert data.event.sender.sender_id.user_id == "ou_123"


def test_bridge_requires_token_and_accepts_event(tmp_path):
    received = []
    bridge = IngressBridge(received.append, tmp_path / "token", port=0).start()
    port = bridge.server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/feishu/events",
            data=json.dumps(_payload()).encode(), method="POST",
        )
        try:
            urllib.request.urlopen(request)
            assert False, "missing token must fail"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        request.add_header("Authorization", f"Bearer {bridge.token}")
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        assert received[0].event.message.chat_id == "oc_123"
    finally:
        bridge.close()
