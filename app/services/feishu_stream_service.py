from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from app.core.config import settings

logger = logging.getLogger("webapi.feishu_stream")


class FeishuStreamService:
    """Feishu Stream mode listener (optional, enabled by env)."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        mode = str(getattr(settings, "FEISHU_BOT_MODE", "webhook") or "webhook").strip().lower()
        if mode != "stream":
            logger.info("Feishu stream listener disabled (FEISHU_BOT_MODE=%s)", mode)
            return
        if self._started:
            return

        if not settings.FEISHU_BOT_APP_ID or not settings.FEISHU_BOT_APP_SECRET:
            logger.warning("Feishu stream listener not started: missing FEISHU_BOT_APP_ID/FEISHU_BOT_APP_SECRET")
            return

        self._loop = asyncio.get_running_loop()
        self._started = True

        self._thread = threading.Thread(
            target=self._start_ws_blocking,
            kwargs={
                "app_id": settings.FEISHU_BOT_APP_ID,
                "app_secret": settings.FEISHU_BOT_APP_SECRET,
                "api_base": settings.FEISHU_BOT_API_BASE,
                "encrypt_key": settings.FEISHU_BOT_ENCRYPT_KEY or "",
                "verification_token": settings.FEISHU_BOT_VERIFICATION_TOKEN or "",
            },
            name="feishu-stream-listener",
            daemon=True,
        )
        self._thread.start()
        logger.info("Feishu stream listener started")

    async def stop(self) -> None:
        # lark.ws.Client currently has no public stop API; daemon thread exits with process.
        if self._started:
            logger.info("Feishu stream listener stop requested")
        self._started = False

    def _start_ws_blocking(
        self,
        app_id: str,
        app_secret: str,
        api_base: str,
        encrypt_key: str,
        verification_token: str,
    ) -> None:
        try:
            # lark_oapi 在模块级缓存 loop，必须在线程内初始化独立 loop 和 client
            thread_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_loop)

            import lark_oapi as lark
            import lark_oapi.ws.client as ws_client_module

            ws_client_module.loop = thread_loop

            event_handler = (
                lark.EventDispatcherHandler.builder(encrypt_key, verification_token, lark.LogLevel.INFO)
                .register_p2_im_message_receive_v1(self._on_message_receive)
                .register_p2_card_action_trigger(self._on_card_action_trigger)
                .build()
            )
            client = lark.ws.Client(
                app_id,
                app_secret,
                log_level=lark.LogLevel.INFO,
                event_handler=event_handler,
                domain=api_base,
            )
            client.start()
        except Exception:
            logger.exception("Feishu stream listener exited unexpectedly")

    def _on_message_receive(self, data: Any) -> None:
        payload = self._event_to_payload(data)
        if not payload or not self._loop:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._dispatch_payload(payload), self._loop)
            fut.add_done_callback(self._log_future_exception)
        except RuntimeError:
            logger.warning("Feishu stream message dropped: event loop already closed")

    def _on_card_action_trigger(self, data: Any) -> Any:
        payload = self._card_action_to_payload(data)
        if not payload or not self._loop:
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(self._dispatch_card_action_payload(payload), self._loop)
            fut.add_done_callback(self._log_future_exception)
        except RuntimeError:
            logger.warning("Feishu stream card action dropped: event loop already closed")
        return None

    @staticmethod
    def _log_future_exception(fut: "asyncio.Future[Any]") -> None:
        try:
            fut.result()
        except Exception:
            logger.exception("Feishu stream payload handling failed")

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        from app.routers.feishu_bot import handle_feishu_event_payload

        await handle_feishu_event_payload(payload)

    async def _dispatch_card_action_payload(self, payload: Dict[str, Any]) -> None:
        from app.routers.feishu_bot import handle_feishu_card_action_payload

        await handle_feishu_card_action_payload(payload)

    @staticmethod
    def _event_to_payload(data: Any) -> Dict[str, Any]:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)

        return {
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": getattr(sender_id, "open_id", None),
                        "user_id": getattr(sender_id, "user_id", None),
                        "union_id": getattr(sender_id, "union_id", None),
                    }
                },
                "message": {
                    "message_id": getattr(message, "message_id", None),
                    "parent_id": getattr(message, "parent_id", None),
                    "root_id": getattr(message, "root_id", None),
                    "chat_id": getattr(message, "chat_id", None),
                    "message_type": getattr(message, "message_type", None),
                    "content": getattr(message, "content", None),
                },
            }
        }

    @staticmethod
    def _card_action_to_payload(data: Any) -> Dict[str, Any]:
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        context = getattr(event, "context", None)
        operator = getattr(event, "operator", None)
        return {
            "event": {
                "token": getattr(event, "token", None),
                "action": {
                    "tag": getattr(action, "tag", None),
                    "value": getattr(action, "value", None),
                },
                "context": {
                    "open_chat_id": getattr(context, "open_chat_id", None),
                    "open_message_id": getattr(context, "open_message_id", None),
                },
                "operator": {
                    "open_id": getattr(operator, "open_id", None),
                    "user_id": getattr(operator, "user_id", None),
                    "union_id": getattr(operator, "union_id", None),
                },
            }
        }


feishu_stream_service = FeishuStreamService()
