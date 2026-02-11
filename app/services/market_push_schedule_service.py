from __future__ import annotations

from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_mongo_db
from app.services.advisor_service import advisor_service
from app.services.feishu_push_service import feishu_push_service
from app.services.market_calendar_service import market_calendar_service
from app.services.portfolio_service import portfolio_service


class MarketPushScheduleService:
    def __init__(self):
        self.window_seconds = settings.MARKET_PUSH_WINDOW_SECONDS
        self._index_ensured = False

    async def scan_and_dispatch(self, user_id: str = "default", force: bool = False) -> Dict[str, int]:
        if not settings.MARKET_PUSH_ENABLED and not force:
            return {"sent": 0, "skipped": 0, "failed": 0}
        await self._ensure_indexes()

        markets = [m.strip().upper() for m in settings.MARKET_PUSH_MARKETS.split(",") if m.strip()]
        now_utc = datetime.now(ZoneInfo("UTC"))

        exposure_markets = await portfolio_service.get_exposure_markets(user_id)
        stats = {"sent": 0, "skipped": 0, "failed": 0}

        for market in markets:
            if settings.MARKET_PUSH_EXPOSURE_ONLY and market not in exposure_markets and not force:
                stats["skipped"] += 1
                continue

            if not market_calendar_service.is_trading_day(market):
                await self._record_event(user_id, market, now_utc.date().isoformat(), "open_plus_30", now_utc, "skipped", "market_closed")
                stats["skipped"] += 1
                continue

            push_times = market_calendar_service.today_push_times(market, ref_dt=now_utc)
            for push_type, target_time in push_times.items():
                now_local = now_utc.astimezone(target_time.tzinfo)
                delta = abs((now_local - target_time).total_seconds())
                if delta > self.window_seconds and not force:
                    continue

                existed = await self._event_sent(user_id, market, target_time.date().isoformat(), push_type)
                if existed and not force:
                    stats["skipped"] += 1
                    continue

                try:
                    brief = await advisor_service.get_daily_brief(user_id=user_id, trade_date=target_time.date().isoformat())
                    chat_id = settings.FEISHU_BOT_DEFAULT_CHAT_ID
                    if not chat_id:
                        await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "failed", "missing_chat_id")
                        stats["failed"] += 1
                        continue
                    result = await feishu_push_service.push_daily_brief(chat_id=chat_id, brief=brief, market=market, push_type=push_type)
                    if result.get("success"):
                        await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "sent", None)
                        stats["sent"] += 1
                    else:
                        await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "failed", "push_failed")
                        stats["failed"] += 1
                except Exception as exc:
                    await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "failed", str(exc))
                    stats["failed"] += 1

        return stats

    async def next_push_times(self) -> List[Dict[str, str]]:
        await self._ensure_indexes()
        now_utc = datetime.now(ZoneInfo("UTC"))
        markets = [m.strip().upper() for m in settings.MARKET_PUSH_MARKETS.split(",") if m.strip()]
        rows: List[Dict[str, str]] = []
        for market in markets:
            points = market_calendar_service.today_push_times(market, ref_dt=now_utc)
            for push_type, dt in points.items():
                rows.append({
                    "market": market,
                    "push_type": push_type,
                    "scheduled_at": dt.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
                })
        rows.sort(key=lambda x: x["scheduled_at"])
        return rows

    async def dispatch_one(self, user_id: str, market: str, push_type: str, force: bool = False) -> Dict[str, int]:
        await self._ensure_indexes()
        market = market.upper()
        if not market_calendar_service.is_trading_day(market) and not force:
            return {"sent": 0, "skipped": 1, "failed": 0}

        points = market_calendar_service.today_push_times(market)
        target_time = points.get(push_type)
        if not target_time:
            return {"sent": 0, "skipped": 1, "failed": 0}

        existed = await self._event_sent(user_id, market, target_time.date().isoformat(), push_type)
        if existed and not force:
            return {"sent": 0, "skipped": 1, "failed": 0}

        try:
            brief = await advisor_service.get_daily_brief(user_id=user_id, trade_date=target_time.date().isoformat())
            chat_id = settings.FEISHU_BOT_DEFAULT_CHAT_ID
            if not chat_id:
                await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "failed", "missing_chat_id")
                return {"sent": 0, "skipped": 0, "failed": 1}
            result = await feishu_push_service.push_daily_brief(chat_id=chat_id, brief=brief, market=market, push_type=push_type)
            if result.get("success"):
                await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "sent", None)
                return {"sent": 1, "skipped": 0, "failed": 0}
            await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "failed", "push_failed")
            return {"sent": 0, "skipped": 0, "failed": 1}
        except Exception as exc:
            await self._record_event(user_id, market, target_time.date().isoformat(), push_type, target_time, "failed", str(exc))
            return {"sent": 0, "skipped": 0, "failed": 1}

    async def _event_sent(self, user_id: str, market: str, trade_date: str, push_type: str) -> bool:
        db = get_mongo_db()
        doc = await db.market_push_events.find_one(
            {
                "user_id": user_id,
                "market": market,
                "trading_date": trade_date,
                "push_type": push_type,
                "status": "sent",
            }
        )
        return bool(doc)

    async def _ensure_indexes(self) -> None:
        if self._index_ensured:
            return
        db = get_mongo_db()
        await db.market_push_events.create_index(
            [("user_id", 1), ("market", 1), ("trading_date", 1), ("push_type", 1)],
            unique=True,
            name="uniq_market_push_event",
        )
        await db.feishu_pending_actions.create_index(
            [("expires_at", 1)],
            expireAfterSeconds=0,
            name="ttl_feishu_pending_actions",
        )
        self._index_ensured = True

    async def _record_event(
        self,
        user_id: str,
        market: str,
        trade_date: str,
        push_type: str,
        scheduled_at: datetime,
        status: str,
        reason: str | None,
    ) -> None:
        db = get_mongo_db()
        event_id = f"{user_id}:{market}:{trade_date}:{push_type}"
        await db.market_push_events.update_one(
            {"event_id": event_id},
            {
                "$set": {
                    "event_id": event_id,
                    "user_id": user_id,
                    "market": market,
                    "trading_date": trade_date,
                    "push_type": push_type,
                    "scheduled_at": scheduled_at,
                    "status": status,
                    "reason": reason,
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )


market_push_schedule_service = MarketPushScheduleService()
