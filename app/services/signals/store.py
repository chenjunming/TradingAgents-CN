from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.database import get_mongo_db


class SignalStore:
    def __init__(self):
        self._index_ensured = False

    def _db(self):
        return get_mongo_db()

    async def ensure_indexes(self) -> None:
        if self._index_ensured:
            return
        db = self._db()
        await db.signal_events.create_index([("user_id", 1), ("ts", -1)], name="idx_signal_user_ts")
        await db.signal_events.create_index([("ticker", 1), ("rule_id", 1), ("level", 1), ("ts", -1)], name="idx_signal_ticker_rule_level")
        await db.signal_events.create_index([("dedupe_key", 1), ("ts", -1)], name="idx_signal_dedupe_ts")

        await db.signal_mutes.create_index([("user_id", 1), ("ticker", 1), ("rule_id", 1)], unique=True, name="uniq_signal_mute")
        await db.signal_mutes.create_index([("mute_until", 1)], expireAfterSeconds=0, name="ttl_signal_mute_until")

        await db.signal_rulesets.create_index([("version_hash", 1)], unique=True, name="uniq_signal_ruleset_hash")
        await db.signal_rulesets.create_index([("created_at", -1)], name="idx_signal_ruleset_created")
        await db.feishu_delivery_targets.create_index(
            [("system_user_id", 1)],
            unique=True,
            name="uniq_feishu_delivery_target_user",
        )

        self._index_ensured = True

    async def save_ruleset(self, version_hash: str, rules: List[Dict[str, Any]]) -> None:
        db = self._db()
        await db.signal_rulesets.update_one(
            {"version_hash": version_hash},
            {
                "$set": {
                    "version_hash": version_hash,
                    "rules": rules,
                    "enabled": True,
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )

    async def current_ruleset(self) -> Optional[Dict[str, Any]]:
        db = self._db()
        doc = await db.signal_rulesets.find_one({}, sort=[("created_at", -1)])
        if doc:
            doc.pop("_id", None)
        return doc

    async def save_event(self, event: Dict[str, Any]) -> None:
        db = self._db()
        await db.signal_events.insert_one(event)

    async def list_events(
        self,
        user_id: str,
        ticker: Optional[str] = None,
        level: Optional[str] = None,
        rule_id: Optional[str] = None,
        acked: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        db = self._db()
        query: Dict[str, Any] = {"user_id": user_id}
        if ticker:
            query["ticker"] = ticker.upper()
        if level:
            query["level"] = level
        if rule_id:
            query["rule_id"] = rule_id
        if acked is not None:
            query["status.acked"] = bool(acked)

        total = await db.signal_events.count_documents(query)
        cursor = db.signal_events.find(query, {"_id": 0}).sort("ts", -1).skip(offset).limit(limit)
        items = [doc async for doc in cursor]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def get_event(self, user_id: str, event_id: str) -> Optional[Dict[str, Any]]:
        db = self._db()
        doc = await db.signal_events.find_one({"user_id": user_id, "event_id": event_id})
        if doc:
            doc.pop("_id", None)
        return doc

    async def get_event_by_id(self, event_id: str) -> Optional[Dict[str, Any]]:
        db = self._db()
        doc = await db.signal_events.find_one({"event_id": event_id})
        if doc:
            doc.pop("_id", None)
        return doc

    async def ack_event(self, user_id: str, event_id: str) -> bool:
        db = self._db()
        result = await db.signal_events.update_one(
            {"user_id": user_id, "event_id": event_id},
            {"$set": {"status.acked": True, "status.acked_at": datetime.utcnow()}},
        )
        return result.modified_count > 0

    async def mute_from_event(self, user_id: str, event_id: str, days: int = 7) -> bool:
        event = await self.get_event(user_id, event_id)
        if not event:
            return False
        await self.upsert_mute(
            user_id=user_id,
            ticker=str(event.get("ticker") or "").upper(),
            rule_id=str(event.get("rule_id") or ""),
            days=days,
        )
        await self._db().signal_events.update_one(
            {"user_id": user_id, "event_id": event_id},
            {
                "$set": {
                    "status.muted": True,
                    "status.mute_until": datetime.utcnow() + timedelta(days=days),
                }
            },
        )
        return True

    async def set_event_analysis_task(
        self,
        user_id: str,
        event_id: str,
        task_id: str,
        status: str = "pending",
    ) -> bool:
        db = self._db()
        result = await db.signal_events.update_one(
            {"user_id": user_id, "event_id": event_id},
            {
                "$set": {
                    "analysis.task_id": task_id,
                    "analysis.status": status,
                    "analysis.updated_at": datetime.utcnow(),
                }
            },
        )
        return result.modified_count > 0

    async def upsert_mute(self, user_id: str, ticker: str, rule_id: str, days: int = 7) -> None:
        db = self._db()
        mute_until = datetime.utcnow() + timedelta(days=max(1, int(days)))
        await db.signal_mutes.update_one(
            {"user_id": user_id, "ticker": ticker.upper(), "rule_id": rule_id},
            {
                "$set": {
                    "user_id": user_id,
                    "ticker": ticker.upper(),
                    "rule_id": rule_id,
                    "mute_until": mute_until,
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )

    async def get_active_mute(self, user_id: str, ticker: str, rule_id: str) -> Optional[Dict[str, Any]]:
        db = self._db()
        now = datetime.utcnow()
        doc = await db.signal_mutes.find_one(
            {
                "user_id": user_id,
                "ticker": ticker.upper(),
                "rule_id": rule_id,
                "mute_until": {"$gt": now},
            },
            {"_id": 0},
        )
        return doc

    async def latest_event_by_dedupe_key(self, user_id: str, dedupe_key: str) -> Optional[Dict[str, Any]]:
        db = self._db()
        doc = await db.signal_events.find_one(
            {"user_id": user_id, "dedupe_key": dedupe_key},
            sort=[("ts", -1)],
            projection={"_id": 0},
        )
        return doc

    async def upsert_delivery_target(
        self,
        system_user_id: str,
        receive_id_type: str,
        receive_id: str,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        db = self._db()
        payload = {
            "system_user_id": str(system_user_id),
            "receive_id_type": str(receive_id_type).lower(),
            "receive_id": str(receive_id),
            "enabled": bool(enabled),
            "updated_at": datetime.utcnow(),
        }
        await db.feishu_delivery_targets.update_one(
            {"system_user_id": str(system_user_id)},
            {"$set": payload, "$setOnInsert": {"created_at": datetime.utcnow()}},
            upsert=True,
        )
        payload["created_at"] = datetime.utcnow()
        return payload

    async def get_delivery_target(self, system_user_id: str) -> Optional[Dict[str, Any]]:
        db = self._db()
        doc = await db.feishu_delivery_targets.find_one({"system_user_id": str(system_user_id)}, {"_id": 0})
        return doc
