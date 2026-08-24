import asyncio
import json
import logging
import os
import urllib.request

from plugins.platforms.feishu import adapter as builtin


logger = logging.getLogger(__name__)


class OrchestrationFeishuAdapter(builtin.FeishuAdapter):
    async def _handle_message_event_data(self, data):
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        if not message or not sender_id:
            return await super()._handle_message_event_data(data)

        payload = {
            "message_id": getattr(message, "message_id", "") or "",
            "chat_id": getattr(message, "chat_id", "") or "",
            "chat_type": getattr(message, "chat_type", "p2p") or "p2p",
            "message_type": getattr(message, "message_type", "") or "",
            "content": getattr(message, "content", "") or "",
            "thread_id": getattr(message, "thread_id", None),
            "root_id": getattr(message, "root_id", None),
            "parent_id": getattr(message, "parent_id", None),
            "user_id": getattr(sender_id, "user_id", "") or getattr(sender_id, "open_id", "") or "",
            "open_id": getattr(sender_id, "open_id", "") or "",
            "union_id": getattr(sender_id, "union_id", "") or "",
        }
        try:
            await asyncio.to_thread(self._delegate, payload)
            logger.info("[Feishu] Delegated inbound message %s to orchestrator", payload["message_id"])
        except Exception:
            # Fail open to Hermes so a local orchestrator outage never makes the
            # Feishu bot silently unavailable.
            logger.exception("[Feishu] Orchestrator delegation failed; falling back to Hermes")
            await super()._handle_message_event_data(data)

    @staticmethod
    def _delegate(payload):
        url = os.environ.get("FEISHU_ORCHESTRATOR_BRIDGE_URL", "http://127.0.0.1:8765/v1/feishu/events")
        token_file = os.environ.get("FEISHU_ORCHESTRATOR_TOKEN_FILE", "/mnt/e/feishu-agent/workspace/ingress-bridge.token")
        with open(token_file, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 202:
                raise RuntimeError(f"orchestrator returned HTTP {response.status}")


def _build_adapter(config):
    return OrchestrationFeishuAdapter(config)


def register(ctx):
    ctx.register_platform(
        name="feishu", label="Feishu / Lark", adapter_factory=_build_adapter,
        check_fn=builtin.feishu_deps_present, ensure_deps_fn=builtin.check_feishu_requirements,
        is_connected=builtin._is_connected, validate_config=builtin._is_connected,
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        install_hint="Run `hermes setup` to install Feishu support.",
        setup_fn=builtin.interactive_setup, apply_yaml_config_fn=builtin._apply_yaml_config,
        allowed_users_env="FEISHU_ALLOWED_USERS", allow_all_env="FEISHU_ALLOW_ALL_USERS",
        cron_deliver_env_var="FEISHU_HOME_CHANNEL", standalone_sender_fn=builtin._standalone_send,
        max_message_length=8000, emoji="🪽", allow_update_command=True,
    )
