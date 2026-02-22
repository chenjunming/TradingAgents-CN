from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.database import get_mongo_db
from app.services.data_sources.manager import DataSourceManager
from app.services.foreign_stock_service import ForeignStockService
from app.services.portfolio_service import portfolio_service
from app.services.signals.dedupe import SignalDedupe
from app.services.signals.indicators import build_indicator_context
from app.services.signals.models import MarketSnapshot, ScanResult
from app.services.signals.notifier import SignalNotifier
from app.services.signals.rule_dsl import load_ruleset_from_glob
from app.services.signals.rule_engine import RuleEngine
from app.services.signals.store import SignalStore
from app.services.market_calendar_service import market_calendar_service

logger = logging.getLogger(__name__)

_SIGNALS_RECEIVE_ID_USER_TYPES = {"open_id", "user_id", "union_id"}


def resolve_default_signals_user_id() -> str:
    rid_type = str(settings.FEISHU_SIGNAL_RECEIVE_ID_TYPE or "").strip().lower()
    rid = str(settings.FEISHU_SIGNAL_RECEIVE_ID or "").strip()
    if rid and rid_type in _SIGNALS_RECEIVE_ID_USER_TYPES:
        return rid
    return "default"


DEFAULT_SIGNALS_USER_ID = resolve_default_signals_user_id()
DEFAULT_SIGNALS_RULES_GLOB = "rules/*.yaml"


