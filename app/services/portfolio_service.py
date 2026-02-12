from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Set

from app.core.database import get_mongo_db
from app.models.advisor_models import UnifiedPosition
from app.services.a_share_sqlite_service import get_a_share_sqlite_service
from app.services.longport_portfolio_service import get_longport_portfolio_service


class PortfolioService:
    async def get_unified_positions(self, user_id: str) -> List[UnifiedPosition]:
        cn_positions = get_a_share_sqlite_service().list_positions()

        hk_us_positions: List[UnifiedPosition] = []
        stale = False
        try:
            # 优先复用已有的 longport 同步结果（paper_positions），避免重复拉取和口径不一致
            hk_us_positions = await self._load_positions_from_paper_sync(user_id)
            if not hk_us_positions:
                creds = await self._resolve_longport_credentials(user_id)
                if creds:
                    hk_us_positions = get_longport_portfolio_service().get_positions_with_credentials(
                        creds["app_key"],
                        creds["app_secret"],
                        creds["access_token"],
                    )
                else:
                    hk_us_positions = get_longport_portfolio_service().get_positions()
        except Exception:
            stale = True
            hk_us_positions = await self._load_last_snapshot(user_id)
            for p in hk_us_positions:
                p.stale_data = True

        # 去重合并：同 market+symbol 仅保留一条，优先使用 longport 同步数据
        merged: Dict[tuple[str, str], UnifiedPosition] = {}
        for p in cn_positions:
            key = (str(p.market or "").upper(), str(p.symbol or "").upper())
            merged[key] = p
        for p in hk_us_positions:
            key = (str(p.market or "").upper(), str(p.symbol or "").upper())
            merged[key] = p

        all_positions = list(merged.values())
        await self._save_snapshot(user_id, all_positions, stale)
        return all_positions

    async def get_exposure_markets(self, user_id: str) -> Set[str]:
        markets: Set[str] = set()
        for p in await self.get_unified_positions(user_id):
            if p.quantity and p.quantity > 0:
                markets.add(p.market)

        # 关注列表作为补充
        watch_markets = await self._get_watchlist_markets(user_id)
        markets.update(watch_markets)
        return markets

    async def bind_longport_credentials(self, user_id: str, app_key: str, app_secret: str, access_token: str) -> None:
        db = get_mongo_db()
        await db.longport_bindings.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "app_key": app_key,
                    "app_secret": app_secret,
                    "access_token": access_token,
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )

    async def _save_snapshot(self, user_id: str, positions: List[UnifiedPosition], stale: bool) -> None:
        db = get_mongo_db()
        payload = {
            "user_id": user_id,
            "created_at": datetime.utcnow(),
            "stale": stale,
            "positions": [p.model_dump() for p in positions],
        }
        await db.portfolio_snapshots.insert_one(payload)

    async def _load_last_snapshot(self, user_id: str) -> List[UnifiedPosition]:
        db = get_mongo_db()
        doc = await db.portfolio_snapshots.find_one(
            {"user_id": user_id}, sort=[("created_at", -1)]
        )
        if not doc:
            return []
        result: List[UnifiedPosition] = []
        for item in doc.get("positions", []):
            try:
                result.append(UnifiedPosition.model_validate(item))
            except Exception:
                continue
        return result

    async def _get_watchlist_markets(self, user_id: str) -> Set[str]:
        db = get_mongo_db()
        markets: Set[str] = set()
        doc = await db.user_favorites.find_one({"user_id": user_id})
        favorites = (doc or {}).get("favorites", [])
        for fav in favorites:
            raw_market = str(fav.get("market", "")).upper()
            if raw_market in {"A股", "CN", "CHINA_A"}:
                markets.add("CN")
            elif raw_market in {"港股", "HK", "HONG_KONG"}:
                markets.add("HK")
            elif raw_market in {"美股", "US", "USA"}:
                markets.add("US")
        return markets

    async def _resolve_longport_credentials(self, user_id: str) -> Optional[Dict[str, str]]:
        db = get_mongo_db()
        doc = await db.longport_bindings.find_one({"user_id": user_id})
        if not doc:
            return None
        app_key = str(doc.get("app_key") or "")
        app_secret = str(doc.get("app_secret") or "")
        access_token = str(doc.get("access_token") or "")
        if app_key and app_secret and access_token:
            return {
                "app_key": app_key,
                "app_secret": app_secret,
                "access_token": access_token,
            }
        return None

    async def _load_positions_from_paper_sync(self, user_id: str) -> List[UnifiedPosition]:
        db = get_mongo_db()
        cursor = db.paper_positions.find(
            {"user_id": user_id, "source": "longport"},
            {"_id": 0},
        )
        rows = await cursor.to_list(length=None)
        result: List[UnifiedPosition] = []
        for row in rows:
            market = str(row.get("market") or "").upper()
            if market not in {"CN", "HK", "US"}:
                continue
            quantity = float(row.get("quantity") or 0.0)
            if quantity <= 0:
                continue
            avg_cost = row.get("avg_cost")
            avg_cost_f = float(avg_cost) if avg_cost is not None else None
            market_value = row.get("market_value")
            market_value_f = float(market_value) if market_value is not None else None
            result.append(
                UnifiedPosition(
                    symbol=str(row.get("code") or ""),
                    name=str(row.get("symbol_name") or ""),
                    market=market,
                    quantity=quantity,
                    available_quantity=float(row.get("available_qty")) if row.get("available_qty") is not None else None,
                    avg_cost=avg_cost_f,
                    market_value=market_value_f,
                    currency=str(row.get("currency") or ("HKD" if market == "HK" else "USD")),
                    source="paper_longport_sync",
                    stale_data=False,
                )
            )
        return result


portfolio_service = PortfolioService()
