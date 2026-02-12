from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.database import get_mongo_db
from tradingagents.agents.utils.memory import FinancialSituationMemory
from tradingagents.utils.stock_utils import StockUtils

logger = logging.getLogger("webapi")


class AnalysisFeedbackService:
    """分析结果长期反馈服务（可持续迭代评估，不依赖固定验证天数）。"""

    _MARKET_COLLECTIONS = {
        "CN": {"daily": "stock_daily_quotes", "quotes": "market_quotes"},
        "HK": {"daily": "stock_daily_quotes_hk", "quotes": "market_quotes_hk"},
        "US": {"daily": "stock_daily_quotes_us", "quotes": "market_quotes_us"},
    }
    _BASIC_INFO_COLLECTIONS = {
        "CN": "stock_basic_info",
        "HK": "stock_basic_info_hk",
        "US": "stock_basic_info_us",
    }

    def __init__(self) -> None:
        self.jobs_collection_name = "analysis_feedback_jobs"
        self.events_collection_name = "analysis_feedback_events"
        self.portfolio_metrics_collection_name = "analysis_feedback_portfolio_metrics"
        self.monthly_reports_collection_name = "analysis_feedback_monthly_reports"
        self._indexes_ready = False
        self._trader_memory: Optional[FinancialSituationMemory] = None

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return

        db = get_mongo_db()
        jobs = db[self.jobs_collection_name]
        events = db[self.events_collection_name]
        portfolio_metrics = db[self.portfolio_metrics_collection_name]
        monthly_reports = db[self.monthly_reports_collection_name]

        await jobs.create_index("task_id", unique=True)
        await jobs.create_index([("status", 1), ("next_eval_at", 1)])
        await jobs.create_index([("status", 1), ("closed_at", -1)])
        await jobs.create_index("updated_at")
        await events.create_index([("job_id", 1), ("eval_at", -1)])
        await events.create_index("task_id")
        await portfolio_metrics.create_index([("scope", 1), ("as_of", -1)])
        await monthly_reports.create_index("month_key", unique=True)
        self._indexes_ready = True

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            s = value.strip().replace(",", "").replace("%", "")
            s = s.replace("¥", "").replace("$", "").replace("HK$", "")
            if not s or s in {"N/A", "None", "-", "null"}:
                return None
            try:
                return float(s)
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except Exception:
                continue
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 8:
            try:
                return datetime.strptime(digits[:8], "%Y%m%d").date()
            except Exception:
                pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except Exception:
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

    @staticmethod
    def _normalize_action(action: str) -> str:
        a = (action or "").strip().lower()
        buy_words = {"buy", "long", "accumulate", "买入", "加仓", "增持", "看多"}
        sell_words = {"sell", "short", "reduce", "exit", "卖出", "减仓", "清仓", "看空"}
        hold_words = {"hold", "wait", "observe", "持有", "观望", "中性"}
        if a in buy_words:
            return "buy"
        if a in sell_words:
            return "sell"
        if a in hold_words:
            return "hold"
        if "买" in a or "多" in a:
            return "buy"
        if "卖" in a or "空" in a or "减" in a:
            return "sell"
        return "hold"

    def _resolve_market(self, symbol: str, market_type: Optional[str] = None) -> str:
        mt = (market_type or "").strip().upper()
        if mt in {"CN", "A股", "A_SHARES", "CHINA_A"}:
            return "CN"
        if mt in {"HK", "港股", "HONG_KONG"}:
            return "HK"
        if mt in {"US", "美股"}:
            return "US"

        market_info = StockUtils.get_market_info(symbol or "")
        market_map = {
            "china_a": "CN",
            "hong_kong": "HK",
            "us": "US",
        }
        return market_map.get(market_info.get("market", ""), "CN")

    @staticmethod
    def _code_candidates(symbol: str, market: str) -> List[str]:
        raw = (symbol or "").strip()
        if not raw:
            return []

        candidates: List[str] = []

        if market == "CN":
            digits = "".join(ch for ch in raw if ch.isdigit())
            if digits:
                candidates.extend([digits.zfill(6), digits])
            candidates.append(raw)
        elif market == "HK":
            upper = raw.upper()
            base = upper.replace(".HK", "")
            digits = "".join(ch for ch in base if ch.isdigit())
            candidates.extend([upper, base])
            if digits:
                d4 = digits.zfill(4)
                d5 = digits.zfill(5)
                candidates.extend([d4, d5, f"{d4}.HK", f"{d5}.HK"])
        else:
            candidates.append(raw.upper())

        uniq: List[str] = []
        seen = set()
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                uniq.append(item)
        return uniq

    async def _get_daily_rows(self, market: str, symbol: str, limit: int = 800) -> List[Dict[str, Any]]:
        db = get_mongo_db()
        collection_name = self._MARKET_COLLECTIONS.get(market, self._MARKET_COLLECTIONS["CN"])["daily"]
        candidates = self._code_candidates(symbol, market)
        if not candidates:
            return []
        cursor = db[collection_name].find(
            {"code": {"$in": candidates}},
            {"_id": 0, "code": 1, "trade_date": 1, "close": 1},
        ).sort("trade_date", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def _get_price_near_date(
        self,
        market: str,
        symbol: str,
        target: Optional[date],
    ) -> Optional[Dict[str, Any]]:
        rows = await self._get_daily_rows(market, symbol)
        if not rows:
            return None

        parsed_rows: List[Dict[str, Any]] = []
        for row in rows:
            td = self._parse_date(row.get("trade_date"))
            close = self._to_float(row.get("close"))
            if td is None or close is None:
                continue
            parsed_rows.append(
                {
                    "trade_date": td,
                    "trade_date_raw": row.get("trade_date"),
                    "close": close,
                    "code": row.get("code"),
                    "source": "daily_quotes",
                }
            )
        if not parsed_rows:
            return None

        parsed_rows.sort(key=lambda x: x["trade_date"], reverse=True)
        if target is None:
            return parsed_rows[0]

        not_after = [r for r in parsed_rows if r["trade_date"] <= target]
        if not_after:
            return not_after[0]

        parsed_rows.sort(key=lambda x: x["trade_date"])
        return parsed_rows[0]

    async def _get_latest_price(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        db = get_mongo_db()
        collection_name = self._MARKET_COLLECTIONS.get(market, self._MARKET_COLLECTIONS["CN"])["quotes"]
        candidates = self._code_candidates(symbol, market)
        if candidates:
            quote = await db[collection_name].find_one(
                {"code": {"$in": candidates}},
                {"_id": 0, "code": 1, "trade_date": 1, "close": 1, "updated_at": 1},
                sort=[("updated_at", -1)],
            )
            if quote:
                close = self._to_float(quote.get("close"))
                if close is not None:
                    td = self._parse_date(quote.get("trade_date")) or datetime.utcnow().date()
                    return {
                        "trade_date": td,
                        "trade_date_raw": quote.get("trade_date"),
                        "close": close,
                        "code": quote.get("code"),
                        "source": "market_quotes",
                    }
        return await self._get_price_near_date(market, symbol, datetime.utcnow().date())

    async def _resolve_industry(self, market: str, symbol: str) -> Optional[str]:
        db = get_mongo_db()
        collection_name = self._BASIC_INFO_COLLECTIONS.get(market, self._BASIC_INFO_COLLECTIONS["CN"])
        candidates = self._code_candidates(symbol, market)
        if not candidates:
            return None
        doc = await db[collection_name].find_one(
            {"code": {"$in": candidates}},
            {"_id": 0, "industry": 1, "industry_name": 1, "sector": 1},
        )
        if not doc:
            return None
        return (
            str(doc.get("industry") or doc.get("industry_name") or doc.get("sector") or "").strip()
            or None
        )

    async def register_analysis_feedback_job(
        self,
        *,
        task_id: str,
        user_id: str,
        result: Dict[str, Any],
        memory_enabled: bool = True,
    ) -> None:
        if not getattr(settings, "ANALYSIS_FEEDBACK_ENABLED", True):
            return

        await self.ensure_indexes()
        symbol = str(result.get("stock_symbol") or result.get("stock_code") or "").strip()
        if not symbol:
            logger.warning("⚠️ 反馈任务创建跳过：缺少股票代码")
            return

        decision = result.get("decision") or {}
        if not isinstance(decision, dict):
            decision = {}

        action_text = str(decision.get("action") or "持有")
        action = self._normalize_action(action_text)
        target_price = self._to_float(decision.get("target_price"))
        if target_price is not None and target_price <= 0:
            target_price = None

        analysis_date = self._parse_date(result.get("analysis_date")) or datetime.utcnow().date()
        market = self._resolve_market(symbol, result.get("market_type"))
        industry = await self._resolve_industry(market, symbol)

        reference_price = None
        if target_price is None:
            reference_price = await self._get_price_near_date(market, symbol, analysis_date)

        entry_price = target_price if target_price is not None else (
            reference_price.get("close") if reference_price else None
        )
        entry_trade_date = (
            analysis_date
            if target_price is not None
            else (reference_price.get("trade_date") if reference_price else analysis_date)
        )
        entry_source = "decision_target_price" if target_price is not None else (
            reference_price.get("source") if reference_price else "unknown"
        )

        now = datetime.utcnow()
        interval_days = max(1, int(getattr(settings, "ANALYSIS_FEEDBACK_INTERVAL_DAYS", 7)))
        next_eval_at = now + timedelta(days=interval_days)

        db = get_mongo_db()
        jobs = db[self.jobs_collection_name]

        job_doc = {
            "job_id": str(uuid.uuid4()),
            "task_id": task_id,
            "analysis_id": result.get("analysis_id"),
            "user_id": str(user_id),
            "stock_symbol": symbol,
            "stock_name": result.get("stock_name") or symbol,
            "industry": industry,
            "market": market,
            "status": "active",
            "memory_enabled": bool(memory_enabled),
            "decision": {
                "action_text": action_text,
                "action": action,
                "confidence": self._to_float(decision.get("confidence")),
                "risk_score": self._to_float(decision.get("risk_score")),
                "target_price": target_price,
                "reasoning": str(decision.get("reasoning") or ""),
            },
            "entry": {
                "price": entry_price,
                "trade_date": entry_trade_date.isoformat() if entry_trade_date else None,
                "price_source": entry_source,
            },
            "policy": {
                "interval_days": interval_days,
                "max_tracking_days": int(getattr(settings, "ANALYSIS_FEEDBACK_MAX_TRACKING_DAYS", 365)),
                "max_evaluations": int(getattr(settings, "ANALYSIS_FEEDBACK_MAX_EVALUATIONS", 120)),
                "hold_tolerance_pct": float(getattr(settings, "ANALYSIS_FEEDBACK_HOLD_TOLERANCE_PCT", 3.0)),
                "min_effective_change_pct": float(
                    getattr(settings, "ANALYSIS_FEEDBACK_MIN_EFFECTIVE_CHANGE_PCT", 0.2)
                ),
                "stop_gain_pct": float(getattr(settings, "ANALYSIS_FEEDBACK_STOP_GAIN_PCT", 15.0)),
                "stop_loss_pct": float(getattr(settings, "ANALYSIS_FEEDBACK_STOP_LOSS_PCT", 8.0)),
                "time_take_profit_days": int(
                    getattr(settings, "ANALYSIS_FEEDBACK_TIME_TAKE_PROFIT_DAYS", 30)
                ),
                "max_holding_days": int(getattr(settings, "ANALYSIS_FEEDBACK_MAX_HOLDING_DAYS", 180)),
            },
            "eval_count": 0,
            "last_eval_at": None,
            "next_eval_at": next_eval_at,
            "created_at": now,
            "updated_at": now,
        }

        result_update = await jobs.update_one(
            {"task_id": task_id},
            {"$setOnInsert": job_doc},
            upsert=True,
        )

        if result_update.upserted_id:
            logger.info(
                "✅ 已创建长期反馈任务: task_id=%s symbol=%s next_eval_at=%s",
                task_id,
                symbol,
                next_eval_at.isoformat(),
            )
        else:
            logger.info("ℹ️ 反馈任务已存在，跳过重复创建: task_id=%s", task_id)

    async def _write_memory_feedback(
        self,
        *,
        symbol: str,
        action_text: str,
        entry_price: float,
        entry_date: date,
        current_price: float,
        current_date: date,
        holding_days: int,
        strategy_return_pct: float,
        success: bool,
        reasoning: str,
    ) -> None:
        if not getattr(settings, "ANALYSIS_FEEDBACK_MEMORY_WRITE_ENABLED", True):
            return

        if self._trader_memory is None:
            self._trader_memory = FinancialSituationMemory("trader")

        outcome = "成功" if success else "失败"
        situation = (
            f"股票={symbol}; 动作={action_text}; 入场价={entry_price:.4f}({entry_date.isoformat()}); "
            f"评估价={current_price:.4f}({current_date.isoformat()}); 持有天数={holding_days}; "
            f"策略收益率={strategy_return_pct:.2f}%; 结果={outcome}"
        )
        advice = (
            "保留当前决策模式并继续跟踪验证。"
            if success
            else "本次策略效果不佳，下一次需重点复盘入场价格、风险控制和触发条件。"
        )
        if reasoning:
            advice = f"{advice} 原始推理摘要: {reasoning[:120]}"

        self._trader_memory.add_situations([(situation, advice)])

    def _should_write_memory(
        self,
        *,
        job: Dict[str, Any],
        strategy_return_pct: float,
        success: bool,
        status: str,
        now: datetime,
    ) -> bool:
        if status == "closed":
            return True

        last_written_at = self._parse_datetime(job.get("last_memory_write_at"))
        if last_written_at is None:
            return True

        last_success = job.get("last_memory_write_success")
        if last_success is not None and bool(last_success) != bool(success):
            return True

        last_return = self._to_float(job.get("last_memory_write_return_pct"))
        if last_return is None:
            return True

        delta_threshold = float(
            getattr(settings, "ANALYSIS_FEEDBACK_MEMORY_MIN_RETURN_DELTA_PCT", 1.0)
        )
        min_days = max(
            0, int(getattr(settings, "ANALYSIS_FEEDBACK_MEMORY_MIN_DAYS_BETWEEN_WRITES", 14))
        )
        days_since = (now - last_written_at).days
        return days_since >= min_days and abs(strategy_return_pct - last_return) >= delta_threshold

    async def _evaluate_single_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.utcnow()
        job_id = job.get("job_id") or ""
        task_id = job.get("task_id") or ""
        symbol = str(job.get("stock_symbol") or "")
        market = str(job.get("market") or "CN")
        decision = job.get("decision") or {}
        policy = job.get("policy") or {}

        entry_info = job.get("entry") or {}
        entry_price = self._to_float(entry_info.get("price"))
        entry_date = self._parse_date(entry_info.get("trade_date")) or now.date()
        if entry_price is None:
            entry_reference = await self._get_price_near_date(market, symbol, entry_date)
            if entry_reference:
                entry_price = self._to_float(entry_reference.get("close"))
                entry_date = entry_reference.get("trade_date") or entry_date

        latest = await self._get_latest_price(market, symbol)
        current_price = self._to_float(latest.get("close")) if latest else None
        current_date = (latest.get("trade_date") if latest else None) or now.date()

        db = get_mongo_db()
        events = db[self.events_collection_name]
        jobs = db[self.jobs_collection_name]

        interval_days = max(1, int(policy.get("interval_days", 7)))
        max_tracking_days = max(1, int(policy.get("max_tracking_days", 365)))
        max_evaluations = max(1, int(policy.get("max_evaluations", 120)))
        hold_tolerance_pct = float(policy.get("hold_tolerance_pct", 3.0))
        min_effective_change_pct = float(policy.get("min_effective_change_pct", 0.2))
        stop_gain_pct = float(policy.get("stop_gain_pct", 15.0))
        stop_loss_pct = float(policy.get("stop_loss_pct", 8.0))
        time_take_profit_days = max(1, int(policy.get("time_take_profit_days", 30)))
        max_holding_days = max(1, int(policy.get("max_holding_days", 180)))

        eval_count = int(job.get("eval_count", 0)) + 1
        holding_days = max(0, (current_date - entry_date).days)
        action = self._normalize_action(str(decision.get("action") or "hold"))
        action_text = str(decision.get("action_text") or decision.get("action") or "持有")
        reasoning = str(decision.get("reasoning") or "")

        success = False
        benchmark_return_pct: Optional[float] = None
        strategy_return_pct: Optional[float] = None
        alpha_pct: Optional[float] = None
        summary = "缺少价格数据，等待下一次评估。"
        status = "active"
        exit_reason: Optional[str] = None

        if entry_price is not None and current_price is not None and entry_price > 0:
            benchmark_return_pct = (current_price - entry_price) / entry_price * 100.0
            if action == "sell":
                strategy_return_pct = (entry_price - current_price) / entry_price * 100.0
                success = strategy_return_pct > min_effective_change_pct
            elif action == "hold":
                strategy_return_pct = benchmark_return_pct
                success = abs(strategy_return_pct) <= hold_tolerance_pct
            else:
                strategy_return_pct = benchmark_return_pct
                success = strategy_return_pct > min_effective_change_pct
            alpha_pct = strategy_return_pct - benchmark_return_pct
            summary = (
                f"{symbol} 评估完成: 动作={action_text}, 持有{holding_days}天, "
                f"策略收益={strategy_return_pct:.2f}%, 基准收益={benchmark_return_pct:.2f}%"
            )

            # 退出规则：止盈 / 止损 / 时间止盈 / 最大持有时间
            if strategy_return_pct >= stop_gain_pct:
                status = "closed"
                exit_reason = "stop_gain"
            elif strategy_return_pct <= -abs(stop_loss_pct):
                status = "closed"
                exit_reason = "stop_loss"
            elif holding_days >= time_take_profit_days and strategy_return_pct > min_effective_change_pct:
                status = "closed"
                exit_reason = "time_take_profit"
            elif holding_days >= max_holding_days:
                status = "closed"
                exit_reason = "max_holding_days"
        else:
            summary = f"{symbol} 评估缺少价格数据，跳过收益计算。"

        if holding_days >= max_tracking_days:
            status = "closed"
            exit_reason = exit_reason or "max_tracking_days"
        if eval_count >= max_evaluations:
            status = "closed"
            exit_reason = exit_reason or "max_evaluations"

        event_doc = {
            "event_id": str(uuid.uuid4()),
            "job_id": job_id,
            "task_id": task_id,
            "stock_symbol": symbol,
            "eval_index": eval_count,
            "eval_at": now,
            "entry_price": entry_price,
            "entry_date": entry_date.isoformat() if entry_date else None,
            "current_price": current_price,
            "current_date": current_date.isoformat() if current_date else None,
            "action": action,
            "action_text": action_text,
            "holding_days": holding_days,
            "strategy_return_pct": strategy_return_pct,
            "benchmark_return_pct": benchmark_return_pct,
            "alpha_pct": alpha_pct,
            "success": bool(success),
            "summary": summary,
            "status_after_eval": status,
            "exit_reason": exit_reason,
            "created_at": now,
        }
        await events.insert_one(event_doc)

        memory_written = False
        if (
            bool(job.get("memory_enabled", True))
            and strategy_return_pct is not None
            and entry_price is not None
            and current_price is not None
            and self._should_write_memory(
                job=job,
                strategy_return_pct=strategy_return_pct,
                success=success,
                status=status,
                now=now,
            )
        ):
            try:
                await self._write_memory_feedback(
                    symbol=symbol,
                    action_text=action_text,
                    entry_price=entry_price,
                    entry_date=entry_date,
                    current_price=current_price,
                    current_date=current_date,
                    holding_days=holding_days,
                    strategy_return_pct=strategy_return_pct,
                    success=success,
                    reasoning=reasoning,
                )
                memory_written = True
            except Exception as memory_err:
                logger.warning("⚠️ 写入反馈记忆失败(已忽略): %s", memory_err)

        next_eval_at = None if status == "closed" else now + timedelta(days=interval_days)
        set_fields = {
            "status": status,
            "eval_count": eval_count,
            "last_eval_at": now,
            "next_eval_at": next_eval_at,
            "last_event_id": event_doc["event_id"],
            "last_strategy_return_pct": strategy_return_pct,
            "last_benchmark_return_pct": benchmark_return_pct,
            "last_alpha_pct": alpha_pct,
            "exit_reason": exit_reason,
            "updated_at": now,
        }
        if status == "closed":
            set_fields["closed_at"] = now
        if memory_written:
            set_fields.update(
                {
                    "last_memory_write_at": now,
                    "last_memory_write_return_pct": strategy_return_pct,
                    "last_memory_write_success": bool(success),
                }
            )
        await jobs.update_one(
            {"_id": job["_id"]},
            {"$set": set_fields},
        )

        return {
            "job_id": job_id,
            "task_id": task_id,
            "symbol": symbol,
            "status": status,
            "exit_reason": exit_reason,
            "summary": summary,
        }

    @staticmethod
    def _safe_div(numerator: float, denominator: float) -> Optional[float]:
        if denominator == 0:
            return None
        return numerator / denominator

    def _compute_max_drawdown_pct(self, returns_pct: List[float]) -> float:
        if not returns_pct:
            return 0.0
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in returns_pct:
            equity *= (1.0 + r / 100.0)
            if equity > peak:
                peak = equity
            if peak > 0:
                drawdown = (peak - equity) / peak
                if drawdown > max_dd:
                    max_dd = drawdown
        return max_dd * 100.0

    async def _update_portfolio_metrics(self) -> Dict[str, Any]:
        db = get_mongo_db()
        jobs = db[self.jobs_collection_name]
        metrics_coll = db[self.portfolio_metrics_collection_name]

        closed_jobs = await jobs.find(
            {"status": "closed", "last_strategy_return_pct": {"$ne": None}},
            {"_id": 0, "task_id": 1, "last_strategy_return_pct": 1, "closed_at": 1},
        ).sort("closed_at", 1).to_list(length=5000)

        returns: List[float] = []
        for item in closed_jobs:
            value = self._to_float(item.get("last_strategy_return_pct"))
            if value is not None:
                returns.append(value)

        total = len(returns)
        wins = len([r for r in returns if r > 0])
        losses = len([r for r in returns if r < 0])
        win_rate = (wins / total * 100.0) if total > 0 else 0.0
        gross_profit = sum(r for r in returns if r > 0)
        gross_loss_abs = abs(sum(r for r in returns if r < 0))
        profit_factor = self._safe_div(gross_profit, gross_loss_abs)
        avg_return = (sum(returns) / total) if total > 0 else 0.0
        max_drawdown_pct = self._compute_max_drawdown_pct(returns)

        as_of = datetime.utcnow()
        doc = {
            "scope": "global_latest",
            "as_of": as_of,
            "total_closed_positions": total,
            "win_count": wins,
            "loss_count": losses,
            "win_rate_pct": round(win_rate, 4),
            "avg_return_pct": round(avg_return, 4),
            "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "updated_at": as_of,
        }
        await metrics_coll.update_one({"scope": "global_latest"}, {"$set": doc}, upsert=True)
        return doc

    async def _update_monthly_attribution(self) -> Dict[str, Any]:
        db = get_mongo_db()
        jobs = db[self.jobs_collection_name]
        report_coll = db[self.monthly_reports_collection_name]

        now = datetime.utcnow()
        month_start = datetime(now.year, now.month, 1)
        if now.month == 12:
            month_end = datetime(now.year + 1, 1, 1)
        else:
            month_end = datetime(now.year, now.month + 1, 1)

        rows = await jobs.find(
            {
                "status": "closed",
                "closed_at": {"$gte": month_start, "$lt": month_end},
                "last_strategy_return_pct": {"$ne": None},
            },
            {
                "_id": 0,
                "market": 1,
                "industry": 1,
                "decision.action": 1,
                "last_strategy_return_pct": 1,
            },
        ).to_list(length=5000)

        def _group_stats(items: List[float]) -> Dict[str, Any]:
            total = len(items)
            wins = len([x for x in items if x > 0])
            avg = (sum(items) / total) if total > 0 else 0.0
            return {
                "count": total,
                "win_rate_pct": round((wins / total * 100.0) if total > 0 else 0.0, 4),
                "avg_return_pct": round(avg, 4),
            }

        by_market: Dict[str, List[float]] = defaultdict(list)
        by_action: Dict[str, List[float]] = defaultdict(list)
        by_industry: Dict[str, List[float]] = defaultdict(list)

        for row in rows:
            ret = self._to_float(row.get("last_strategy_return_pct"))
            if ret is None:
                continue
            market = str(row.get("market") or "UNKNOWN")
            action = str((row.get("decision") or {}).get("action") or "hold")
            industry = str(row.get("industry") or "UNKNOWN")
            by_market[market].append(ret)
            by_action[action].append(ret)
            by_industry[industry].append(ret)

        month_key = f"{now.year:04d}-{now.month:02d}"
        report_doc = {
            "month_key": month_key,
            "range_start": month_start,
            "range_end": month_end,
            "total_closed_positions": len(rows),
            "by_market": {k: _group_stats(v) for k, v in by_market.items()},
            "by_action": {k: _group_stats(v) for k, v in by_action.items()},
            "by_industry": {k: _group_stats(v) for k, v in by_industry.items()},
            "updated_at": now,
        }
        await report_coll.update_one({"month_key": month_key}, {"$set": report_doc}, upsert=True)
        return report_doc

    async def run_due_feedback_jobs(self, batch_size: Optional[int] = None) -> Dict[str, Any]:
        if not getattr(settings, "ANALYSIS_FEEDBACK_ENABLED", True):
            return {"enabled": False, "processed": 0, "closed": 0, "errors": 0}

        await self.ensure_indexes()
        now = datetime.utcnow()
        batch = batch_size or int(getattr(settings, "ANALYSIS_FEEDBACK_BATCH_SIZE", 50))

        db = get_mongo_db()
        jobs = db[self.jobs_collection_name]
        due_jobs = await jobs.find(
            {"status": "active", "next_eval_at": {"$lte": now}}
        ).sort("next_eval_at", 1).limit(batch).to_list(length=batch)

        processed = 0
        closed = 0
        errors = 0
        for job in due_jobs:
            try:
                result = await self._evaluate_single_job(job)
                processed += 1
                if result.get("status") == "closed":
                    closed += 1
            except Exception as e:
                errors += 1
                logger.error(
                    "❌ 分析反馈评估失败: task_id=%s symbol=%s err=%s",
                    job.get("task_id"),
                    job.get("stock_symbol"),
                    e,
                    exc_info=True,
                )

        portfolio_metrics = await self._update_portfolio_metrics()
        monthly_report = await self._update_monthly_attribution()

        return {
            "enabled": True,
            "processed": processed,
            "closed": closed,
            "errors": errors,
            "due_jobs": len(due_jobs),
            "portfolio_metrics": portfolio_metrics,
            "monthly_report_month_key": monthly_report.get("month_key"),
        }


_analysis_feedback_service: Optional[AnalysisFeedbackService] = None


def get_analysis_feedback_service() -> AnalysisFeedbackService:
    global _analysis_feedback_service
    if _analysis_feedback_service is None:
        _analysis_feedback_service = AnalysisFeedbackService()
    return _analysis_feedback_service
