from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.core.database import get_mongo_db
from app.models.advisor_models import DailyBriefResponse, FeishuCommandResponse


class FeishuPushService:
    def _render_rich_markdown(self, lines: list[str]) -> str:
        # 富文本（Markdown）内容：空行分段，保持已有 markdown 语法。
        content = "\n".join([str(x) for x in lines]).strip()
        return content if content else "-"

    def _build_simple_card(self, title: str, lines: list[str], template: str = "blue") -> Dict[str, Any]:
        markdown = self._render_rich_markdown(lines)
        return {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "template": template,
                "title": {
                    "tag": "plain_text",
                    "content": title,
                },
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": markdown,
                    },
                }
            ],
        }

    async def _get_tenant_access_token(self) -> Optional[str]:
        if not settings.FEISHU_BOT_APP_ID or not settings.FEISHU_BOT_APP_SECRET:
            return None

        url = f"{settings.FEISHU_BOT_API_BASE}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": settings.FEISHU_BOT_APP_ID,
            "app_secret": settings.FEISHU_BOT_APP_SECRET,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    return None
                return data.get("tenant_access_token")
        except Exception:
            return None

    def build_card_text(self, title: str, lines: list[str]) -> str:
        content = [title]
        content.extend(lines)
        return "\n".join(content)

    def command_response(self, text: str) -> FeishuCommandResponse:
        return FeishuCommandResponse(message_type="text", fallback_text=text)

    async def send_text(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        # 默认使用富文本卡片（Markdown）发送；失败再降级纯文本，保证消息可达。
        card = self._build_simple_card(
            title="投研助手",
            lines=[text],
            template="blue",
        )
        card_result = await self.send_custom_card(
            chat_id,
            card,
            action="send_text_as_card",
            reply_to_message_id=reply_to_message_id,
        )
        if card_result.get("success"):
            return card_result

        return await self._send_plain_text(chat_id, text, reply_to_message_id=reply_to_message_id)

    async def _send_plain_text(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = await self._get_tenant_access_token()
        if not token:
            return {"success": False, "message": "missing_feishu_credentials"}

        url = f"{settings.FEISHU_BOT_API_BASE}/open-apis/im/v1/messages"
        reply_target = str(reply_to_message_id or "").strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async def _post(receive_id_type: str, receive_id: str) -> Dict[str, Any]:
            body = {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, params={"receive_id_type": receive_id_type}, headers=headers, json=body)
                return resp.json()

        try:
            if reply_target:
                data = await _post("message_id", reply_target)
                if data.get("code") != 0:
                    data = await _post("chat_id", chat_id)
            else:
                data = await _post("chat_id", chat_id)
        except Exception as exc:
            data = {"code": -1, "msg": str(exc), "error_type": type(exc).__name__}

        db = get_mongo_db()
        try:
            await db.feishu_message_logs.insert_one(
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": str(reply_to_message_id or ""),
                    "action": "send_plain_text",
                    "text": text,
                    "response": data,
                    "created_at": datetime.utcnow(),
                }
            )
        except Exception:
            pass

        return {"success": data.get("code") == 0, "response": data}

    async def send_card(
        self,
        chat_id: str,
        title: str,
        lines: list[str],
        template: str = "blue",
        reply_to_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = await self._get_tenant_access_token()
        if not token:
            return {"success": False, "message": "missing_feishu_credentials"}

        card = self._build_simple_card(title=title, lines=lines, template=template)
        url = f"{settings.FEISHU_BOT_API_BASE}/open-apis/im/v1/messages"
        reply_target = str(reply_to_message_id or "").strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async def _post(receive_id_type: str, receive_id: str) -> Dict[str, Any]:
            body = {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, params={"receive_id_type": receive_id_type}, headers=headers, json=body)
                return resp.json()

        try:
            if reply_target:
                data = await _post("message_id", reply_target)
                if data.get("code") != 0:
                    data = await _post("chat_id", chat_id)
            else:
                data = await _post("chat_id", chat_id)
        except Exception as exc:
            data = {"code": -1, "msg": str(exc), "error_type": type(exc).__name__}

        db = get_mongo_db()
        try:
            await db.feishu_message_logs.insert_one(
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": str(reply_to_message_id or ""),
                    "action": "send_card",
                    "title": title,
                    "lines": lines,
                    "response": data,
                    "created_at": datetime.utcnow(),
                }
            )
        except Exception:
            pass

        return {"success": data.get("code") == 0, "response": data}

    async def send_custom_card(
        self,
        chat_id: str,
        card: Dict[str, Any],
        action: str = "send_custom_card",
        reply_to_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = await self._get_tenant_access_token()
        if not token:
            return {"success": False, "message": "missing_feishu_credentials"}

        url = f"{settings.FEISHU_BOT_API_BASE}/open-apis/im/v1/messages"
        reply_target = str(reply_to_message_id or "").strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async def _post(receive_id_type: str, receive_id: str) -> Dict[str, Any]:
            body = {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, params={"receive_id_type": receive_id_type}, headers=headers, json=body)
                return resp.json()

        try:
            if reply_target:
                data = await _post("message_id", reply_target)
                if data.get("code") != 0:
                    data = await _post("chat_id", chat_id)
            else:
                data = await _post("chat_id", chat_id)
        except Exception as exc:
            data = {"code": -1, "msg": str(exc), "error_type": type(exc).__name__}

        db = get_mongo_db()
        try:
            await db.feishu_message_logs.insert_one(
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": str(reply_to_message_id or ""),
                    "action": action,
                    "card": card,
                    "response": data,
                    "created_at": datetime.utcnow(),
                }
            )
        except Exception:
            pass

        return {"success": data.get("code") == 0, "response": data}

    async def update_text_message(self, message_id: str, text: str) -> Dict[str, Any]:
        token = await self._get_tenant_access_token()
        if not token:
            return {"success": False, "message": "missing_feishu_credentials"}
        if not message_id:
            return {"success": False, "message": "missing_message_id"}

        url = f"{settings.FEISHU_BOT_API_BASE}/open-apis/im/v1/messages/{message_id}"
        body = {
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.patch(url, headers=headers, json=body)
                data = resp.json()
        except Exception as exc:
            data = {"code": -1, "msg": str(exc), "error_type": type(exc).__name__}

        db = get_mongo_db()
        try:
            await db.feishu_message_logs.insert_one(
                {
                    "message_id": message_id,
                    "text": text,
                    "action": "update_text_message",
                    "response": data,
                    "created_at": datetime.utcnow(),
                }
            )
        except Exception:
            pass
        return {"success": data.get("code") == 0, "response": data}

    async def update_card_message(
        self,
        message_id: str,
        title: str,
        lines: list[str],
        template: str = "blue",
    ) -> Dict[str, Any]:
        token = await self._get_tenant_access_token()
        if not token:
            return {"success": False, "message": "missing_feishu_credentials"}
        if not message_id:
            return {"success": False, "message": "missing_message_id"}

        card = self._build_simple_card(title=title, lines=lines, template=template)
        url = f"{settings.FEISHU_BOT_API_BASE}/open-apis/im/v1/messages/{message_id}"
        body = {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.patch(url, headers=headers, json=body)
                data = resp.json()
        except Exception as exc:
            data = {"code": -1, "msg": str(exc), "error_type": type(exc).__name__}

        db = get_mongo_db()
        try:
            await db.feishu_message_logs.insert_one(
                {
                    "message_id": message_id,
                    "action": "update_card_message",
                    "title": title,
                    "lines": lines,
                    "response": data,
                    "created_at": datetime.utcnow(),
                }
            )
        except Exception:
            pass

        return {"success": data.get("code") == 0, "response": data}

    async def push_daily_brief(self, chat_id: str, brief: DailyBriefResponse, market: str, push_type: str) -> Dict[str, Any]:
        picks = [p for p in brief.picks if p.market == market][:5]
        rebalance = [r for r in brief.rebalance if r.market == market][:5]

        lines = [
            f"市场: {market}",
            f"时点: {push_type}",
            f"候选: {len(picks)}，调仓: {len(rebalance)}",
        ]
        for p in picks:
            lines.append(f"[选股] {p.symbol} {p.action} 分数:{p.score_total} 目标仓位:{p.target_weight:.0%}")
        for r in rebalance:
            lines.append(f"[调仓] {r.symbol} {r.action} 当前:{r.current_weight:.0%} -> 目标:{r.target_weight:.0%}")

        text = self.build_card_text(f"{brief.date} 投研推送", lines)
        return await self.send_text(chat_id, text)


feishu_push_service = FeishuPushService()
