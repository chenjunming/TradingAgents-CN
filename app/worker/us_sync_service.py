#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股数据同步服务（全量股票池 + 定期增量更新）

能力：
1. 股票池全量入库：优先 Finnhub，其次 SEC 官方公开列表
2. 基础信息增量更新：按批次从 yfinance 拉取并 upsert 到 stock_basic_info_us
3. 行情增量更新：按批次从 yfinance 拉取并 upsert 到 market_quotes_us
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pymongo import UpdateOne

# 导入美股数据提供器
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.core.database import get_mongo_db
from app.core.config import settings

logger = logging.getLogger(__name__)


class USSyncService:
    """美股数据同步服务（全量股票池 + 定期增量更新）"""

    SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
    EXCLUDED_NAME_PATTERNS = (
        r"\bETF\b",
        r"\bETN\b",
        r"\bWARRANTS?\b",
        r"\bUNITS?\b",
        r"\bRIGHTS?\b",
        r"\bPREFERRED\b",
        r"\bTRUST\b",
        r"\bFUND\b",
    )

    def __init__(self):
        self.db = get_mongo_db()
        self.settings = settings

        # 股票池缓存
        self.us_stock_list: List[str] = []
        self.us_stock_meta_map: Dict[str, Dict[str, Any]] = {}
        self._stock_list_source: str = "unknown"
        self._stock_list_cache_time: Optional[datetime] = None
        self._stock_list_cache_ttl = 3600 * 24  # 24h

        # Finnhub 客户端（延迟初始化）
        self._finnhub_client = None

    async def initialize(self):
        # 初始化必要索引，避免增量调度扫描性能退化
        try:
            await self.db.stock_universe_us.create_index([("code", 1)], unique=True)
            await self.db.stock_universe_us.create_index([("active", 1), ("last_basic_sync_at", 1), ("code", 1)])
            await self.db.stock_universe_us.create_index([("active", 1), ("last_quote_sync_at", 1), ("code", 1)])
            await self.db.stock_basic_info_us.create_index([("code", 1), ("source", 1)], unique=True)
            await self.db.market_quotes_us.create_index([("code", 1)], unique=True)
        except Exception as e:
            logger.warning("⚠️ 美股同步索引初始化失败（忽略，不影响运行）: %s", e)
        logger.info("✅ 美股同步服务初始化完成")

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.utcnow()

    @classmethod
    def _normalize_us_symbol(cls, symbol: Any) -> Optional[str]:
        s = str(symbol or "").strip().upper()
        if not s:
            return None
        if s.startswith("$"):
            s = s[1:]
        if " " in s:
            return None
        if not cls.SYMBOL_RE.fullmatch(s):
            return None
        return s

    @classmethod
    def _is_supported_security_name(cls, name: Any) -> bool:
        text = str(name or "").strip().upper()
        if not text:
            return True
        for pattern in cls.EXCLUDED_NAME_PATTERNS:
            if re.search(pattern, text):
                return False
        return True

    @staticmethod
    def _to_yfinance_symbol(symbol: str) -> str:
        # Yahoo 对 A/B 类股等代码使用 '-' 分隔（如 BRK-B, BF-B）
        return str(symbol or "").upper().replace(".", "-")

    @staticmethod
    def _chunked(seq: List[Any], size: int) -> List[List[Any]]:
        if size <= 0:
            size = 1000
        return [seq[i : i + size] for i in range(0, len(seq), size)]

    @staticmethod
    def _fetch_stock_info_sync(stock_code: str) -> Dict[str, Any]:
        """直接调用 yfinance 获取基础信息，避免装饰器实例调用歧义。"""
        import yfinance as yf

        yf_symbol = USSyncService._to_yfinance_symbol(stock_code)
        ticker = yf.Ticker(yf_symbol)

        info: Dict[str, Any] = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        if not isinstance(info, dict):
            info = {}

        # 名称兜底，避免因 shortName 缺失被判失败
        if not info.get("shortName") and not info.get("longName"):
            info["shortName"] = stock_code
            info["longName"] = stock_code

        return info

    def _get_finnhub_client(self):
        if self._finnhub_client is None:
            try:
                import finnhub

                api_key = (os.getenv("FINNHUB_API_KEY") or "").strip()
                if not api_key:
                    logger.warning("⚠️ 未配置 FINNHUB_API_KEY，跳过 Finnhub 股票池")
                    return None

                self._finnhub_client = finnhub.Client(api_key=api_key)
                logger.info("✅ Finnhub 客户端初始化成功")
            except Exception as e:
                logger.error(f"❌ Finnhub 客户端初始化失败: {e}")
                return None

        return self._finnhub_client

    def _get_us_stock_list_from_finnhub(self) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        """从 Finnhub 拉取 US 普通股列表。"""
        client = self._get_finnhub_client()
        if client is None:
            return [], {}

        try:
            logger.info("🔄 从 Finnhub 拉取美股股票池...")
            symbols = client.stock_symbols("US")
            if not symbols:
                logger.warning("⚠️ Finnhub 返回空股票池")
                return [], {}

            codes: List[str] = []
            meta_map: Dict[str, Dict[str, Any]] = {}
            for item in symbols:
                if not isinstance(item, dict):
                    continue
                symbol_type = str(item.get("type") or "").strip().lower()
                # 只保留普通股，避免 ETF/基金噪音
                if symbol_type and "common" not in symbol_type:
                    continue

                code = self._normalize_us_symbol(item.get("symbol"))
                if not code:
                    continue

                if code not in meta_map:
                    name = str(item.get("description") or item.get("displaySymbol") or code).strip() or code
                    if not self._is_supported_security_name(name):
                        continue
                    exchange = str(item.get("mic") or item.get("exchange") or "").strip() or None
                    meta_map[code] = {
                        "name": name,
                        "exchange": exchange,
                        "asset_type": str(item.get("type") or "Common Stock"),
                    }
                    codes.append(code)

            logger.info(f"✅ Finnhub 股票池拉取完成: {len(codes)} 只")
            return codes, meta_map
        except Exception as e:
            logger.error(f"❌ Finnhub 股票池拉取失败: {e}")
            return [], {}

    def _get_us_stock_list_from_sec(self) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        """
        从 SEC 官方公开列表拉取 US 股票池。
        公开地址（无需密钥）：https://www.sec.gov/files/company_tickers_exchange.json
        """
        url = "https://www.sec.gov/files/company_tickers_exchange.json"
        headers = {
            "User-Agent": "TradingAgents-CN/1.0 (stock-sync)",
            "Accept": "application/json",
        }

        try:
            logger.info("🔄 从 SEC 官方列表拉取美股股票池...")
            req = Request(url, headers=headers)
            with urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            codes: List[str] = []
            meta_map: Dict[str, Dict[str, Any]] = {}

            fields = payload.get("fields") if isinstance(payload, dict) else None
            rows = payload.get("data") if isinstance(payload, dict) else None

            if isinstance(fields, list) and isinstance(rows, list):
                index_map = {str(name): idx for idx, name in enumerate(fields)}
                ticker_idx = index_map.get("ticker")
                name_idx = index_map.get("name")
                exchange_idx = index_map.get("exchange")
                for row in rows:
                    if not isinstance(row, list):
                        continue
                    code = self._normalize_us_symbol(row[ticker_idx] if ticker_idx is not None and ticker_idx < len(row) else "")
                    if not code or code in meta_map:
                        continue
                    name = str(row[name_idx] if name_idx is not None and name_idx < len(row) else code).strip() or code
                    if not self._is_supported_security_name(name):
                        continue
                    exchange = (
                        str(row[exchange_idx] if exchange_idx is not None and exchange_idx < len(row) else "").strip() or None
                    )
                    meta_map[code] = {
                        "name": name,
                        "exchange": exchange,
                        "asset_type": "Common Stock",
                    }
                    codes.append(code)
            else:
                logger.warning("⚠️ SEC 股票池返回结构非预期，忽略")

            logger.info(f"✅ SEC 股票池拉取完成: {len(codes)} 只")
            return codes, meta_map

        except (HTTPError, URLError, TimeoutError) as e:
            logger.warning(f"⚠️ SEC 股票池拉取失败: {e}")
        except Exception as e:
            logger.warning(f"⚠️ 解析 SEC 股票池失败: {e}")

        return [], {}

    def _get_us_stock_list_from_nasdaq_trader(self) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        """
        从 NasdaqTrader 官方符号文件拉取股票池。
        公开地址（无需密钥）：
        - https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
        - https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt
        """
        urls = [
            "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
            "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
        ]
        headers = {
            "User-Agent": "TradingAgents-CN/1.0 (stock-sync)",
            "Accept": "text/plain",
        }

        codes: List[str] = []
        meta_map: Dict[str, Dict[str, Any]] = {}

        def _append_code(raw_symbol: Any, name: Any, exchange: Any, asset_type: str = "Common Stock") -> None:
            code = self._normalize_us_symbol(raw_symbol)
            if not code or code in meta_map:
                return
            sec_name = str(name or code).strip() or code
            if not self._is_supported_security_name(sec_name):
                return
            meta_map[code] = {
                "name": sec_name,
                "exchange": str(exchange or "").strip() or None,
                "asset_type": asset_type,
            }
            codes.append(code)

        logger.info("🔄 从 NasdaqTrader 拉取美股股票池...")
        for url in urls:
            try:
                req = Request(url, headers=headers)
                with urlopen(req, timeout=25) as resp:
                    text = resp.read().decode("utf-8", errors="ignore")
            except (HTTPError, URLError, TimeoutError) as e:
                logger.warning("⚠️ NasdaqTrader 股票池拉取失败: %s, err=%s", url, e)
                continue
            except Exception as e:
                logger.warning("⚠️ NasdaqTrader 股票池解析失败: %s, err=%s", url, e)
                continue

            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue

            header = [x.strip() for x in lines[0].split("|")]
            index_map = {name: idx for idx, name in enumerate(header)}

            for line in lines[1:]:
                if line.startswith("File Creation Time"):
                    break
                parts = [x.strip() for x in line.split("|")]
                if not parts:
                    continue

                test_idx = index_map.get("Test Issue")
                if test_idx is not None and test_idx < len(parts) and parts[test_idx].upper() == "Y":
                    continue

                etf_idx = index_map.get("ETF")
                if etf_idx is not None and etf_idx < len(parts) and parts[etf_idx].upper() == "Y":
                    continue

                if "Symbol" in index_map:
                    sym = parts[index_map["Symbol"]] if index_map["Symbol"] < len(parts) else ""
                    sec_name = (
                        parts[index_map["Security Name"]]
                        if index_map.get("Security Name") is not None and index_map["Security Name"] < len(parts)
                        else sym
                    )
                    _append_code(sym, sec_name, "NASDAQ")
                elif "ACT Symbol" in index_map:
                    sym = parts[index_map["ACT Symbol"]] if index_map["ACT Symbol"] < len(parts) else ""
                    sec_name = (
                        parts[index_map["Security Name"]]
                        if index_map.get("Security Name") is not None and index_map["Security Name"] < len(parts)
                        else sym
                    )
                    exch = parts[index_map["Exchange"]] if index_map.get("Exchange") is not None and index_map["Exchange"] < len(parts) else ""
                    exch_map = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "BATS", "V": "IEX"}
                    _append_code(sym, sec_name, exch_map.get(exch.upper(), exch.upper() or None))

        logger.info("✅ NasdaqTrader 股票池拉取完成: %s 只", len(codes))
        return codes, meta_map

    def _get_fallback_stock_list(self) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        fallback = [
            "AAPL",
            "MSFT",
            "GOOGL",
            "AMZN",
            "META",
            "TSLA",
            "NVDA",
            "AMD",
            "INTC",
            "NFLX",
            "JPM",
            "BAC",
            "WFC",
            "GS",
            "MS",
            "KO",
            "PEP",
            "WMT",
            "HD",
            "MCD",
            "JNJ",
            "PFE",
            "UNH",
            "ABBV",
            "XOM",
            "CVX",
        ]
        meta_map = {code: {"name": code, "exchange": None, "asset_type": "Common Stock"} for code in fallback}
        return fallback, meta_map

    def _get_us_stock_universe(self, force_refresh: bool = False) -> Tuple[List[str], Dict[str, Dict[str, Any]], str]:
        if (
            not force_refresh
            and self.us_stock_list
            and self._stock_list_cache_time
            and datetime.now() - self._stock_list_cache_time < timedelta(seconds=self._stock_list_cache_ttl)
        ):
            return self.us_stock_list, self.us_stock_meta_map, self._stock_list_source

        min_count = int(getattr(self.settings, "US_UNIVERSE_MIN_COUNT", 3000))

        finnhub_list, finnhub_meta = self._get_us_stock_list_from_finnhub()
        sec_list, sec_meta = self._get_us_stock_list_from_sec()
        nasdaq_list, nasdaq_meta = self._get_us_stock_list_from_nasdaq_trader()

        chosen_list: List[str] = []
        chosen_meta: Dict[str, Dict[str, Any]] = {}
        source = "fallback"

        source_candidates: List[Tuple[str, List[str], Dict[str, Dict[str, Any]]]] = [
            ("finnhub", finnhub_list, finnhub_meta),
            ("sec", sec_list, sec_meta),
            ("nasdaqtrader", nasdaq_list, nasdaq_meta),
        ]

        # 优先选择达到最低规模阈值的数据源（按优先级）
        for src_name, src_list, src_meta in source_candidates:
            if len(src_list) >= min_count:
                chosen_list, chosen_meta, source = src_list, src_meta, src_name
                break

        # 若都不满足阈值，选择规模最大的可用源
        if not chosen_list:
            best_source = max(source_candidates, key=lambda x: len(x[1]))
            if best_source[1]:
                source, chosen_list, chosen_meta = best_source[0], best_source[1], best_source[2]
            else:
                chosen_list, chosen_meta = self._get_fallback_stock_list()
                source = "fallback"

        # 最终去重与规范化
        dedup_codes: List[str] = []
        dedup_meta: Dict[str, Dict[str, Any]] = {}
        for code in chosen_list:
            c = self._normalize_us_symbol(code)
            if not c or c in dedup_meta:
                continue
            dedup_codes.append(c)
            dedup_meta[c] = chosen_meta.get(c, {"name": c, "exchange": None, "asset_type": "Common Stock"})

        self.us_stock_list = dedup_codes
        self.us_stock_meta_map = dedup_meta
        self._stock_list_source = source
        self._stock_list_cache_time = datetime.now()

        logger.info("📦 美股股票池已更新: count=%s, source=%s", len(dedup_codes), source)
        return dedup_codes, dedup_meta, source

    async def sync_stock_universe(self, force_update: bool = False) -> Dict[str, Any]:
        """全量同步美股股票池到 stock_universe_us。"""
        if force_update:
            self._stock_list_cache_time = None

        symbols, meta_map, source = self._get_us_stock_universe(force_refresh=force_update)
        if not symbols:
            return {
                "success": False,
                "message": "未获取到美股股票池",
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
                "exchange": meta.get("exchange"),
                "asset_type": str(meta.get("asset_type") or "Common Stock"),
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
            bulk_result = await self.db.stock_universe_us.bulk_write(chunk, ordered=False)
            inserted += int(getattr(bulk_result, "upserted_count", 0) or 0)
            updated += int(getattr(bulk_result, "modified_count", 0) or 0)

        deactivated = 0
        if force_update:
            deact_result = await self.db.stock_universe_us.update_many(
                {"code": {"$nin": symbols}, "active": True},
                {"$set": {"active": False, "updated_at": now}},
            )
            deactivated = int(getattr(deact_result, "modified_count", 0) or 0)

        logger.info(
            "✅ 美股股票池同步完成: total=%s, inserted=%s, updated=%s, deactivated=%s, source=%s",
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
        min_count = int(getattr(self.settings, "US_UNIVERSE_MIN_COUNT", 3000))
        try:
            current_count = await self.db.stock_universe_us.count_documents({"active": True})
        except Exception:
            current_count = 0

        if force_update or current_count < min_count:
            reason = "force" if force_update else f"count<{min_count}"
            logger.info("🔄 开始刷新美股股票池（原因: %s）", reason)
            await self.sync_stock_universe(force_update=force_update)

    async def _load_universe_symbols(self, *, purpose: str, max_symbols: Optional[int] = None) -> List[str]:
        field = f"last_{purpose}_sync_at"
        cursor = self.db.stock_universe_us.find(
            {"active": True},
            {"_id": 0, "code": 1, "name": 1, field: 1},
        ).sort([(field, 1), ("code", 1)])

        if max_symbols is not None and max_symbols > 0:
            cursor = cursor.limit(max_symbols)

        docs = await cursor.to_list(length=max_symbols or 200000)
        symbols: List[str] = []
        for doc in docs:
            code = self._normalize_us_symbol(doc.get("code"))
            if not code:
                continue
            symbols.append(code)
            if code not in self.us_stock_meta_map:
                self.us_stock_meta_map[code] = {
                    "name": str(doc.get("name") or code),
                    "exchange": None,
                    "asset_type": "Common Stock",
                }

        if symbols:
            return symbols

        # DB 无股票池时，退回内存缓存
        stock_list, meta_map, _ = self._get_us_stock_universe(force_refresh=False)
        self.us_stock_meta_map.update(meta_map)
        if max_symbols is not None and max_symbols > 0:
            return stock_list[:max_symbols]
        return stock_list

    async def _mark_universe_sync(self, symbols: List[str], purpose: str) -> None:
        if not symbols:
            return
        now = self._now_utc()
        field = f"last_{purpose}_sync_at"
        await self.db.stock_universe_us.update_many(
            {"code": {"$in": symbols}},
            {"$set": {field: now, "updated_at": now}},
        )

    async def sync_basic_info_from_source(
        self,
        source: str = "yfinance",
        force_update: bool = False,
        max_symbols: Optional[int] = None,
    ) -> Dict[str, int]:
        """按批次同步美股基础信息（增量优先）。"""
        if source != "yfinance":
            logger.error(f"❌ 不支持的数据源: {source}")
            return {"updated": 0, "inserted": 0, "failed": 0, "processed": 0}

        await self._ensure_universe_ready(force_update=force_update)

        if max_symbols is None:
            if force_update:
                max_symbols = None
            else:
                max_symbols = int(getattr(self.settings, "US_BASIC_INFO_INCREMENTAL_BATCH_SIZE", 300))

        stock_list = await self._load_universe_symbols(purpose="basic", max_symbols=max_symbols)
        if not stock_list:
            logger.error("❌ 无可用美股股票池")
            return {"updated": 0, "inserted": 0, "failed": 0, "processed": 0}

        logger.info(
            "🇺🇸 开始同步美股基础信息: source=%s, symbols=%s, mode=%s",
            source,
            len(stock_list),
            "full" if max_symbols is None else "incremental",
        )

        concurrency = int(getattr(self.settings, "US_BASIC_INFO_SYNC_CONCURRENCY", 6))
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _fetch_one(stock_code: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[Exception]]:
            async with semaphore:
                try:
                    info = await asyncio.to_thread(self._fetch_stock_info_sync, stock_code)
                    return stock_code, info, None
                except Exception as exc:
                    return stock_code, None, exc

        fetched = await asyncio.gather(*[_fetch_one(code) for code in stock_list], return_exceptions=False)

        operations: List[UpdateOne] = []
        failed_count = 0
        processed_symbols: List[str] = []
        success_symbols: List[str] = []
        now = self._now_utc()

        for stock_code, stock_info, err in fetched:
            processed_symbols.append(stock_code)
            if err is not None:
                logger.debug("⚠️ yfinance获取失败: %s, err=%s", stock_code, err)
                failed_count += 1
                continue

            if not isinstance(stock_info, dict) or not stock_info:
                failed_count += 1
                continue

            try:
                normalized_info = self._normalize_stock_info(stock_info, source)
                normalized_info["code"] = stock_code
                normalized_info["symbol"] = stock_code
                normalized_info["source"] = source
                normalized_info["updated_at"] = now

                if not normalized_info.get("name"):
                    normalized_info["name"] = str(
                        (self.us_stock_meta_map.get(stock_code) or {}).get("name") or stock_code
                    )

                operations.append(
                    UpdateOne(
                        {"code": stock_code, "source": source},
                        {"$set": normalized_info, "$setOnInsert": {"created_at": now}},
                        upsert=True,
                    )
                )
                success_symbols.append(stock_code)
            except Exception:
                failed_count += 1

        result = {"updated": 0, "inserted": 0, "failed": failed_count, "processed": len(processed_symbols)}
        for chunk in self._chunked(operations, 500):
            try:
                bulk_result = await self.db.stock_basic_info_us.bulk_write(chunk, ordered=False)
                result["updated"] += int(getattr(bulk_result, "modified_count", 0) or 0)
                result["inserted"] += int(getattr(bulk_result, "upserted_count", 0) or 0)
            except Exception as e:
                logger.error(f"❌ 美股基础信息批量写入失败: {e}")
                result["failed"] += len(chunk)

        await self._mark_universe_sync(success_symbols, "basic")

        logger.info(
            "✅ 美股基础信息同步完成: processed=%s, updated=%s, inserted=%s, failed=%s",
            result["processed"],
            result["updated"],
            result["inserted"],
            result["failed"],
        )
        return result

    @staticmethod
    def _fetch_quote_sync(stock_code: str) -> Optional[Dict[str, Any]]:
        import yfinance as yf

        yf_symbol = USSyncService._to_yfinance_symbol(stock_code)
        ticker = yf.Ticker(yf_symbol)
        # 用2日数据计算涨跌幅（相对昨收），若不足2日则退化为相对开盘
        data = ticker.history(period="2d")
        if data is None or data.empty:
            return None

        latest = data.iloc[-1]
        close = float(latest.get("Close"))
        open_price = float(latest.get("Open")) if latest.get("Open") is not None else None
        high = float(latest.get("High")) if latest.get("High") is not None else None
        low = float(latest.get("Low")) if latest.get("Low") is not None else None
        volume = int(latest.get("Volume") or 0)

        prev_close = None
        if len(data) >= 2:
            prev_row = data.iloc[-2]
            prev_close = float(prev_row.get("Close")) if prev_row.get("Close") is not None else None

        pct_chg = None
        if prev_close not in (None, 0.0):
            pct_chg = round(((close - prev_close) / prev_close) * 100.0, 3)
        elif open_price not in (None, 0.0):
            pct_chg = round(((close - open_price) / open_price) * 100.0, 3)

        return {
            "code": stock_code,
            "symbol": stock_code,
            "close": close,
            "open": open_price,
            "high": high,
            "low": low,
            "volume": volume,
            "pre_close": prev_close,
            "pct_chg": pct_chg,
            "currency": "USD",
            "source": "yfinance",
            "updated_at": datetime.utcnow(),
        }

    async def sync_quotes_from_source(
        self,
        source: str = "yfinance",
        max_symbols: Optional[int] = None,
    ) -> Dict[str, int]:
        """按批次同步美股行情（增量优先）。"""
        if source != "yfinance":
            logger.error(f"❌ 不支持的数据源: {source}")
            return {"updated": 0, "inserted": 0, "failed": 0, "processed": 0}

        await self._ensure_universe_ready(force_update=False)

        if max_symbols is None:
            max_symbols = int(getattr(self.settings, "US_QUOTES_INCREMENTAL_BATCH_SIZE", 300))

        stock_list = await self._load_universe_symbols(purpose="quote", max_symbols=max_symbols)
        if not stock_list:
            logger.error("❌ 无可用美股股票池")
            return {"updated": 0, "inserted": 0, "failed": 0, "processed": 0}

        logger.info("🇺🇸 开始同步美股行情: source=%s, symbols=%s", source, len(stock_list))

        concurrency = int(getattr(self.settings, "US_QUOTES_SYNC_CONCURRENCY", 8))
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _fetch_one(stock_code: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[Exception]]:
            async with semaphore:
                try:
                    quote = await asyncio.to_thread(self._fetch_quote_sync, stock_code)
                    return stock_code, quote, None
                except Exception as exc:
                    return stock_code, None, exc

        fetched = await asyncio.gather(*[_fetch_one(code) for code in stock_list], return_exceptions=False)

        operations: List[UpdateOne] = []
        failed_count = 0
        processed_symbols: List[str] = []
        success_symbols: List[str] = []

        for stock_code, quote, err in fetched:
            processed_symbols.append(stock_code)
            if err is not None or not isinstance(quote, dict):
                failed_count += 1
                continue
            operations.append(
                UpdateOne(
                    {"code": stock_code},
                    {"$set": quote, "$setOnInsert": {"created_at": self._now_utc()}},
                    upsert=True,
                )
            )
            success_symbols.append(stock_code)

        result = {"updated": 0, "inserted": 0, "failed": failed_count, "processed": len(processed_symbols)}
        for chunk in self._chunked(operations, 500):
            try:
                bulk_result = await self.db.market_quotes_us.bulk_write(chunk, ordered=False)
                result["updated"] += int(getattr(bulk_result, "modified_count", 0) or 0)
                result["inserted"] += int(getattr(bulk_result, "upserted_count", 0) or 0)
            except Exception as e:
                logger.error(f"❌ 美股行情批量写入失败: {e}")
                result["failed"] += len(chunk)

        await self._mark_universe_sync(success_symbols, "quote")

        logger.info(
            "✅ 美股行情同步完成: processed=%s, updated=%s, inserted=%s, failed=%s",
            result["processed"],
            result["updated"],
            result["inserted"],
            result["failed"],
        )
        return result

    def _normalize_stock_info(self, stock_info: Dict[str, Any], source: str) -> Dict[str, Any]:
        normalized = {
            "name": stock_info.get("shortName") or stock_info.get("longName") or "",
            "name_en": stock_info.get("longName") or stock_info.get("shortName") or "",
            "currency": stock_info.get("currency", "USD"),
            "exchange": stock_info.get("exchange", "US"),
            "market": stock_info.get("exchange", "US"),
            "area": stock_info.get("country", "US"),
        }

        if stock_info.get("marketCap"):
            normalized["total_mv"] = stock_info["marketCap"] / 100000000

        if stock_info.get("sector"):
            normalized["sector"] = stock_info.get("sector")
        if stock_info.get("industry"):
            normalized["industry"] = stock_info.get("industry")

        # 直接补齐可用于选股的关键字段
        if stock_info.get("trailingPE") is not None:
            normalized["pe"] = stock_info.get("trailingPE")
        if stock_info.get("priceToBook") is not None:
            normalized["pb"] = stock_info.get("priceToBook")
        if stock_info.get("returnOnEquity") is not None:
            roe_raw = stock_info.get("returnOnEquity")
            try:
                roe_num = float(roe_raw)
                # yfinance 常返回 0.12 这种比例，统一转百分比
                normalized["roe"] = roe_num * 100.0 if -2.0 <= roe_num <= 2.0 else roe_num
            except Exception:
                pass

        return normalized


# ==================== 全局服务实例 ====================

_us_sync_service: Optional[USSyncService] = None


async def get_us_sync_service() -> USSyncService:
    global _us_sync_service
    if _us_sync_service is None:
        _us_sync_service = USSyncService()
        await _us_sync_service.initialize()
    return _us_sync_service


# ==================== APScheduler 兼容任务 ====================

async def run_us_stock_universe_sync(force_update: bool = False):
    """APScheduler任务：美股全量股票池同步"""
    try:
        service = await get_us_sync_service()
        result = await service.sync_stock_universe(force_update=force_update)
        logger.info(f"✅ 美股股票池同步完成: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ 美股股票池同步失败: {e}")
        raise


async def run_us_yfinance_basic_info_sync(force_update: bool = False, max_symbols: Optional[int] = None):
    """APScheduler任务：美股基础信息增量同步（yfinance）"""
    try:
        service = await get_us_sync_service()
        result = await service.sync_basic_info_from_source(
            source="yfinance",
            force_update=force_update,
            max_symbols=max_symbols,
        )
        logger.info(f"✅ 美股基础信息同步完成 (yfinance): {result}")
        return result
    except Exception as e:
        logger.error(f"❌ 美股基础信息同步失败 (yfinance): {e}")
        raise


async def run_us_yfinance_quotes_sync(max_symbols: Optional[int] = None):
    """APScheduler任务：美股行情增量同步（yfinance）"""
    try:
        service = await get_us_sync_service()
        result = await service.sync_quotes_from_source(source="yfinance", max_symbols=max_symbols)
        logger.info(f"✅ 美股行情同步完成: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ 美股行情同步失败: {e}")
        raise


async def run_us_status_check():
    """APScheduler任务：美股同步状态检查"""
    try:
        service = await get_us_sync_service()
        await service._ensure_universe_ready(force_update=False)

        universe_count = await service.db.stock_universe_us.count_documents({"active": True})
        basic_count = await service.db.stock_basic_info_us.count_documents({})
        quote_count = await service.db.market_quotes_us.count_documents({})

        latest_basic = await service.db.stock_basic_info_us.find_one({}, sort=[("updated_at", -1)])
        latest_quote = await service.db.market_quotes_us.find_one({}, sort=[("updated_at", -1)])

        result = {
            "status": "ok",
            "universe_count": universe_count,
            "basic_count": basic_count,
            "quote_count": quote_count,
            "latest_basic_updated_at": (latest_basic or {}).get("updated_at"),
            "latest_quote_updated_at": (latest_quote or {}).get("updated_at"),
            "timestamp": datetime.utcnow().isoformat(),
        }
        logger.info(f"✅ 美股状态检查完成: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ 美股状态检查失败: {e}")
        return {"status": "error", "error": str(e)}