class SignalScanner:
    def __init__(self):
        self.store = SignalStore()
        self.dedupe = SignalDedupe(self.store)
        self.notifier = SignalNotifier()
        self.rule_engine = RuleEngine()
        self.data_source = None
        self.foreign_stock_service = None

    def _get_data_source(self):
        if self.data_source is None:
            self.data_source = DataSourceManager()
        return self.data_source

    def _get_foreign_stock_service(self):
        if self.foreign_stock_service is None:
            try:
                self.foreign_stock_service = ForeignStockService(db=get_mongo_db())
            except Exception:
                self.foreign_stock_service = ForeignStockService(db=None)
        return self.foreign_stock_service

    def _normalize_market(self, raw: Any) -> str:
        s = str(raw or "").strip().upper()
        if s in {"CN", "A股", "CHINA_A", "CHINA"}:
            return "CN"
        if s in {"HK", "港股", "HONG_KONG", "HONGKONG"}:
            return "HK"
        if s in {"US", "USA", "美股"}:
            return "US"
        return s

    def _normalize_symbol(self, raw: Any, market: str) -> str:
        symbol = str(raw or "").strip().upper()
        if market == "CN":
            return symbol.zfill(6)
        if market == "HK":
            digits = "".join(ch for ch in symbol if ch.isdigit())
            return (digits or symbol).zfill(5)
        if market == "US":
            return re.sub(r"\.US$", "", symbol, flags=re.IGNORECASE)
        return symbol

    async def _collect_targets(self, user_id: str, override_tickers: Optional[List[str]] = None) -> List[Tuple[str, str]]:
        if override_tickers:
            result: List[Tuple[str, str]] = []
            for item in override_tickers:
                token = str(item).strip()
                if not token:
                    continue
                if ":" in token:
                    market, symbol = token.split(":", 1)
                    mk = self._normalize_market(market)
                    result.append((self._normalize_symbol(symbol, mk), mk))
                else:
                    result.append((token.upper(), "US"))
            return result

        db = get_mongo_db()
        merged: Dict[str, Tuple[str, str]] = {}

        fav_doc = await db.user_favorites.find_one({"user_id": user_id}, {"_id": 0, "favorites": 1})
        for fav in (fav_doc or {}).get("favorites", []):
            mk = self._normalize_market(fav.get("market"))
            symbol = self._normalize_symbol(fav.get("stock_code"), mk)
            if not symbol or not mk:
                continue
            merged[f"{mk}:{symbol}"] = (symbol, mk)

        for p in await portfolio_service.get_unified_positions(user_id):
            mk = self._normalize_market(getattr(p, "market", ""))
            symbol = self._normalize_symbol(getattr(p, "symbol", ""), mk)
            if not symbol or not mk:
                continue
            merged[f"{mk}:{symbol}"] = (symbol, mk)

        return list(merged.values())

    async def _get_price_from_db(self, symbol: str) -> Optional[float]:
        db = get_mongo_db()
        doc = await db.market_quotes.find_one({"$or": [{"code": symbol}, {"symbol": symbol}]}, {"_id": 0, "close": 1})
        if not doc:
            return None
        try:
            return float(doc.get("close")) if doc.get("close") is not None else None
        except Exception:
            return None

    async def _get_market_price(self, symbol: str, market: str) -> Optional[float]:
        mk = self._normalize_market(market)
        if mk == "HK":
            try:
                db = get_mongo_db()
                doc = await db.market_quotes_hk.find_one(
                    {"$or": [{"code": symbol}, {"symbol": symbol}]},
                    {"_id": 0, "close": 1},
                )
                if doc and doc.get("close") is not None:
                    try:
                        return float(doc.get("close"))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                quote = await self._get_foreign_stock_service().get_quote(market="HK", code=symbol, force_refresh=False)
                for k in ("price", "close", "last_price"):
                    if quote.get(k) is not None:
                        return float(quote.get(k))
            except Exception:
                return None
            return None
        if mk == "US":
            try:
                db = get_mongo_db()
                doc = await db.market_quotes_us.find_one(
                    {"$or": [{"code": symbol}, {"symbol": symbol}]},
                    {"_id": 0, "close": 1},
                )
                if doc and doc.get("close") is not None:
                    try:
                        return float(doc.get("close"))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                quote = await self._get_foreign_stock_service().get_quote(market="US", code=symbol, force_refresh=False)
                for k in ("price", "close", "last_price"):
                    if quote.get(k) is not None:
                        return float(quote.get(k))
            except Exception:
                return None
            return None
        return await self._get_price_from_db(symbol)

    async def _get_market_kline(self, symbol: str, market: str, limit: int = 220) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        mk = self._normalize_market(market)
        if mk == "HK":
            try:
                items = await self._get_foreign_stock_service().get_kline(
                    market="HK",
                    code=symbol,
                    period="day",
                    limit=limit,
                    force_refresh=False,
                )
                if items:
                    return items, "foreign_hk"
            except Exception as exc:
                logger.warning("signals hk kline failed for %s: %s", symbol, exc)
            return None, None
        if mk == "US":
            try:
                items = await self._get_foreign_stock_service().get_kline(
                    market="US",
                    code=symbol,
                    period="day",
                    limit=limit,
                    force_refresh=False,
                )
                if items:
                    return items, "foreign_us"
            except Exception as exc:
                logger.warning("signals us kline failed for %s: %s", symbol, exc)
            return None, None
        kline, source = self._get_data_source().get_kline_with_fallback(code=symbol, period="day", limit=limit)
        return kline, source

    def _build_links(self, event_id: str, ticker: str, market: str, analysis_token: str) -> Dict[str, str]:
        api_base = f"http://127.0.0.1:{settings.PORT}"
        return {
            "web_ui": f"/signals/{event_id}",
            "analysis_url": f"{api_base}/api/signals/{event_id}/analysis/run?token={analysis_token}",
            "ack_url": f"{api_base}/api/signals/{event_id}/ack",
            "mute_url": f"{api_base}/api/signals/{event_id}/mute?days=7",
        }

    async def scan_once(
        self,
        user_id: str = DEFAULT_SIGNALS_USER_ID,
        force: bool = False,
        rules_glob: Optional[str] = None,
        tickers_override: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        result = ScanResult()
        pattern = rules_glob or DEFAULT_SIGNALS_RULES_GLOB

        await self.store.ensure_indexes()
        ruleset = load_ruleset_from_glob(pattern)
        await self.store.save_ruleset(version_hash=ruleset.version_hash, rules=ruleset.raw_rules)

        targets = await self._collect_targets(user_id=user_id, override_tickers=tickers_override)
        result.scanned = len(targets)

        for ticker, market in targets:
            try:
                market = self._normalize_market(market)
                if market not in {"CN", "HK", "US"}:
                    logger.warning("signals scan skipped: unsupported market for %s (%s)", ticker, market)
                    result.failed += 1
                    continue

                if settings.SIGNALS_RESPECT_MARKET_HOURS and not force:
                    if not market_calendar_service.is_market_open(market):
                        logger.info("signals scan skipped: market closed for %s (%s)", ticker, market)
                        result.skipped_market_closed += 1
                        continue

                price = await self._get_market_price(ticker, market)
                kline, source = await self._get_market_kline(symbol=ticker, market=market, limit=220)
                if not kline:
                    logger.warning("signals scan skipped: no kline for %s (%s)", ticker, market)
                    result.failed += 1
                    continue

                ctx = build_indicator_context(kline=kline, current_price=price)
                snapshot = MarketSnapshot(
                    ticker=ticker,
                    market=market,
                    timeframe="1d",
                    values=ctx.get("values", {}),
                    prev_values=ctx.get("prev_values", {}),
                    series=ctx.get("series", {}),
                )

                candidates = self.rule_engine.evaluate(snapshot=snapshot, rules=ruleset.rules, ruleset_version_hash=ruleset.version_hash)
                result.matched += len(candidates)

                for candidate in candidates:
                    decision = await self.dedupe.should_emit(
                        user_id=user_id,
                        ticker=candidate.ticker,
                        rule_id=candidate.rule_id,
                        level=candidate.level,
                        cooldown_minutes=candidate.cooldown_minutes,
                        force=force,
                    )
                    if not decision.emit:
                        if decision.reason == "muted":
                            result.skipped_muted += 1
                        else:
                            result.skipped_dedupe += 1
                        continue

                    event_id = f"evt_{uuid.uuid4().hex[:16]}"
                    action_token = secrets.token_urlsafe(24)
                    event = {
                        "event_id": event_id,
                        "ts": datetime.utcnow(),
                        "user_id": user_id,
                        "ticker": candidate.ticker,
                        "market": candidate.market,
                        "rule_id": candidate.rule_id,
                        "rule_name": candidate.rule_name,
                        "level": candidate.level,
                        "ruleset_version_hash": candidate.ruleset_version_hash,
                        "dedupe_key": f"{candidate.ticker}|{candidate.rule_id}|{candidate.level}",
                        "cooldown_until": decision.cooldown_until,
                        "snapshot": candidate.snapshot,
                        "evidence": candidate.evidence,
                        "status": {
                            "acked": False,
                            "muted": False,
                            "mute_until": None,
                        },
                        "links": self._build_links(
                            event_id=event_id,
                            ticker=candidate.ticker,
                            market=candidate.market,
                            analysis_token=action_token,
                        ),
                        "action_tokens": {
                            "card_action": action_token,
                            "analysis": action_token,
                        },
                        "action_hint": candidate.action_hint,
                        "meta": {
                            "kline_source": source,
                        },
                    }
                    await self.store.save_event(event)
                    push_result = await self.notifier.send_feishu_card(event)
                    event["notify"] = {
                        "success": bool(push_result.get("success")),
                        "reason": push_result.get("reason"),
                    }
                    result.emitted += 1
                    result.events.append(event)
            except Exception as exc:
                logger.error("signals scan failed for %s(%s): %s", ticker, market, exc, exc_info=True)
                result.failed += 1

        return {
            "scanned": result.scanned,
            "matched": result.matched,
            "emitted": result.emitted,
            "skipped_market_closed": result.skipped_market_closed,
            "skipped_dedupe": result.skipped_dedupe,
            "skipped_muted": result.skipped_muted,
            "failed": result.failed,
            "events": result.events,
            "ruleset_version_hash": ruleset.version_hash,
        }


signal_scanner = SignalScanner()
