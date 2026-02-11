from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional

from app.core.database import get_mongo_db
from app.models.advisor_models import CandidateStock, DailyBriefResponse, RebalanceAction, UnifiedPosition
from app.services.a_share_sqlite_service import get_a_share_sqlite_service
from app.services.portfolio_service import portfolio_service


class AdvisorService:
    async def generate_daily_picks(
        self,
        user_id: str,
        market: Optional[str] = None,
        trade_date: Optional[str] = None,
    ) -> List[CandidateStock]:
        db = get_mongo_db()
        trade_date = trade_date or datetime.utcnow().date().isoformat()
        symbols = await self._collect_candidate_symbols(user_id=user_id, market=market)

        picks: List[CandidateStock] = []
        for info in symbols:
            symbol = info["symbol"]
            mk = info["market"]
            quote = await self._get_quote_snapshot(symbol, mk)
            score, breakdown = self._score_stock(quote)
            action = "increase" if score >= 75 else "watch"
            reason = self._build_reason(breakdown)
            risk = "波动风险可控" if score >= 75 else "信号不足，建议观察"
            invalid_condition = "收盘跌破5日均线且成交量放大" if score >= 75 else "未形成有效趋势"
            target_weight = 0.08 if score >= 85 else (0.05 if score >= 75 else 0.0)
            picks.append(
                CandidateStock(
                    symbol=symbol,
                    name=info.get("name", ""),
                    market=mk,
                    score_total=score,
                    score_breakdown=breakdown,
                    action=action,
                    reason=reason,
                    risk=risk,
                    invalid_condition=invalid_condition,
                    target_weight=target_weight,
                )
            )

        picks.sort(key=lambda x: x.score_total, reverse=True)
        await self._save_picks_sqlite(trade_date, picks)
        await db.advisor_daily_briefs.update_one(
            {"user_id": user_id, "trade_date": trade_date},
            {"$set": {"updated_at": datetime.utcnow(), "picks": [p.model_dump() for p in picks]}},
            upsert=True,
        )
        return picks

    async def generate_rebalance(
        self,
        user_id: str,
        market: Optional[str] = None,
        trade_date: Optional[str] = None,
    ) -> List[RebalanceAction]:
        trade_date = trade_date or datetime.utcnow().date().isoformat()
        positions = await portfolio_service.get_unified_positions(user_id)
        if market:
            positions = [p for p in positions if p.market == market]

        total_mv = sum(max(float(p.market_value or 0), 0) for p in positions) or 1.0
        actions: List[RebalanceAction] = []

        for p in positions:
            mv = float(p.market_value or 0)
            current_weight = mv / total_mv
            target_weight = current_weight
            action = "hold"
            reason = "仓位在风险阈值内"
            risk = "常规波动"
            invalid_condition = "基本面显著恶化"

            if current_weight > 0.12:
                target_weight = 0.10
                action = "reduce"
                reason = "单票权重超过12%，触发稳健减仓"
                risk = "集中度过高"
                invalid_condition = "权重回落至10%以下"
            elif current_weight < 0.03:
                action = "watch"
                reason = "仓位较小，维持观察"
                risk = "贡献有限"
                invalid_condition = "出现明确增强信号"

            actions.append(
                RebalanceAction(
                    symbol=p.symbol,
                    name=p.name,
                    market=p.market,
                    action=action,
                    current_weight=round(current_weight, 4),
                    target_weight=round(target_weight, 4),
                    delta_weight=round(target_weight - current_weight, 4),
                    reason=reason,
                    risk=risk,
                    invalid_condition=invalid_condition,
                    priority=1 if action == "reduce" else 2,
                )
            )

        actions.sort(key=lambda x: (x.priority, -abs(x.delta_weight)))
        await self._save_rebalance_sqlite(trade_date, actions)
        db = get_mongo_db()
        await db.advisor_daily_briefs.update_one(
            {"user_id": user_id, "trade_date": trade_date},
            {"$set": {"updated_at": datetime.utcnow(), "rebalance": [a.model_dump() for a in actions]}},
            upsert=True,
        )
        return actions

    async def get_daily_brief(
        self,
        user_id: str,
        trade_date: Optional[str] = None,
    ) -> DailyBriefResponse:
        trade_date = trade_date or datetime.utcnow().date().isoformat()
        db = get_mongo_db()
        doc = await db.advisor_daily_briefs.find_one({"user_id": user_id, "trade_date": trade_date})

        positions = await portfolio_service.get_unified_positions(user_id)
        picks = [CandidateStock.model_validate(x) for x in (doc or {}).get("picks", [])]
        rebalance = [RebalanceAction.model_validate(x) for x in (doc or {}).get("rebalance", [])]

        summary = f"{trade_date} 投研摘要：候选{len(picks)}只，调仓动作{len(rebalance)}条"
        risk_alerts: List[str] = []
        if any(r.action == "reduce" for r in rebalance):
            risk_alerts.append("存在仓位超限标的，建议优先减仓")

        brief = DailyBriefResponse(
            date=trade_date,
            user_id=user_id,
            summary=summary,
            positions=positions,
            picks=picks,
            rebalance=rebalance,
            risk_alerts=risk_alerts,
            generated_at=datetime.utcnow(),
        )

        sqlite = get_a_share_sqlite_service()
        with sqlite._lock:
            conn = sqlite._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_brief(trade_date, payload_json, created_at) VALUES(?,?,?)",
                    (trade_date, brief.model_dump_json(), datetime.utcnow().isoformat()),
                )
                conn.commit()
            finally:
                conn.close()
        return brief

    async def _collect_candidate_symbols(self, user_id: str, market: Optional[str]) -> List[Dict[str, str]]:
        db = get_mongo_db()
        symbols: Dict[str, Dict[str, str]] = {}

        favorites = await db.user_favorites.find_one({"user_id": user_id})
        for f in (favorites or {}).get("favorites", []):
            symbol = str(f.get("stock_code") or "").strip()
            if not symbol:
                continue
            m = self._normalize_market(f.get("market"))
            if market and m != market:
                continue
            symbols[f"{m}:{symbol}"] = {"symbol": symbol, "market": m, "name": f.get("stock_name", "")}

        positions = await portfolio_service.get_unified_positions(user_id)
        for p in positions:
            if market and p.market != market:
                continue
            symbols[f"{p.market}:{p.symbol}"] = {"symbol": p.symbol, "market": p.market, "name": p.name}

        return list(symbols.values())

    async def _get_quote_snapshot(self, symbol: str, market: str) -> Dict[str, float]:
        db = get_mongo_db()
        if market == "CN":
            row = await db.market_quotes.find_one({"code": symbol.zfill(6)})
            if not row:
                return {"pct_chg": 0.0, "pe": 15.0, "pb": 1.5, "amount": 0.0}
            return {
                "pct_chg": float(row.get("pct_chg") or 0.0),
                "pe": float(row.get("pe") or 15.0),
                "pb": float(row.get("pb") or 1.5),
                "amount": float(row.get("amount") or 0.0),
            }

        return {"pct_chg": 0.0, "pe": 20.0, "pb": 2.0, "amount": 0.0}

    def _score_stock(self, quote: Dict[str, float]) -> tuple[float, Dict[str, float]]:
        pct = float(quote.get("pct_chg") or 0.0)
        pe = float(quote.get("pe") or 20.0)
        pb = float(quote.get("pb") or 2.0)
        amount = float(quote.get("amount") or 0.0)

        fundamentals = max(0.0, min(35.0, 35.0 - max(pe - 20, 0) * 1.2 - max(pb - 3, 0) * 3))
        valuation = max(0.0, min(20.0, 20.0 - max(pe - 18, 0) * 0.8))
        trend = max(0.0, min(20.0, 10.0 + pct * 2.0))
        sentiment = 10.0 if pct >= 0 else 7.0
        liquidity = 10.0 if amount >= 1e8 else 6.0

        total = round(fundamentals + valuation + trend + sentiment + liquidity, 2)
        breakdown = {
            "fundamentals": round(fundamentals, 2),
            "valuation": round(valuation, 2),
            "trend": round(trend, 2),
            "sentiment": round(sentiment, 2),
            "liquidity": round(liquidity, 2),
        }
        return total, breakdown

    @staticmethod
    def _build_reason(breakdown: Dict[str, float]) -> str:
        top = sorted(breakdown.items(), key=lambda x: x[1], reverse=True)[:2]
        return "；".join([f"{k}得分{v}" for k, v in top])

    @staticmethod
    def _normalize_market(raw: object) -> str:
        s = str(raw or "").upper()
        if s in {"CN", "A股", "CHINA_A"}:
            return "CN"
        if s in {"HK", "港股", "HONG_KONG"}:
            return "HK"
        return "US"

    async def _save_picks_sqlite(self, trade_date: str, picks: List[CandidateStock]) -> None:
        sqlite = get_a_share_sqlite_service()
        with sqlite._lock:
            conn = sqlite._connect()
            try:
                conn.execute("DELETE FROM advisor_picks WHERE trade_date=?", (trade_date,))
                for p in picks:
                    conn.execute(
                        """
                        INSERT INTO advisor_picks(
                            trade_date, symbol, name, market, score_total, action, reason, risk, invalid_condition, target_weight, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            trade_date,
                            p.symbol,
                            p.name,
                            p.market,
                            p.score_total,
                            p.action,
                            p.reason,
                            p.risk,
                            p.invalid_condition,
                            p.target_weight,
                            datetime.utcnow().isoformat(),
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    async def _save_rebalance_sqlite(self, trade_date: str, rebalance: List[RebalanceAction]) -> None:
        sqlite = get_a_share_sqlite_service()
        with sqlite._lock:
            conn = sqlite._connect()
            try:
                conn.execute("DELETE FROM advisor_rebalance WHERE trade_date=?", (trade_date,))
                for r in rebalance:
                    conn.execute(
                        """
                        INSERT INTO advisor_rebalance(
                            trade_date, symbol, name, market, action, current_weight, target_weight, delta_weight,
                            reason, risk, invalid_condition, priority, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            trade_date,
                            r.symbol,
                            r.name,
                            r.market,
                            r.action,
                            r.current_weight,
                            r.target_weight,
                            r.delta_weight,
                            r.reason,
                            r.risk,
                            r.invalid_condition,
                            r.priority,
                            datetime.utcnow().isoformat(),
                        ),
                    )
                conn.commit()
            finally:
                conn.close()


advisor_service = AdvisorService()
