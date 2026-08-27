"""Authenticated loopback bridge used when Hermes owns the Feishu connection."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def event_from_payload(payload: dict) -> SimpleNamespace:
    """Build the small lark event surface consumed by bot.handle_message."""
    required = ("message_id", "chat_id", "message_type", "content", "user_id")
    missing = [key for key in required if not str(payload.get(key) or "").strip()]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    sender_id = SimpleNamespace(
        user_id=str(payload.get("user_id") or ""),
        open_id=str(payload.get("open_id") or ""),
        union_id=str(payload.get("union_id") or ""),
    )
    message = SimpleNamespace(
        message_id=str(payload["message_id"]),
        chat_id=str(payload["chat_id"]),
        chat_type=str(payload.get("chat_type") or "p2p"),
        message_type=str(payload["message_type"]),
        content=str(payload["content"]),
        thread_id=payload.get("thread_id"),
        root_id=payload.get("root_id"),
        parent_id=payload.get("parent_id"),
    )
    return SimpleNamespace(
        event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(sender_id=sender_id, sender_type="user"),
        )
    )


def ensure_token(path: str | os.PathLike[str]) -> str:
    token_path = Path(path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(48)
    temporary = token_path.with_suffix(token_path.suffix + ".tmp")
    temporary.write_text(token, encoding="utf-8")
    os.replace(temporary, token_path)
    return token


class IngressBridge:
    def __init__(self, callback, token_path, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.callback = callback
        self.token = ensure_token(token_path)
        self.host = host
        self.port = int(port)
        self.server = None
        self.thread = None

    def start(self):
        bridge = self

        class ThreadedHTTPServerWithReuse(ThreadingHTTPServer):
            """HTTP server with SO_REUSEADDR to allow quick restart."""
            allow_reuse_address = True
            
            def server_bind(self):
                import socket
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                super().server_bind()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/health":
                    self.send_error(404)
                    return
                self._json(200, {"status": "ok", "mode": "hermes-delegate"})

            def do_POST(self):
                if self.path != "/v1/feishu/events":
                    self.send_error(404)
                    return
                supplied = self.headers.get("Authorization", "")
                expected = f"Bearer {bridge.token}"
                if not hmac.compare_digest(supplied, expected):
                    self._json(401, {"accepted": False, "error": "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 1024 * 1024:
                        raise ValueError("invalid content length")
                    event = event_from_payload(json.loads(self.rfile.read(length)))
                    bridge.callback(event)
                    self._json(202, {"accepted": True})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {"accepted": False, "error": str(exc)})

            def _json(self, status, value):
                body = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        self.server = ThreadedHTTPServerWithReuse((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="ingress-bridge", daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

