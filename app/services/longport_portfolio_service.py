from __future__ import annotations

import os
from typing import Any, List, Optional

from app.core.config import settings
from app.models.advisor_models import UnifiedPosition


class LongPortPortfolioService:
    def _credentials(self) -> tuple[str, str, str]:
        return (
            settings.LONGPORT_APP_KEY or os.getenv("LONGPORT_APP_KEY", ""),
            settings.LONGPORT_APP_SECRET or os.getenv("LONGPORT_APP_SECRET", ""),
            settings.LONGPORT_ACCESS_TOKEN or os.getenv("LONGPORT_ACCESS_TOKEN", ""),
        )

    @staticmethod
    def _available_from(app_key: str, app_secret: str, access_token: str) -> bool:
        return bool(app_key and app_secret and access_token)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _attr(obj: Any, *candidates: str) -> Any:
        for name in candidates:
            if hasattr(obj, name):
                return getattr(obj, name)
        return None

    @staticmethod
    def _parse_symbol(symbol: str) -> tuple[str, str]:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return "", "US"

        if "." in raw:
            parts = raw.split(".")
            code = parts[0]
            suf = parts[-1]
            if suf in {"HK"}:
                return code, "HK"
            if suf in {"US"}:
                return code, "US"
            if suf in {"SH", "SZ"}:
                return code.zfill(6), "CN"

        if raw.isdigit() and len(raw) <= 5:
            return raw.zfill(5), "HK"

        return raw, "US"

    def get_positions_with_credentials(
        self,
        app_key: str,
        app_secret: str,
        access_token: str,
    ) -> List[UnifiedPosition]:
        if not self._available_from(app_key, app_secret, access_token):
            return []

        from longport.openapi import Config, TradeContext

        os.environ["LONGPORT_APP_KEY"] = app_key
        os.environ["LONGPORT_APP_SECRET"] = app_secret
        os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

        cfg = Config.from_env()
        positions: List[UnifiedPosition] = []

        ctx = TradeContext(cfg)
        raw_positions = ctx.stock_positions() or []
        for item in raw_positions:
            symbol_raw = self._attr(item, "symbol", "stock_symbol")
            code, market = self._parse_symbol(symbol_raw)
            if market not in {"HK", "US"}:
                continue

            quantity = self._safe_float(self._attr(item, "quantity", "qty", "total_qty")) or 0.0
            current_price = self._safe_float(self._attr(item, "last_done", "current_price", "market_price"))
            avg_cost = self._safe_float(self._attr(item, "cost_price", "avg_price", "average_cost"))
            market_value = self._safe_float(self._attr(item, "market_value", "position_value"))
            pnl = self._safe_float(self._attr(item, "unrealized_pnl", "unrealized_pl", "profit"))
            pnl_pct = self._safe_float(self._attr(item, "unrealized_pnl_ratio", "profit_ratio"))

            if market_value is None and current_price is not None:
                market_value = current_price * quantity
            if pnl is None and market_value is not None and avg_cost is not None:
                pnl = market_value - (avg_cost * quantity)
            if pnl_pct is None and pnl is not None and avg_cost and quantity:
                base = avg_cost * quantity
                pnl_pct = (pnl / base) * 100 if base else None

            positions.append(
                UnifiedPosition(
                    symbol=code,
                    name=str(self._attr(item, "name", "stock_name") or ""),
                    market=market,
                    quantity=quantity,
                    available_quantity=self._safe_float(self._attr(item, "available_quantity", "available_qty")),
                    avg_cost=avg_cost,
                    current_price=current_price,
                    market_value=market_value,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    currency="HKD" if market == "HK" else "USD",
                    source="longport",
                    stale_data=False,
                )
            )

        return positions

    def get_positions(self) -> List[UnifiedPosition]:
        app_key, app_secret, access_token = self._credentials()
        return self.get_positions_with_credentials(app_key, app_secret, access_token)


_longport_portfolio_service: Optional[LongPortPortfolioService] = None


def get_longport_portfolio_service() -> LongPortPortfolioService:
    global _longport_portfolio_service
    if _longport_portfolio_service is None:
        _longport_portfolio_service = LongPortPortfolioService()
    return _longport_portfolio_service
