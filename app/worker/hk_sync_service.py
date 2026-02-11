#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股数据同步服务（全量股票池 + 定期增量更新）

能力：
1. 股票池全量入库：优先 AKShare（stock_hk_spot），失败则使用内置兜底列表
2. 基础信息增量更新：按批次写入 stock_basic_info_hk
3. 行情增量更新：按批次写入 market_quotes_hk，并可选用 Longport 补齐涨跌幅
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pymongo import UpdateOne

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.core.config import settings
from app.core.database import get_mongo_db
from tradingagents.dataflows.providers.hk.hk_stock import HKStockProvider

logger = logging.getLogger(__name__)


class HKSyncService:
    """港股数据同步服务（全量股票池 + 定期增量更新）"""

    HK_CODE_RE = re.compile(r"^\d{1,5}$")

    def __init__(self):
        self.db = get_mongo_db()
        self.settings = settings

        self.yfinance_provider = HKStockProvider()

        self.hk_stock_list: List[str] = []
        self.hk_stock_meta_map: Dict[str, Dict[str, Any]] = {}
        self._stock_list_source: str = "unknown"
        self._stock_list_cache_time: Optional[datetime] = None
        self._stock_list_cache_ttl = 3600 * 24

    async def initialize(self):
        try:
            await self.db.stock_universe_hk.create_index([("code", 1)], unique=True)
            await self.db.stock_universe_hk.create_index([("active", 1), ("last_basic_sync_at", 1), ("code", 1)])
            await self.db.stock_universe_hk.create_index([("active", 1), ("last_quote_sync_at", 1), ("code", 1)])
            await self.db.stock_basic_info_hk.create_index([("code", 1), ("source", 1)], unique=True)
            await self.db.market_quotes_hk.create_index([("code", 1)], unique=True)
        except Exception as e:
            logger.warning("⚠️ 港股同步索引初始化失败（忽略）: %s", e)
        logger.info("✅ 港股同步服务初始化完成")

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.utcnow()

    @classmethod
    def _normalize_hk_code(cls, code: Any) -> Optional[str]:
        s = str(code or "").strip().upper()
        if not s:
            return None
        if s.endswith(".HK"):
            s = s[:-3]
        s = s.lstrip("0") or "0"
        if not s.isdigit() or not cls.HK_CODE_RE.fullmatch(s):
            return None
        return s.zfill(5)

    @staticmethod
    def _chunked(seq: List[Any], size: int) -> List[List[Any]]:
        if size <= 0:
            size = 1000
        return [seq[i : i + size] for i in range(0, len(seq), size)]

    @staticmethod
    def _safe_float(v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            if isinstance(v, str):
                t = v.strip().replace(",", "")
                if not t or t in {"-", "--", "nan", "NaN", "None"}:
                    return None
                return float(t)
            return float(v)
        except Exception:
            return None

    @staticmethod
    def _safe_int(v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            if isinstance(v, str):
                t = v.strip().replace(",", "")
                if not t or t in {"-", "--", "nan", "NaN", "None"}:
                    return None
                return int(float(t))
            return int(v)
        except Exception:
            return None

    def _get_fallback_stock_list(self) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        fallback = [
            "00700", "09988", "03690", "01810", "00941", "00762", "00728", "00939", "01398", "03988",
            "00005", "01299", "02318", "02628", "00857", "00386", "01211", "02015", "09868", "09866",
        ]
        meta_map = {c: {"name": c, "exchange": "HKEX", "asset_type": "Equity"} for c in fallback}
        return fallback, meta_map

    def _get_hk_stock_list_from_akshare(self) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        try:
            import akshare as ak

            logger.info("🔄 从 AKShare 拉取港股股票池...")
            df = ak.stock_hk_spot()
            if df is None or df.empty:
                logger.warning("⚠️ AKShare 返回空港股列表")
                return [], {}

            codes: List[str] = []
            meta_map: Dict[str, Dict[str, Any]] = {}
            for _, row in df.iterrows():
                code = self._normalize_hk_code(row.get("代码") or row.get("symbol") or row.get("code"))
                if not code or code in meta_map:
                    continue
                name = str(row.get("中文名称") or row.get("名称") or row.get("name") or code).strip() or code
                meta_map[code] = {"name": name, "exchange": "HKEX", "asset_type": "Equity"}
                codes.append(code)

            logger.info("✅ AKShare 港股股票池拉取完成: %s 只", len(codes))
            return codes, meta_map
        except Exception as e:
            logger.warning("⚠️ AKShare 港股股票池拉取失败: %s", e)
            return [], {}

    def _get_hk_stock_universe(self, force_refresh: bool = False) -> Tuple[List[str], Dict[str, Dict[str, Any]], str]:
        if (
            not force_refresh
            and self.hk_stock_list
            and self._stock_list_cache_time
            and datetime.now() - self._stock_list_cache_time < timedelta(seconds=self._stock_list_cache_ttl)
        ):
            return self.hk_stock_list, self.hk_stock_meta_map, self._stock_list_source

        ak_list, ak_meta = self._get_hk_stock_list_from_akshare()
        if ak_list:
            chosen_list, chosen_meta, source = ak_list, ak_meta, "akshare"
        else:
            chosen_list, chosen_meta = self._get_fallback_stock_list()
            source = "fallback"

        dedup_codes: List[str] = []
        dedup_meta: Dict[str, Dict[str, Any]] = {}
        for code in chosen_list:
            c = self._normalize_hk_code(code)
            if not c or c in dedup_meta:
                continue
            dedup_codes.append(c)
            dedup_meta[c] = chosen_meta.get(c, {"name": c, "exchange": "HKEX", "asset_type": "Equity"})

        self.hk_stock_list = dedup_codes
        self.hk_stock_meta_map = dedup_meta
        self._stock_list_source = source
        self._stock_list_cache_time = datetime.now()

        logger.info("📦 港股股票池已更新: count=%s, source=%s", len(dedup_codes), source)
        return dedup_codes, dedup_meta, source

    async def sync_stock_universe(self, force_update: bool = False) -> Dict[str, Any]:
        if force_update:
            self._stock_list_cache_time = None

        symbols, meta_map, source = self._get_hk_stock_universe(force_refresh=force_update)
        if not symbols:
            return {
                "success": False,
                "message": "未获取到港股股票池",
                "total_symbols": 0,
                "inserted": 0,
                "updated": 0,
                "deactivated": 0,
                "source": source,
            }

        now = self._now_utc()
        operations: List[UpdateOne] = []
        for code in symbols:
            meta = meta_map.get(code) or {}
            doc = {
                "code": code,
                "symbol": code,
                "name": str(meta.get("name") or code).strip() or code,
                "exchange": meta.get("exchange") or "HKEX",
                "asset_type": str(meta.get("asset_type") or "Equity"),
                "active": True,
                "source": source,
                "updated_at": now,
            }
            operations.append(
                UpdateOne(
                    {"code": code},
                    {"$set": doc, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                )
            )

        inserted = 0
        updated = 0
        for chunk in self._chunked(operations, 1000):
            bulk_result = await self.db.stock_universe_hk.bulk_write(chunk, ordered=False)
            inserted += int(getattr(bulk_result, "upserted_count", 0) or 0)
            updated += int(getattr(bulk_result, "modified_count", 0) or 0)

        deactivated = 0
        if force_update:
            deact_result = await self.db.stock_universe_hk.update_many(
                {"code": {"$nin": symbols}, "active": True},
                {"$set": {"active": False, "updated_at": now}},
            )
            deactivated = int(getattr(deact_result, "modified_count", 0) or 0)

        logger.info(
            "✅ 港股股票池同步完成: total=%s, inserted=%s, updated=%s, deactivated=%s, source=%s",
            len(symbols),
            inserted,
            updated,
            deactivated,
            source,
        )
        return {
            "success": True,
            "total_symbols": len(symbols),
            "inserted": inserted,
            "updated": updated,
            "deactivated": deactivated,
            "source": source,
        }

    async def _ensure_universe_ready(self, force_update: bool = False) -> None:
        min_count = int(getattr(self.settings, "HK_UNIVERSE_MIN_COUNT", 1500))
        try:
            current_count = await self.db.stock_universe_hk.count_documents({"active": True})
        except Exception:
            current_count = 0

        if force_update or current_count < min_count:
            reason = "force" if force_update else f"count<{min_count}"
            logger.info("🔄 开始刷新港股股票池（原因: %s）", reason)
            await self.sync_stock_universe(force_update=force_update)

    async def _load_universe_symbols(self, *, purpose: str, max_symbols: Optional[int] = None) -> List[str]:
        field = f"last_{purpose}_sync_at"
        cursor = self.db.stock_universe_hk.find(
            {"active": True},
            {"_id": 0, "code": 1, "name": 1, field: 1},
        ).sort([(field, 1), ("code", 1)])

        if max_symbols is not None and max_symbols > 0:
            cursor = cursor.limit(max_symbols)

        docs = await cursor.to_list(length=max_symbols or 10000)
        symbols: List[str] = []
        for doc in docs:
            code = self._normalize_hk_code(doc.get("code"))
            if not code:
                continue
            symbols.append(code)
            if code not in self.hk_stock_meta_map:
                self.hk_stock_meta_map[code] = {
                    "name": str(doc.get("name") or code),
                    "exchange": "HKEX",
                    "asset_type": "Equity",
                }

        if symbols:
            return symbols

        stock_list, meta_map, _ = self._get_hk_stock_universe(force_refresh=False)
        self.hk_stock_meta_map.update(meta_map)
        if max_symbols is not None and max_symbols > 0:
            return stock_list[:max_symbols]
        return stock_list

    async def _mark_universe_sync(self, symbols: List[str], purpose: str) -> None:
        if not symbols:
            return
        now = self._now_utc()
        field = f"last_{purpose}_sync_at"
        await self.db.stock_universe_hk.update_many(
            {"code": {"$in": symbols}},
            {"$set": {field: now, "updated_at": now}},
        )

    async def _fetch_akshare_spot(self) -> Optional[Any]:
        try:
            import akshare as ak

            return await asyncio.to_thread(ak.stock_hk_spot)
        except Exception as e:
            logger.warning("⚠️ AKShare 港股spot获取失败: %s", e)
            return None

    async def sync_basic_info_from_source(
        self,
        source: str = "akshare",
        force_update: bool = False,
        max_symbols: Optional[int] = None,
    ) -> Dict[str, int]:
        await self._ensure_universe_ready(force_update=force_update)

        if max_symbols is None:
            max_symbols = int(getattr(self.settings, "HK_BASIC_INFO_INCREMENTAL_BATCH_SIZE", 300))

        stock_list = await self._load_universe_symbols(purpose="basic", max_symbols=max_symbols)
        if not stock_list:
            logger.error("❌ 无可用港股股票池")
            return {"updated": 0, "inserted": 0, "failed": 0, "processed": 0}

        logger.info("🇭🇰 开始同步港股基础信息: source=%s, symbols=%s", source, len(stock_list))

        operations: List[UpdateOne] = []
        success_symbols: List[str] = []
        failed_count = 0
        now = self._now_utc()

        if source == "akshare":
            df = await self._fetch_akshare_spot()
            if df is None or df.empty:
                return {"updated": 0, "inserted": 0, "failed": len(stock_list), "processed": len(stock_list)}

            target = set(stock_list)
            for _, row in df.iterrows():
                code = self._normalize_hk_code(row.get("代码") or row.get("symbol") or row.get("code"))
                if not code or code not in target:
                    continue
                name = str(row.get("中文名称") or row.get("名称") or row.get("name") or code).strip() or code

                doc = {
                    "code": code,
                    "symbol": code,
                    "name": name,
                    "currency": "HKD",
                    "exchange": "HKEX",
                    "market": "HK",
                    "area": "HK",
                    "source": "akshare",
                    "updated_at": now,
                }

                pe = self._safe_float(row.get("市盈率") or row.get("PE"))
                pb = self._safe_float(row.get("市净率") or row.get("PB"))
                total_mv = self._safe_float(row.get("总市值") or row.get("总市值(港元)"))
                if pe is not None:
                    doc["pe"] = pe
                if pb is not None:
                    doc["pb"] = pb
                if total_mv is not None:
                    doc["total_mv"] = total_mv / 100000000

                operations.append(
                    UpdateOne(
                        {"code": code, "source": "akshare"},
                        {"$set": doc, "$setOnInsert": {"created_at": now}},
                        upsert=True,
                    )
                )
                success_symbols.append(code)

            missing = set(stock_list) - set(success_symbols)
            failed_count += len(missing)

        elif source == "yfinance":
            concurrency = int(getattr(self.settings, "HK_BASIC_INFO_SYNC_CONCURRENCY", 4))
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def _fetch_one(code: str):
                async with semaphore:
                    try:
                        info = await asyncio.to_thread(self.yfinance_provider.get_stock_info, code)
                        return code, info, None
                    except Exception as exc:
                        return code, None, exc

            fetched = await asyncio.gather(*[_fetch_one(c) for c in stock_list], return_exceptions=False)
            for code, info, err in fetched:
                if err is not None or not isinstance(info, dict):
                    failed_count += 1
                    continue
                name = str(info.get("name") or (self.hk_stock_meta_map.get(code) or {}).get("name") or code).strip() or code
                doc = {
                    "code": code,
                    "symbol": code,
                    "name": name,
                    "currency": "HKD",
                    "exchange": "HKEX",
                    "market": "HK",
                    "area": "HK",
                    "source": "yfinance",
                    "updated_at": now,
                }
                mcap = self._safe_float(info.get("market_cap"))
                if mcap is not None:
                    doc["total_mv"] = mcap / 100000000
                operations.append(
                    UpdateOne(
                        {"code": code, "source": "yfinance"},
                        {"$set": doc, "$setOnInsert": {"created_at": now}},
                        upsert=True,
                    )
                )
                success_symbols.append(code)
        else:
            logger.error("❌ 不支持的港股基础信息数据源: %s", source)
            return {"updated": 0, "inserted": 0, "failed": len(stock_list), "processed": len(stock_list)}

        result = {"updated": 0, "inserted": 0, "failed": failed_count, "processed": len(stock_list)}
        for chunk in self._chunked(operations, 500):
            try:
                bulk_result = await self.db.stock_basic_info_hk.bulk_write(chunk, ordered=False)
                result["updated"] += int(getattr(bulk_result, "modified_count", 0) or 0)
                result["inserted"] += int(getattr(bulk_result, "upserted_count", 0) or 0)
            except Exception as e:
                logger.error("❌ 港股基础信息批量写入失败: %s", e)
                result["failed"] += len(chunk)

        await self._mark_universe_sync(success_symbols, "basic")
        logger.info(
            "✅ 港股基础信息同步完成: processed=%s, updated=%s, inserted=%s, failed=%s",
            result["processed"],
            result["updated"],
            result["inserted"],
            result["failed"],
        )
        return result

    def _fetch_hk_pct_from_longport_sync(self, hk_codes: List[str]) -> Dict[str, float]:
        if not hk_codes:
            return {}
        try:
            from longport.openapi import Config, QuoteContext
            from app.services.data_sources.longport_adapter import LongportAdapter
        except Exception:
            return {}

        ad = LongportAdapter()
        app_key, app_secret, access_token = ad._read_credentials()
        if not (app_key and app_secret and access_token):
            return {}

        os.environ["LONGPORT_APP_KEY"] = app_key
        os.environ["LONGPORT_APP_SECRET"] = app_secret
        os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

        symbols = [f"{int(code)}.HK" for code in hk_codes]
        result: Dict[str, float] = {}
        try:
            ctx = QuoteContext(Config.from_env())
            for i in range(0, len(symbols), 50):
                quotes = ctx.quote(symbols[i : i + 50])
                for q in quotes or []:
                    symbol = str(getattr(q, "symbol", "") or "")
                    m = re.search(r"(\d+)\.HK$", symbol, flags=re.IGNORECASE)
                    if not m:
                        continue
                    code = str(m.group(1)).zfill(5)
                    close = self._safe_float(getattr(q, "last_done", None) or getattr(q, "latest_done", None))
                    pre_close = self._safe_float(getattr(q, "prev_close", None))
                    if close is not None and pre_close not in (None, 0, 0.0):
                        result[code] = round((close / pre_close - 1.0) * 100.0, 3)
        except Exception:
            return {}
        return result

    async def sync_quotes_from_source(self, source: str = "akshare", max_symbols: Optional[int] = None) -> Dict[str, int]:
        await self._ensure_universe_ready(force_update=False)

        if max_symbols is None:
            max_symbols = int(getattr(self.settings, "HK_QUOTES_INCREMENTAL_BATCH_SIZE", 300))

        stock_list = await self._load_universe_symbols(purpose="quote", max_symbols=max_symbols)
        if not stock_list:
            logger.error("❌ 无可用港股股票池")
            return {"updated": 0, "inserted": 0, "failed": 0, "processed": 0}

        logger.info("🇭🇰 开始同步港股行情: source=%s, symbols=%s", source, len(stock_list))

        operations: List[UpdateOne] = []
        success_symbols: List[str] = []
        failed_count = 0
        now = self._now_utc()

        if source == "akshare":
            df = await self._fetch_akshare_spot()
            if df is None or df.empty:
                return {"updated": 0, "inserted": 0, "failed": len(stock_list), "processed": len(stock_list)}

            target = set(stock_list)
            pending_for_longport: List[str] = []

            for _, row in df.iterrows():
                code = self._normalize_hk_code(row.get("代码") or row.get("symbol") or row.get("code"))
                if not code or code not in target:
                    continue

                close = self._safe_float(row.get("最新价") or row.get("现价") or row.get("close"))
                open_price = self._safe_float(row.get("今开") or row.get("开盘") or row.get("open"))
                high = self._safe_float(row.get("最高") or row.get("high"))
                low = self._safe_float(row.get("最低") or row.get("low"))
                volume = self._safe_int(row.get("成交量") or row.get("volume"))
                pre_close = self._safe_float(row.get("昨收") or row.get("前收盘") or row.get("pre_close"))
                pct_chg = self._safe_float(row.get("涨跌幅") or row.get("pct_chg"))

                if pct_chg is None and close is not None and pre_close not in (None, 0, 0.0):
                    pct_chg = round((close / pre_close - 1.0) * 100.0, 3)
                if pct_chg is None:
                    pending_for_longport.append(code)

                doc = {
                    "code": code,
                    "symbol": code,
                    "close": close,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "volume": volume,
                    "pre_close": pre_close,
                    "pct_chg": pct_chg,
                    "currency": "HKD",
                    "source": "akshare",
                    "updated_at": now,
                }
                operations.append(
                    UpdateOne(
                        {"code": code},
                        {"$set": doc, "$setOnInsert": {"created_at": now}},
                        upsert=True,
                    )
                )
                success_symbols.append(code)

            if pending_for_longport:
                lp_map = await asyncio.to_thread(self._fetch_hk_pct_from_longport_sync, pending_for_longport)
                if lp_map:
                    patch_ops: List[UpdateOne] = []
                    for c, pct in lp_map.items():
                        patch_ops.append(
                            UpdateOne(
                                {"code": c, "$or": [{"pct_chg": None}, {"pct_chg": {"$exists": False}}]},
                                {"$set": {"pct_chg": pct, "source": "akshare+longport", "updated_at": now}},
                            )
                        )
                    if patch_ops:
                        for chunk in self._chunked(patch_ops, 500):
                            await self.db.market_quotes_hk.bulk_write(chunk, ordered=False)

            missing = set(stock_list) - set(success_symbols)
            failed_count += len(missing)

        elif source == "yfinance":
            concurrency = int(getattr(self.settings, "HK_QUOTES_SYNC_CONCURRENCY", 6))
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def _fetch_one(code: str):
                async with semaphore:
                    try:
                        quote = await asyncio.to_thread(self.yfinance_provider.get_real_time_price, code)
                        return code, quote, None
                    except Exception as exc:
                        return code, None, exc

            fetched = await asyncio.gather(*[_fetch_one(c) for c in stock_list], return_exceptions=False)
            for code, quote, err in fetched:
                if err is not None or not isinstance(quote, dict) or not quote.get("price"):
                    failed_count += 1
                    continue
                close = self._safe_float(quote.get("price"))
                open_price = self._safe_float(quote.get("open"))
                pre_close = None
                pct_chg = None
                if close is not None and open_price not in (None, 0, 0.0):
                    pct_chg = round((close / open_price - 1.0) * 100.0, 3)
                doc = {
                    "code": code,
                    "symbol": code,
                    "close": close,
                    "open": open_price,
                    "high": self._safe_float(quote.get("high")),
                    "low": self._safe_float(quote.get("low")),
                    "volume": self._safe_int(quote.get("volume")),
                    "pre_close": pre_close,
                    "pct_chg": pct_chg,
                    "currency": "HKD",
                    "source": "yfinance",
                    "updated_at": now,
                }
                operations.append(
                    UpdateOne(
                        {"code": code},
                        {"$set": doc, "$setOnInsert": {"created_at": now}},
                        upsert=True,
                    )
                )
                success_symbols.append(code)
        else:
            logger.error("❌ 不支持的港股行情数据源: %s", source)
            return {"updated": 0, "inserted": 0, "failed": len(stock_list), "processed": len(stock_list)}

        result = {"updated": 0, "inserted": 0, "failed": failed_count, "processed": len(stock_list)}
        for chunk in self._chunked(operations, 500):
            try:
                bulk_result = await self.db.market_quotes_hk.bulk_write(chunk, ordered=False)
                result["updated"] += int(getattr(bulk_result, "modified_count", 0) or 0)
                result["inserted"] += int(getattr(bulk_result, "upserted_count", 0) or 0)
            except Exception as e:
                logger.error("❌ 港股行情批量写入失败: %s", e)
                result["failed"] += len(chunk)

        await self._mark_universe_sync(success_symbols, "quote")
        logger.info(
            "✅ 港股行情同步完成: processed=%s, updated=%s, inserted=%s, failed=%s",
            result["processed"],
            result["updated"],
            result["inserted"],
            result["failed"],
        )
        return result


_hk_sync_service: Optional[HKSyncService] = None


async def get_hk_sync_service() -> HKSyncService:
    global _hk_sync_service
    if _hk_sync_service is None:
        _hk_sync_service = HKSyncService()
        await _hk_sync_service.initialize()
    return _hk_sync_service


async def run_hk_stock_universe_sync(force_update: bool = False):
    try:
        service = await get_hk_sync_service()
        result = await service.sync_stock_universe(force_update=force_update)
        logger.info("✅ 港股股票池同步完成: %s", result)
        return result
    except Exception as e:
        logger.error("❌ 港股股票池同步失败: %s", e)
        raise


async def run_hk_akshare_basic_info_sync(force_update: bool = False, max_symbols: Optional[int] = None):
    try:
        service = await get_hk_sync_service()
        result = await service.sync_basic_info_from_source("akshare", force_update=force_update, max_symbols=max_symbols)
        logger.info("✅ 港股基础信息同步完成 (akshare): %s", result)
        return result
    except Exception as e:
        logger.error("❌ 港股基础信息同步失败 (akshare): %s", e)
        raise


async def run_hk_yfinance_basic_info_sync(force_update: bool = False, max_symbols: Optional[int] = None):
    try:
        service = await get_hk_sync_service()
        result = await service.sync_basic_info_from_source("yfinance", force_update=force_update, max_symbols=max_symbols)
        logger.info("✅ 港股基础信息同步完成 (yfinance): %s", result)
        return result
    except Exception as e:
        logger.error("❌ 港股基础信息同步失败 (yfinance): %s", e)
        raise


async def run_hk_akshare_quotes_sync(max_symbols: Optional[int] = None):
    try:
        service = await get_hk_sync_service()
        result = await service.sync_quotes_from_source("akshare", max_symbols=max_symbols)
        logger.info("✅ 港股行情同步完成 (akshare): %s", result)
        return result
    except Exception as e:
        logger.error("❌ 港股行情同步失败 (akshare): %s", e)
        raise


async def run_hk_yfinance_quotes_sync(max_symbols: Optional[int] = None):
    try:
        service = await get_hk_sync_service()
        result = await service.sync_quotes_from_source("yfinance", max_symbols=max_symbols)
        logger.info("✅ 港股行情同步完成 (yfinance): %s", result)
        return result
    except Exception as e:
        logger.error("❌ 港股行情同步失败 (yfinance): %s", e)
        raise


async def run_hk_status_check():
    try:
        service = await get_hk_sync_service()
        await service._ensure_universe_ready(force_update=False)

        universe_count = await service.db.stock_universe_hk.count_documents({"active": True})
        basic_count = await service.db.stock_basic_info_hk.count_documents({})
        quote_count = await service.db.market_quotes_hk.count_documents({})

        latest_basic = await service.db.stock_basic_info_hk.find_one({}, sort=[("updated_at", -1)])
        latest_quote = await service.db.market_quotes_hk.find_one({}, sort=[("updated_at", -1)])

        result = {
            "status": "ok",
            "universe_count": universe_count,
            "basic_count": basic_count,
            "quote_count": quote_count,
            "latest_basic_updated_at": (latest_basic or {}).get("updated_at"),
            "latest_quote_updated_at": (latest_quote or {}).get("updated_at"),
            "timestamp": datetime.utcnow().isoformat(),
        }
        logger.info("✅ 港股状态检查完成: %s", result)
        return result
    except Exception as e:
        logger.error("❌ 港股状态检查失败: %s", e)
        return {"status": "error", "error": str(e)}
