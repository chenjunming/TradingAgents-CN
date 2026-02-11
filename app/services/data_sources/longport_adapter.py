"""
Longport (长桥) data source adapter.

Current scope:
- Realtime quotes only (batch quote by symbol list)
- Other interfaces return None so manager can fallback to other sources
"""
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, List, Tuple

import pandas as pd

from .base import DataSourceAdapter

logger = logging.getLogger(__name__)


class LongportAdapter(DataSourceAdapter):
    """Longport 数据源适配器（实时行情）"""

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "longport"

    def _get_default_priority(self) -> int:
        # 默认高于 tushare，便于用户启用后优先用长桥实时行情
        return 4

    def _read_credentials(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Read credentials from env first, then DB config as fallback."""
        app_key = os.getenv("LONGPORT_APP_KEY")
        app_secret = os.getenv("LONGPORT_APP_SECRET")
        access_token = os.getenv("LONGPORT_ACCESS_TOKEN")

        if app_key and app_secret and access_token:
            return app_key, app_secret, access_token

        # Fallback: read from active system config in DB
        try:
            from app.core.database import get_mongo_db_sync
            db = get_mongo_db_sync()
            config_data = db.system_configs.find_one({"is_active": True}, sort=[("version", -1)])
            if not config_data:
                return app_key, app_secret, access_token

            for ds in config_data.get("data_source_configs", []):
                ds_type = str(ds.get("type", "")).lower()
                ds_name = str(ds.get("name", "")).lower()
                if ds_type != "longport" and ds_name != "longport":
                    continue

                cfg = ds.get("config_params", {}) or {}
                app_key = app_key or ds.get("api_key")
                app_secret = app_secret or ds.get("api_secret")
                access_token = access_token or cfg.get("access_token")
                break
        except Exception as e:
            logger.debug(f"Longport: read credentials from DB failed: {e}")

        return app_key, app_secret, access_token

    def is_available(self) -> bool:
        try:
            import longport.openapi  # noqa: F401
        except ImportError:
            return False

        app_key, app_secret, access_token = self._read_credentials()
        return bool(app_key and app_secret and access_token)

    def get_stock_list(self) -> Optional[pd.DataFrame]:
        return None

    def get_daily_basic(self, trade_date: str) -> Optional[pd.DataFrame]:
        return None

    def find_latest_trade_date(self) -> Optional[str]:
        # 交给其他数据源处理，避免影响交易日判断精度
        return None

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _to_longport_symbol(code: str) -> Optional[str]:
        code6 = str(code).strip().zfill(6)
        if not code6.isdigit():
            return None
        if code6.startswith(("60", "68", "90")):
            return f"{code6}.SH"
        if code6.startswith(("00", "30", "20")):
            return f"{code6}.SZ"
        # 暂不处理北交所等其他前缀
        return None

    @staticmethod
    def _from_longport_symbol(symbol: str) -> Optional[str]:
        if not symbol:
            return None
        code = str(symbol).split(".")[0].strip()
        return code.zfill(6) if code.isdigit() else None

    def _get_target_codes(self, max_symbols: int) -> List[str]:
        """
        Collect target A-share universe from DB:
        1. market_quotes.code (already used by app)
        2. stock_basic_info.code (fallback)
        """
        codes: List[str] = []
        try:
            from app.core.database import get_mongo_db_sync
            db = get_mongo_db_sync()

            # 优先 market_quotes，查询更轻
            mq_codes = db.market_quotes.distinct("code")
            for c in mq_codes:
                c6 = str(c).strip().zfill(6)
                if c6.isdigit():
                    codes.append(c6)
                if len(codes) >= max_symbols:
                    return codes

            # 回退到 stock_basic_info
            need = max_symbols - len(codes)
            if need > 0:
                sb_codes = db.stock_basic_info.distinct("code")
                seen = set(codes)
                for c in sb_codes:
                    c6 = str(c).strip().zfill(6)
                    if c6.isdigit() and c6 not in seen:
                        codes.append(c6)
                        seen.add(c6)
                    if len(codes) >= max_symbols:
                        break
        except Exception as e:
            logger.warning(f"Longport: load target codes from DB failed: {e}")

        return codes

    def get_realtime_quotes(self) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
        if not self.is_available():
            return None

        try:
            from longport.openapi import Config, QuoteContext
        except ImportError:
            logger.warning("Longport SDK not installed")
            return None

        app_key, app_secret, access_token = self._read_credentials()
        if not (app_key and app_secret and access_token):
            logger.warning("Longport credentials are missing")
            return None

        # Ensure Config.from_env can read credentials
        os.environ["LONGPORT_APP_KEY"] = app_key
        os.environ["LONGPORT_APP_SECRET"] = app_secret
        os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

        max_symbols = int(os.getenv("LONGPORT_MAX_SYMBOLS", "5000"))
        batch_size = int(os.getenv("LONGPORT_BATCH_SIZE", "500"))

        codes = self._get_target_codes(max_symbols=max_symbols)
        if not codes:
            logger.warning("Longport: no target codes loaded from DB")
            return None

        symbols = [s for s in (self._to_longport_symbol(c) for c in codes) if s]
        if not symbols:
            logger.warning("Longport: no convertible A-share symbols")
            return None

        result: Dict[str, Dict[str, Optional[float]]] = {}
        total = 0
        failed_batches = 0

        try:
            config = Config.from_env()
            ctx = QuoteContext(config)
            for i in range(0, len(symbols), max(1, batch_size)):
                batch = symbols[i:i + batch_size]
                try:
                    quotes = ctx.quote(batch)
                except Exception as e:
                    failed_batches += 1
                    logger.warning(f"Longport quote batch failed ({i}-{i + len(batch)}): {e}")
                    continue

                for q in quotes or []:
                    code6 = self._from_longport_symbol(getattr(q, "symbol", ""))
                    if not code6:
                        continue

                    close = self._safe_float(
                        getattr(q, "last_done", None) or getattr(q, "latest_done", None)
                    )
                    pre_close = self._safe_float(getattr(q, "prev_close", None))
                    pct_chg = None
                    if close is not None and pre_close not in (None, 0, 0.0):
                        pct_chg = (close / pre_close - 1.0) * 100.0

                    result[code6] = {
                        "close": close,
                        "pct_chg": pct_chg,
                        "amount": self._safe_float(getattr(q, "turnover", None)),
                        "volume": self._safe_float(getattr(q, "volume", None)),
                        "open": self._safe_float(getattr(q, "open", None)),
                        "high": self._safe_float(getattr(q, "high", None)),
                        "low": self._safe_float(getattr(q, "low", None)),
                        "pre_close": pre_close,
                    }
                    total += 1
        except Exception as e:
            logger.error(f"Longport realtime quotes failed: {e}")
            return None

        if not result:
            logger.warning("Longport realtime quotes returned empty data")
            return None

        logger.info(
            f"✅ Longport realtime quotes loaded: {len(result)} symbols, "
            f"parsed={total}, failed_batches={failed_batches}"
        )
        return result

    def get_kline(self, code: str, period: str = "day", limit: int = 120, adj: Optional[str] = None):
        return None

    def get_news(self, code: str, days: int = 2, limit: int = 50, include_announcements: bool = True):
        return None
