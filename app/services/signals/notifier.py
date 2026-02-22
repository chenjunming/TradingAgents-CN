from __future__ import annotations

from typing import Any, Dict

from app.core.config import settings
from app.core.database import get_mongo_db
from app.services.feishu_push_service import feishu_push_service


class SignalNotifier:
    VALID_RECEIVE_ID_TYPES = {"chat_id", "open_id", "user_id", "union_id"}

    async def _resolve_delivery_target(self, system_user_id: str) -> Dict[str, str]:
        db = get_mongo_db()
        uid = str(system_user_id or "").strip() or "default"

        # 1) explicit env override for single-user deployment
        env_rid_type = str(settings.FEISHU_SIGNAL_RECEIVE_ID_TYPE or "").strip().lower()
        env_rid = str(settings.FEISHU_SIGNAL_RECEIVE_ID or "").strip()
        if env_rid_type in self.VALID_RECEIVE_ID_TYPES and env_rid:
            return {"receive_id_type": env_rid_type, "receive_id": env_rid, "source": "env"}

        # 2) explicit binding by system user id
        try:
            doc = await db.feishu_delivery_targets.find_one(
                {"system_user_id": uid, "enabled": {"$ne": False}},
                {"_id": 0, "receive_id_type": 1, "receive_id": 1},
            )
            if isinstance(doc, dict):
                rid_type = str(doc.get("receive_id_type") or "").strip().lower()
                rid = str(doc.get("receive_id") or "").strip()
                if rid_type in self.VALID_RECEIVE_ID_TYPES and rid:
                    return {"receive_id_type": rid_type, "receive_id": rid, "source": "binding"}
        except Exception:
            pass

        # 3) fallback from latest feishu conversation
        try:
            conv = await db.feishu_conversations.find_one(
                {"user_id": uid, "chat_id": {"$exists": True, "$nin": ["", None]}},
                {"_id": 0, "chat_id": 1},
                sort=[("updated_at", -1)],
            )
            chat_id = str((conv or {}).get("chat_id") or "").strip()
            if chat_id:
                return {"receive_id_type": "chat_id", "receive_id": chat_id, "source": "conversation"}
        except Exception:
            pass

        # 4) global default chat fallback
        default_chat = str(settings.FEISHU_BOT_DEFAULT_CHAT_ID or "").strip()
        if default_chat:
            return {"receive_id_type": "chat_id", "receive_id": default_chat, "source": "default_chat"}

        return {}

    def _fmt(self, value: Any, digits: int = 2) -> str:
        try:
            if value is None:
                return "-"
            return f"{float(value):.{digits}f}"
        except Exception:
            return "-"

    def _build_card(self, event: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = event.get("snapshot") or {}
        level = str(event.get("level") or "prewarn")
        template_map = {"prewarn": "wathet", "confirm": "blue", "risk": "red"}
        event_id = str(event.get("event_id") or "")
        token_map = event.get("action_tokens") or {}
        action_token = str(token_map.get("card_action") or token_map.get("analysis") or "")
        ticker = str(event.get("ticker") or "")
        market = str(event.get("market") or "")
        rule_id = str(event.get("rule_id") or "")
        rule_name = str(event.get("rule_name") or "")

        text_lines = [
            f"**{ticker} ({market})**  当前价 `{self._fmt(snapshot.get('price'))}`",
            f"规则：{rule_name}  等级：`{level}`",
            f"MA20/50/100: `{self._fmt(snapshot.get('ma20'))}` / `{self._fmt(snapshot.get('ma50'))}` / `{self._fmt(snapshot.get('ma100'))}`",
            f"RSI14: `{self._fmt(snapshot.get('rsi14'))}`  VOL比: `{self._fmt(snapshot.get('vol_ratio_20d'))}`  VWAP: `{self._fmt(snapshot.get('vwap'))}`",
            f"动作建议：{event.get('action_hint') or '-'}",
        ]

        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看详情"},
                "type": "default",
                "value": {
                    "intent": "signal_show_detail",
                    "event_id": event_id,
                    "token": action_token,
                    "ticker": ticker,
                    "market": market,
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "一键复盘"},
                "type": "default",
                "value": {
                    "intent": "signal_run_analysis",
                    "event_id": event_id,
                    "token": action_token,
                    "ticker": ticker,
                    "market": market,
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                    "research_depth": "标准",
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "静默7天"},
                "type": "danger",
                "value": {
                    "intent": "signal_mute",
                    "event_id": event_id,
                    "token": action_token,
                    "days": 7,
                    "ticker": ticker,
                    "market": market,
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "标记已读"},
                "type": "primary",
                "value": {
                    "intent": "signal_ack",
                    "event_id": event_id,
                    "token": action_token,
                    "ticker": ticker,
                    "market": market,
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                },
            },
        ]

        return {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "template": template_map.get(level, "blue"),
                "title": {"tag": "plain_text", "content": f"信号提醒 | {ticker} | {rule_name}"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(text_lines)}},
                {"tag": "action", "actions": actions},
            ],
        }

    async def send_feishu_card(self, event: Dict[str, Any]) -> Dict[str, Any]:
        system_user_id = str(event.get("user_id") or "default")
        target = await self._resolve_delivery_target(system_user_id)
        receive_id_type = str(target.get("receive_id_type") or "")
        receive_id = str(target.get("receive_id") or "")
        if not receive_id_type or not receive_id:
            return {"success": False, "reason": "missing_feishu_delivery_target"}

        card = self._build_card(event)
        result = await feishu_push_service.send_custom_card(
            chat_id=receive_id if receive_id_type == "chat_id" else "",
            card=card,
            action="signal_event_card",
            receive_id_type=receive_id_type,
            receive_id=receive_id,
        )
        if isinstance(result, dict):
            result.setdefault("target", target)
        return result
