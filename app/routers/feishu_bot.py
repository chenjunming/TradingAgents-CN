from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Request

from app.core.config import settings
from app.core.database import get_mongo_db, get_mongo_db_sync
from app.core.response import ok
from app.models.analysis import AnalysisParameters, SingleAnalysisRequest
from app.models.screening import OperatorType, ScreeningCondition
from app.services.basics_sync import fetch_latest_roe_map
from app.services.a_share_sqlite_service import get_a_share_sqlite_service
from app.services.advisor_service import advisor_service
from app.services.data_sources.manager import DataSourceManager
from app.services.data_sources.longport_adapter import LongportAdapter
from app.services.enhanced_screening_service import get_enhanced_screening_service
from app.services.feishu_push_service import feishu_push_service
from app.services.feishu_nl_agent_service import feishu_nl_agent_service
from app.services.favorites_service import favorites_service
from app.services.foreign_stock_service import ForeignStockService
from app.services.portfolio_service import portfolio_service
from app.services.simple_analysis_service import get_simple_analysis_service
from app.utils.timezone import to_config_tz

router = APIRouter(prefix="/api/feishu", tags=["feishu"])
logger = logging.getLogger(__name__)
REPORT_TEXT_MAX_CHARS = 100_000
REPORT_SECTION_ORDER: List[Tuple[str, str]] = [
    ("final_trade_decision", "最终决策"),
    ("research_team_decision", "研究团队结论"),
    ("investment_plan", "投资计划"),
    ("trader_investment_plan", "交易计划"),
    ("market_report", "市场技术分析"),
    ("fundamentals_report", "基本面分析"),
    ("sentiment_report", "情绪分析"),
    ("news_report", "新闻分析"),
]
FORCE_REFRESH_TOKENS = {
    "force",
    "-f",
    "--force",
    "强制",
    "重跑",
    "重算",
    "重新",
    "刷新",
    "重新生成",
    "重新分析",
}
HK_SCREEN_FALLBACK_SYMBOLS: List[str] = [
    "00700",  # 腾讯控股
    "09988",  # 阿里巴巴-SW
    "03690",  # 美团-W
    "01810",  # 小米集团-W
    "00941",  # 中国移动
    "02318",  # 中国平安
    "01211",  # 比亚迪股份
    "00005",  # 汇丰控股
    "00981",  # 中芯国际
    "09618",  # 京东集团-SW
    "09868",  # 小鹏汽车-W
    "01024",  # 快手-W
]
US_SCREEN_FALLBACK_SYMBOLS: List[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AMD",
    "NFLX",
    "COST",
    "JPM",
    "XOM",
]


def _normalize_market_type_hint(raw: Optional[str]) -> Optional[str]:
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if s in {"hk", "hkex", "港股", "hongkong", "hong_kong"}:
        return "港股"
    if s in {"us", "usa", "nasdaq", "nyse", "美股"}:
        return "美股"
    if s in {"cn", "a", "a股", "ashare", "ashares", "sh", "sz"}:
        return "A股"
    return None


def _help_text() -> str:
    return (
        "可用功能说明\n"
        "\n"
        "一、基础命令\n"
        "/help - 查看本帮助\n"
        "/daily - 今日投研摘要\n"
        "/pick - 候选股票建议\n"
        "/rebalance - 调仓建议\n"
        "/position - 当前持仓\n"
        "/report <代码|名称> [快速|基础|标准|深度|全面] [force] - 发起个股分析（默认全面；同股当天默认复用）\n"
        "/report <analysis_id> [模块] - 加载报告上下文并查看指定模块（也可点卡片按钮）\n"
        "/screen [价值|质量|动量] [数量] [A股,港股,美股] - 触发选股并返回TopN\n"
        "\n"
        "二、A股持仓维护\n"
        "/wl list - 查看A股持仓\n"
        "/wl add 贵州茅台 [数量] [成本] - 添加A股持仓\n"
        "/wl del 600519 - 删除A股持仓\n"
        "\n"
        "三、自然语言代理\n"
        "你可以直接说：把贵州茅台加入自选股\n"
        "你可以直接说：把平安银行加入持仓 200 12.3\n"
        "你可以直接说：基于贵州茅台的分析报告继续分析\n"
        "\n"
        "四、确认机制\n"
        "写操作默认先进入待确认队列\n"
        "/confirm <编号> - 确认执行\n"
        "/cancel <编号> - 取消执行\n"
        "（默认10分钟有效）"
    )


def _extract_text(payload: Dict[str, Any]) -> str:
    event = payload.get("event", {})
    message = event.get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return str(parsed.get("text", "")).strip()
        except Exception:
            return content.strip()
    return ""


def _extract_user_id(payload: Dict[str, Any]) -> str:
    sender = payload.get("event", {}).get("sender", {}).get("sender_id", {})
    return (
        sender.get("open_id")
        or sender.get("user_id")
        or sender.get("union_id")
        or "default"
    )


def _extract_chat_id(payload: Dict[str, Any]) -> Optional[str]:
    return payload.get("event", {}).get("message", {}).get("chat_id") or settings.FEISHU_BOT_DEFAULT_CHAT_ID


def _extract_message_meta(payload: Dict[str, Any]) -> Dict[str, str]:
    msg = payload.get("event", {}).get("message", {}) or {}
    return {
        "message_id": str(msg.get("message_id") or ""),
        "parent_id": str(msg.get("parent_id") or ""),
        "root_id": str(msg.get("root_id") or ""),
    }


def _conversation_id(user_id: str, chat_id: Optional[str], msg_meta: Dict[str, str]) -> str:
    root = msg_meta.get("root_id") or msg_meta.get("parent_id")
    if root:
        return f"thread:{root}"
    if chat_id:
        return f"chat:{chat_id}:user:{user_id}"
    return f"user:{user_id}"


def _normalize_research_depth(raw: Optional[str], default: str = "全面") -> str:
    value = str(raw or "").strip().lower()
    mapping = {
        "1": "快速",
        "快速": "快速",
        "快": "快速",
        "2": "基础",
        "基础": "基础",
        "3": "标准",
        "标准": "标准",
        "4": "深度",
        "深度": "深度",
        "5": "全面",
        "全面": "全面",
        "完整": "全面",
        "复杂": "全面",
        "full": "全面",
        "comprehensive": "全面",
    }
    return mapping.get(value, default)


def _looks_like_report_id(keyword: str) -> bool:
    q = str(keyword or "").strip()
    return bool(
        re.fullmatch(r"[0-9a-fA-F]{24}", q)
        or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", q)
    )


def _select_report_text(summary: str, reports: Any, max_chars: int = REPORT_TEXT_MAX_CHARS) -> str:
    """按固定优先级选择报告正文，保证同一报告返回风格一致。"""
    if isinstance(reports, dict):
        preferred_keys = [
            "final_trade_decision",
            "research_team_decision",
            "investment_plan",
            "trader_investment_plan",
            "market_report",
            "fundamentals_report",
            "sentiment_report",
            "news_report",
        ]
        for key in preferred_keys:
            val = reports.get(key)
            if isinstance(val, str):
                text = val.strip()
                if text:
                    return text[:max_chars]
    return str(summary or "").strip()[:max_chars]


def _is_force_refresh_token(raw: Optional[str]) -> bool:
    token = str(raw or "").strip().lower()
    if not token:
        return False
    if token in FORCE_REFRESH_TOKENS:
        return True
    for kw in ("强制", "重跑", "重算", "重新", "刷新"):
        if kw in str(raw):
            return True
    return False


def _normalize_report_section(raw: Optional[str]) -> Optional[str]:
    s = str(raw or "").strip().lower()
    if not s:
        return None
    aliases = {
        "decision": "final_trade_decision",
        "final": "final_trade_decision",
        "final_trade_decision": "final_trade_decision",
        "最终决策": "final_trade_decision",
        "决策": "final_trade_decision",
        "research": "research_team_decision",
        "research_team_decision": "research_team_decision",
        "研究团队": "research_team_decision",
        "研究结论": "research_team_decision",
        "investment_plan": "investment_plan",
        "投资计划": "investment_plan",
        "trader_investment_plan": "trader_investment_plan",
        "交易计划": "trader_investment_plan",
        "market_report": "market_report",
        "市场": "market_report",
        "技术": "market_report",
        "技术分析": "market_report",
        "fundamentals_report": "fundamentals_report",
        "基本面": "fundamentals_report",
        "sentiment_report": "sentiment_report",
        "情绪": "sentiment_report",
        "news_report": "news_report",
        "新闻": "news_report",
    }
    return aliases.get(s)


def _section_label(section_key: str) -> str:
    for key, label in REPORT_SECTION_ORDER:
        if key == section_key:
            return label
    return section_key


def _looks_like_placeholder_news_text(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    # 常见“准备去获取新闻”的模板话术，说明并未产出有效新闻内容
    placeholders = [
        "首先让我获取相关新闻数据",
        "我将为您分析股票代码",
        "正在获取相关新闻",
        "将为您分析",
        "暂无相关新闻",
        "未获取到相关新闻",
    ]
    if any(p in t for p in placeholders):
        return True
    # 太短的内容通常不是有效新闻分析
    return len(t) < 80


def _normalize_cn_symbol_for_news(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if not s:
        return s
    if "." in s:
        s = s.split(".")[0]
    if s.isdigit():
        return s.zfill(6)
    return s


def _guess_market_from_report_doc(report_doc: Dict[str, Any]) -> str:
    mt = str(report_doc.get("market_type") or "").strip().upper()
    if mt in {"CN", "A", "A股"}:
        return "CN"
    if mt in {"HK", "港股"}:
        return "HK"
    if mt in {"US", "美股"}:
        return "US"
    symbol = str(report_doc.get("stock_symbol") or "").strip().upper()
    if symbol.isdigit() and len(symbol) <= 6:
        return "CN"
    if symbol.endswith(".HK"):
        return "HK"
    return "US"


def _format_news_items_text(items: List[Dict[str, Any]], source: str, symbol: str) -> str:
    if not items:
        return ""
    lines = [f"已按需回源获取 `{symbol}` 最新新闻（来源：{source}）：", ""]
    for idx, it in enumerate(items[:8], 1):
        title = str(it.get("title") or "").strip() or "-"
        src = str(it.get("source") or "未知来源").strip()
        tm = str(it.get("time") or it.get("publish_time") or "").strip()
        url = str(it.get("url") or "").strip()
        line = f"{idx}. {title}"
        meta = " | ".join([x for x in [src, tm] if x])
        if meta:
            line += f"\n   {meta}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    return "\n".join(lines)


def _looks_like_unavailable_ai_news(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    patterns = [
        "无法进行社交媒体平台的实时搜索",
        "无法进行实时搜索",
        "无法为您获取",
        "我目前无法",
        "建议您",
        "请您自行",
    ]
    return any(p in t for p in patterns)


async def _fetch_news_fallback_text(report_doc: Dict[str, Any]) -> str:
    symbol = str(report_doc.get("stock_symbol") or "").strip()
    if not symbol:
        return ""
    market = _guess_market_from_report_doc(report_doc)
    stock_name = str(report_doc.get("stock_name") or "").strip()

    # 优先：使用已配置的联网模型做实时新闻搜索（失败则自动回退数据源）
    try:
        from tradingagents.dataflows.interface import (
            WebSearchToolNotOpenError,
            get_stock_news_openai,
        )

        query = stock_name or symbol
        curr_date = datetime.utcnow().strftime("%Y-%m-%d")
        ai_news_text = await asyncio.to_thread(get_stock_news_openai, query, curr_date)
        ai_news_text = str(ai_news_text or "").strip()
        if ai_news_text and len(ai_news_text) >= 40 and not _looks_like_unavailable_ai_news(ai_news_text):
            return (
                f"已通过联网AI实时检索 `{query}` 新闻（截至 {curr_date}）：\n\n"
                f"{ai_news_text[:REPORT_TEXT_MAX_CHARS-60]}"
            )
    except WebSearchToolNotOpenError as e:
        logger.warning("⚠️ 新闻模块 AI 联网搜索未开通，将回退数据源: %s", e)
    except Exception as e:
        # 记录失败原因，便于排查“联网搜索未生效”
        logger.warning("⚠️ 新闻模块 AI 联网搜索失败，将回退数据源: %s", e)

    try:
        if market == "CN":
            code = _normalize_cn_symbol_for_news(symbol)
            items, src = await asyncio.to_thread(
                lambda: DataSourceManager().get_news_with_fallback(
                    code=code,
                    days=3,
                    limit=8,
                    include_announcements=True,
                )
            )
            return _format_news_items_text(items or [], src or "unknown", code)

        service = ForeignStockService(db=get_mongo_db())
        if market == "HK":
            data = await service.get_hk_news(symbol, days=3, limit=8)
        else:
            data = await service.get_us_news(symbol, days=3, limit=8)
        items = data.get("items") if isinstance(data, dict) else []
        src = str((data or {}).get("source") or "external")
        return _format_news_items_text(items if isinstance(items, list) else [], src, symbol)
    except Exception:
        return ""


def _available_report_sections(reports: Any) -> List[Tuple[str, str]]:
    if not isinstance(reports, dict):
        return []
    sections: List[Tuple[str, str]] = []
    for key, label in REPORT_SECTION_ORDER:
        val = reports.get(key)
        if isinstance(val, str) and val.strip():
            sections.append((key, label))
    return sections


def _select_report_section_text(
    *,
    summary: str,
    reports: Any,
    requested_section: Optional[str],
    max_chars: int = REPORT_TEXT_MAX_CHARS,
) -> Tuple[str, str, str]:
    sections = _available_report_sections(reports)
    normalized_requested = _normalize_report_section(requested_section)
    if normalized_requested and isinstance(reports, dict):
        val = reports.get(normalized_requested)
        if isinstance(val, str) and val.strip():
            text = val.strip()[:max_chars]
            return normalized_requested, _section_label(normalized_requested), text

    if sections and isinstance(reports, dict):
        key = sections[0][0]
        text = str(reports.get(key) or "").strip()[:max_chars]
        return key, _section_label(key), text

    text = _select_report_text(summary=summary, reports=reports, max_chars=max_chars)
    return "summary", "摘要", text


def _build_report_selector_card(
    *,
    report_id: str,
    stock_name: str,
    stock_symbol: str,
    sections: List[Tuple[str, str]],
    selected: Optional[str] = None,
) -> Dict[str, Any]:
    title = f"报告模块选择 · {stock_name or stock_symbol}"
    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**报告ID**: `{report_id}`\n\n请选择要查看的报告模块：",
            },
        }
    ]

    row_actions: List[Dict[str, Any]] = []
    for idx, (key, label) in enumerate(sections):
        row_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary" if key == selected else "default",
                "value": {
                    "intent": "show_report_section",
                    "report_id": report_id,
                    "section": key,
                },
            }
        )
        if len(row_actions) == 3 or idx == len(sections) - 1:
            elements.append({"tag": "action", "actions": row_actions})
            row_actions = []

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def _today_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _day_start_local() -> datetime:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_report_args(args: List[str], default_depth: str = "全面") -> Tuple[str, bool, Optional[str]]:
    depth = default_depth
    force_refresh = False
    section: Optional[str] = None
    for tok in args:
        raw = str(tok or "").strip()
        if not raw:
            continue
        if _is_force_refresh_token(raw):
            force_refresh = True
            continue
        normalized_depth = _normalize_research_depth(raw, default="")
        if normalized_depth:
            depth = normalized_depth
            continue
        normalized_section = _normalize_report_section(raw)
        if normalized_section:
            section = normalized_section
    return depth, force_refresh, section


async def _resolve_a_share_symbol(keyword: str) -> tuple[str, str]:
    q = str(keyword or "").strip()
    if not q:
        raise ValueError("股票名称或代码不能为空")

    db = get_mongo_db()
    if re.fullmatch(r"\d{6}", q):
        doc = await db.stock_basic_info.find_one(
            {"$or": [{"code": q}, {"symbol": q}]},
            {"_id": 0, "code": 1, "symbol": 1, "name": 1},
        )
        if doc:
            symbol = str(doc.get("code") or doc.get("symbol") or q).zfill(6)
            return symbol, str(doc.get("name") or symbol)
        return q, q

    doc = await db.stock_basic_info.find_one(
        {"name": q},
        {"_id": 0, "code": 1, "symbol": 1, "name": 1},
        sort=[("updated_at", -1)],
    )
    if not doc:
        doc = await db.stock_basic_info.find_one(
            {"name": {"$regex": re.escape(q), "$options": "i"}},
            {"_id": 0, "code": 1, "symbol": 1, "name": 1},
            sort=[("updated_at", -1)],
        )
    if not doc:
        raise ValueError(f"未找到股票: {q}")
    symbol = str(doc.get("code") or doc.get("symbol") or "").zfill(6)
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError(f"仅支持A股标的，当前不支持: {q}")
    return symbol, str(doc.get("name") or q)


async def _latest_a_share_price(symbol: str) -> Optional[float]:
    db = get_mongo_db()
    quote = await db.market_quotes.find_one(
        {"$or": [{"code": symbol.zfill(6)}, {"symbol": symbol.zfill(6)}]},
        {"_id": 0, "close": 1},
    )
    try:
        return float(quote.get("close")) if quote and quote.get("close") is not None else None
    except Exception:
        return None


async def _upsert_conversation(
    conversation_id: str,
    user_id: str,
    chat_id: Optional[str],
    push_turn: Optional[Dict[str, str]] = None,
    set_context: Optional[Dict[str, Any]] = None,
    set_fields: Optional[Dict[str, Any]] = None,
) -> None:
    db = get_mongo_db()
    update: Dict[str, Any] = {
        "$set": {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "updated_at": datetime.utcnow(),
        },
        "$setOnInsert": {"created_at": datetime.utcnow()},
    }
    if set_context is not None:
        update["$set"]["context"] = set_context
    if isinstance(set_fields, dict):
        for k, v in set_fields.items():
            if isinstance(k, str) and k.strip():
                update["$set"][k] = v
    if push_turn is not None:
        update["$push"] = {
            "history": {
                "$each": [push_turn],
                "$slice": -20,
            }
        }
    await db.feishu_conversations.update_one(
        {"conversation_id": conversation_id},
        update,
        upsert=True,
    )


async def _get_conversation(conversation_id: str) -> Dict[str, Any]:
    db = get_mongo_db()
    doc = await db.feishu_conversations.find_one({"conversation_id": conversation_id})
    return doc or {}


async def _find_report_doc(report_keyword: str) -> Optional[Dict[str, Any]]:
    db = get_mongo_db()
    q = str(report_keyword or "").strip()
    if not q:
        return None

    report_doc = None
    # 1) 直接按 report_id / analysis_id / task_id
    report_doc = await db.analysis_reports.find_one(
        {
            "$or": [
                {"analysis_id": q},
                {"task_id": q},
            ]
        },
        sort=[("created_at", -1)],
    )

    # 2) 按股票代码
    if report_doc is None:
        code = q.zfill(6) if re.fullmatch(r"\d{6}", q) else None
        if code:
            report_doc = await db.analysis_reports.find_one(
                {"stock_symbol": code},
                sort=[("created_at", -1)],
            )

    # 3) 按股票名称 -> 代码 -> 报告
    if report_doc is None:
        try:
            symbol, _ = await _resolve_a_share_symbol(q)
            report_doc = await db.analysis_reports.find_one(
                {"stock_symbol": symbol},
                sort=[("created_at", -1)],
            )
        except Exception:
            pass

    return report_doc


async def _find_today_reusable_report(symbol: str) -> Optional[Dict[str, Any]]:
    db = get_mongo_db()
    sym = str(symbol or "").strip()
    if not sym:
        return None
    return await db.analysis_reports.find_one(
        {
            "stock_symbol": sym,
            "analysis_date": _today_date_str(),
            "status": {"$in": ["completed", None]},
        },
        sort=[("created_at", -1)],
    )


async def _find_running_task_for_symbol(user_id: str, symbol: str) -> Optional[Dict[str, Any]]:
    db = get_mongo_db()
    sym = str(symbol or "").strip()
    uid = str(user_id or "default")
    if not sym:
        return None
    return await db.analysis_tasks.find_one(
        {
            "user_id": uid,
            "stock_symbol": sym,
            "status": {"$in": ["pending", "processing", "running"]},
            "created_at": {"$gte": _day_start_local()},
        },
        sort=[("created_at", -1)],
    )


async def _resolve_report_context(report_keyword: str, section: Optional[str] = None) -> Dict[str, Any]:
    q = str(report_keyword or "").strip()
    if not q:
        raise ValueError("报告关键字不能为空")
    report_doc = await _find_report_doc(q)
    if report_doc is None:
        raise ValueError(f"未找到相关分析报告: {q}")

    stock_symbol = str(report_doc.get("stock_symbol") or "")
    stock_name = str(report_doc.get("stock_name") or "")
    summary = str(report_doc.get("summary") or "")
    reports = report_doc.get("reports") or {}
    selected_section, selected_label, selected_text = _select_report_section_text(
        summary=summary,
        reports=reports,
        requested_section=section,
        max_chars=REPORT_TEXT_MAX_CHARS,
    )
    if selected_section == "news_report" and _looks_like_placeholder_news_text(selected_text):
        fallback_news_text = await _fetch_news_fallback_text(report_doc)
        if fallback_news_text:
            selected_text = fallback_news_text[:REPORT_TEXT_MAX_CHARS]
    report_id = str(report_doc.get("analysis_id") or report_doc.get("task_id") or report_doc.get("_id"))
    sections = _available_report_sections(reports)

    return {
        "type": "report",
        "report_id": report_id,
        "stock_symbol": stock_symbol,
        "stock_name": stock_name,
        "summary": selected_text[:REPORT_TEXT_MAX_CHARS],
        "selected_section": selected_section,
        "selected_section_label": selected_label,
        "report_sections": [{"key": k, "label": l} for k, l in sections],
    }


async def _create_pending_action(user_id: str, chat_id: Optional[str], action: str, params: Dict[str, Any]) -> str:
    db = get_mongo_db()
    action_id = secrets.token_hex(4)
    await db.feishu_pending_actions.update_one(
        {"action_id": action_id},
        {
            "$set": {
                "action_id": action_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "action": action,
                "params": params,
                "status": "pending",
                "created_at": datetime.utcnow(),
                "expires_at": datetime.utcnow() + timedelta(minutes=10),
            }
        },
        upsert=True,
    )
    return action_id


async def _load_pending_action(user_id: str, action_id: str) -> Optional[Dict[str, Any]]:
    db = get_mongo_db()
    doc = await db.feishu_pending_actions.find_one(
        {"action_id": action_id, "user_id": user_id, "status": "pending"}
    )
    if not doc:
        return None
    expires_at = doc.get("expires_at")
    if isinstance(expires_at, datetime) and expires_at < datetime.utcnow():
        await db.feishu_pending_actions.update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": "expired", "updated_at": datetime.utcnow()}},
        )
        return None
    return doc


async def _finalize_pending_action(action_id: str, status: str) -> None:
    db = get_mongo_db()
    await db.feishu_pending_actions.update_one(
        {"action_id": action_id},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}},
    )


async def _execute_action(user_id: str, action: str, params: Dict[str, Any]) -> str:
    if action == "help":
        return _help_text()
    if action == "daily_brief":
        brief = await advisor_service.get_daily_brief(user_id)
        return brief.summary
    if action == "daily_picks":
        picks = await advisor_service.generate_daily_picks(user_id)
        lines = [f"{x.symbol} {x.action} 分数:{x.score_total}" for x in picks[:10]]
        return "\n".join(lines) if lines else "暂无候选"
    if action == "rebalance_suggestions":
        actions = await advisor_service.generate_rebalance(user_id)
        lines = [f"{x.symbol} {x.action} {x.current_weight:.0%}->{x.target_weight:.0%}" for x in actions[:10]]
        return "\n".join(lines) if lines else "暂无调仓建议"
    if action == "positions_snapshot":
        positions = await portfolio_service.get_unified_positions(user_id)
        lines = [f"{x.market} {x.symbol} 持仓:{x.quantity}" for x in positions[:20]]
        return "\n".join(lines) if lines else "暂无持仓"
    if action == "run_stock_analysis":
        return "已识别个股分析意图，正在启动任务。"
    if action == "run_stock_screening":
        strategy, limit, markets = _parse_screen_args([
            str(params.get("strategy") or ""),
            str(params.get("limit") or ""),
            ",".join(params.get("markets") or []) if isinstance(params.get("markets"), list) else str(params.get("markets") or ""),
        ])
        return await _run_stock_screening(strategy=strategy, limit=limit, markets=markets)
    if action == "add_watchlist":
        return await _add_watchlist(user_id, str(params.get("stock_name_or_code", "")))
    if action == "remove_watchlist":
        return await _remove_watchlist(user_id, str(params.get("stock_name_or_code", "")))
    if action == "list_watchlist":
        return await _list_watchlist(user_id)
    if action == "add_a_share_position":
        symbol, name = await _resolve_a_share_symbol(str(params.get("stock_name_or_code", "")))
        qty = float(params.get("quantity", 100.0))
        if qty <= 0:
            raise ValueError("数量必须大于0")
        avg_cost = params.get("avg_cost")
        if avg_cost is not None:
            avg_cost = float(avg_cost)
        else:
            avg_cost = await _latest_a_share_price(symbol)
        market_value = avg_cost * qty if avg_cost is not None else None
        get_a_share_sqlite_service().upsert_position(
            symbol=symbol,
            name=name,
            quantity=qty,
            avg_cost=avg_cost,
            market_value=market_value,
        )
        return f"已添加A股持仓: {symbol} {name} 数量:{qty:g}"
    if action == "set_report_context":
        keyword = str(params.get("report_keyword") or "")
        if not keyword:
            raise ValueError("缺少报告关键字")
        return f"报告上下文已识别: {keyword}"
    return "暂不支持该动作，请换种说法。"


async def _add_watchlist(user_id: str, stock_keyword: str) -> str:
    symbol, name = await _resolve_a_share_symbol(stock_keyword)
    exists = await favorites_service.is_favorite(user_id, symbol)
    if exists:
        return f"已在自选股中: {symbol} {name}"
    await favorites_service.add_favorite(
        user_id=user_id,
        stock_code=symbol,
        stock_name=name,
        market="A股",
        tags=[],
        notes="由飞书自然语言代理添加",
    )
    return f"已加入自选股: {symbol} {name}"


async def _remove_watchlist(user_id: str, stock_keyword: str) -> str:
    symbol, name = await _resolve_a_share_symbol(stock_keyword)
    ok_del = await favorites_service.remove_favorite(user_id, symbol)
    return f"已从自选股移除: {symbol} {name}" if ok_del else f"自选股中未找到: {symbol} {name}"


async def _list_watchlist(user_id: str) -> str:
    items = await favorites_service.get_user_favorites(user_id)
    if not items:
        return "自选股为空"
    lines = []
    for it in items[:30]:
        lines.append(f"{it.get('stock_code','')} {it.get('stock_name','')}")
    return "\n".join(lines)


def _normalize_screen_strategy(raw: Optional[str]) -> str:
    s = str(raw or "").strip().lower()
    if s in {"", "value", "lowpe", "价值", "低估"}:
        return "价值"
    if s in {"quality", "roe", "质量"}:
        return "质量"
    if s in {"momentum", "动量", "趋势"}:
        return "动量"
    return "价值"


def _clamp_screen_limit(raw: Optional[str], default: int = 10) -> int:
    try:
        n = int(float(str(raw).strip()))
    except Exception:
        return default
    return max(1, min(30, n))


def _normalize_market_token(raw: Optional[str]) -> Optional[str]:
    t = str(raw or "").strip().lower()
    if not t:
        return None
    if t in {"a股", "a", "cn", "ashare", "ashares"}:
        return "CN"
    if t in {"港股", "hk", "hkex", "hongkong", "hong_kong"}:
        return "HK"
    if t in {"美股", "us", "usa", "nasdaq", "nyse"}:
        return "US"
    return None


def _market_label(market: str) -> str:
    return {"CN": "A股", "HK": "港股", "US": "美股"}.get(market, market)


def _parse_screen_markets_from_text(text: str) -> List[str]:
    tokens = re.split(r"[,/|，、\s]+", str(text or ""))
    markets: List[str] = []
    for token in tokens:
        mk = _normalize_market_token(token)
        if mk and mk not in markets:
            markets.append(mk)

    whole = str(text or "")
    if "A股" in whole and "CN" not in markets:
        markets.append("CN")
    if "港股" in whole and "HK" not in markets:
        markets.append("HK")
    if "美股" in whole and "US" not in markets:
        markets.append("US")
    return markets


def _parse_screen_args(args: List[str]) -> Tuple[str, int, List[str]]:
    strategy = "价值"
    limit = 10
    markets: List[str] = []
    for arg in args:
        token = str(arg or "").strip()
        if not token:
            continue
        if re.fullmatch(r"\d{1,2}", token):
            limit = _clamp_screen_limit(token, default=limit)
            continue
        m = re.search(r"(?:top|前)\s*(\d{1,2})", token, flags=re.IGNORECASE)
        if m:
            limit = _clamp_screen_limit(m.group(1), default=limit)
            continue
        parsed_markets = _parse_screen_markets_from_text(token)
        if parsed_markets:
            for mk in parsed_markets:
                if mk not in markets:
                    markets.append(mk)
            continue
        strategy = _normalize_screen_strategy(token)
    if not markets:
        markets = ["CN"]
    return strategy, limit, markets


def _format_optional_number(value: Any, digits: int = 2) -> str:
    try:
        if value is None:
            return "-"
        v = float(value)
        return f"{v:.{digits}f}"
    except Exception:
        return "-"


def _build_screening_preset(strategy: str, level: int = 0) -> Tuple[List[ScreeningCondition], List[Dict[str, str]], str]:
    s = _normalize_screen_strategy(strategy)
    if s == "质量":
        if level >= 2:
            return (
                [
                    ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 100]),
                    ScreeningCondition(field="pb", operator=OperatorType.BETWEEN, value=[0, 15]),
                ],
                [{"field": "roe", "direction": "desc"}],
                "质量",
            )
        if level == 1:
            return (
                [
                    ScreeningCondition(field="roe", operator=OperatorType.GTE, value=8),
                    ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 60]),
                    ScreeningCondition(field="pb", operator=OperatorType.BETWEEN, value=[0, 12]),
                ],
                [{"field": "roe", "direction": "desc"}],
                "质量",
            )
        return (
            [
                ScreeningCondition(field="roe", operator=OperatorType.GTE, value=12),
                ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 40]),
                ScreeningCondition(field="pb", operator=OperatorType.BETWEEN, value=[0, 8]),
            ],
            [{"field": "roe", "direction": "desc"}],
            "质量",
        )
    if s == "动量":
        if level >= 2:
            return (
                [
                    ScreeningCondition(field="pct_chg", operator=OperatorType.GTE, value=-2),
                    ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 200]),
                ],
                [{"field": "pct_chg", "direction": "desc"}],
                "动量",
            )
        if level == 1:
            return (
                [
                    ScreeningCondition(field="pct_chg", operator=OperatorType.GTE, value=0),
                    ScreeningCondition(field="turnover_rate", operator=OperatorType.GTE, value=0.3),
                    ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 150]),
                ],
                [{"field": "pct_chg", "direction": "desc"}],
                "动量",
            )
        return (
            [
                ScreeningCondition(field="pct_chg", operator=OperatorType.GTE, value=2),
                ScreeningCondition(field="turnover_rate", operator=OperatorType.GTE, value=1),
                ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 100]),
            ],
            [{"field": "pct_chg", "direction": "desc"}],
            "动量",
        )
    if level >= 2:
        return (
            [
                ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 60]),
                ScreeningCondition(field="pb", operator=OperatorType.BETWEEN, value=[0, 10]),
            ],
            [{"field": "pe", "direction": "asc"}],
            "价值",
        )
    if level == 1:
        return (
            [
                ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 35]),
                ScreeningCondition(field="pb", operator=OperatorType.BETWEEN, value=[0, 6]),
                ScreeningCondition(field="roe", operator=OperatorType.GTE, value=5),
            ],
            [{"field": "pe", "direction": "asc"}],
            "价值",
        )
    return (
        [
            ScreeningCondition(field="pe", operator=OperatorType.BETWEEN, value=[0, 25]),
            ScreeningCondition(field="pb", operator=OperatorType.BETWEEN, value=[0, 4]),
            ScreeningCondition(field="roe", operator=OperatorType.GTE, value=6),
        ],
        [{"field": "pe", "direction": "asc"}],
        "价值",
    )


def _safe_num(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return x
    except Exception:
        return None


def _is_match_strategy(item: Dict[str, Any], strategy_label: str, level: int = 0) -> bool:
    pe = _safe_num(item.get("pe"))
    pb = _safe_num(item.get("pb"))
    roe = _safe_num(item.get("roe"))
    pct = _safe_num(item.get("pct_chg"))
    turnover = _safe_num(item.get("turnover_rate"))

    if strategy_label == "质量":
        if level == 0:
            return (roe is not None and roe >= 12) and (pe is not None and 0 <= pe <= 40) and (pb is not None and 0 <= pb <= 8)
        if level == 1:
            return (roe is not None and roe >= 8) and (pe is not None and 0 <= pe <= 60) and (pb is not None and 0 <= pb <= 12)
        return (roe is None or roe >= 5) and (pe is not None and 0 <= pe <= 100) and (pb is not None and 0 <= pb <= 15)

    if strategy_label == "动量":
        if level == 0:
            return (pct is not None and pct >= 2) and (turnover is None or turnover >= 1) and (pe is None or 0 <= pe <= 100)
        if level == 1:
            return (pct is not None and pct >= 0) and (turnover is None or turnover >= 0.3) and (pe is None or 0 <= pe <= 150)
        return (pct is not None and pct >= -2) and (pe is None or 0 <= pe <= 200)

    # 价值
    if level == 0:
        return (pe is not None and 0 <= pe <= 25) and (pb is not None and 0 <= pb <= 4) and (roe is not None and roe >= 6)
    if level == 1:
        return (pe is not None and 0 <= pe <= 35) and (pb is not None and 0 <= pb <= 6) and (roe is not None and roe >= 5)
    return (pe is not None and 0 <= pe <= 60) and (pb is not None and 0 <= pb <= 10) and (roe is None or roe >= 0)


def _sort_market_items(items: List[Dict[str, Any]], strategy_label: str) -> List[Dict[str, Any]]:
    if strategy_label == "质量":
        return sorted(items, key=lambda x: (_safe_num(x.get("roe")) is None, -(_safe_num(x.get("roe")) or -1e9)))
    if strategy_label == "动量":
        return sorted(items, key=lambda x: (_safe_num(x.get("pct_chg")) is None, -(_safe_num(x.get("pct_chg")) or -1e9)))
    return sorted(items, key=lambda x: (_safe_num(x.get("pe")) is None, (_safe_num(x.get("pe")) or 1e9)))


def _normalize_cn_code(raw: Any) -> Optional[str]:
    s = str(raw or "").strip().upper()
    if not s:
        return None
    if "." in s:
        s = s.split(".", 1)[0]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if len(digits) > 6:
        digits = digits[-6:]
    return digits.zfill(6)


def _normalize_hk_code(raw: Any) -> Optional[str]:
    s = str(raw or "").strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if len(digits) > 5:
        digits = digits[-5:]
    return digits.zfill(5)


def _coalesce_num(*values: Any) -> Optional[float]:
    for value in values:
        n = _safe_num(value)
        if n is not None:
            return n
    return None


def _parse_datetime_like(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1_000_000_000_000:
                ts = ts / 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except Exception:
                    dt = None
            if dt is None:
                return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _latest_updated_at(items: List[Dict[str, Any]]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for item in items:
        if not isinstance(item, dict):
            continue
        dt = _parse_datetime_like(item.get("updated_at"))
        if dt is None:
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


def _format_updated_at_display(value: Any) -> str:
    dt = _parse_datetime_like(value)
    if dt is None:
        return "-"
    local_dt = to_config_tz(dt)
    if local_dt is None:
        return "-"
    return local_dt.strftime("%Y-%m-%d %H:%M")


def _normalize_roe_percent(v: Any) -> Optional[float]:
    n = _safe_num(v)
    if n is None:
        return None
    # 某些源会返回 0.12 这类比例值，统一转为百分比展示口径
    if -2 <= n <= 2:
        return n * 100.0
    return n


def _to_ts_code(code6: str) -> Optional[str]:
    c = _normalize_cn_code(code6)
    if not c:
        return None
    if c.startswith(("60", "68", "90")):
        return f"{c}.SH"
    if c.startswith(("00", "30", "20")):
        return f"{c}.SZ"
    return None


def _normalize_cn_code_from_ts(ts_code: str) -> Optional[str]:
    s = str(ts_code or "").strip().upper()
    if "." not in s:
        return _normalize_cn_code(s)
    return _normalize_cn_code(s.split(".", 1)[0])


def _fetch_cn_real_roe_map_sync(codes: List[str], max_tushare_fetch: int = 80) -> Dict[str, float]:
    """
    仅返回“真实财报口径”的 ROE：
    1) 本地 stock_financial_data 最新财报
    2) Tushare fina_indicator（按 ts_code 单股拉取）
    """
    target_codes = [c for c in (_normalize_cn_code(x) for x in codes) if c]
    if not target_codes:
        return {}

    roe_map: Dict[str, float] = {}

    # 1) 本地财报表优先
    try:
        db = get_mongo_db_sync()
        ts_codes = [x for x in (_to_ts_code(c) for c in target_codes) if x]
        cursor = db.stock_financial_data.find(
            {
                "$or": [
                    {"code": {"$in": target_codes}},
                    {"symbol": {"$in": target_codes}},
                    {"ts_code": {"$in": ts_codes}},
                ]
            },
            {
                "_id": 0,
                "code": 1,
                "symbol": 1,
                "ts_code": 1,
                "roe": 1,
                "financial_indicators.roe": 1,
                "report_period": 1,
                "updated_at": 1,
            },
        ).sort([("report_period", -1), ("updated_at", -1)])

        for doc in cursor:
            code = _normalize_cn_code(doc.get("code") or doc.get("symbol"))
            if not code:
                code = _normalize_cn_code_from_ts(str(doc.get("ts_code") or ""))
            if not code or code in roe_map:
                continue
            roe_raw = None
            indicators = doc.get("financial_indicators")
            if isinstance(indicators, dict):
                roe_raw = indicators.get("roe")
            if roe_raw is None:
                roe_raw = doc.get("roe")
            roe_val = _normalize_roe_percent(roe_raw)
            if roe_val is not None:
                roe_map[code] = roe_val
    except Exception:
        pass

    # 2) Tushare 财报接口补齐
    missing = [c for c in target_codes if c not in roe_map]
    if not missing:
        return roe_map
    missing = missing[:max_tushare_fetch]
    try:
        from tradingagents.dataflows.providers.china.tushare import get_tushare_provider

        provider = get_tushare_provider()
        if not provider.connected:
            provider.connect_sync()
        api = provider.api
        if api is not None:
            for code in missing:
                ts_code = _to_ts_code(code)
                if not ts_code:
                    continue
                try:
                    df = api.fina_indicator(ts_code=ts_code, fields="ts_code,end_date,roe")
                    if df is None or df.empty:
                        continue
                    if "roe" not in df.columns:
                        continue
                    df_valid = df[df["roe"].notna()] if hasattr(df, "__getitem__") else df
                    if df_valid is None or df_valid.empty:
                        continue
                    if "end_date" in df_valid.columns:
                        df_valid = df_valid.sort_values("end_date", ascending=False)
                    roe_val = _normalize_roe_percent(df_valid.iloc[0].get("roe"))
                    if roe_val is not None:
                        roe_map[code] = roe_val
                except Exception:
                    continue
    except Exception:
        pass

    return roe_map


def _calc_pct_chg_from_quote_fields(quote_doc: Dict[str, Any]) -> Optional[float]:
    pct = _coalesce_num(quote_doc.get("pct_chg"), quote_doc.get("change_percent"))
    if pct is not None:
        return pct
    price = _coalesce_num(quote_doc.get("price"), quote_doc.get("close"), quote_doc.get("last_done"))
    pre_close = _coalesce_num(quote_doc.get("pre_close"), quote_doc.get("prev_close"))
    if price is not None and pre_close not in (None, 0, 0.0):
        return (price / pre_close - 1.0) * 100.0
    open_price = _coalesce_num(quote_doc.get("open"))
    if price is not None and open_price not in (None, 0, 0.0):
        return (price / open_price - 1.0) * 100.0
    return None


def _fetch_hk_pct_chg_from_longport_sync(hk_codes: List[str]) -> Dict[str, float]:
    if not hk_codes:
        return {}
    try:
        from longport.openapi import Config, QuoteContext
    except Exception:
        return {}

    ad = LongportAdapter()
    app_key, app_secret, access_token = ad._read_credentials()
    if not (app_key and app_secret and access_token):
        return {}

    os.environ["LONGPORT_APP_KEY"] = app_key
    os.environ["LONGPORT_APP_SECRET"] = app_secret
    os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

    normalized_codes: List[str] = []
    for code in hk_codes:
        c = _normalize_hk_code(code)
        if c:
            normalized_codes.append(c)
    if not normalized_codes:
        return {}

    # 长桥港股代码常见格式为 700.HK（不带前导0）
    symbols = [f"{int(code)}.HK" for code in normalized_codes]
    result: Dict[str, float] = {}
    try:
        ctx = QuoteContext(Config.from_env())
        batch_size = 50
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            quotes = ctx.quote(batch)
            for q in quotes or []:
                symbol = str(getattr(q, "symbol", "") or "")
                m = re.search(r"(\d+)\.HK$", symbol, flags=re.IGNORECASE)
                if not m:
                    continue
                code = str(m.group(1)).zfill(5)
                close = _coalesce_num(getattr(q, "last_done", None), getattr(q, "latest_done", None))
                pre_close = _coalesce_num(getattr(q, "prev_close", None))
                if close is not None and pre_close not in (None, 0, 0.0):
                    result[code] = (close / pre_close - 1.0) * 100.0
    except Exception:
        return {}
    return result


async def _screen_cn_from_sources(strategy: str, limit: int) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    strategy_label = _normalize_screen_strategy(strategy)
    manager = DataSourceManager()

    stock_df, stock_source = await asyncio.to_thread(manager.get_stock_list_with_fallback)
    trade_date = await asyncio.to_thread(manager.find_latest_trade_date_with_fallback)
    if not trade_date:
        trade_date = datetime.now().strftime("%Y%m%d")
    basic_df, basic_source = await asyncio.to_thread(manager.get_daily_basic_with_fallback, trade_date)
    quotes_map, quote_source = await asyncio.to_thread(manager.get_realtime_quotes_with_fallback)

    stock_name_map: Dict[str, str] = {}
    code_to_ts_map: Dict[str, str] = {}
    if stock_df is not None and not stock_df.empty:
        for _, row in stock_df.head(8000).iterrows():
            code = _normalize_cn_code(row.get("symbol") or row.get("code") or row.get("ts_code"))
            if not code or code in stock_name_map:
                continue
            stock_name_map[code] = str(row.get("name") or row.get("stock_name") or code).strip() or code
            ts_code = str(row.get("ts_code") or "").strip().upper()
            if ts_code:
                code_to_ts_map[code] = ts_code

    merged_map: Dict[str, Dict[str, Any]] = {}
    if basic_df is not None and not basic_df.empty:
        for _, row in basic_df.iterrows():
            code = _normalize_cn_code(row.get("symbol") or row.get("code") or row.get("ts_code"))
            if not code:
                continue
            merged_map[code] = {
                "code": code,
                "symbol": code,
                "name": str(row.get("name") or stock_name_map.get(code) or code).strip() or code,
                "pe": _coalesce_num(row.get("pe"), row.get("pe_ttm")),
                "pb": _coalesce_num(row.get("pb"), row.get("pb_mrq")),
                "roe": _coalesce_num(row.get("roe")),
                "pct_chg": _coalesce_num(row.get("pct_chg")),
                "turnover_rate": _coalesce_num(row.get("turnover_rate")),
                "source": basic_source or "external",
                "updated_at": row.get("updated_at"),
            }

    if isinstance(quotes_map, dict):
        for code_raw, q in quotes_map.items():
            code = _normalize_cn_code(code_raw)
            if not code:
                continue
            item = merged_map.setdefault(
                code,
                {
                    "code": code,
                    "symbol": code,
                    "name": stock_name_map.get(code, code),
                    "pe": None,
                    "pb": None,
                    "roe": None,
                    "pct_chg": None,
                    "turnover_rate": None,
                    "source": quote_source or "external",
                },
            )
            qd = q if isinstance(q, dict) else {}
            item["pct_chg"] = _coalesce_num(item.get("pct_chg"), qd.get("pct_chg"), qd.get("change_percent"))
            item["turnover_rate"] = _coalesce_num(item.get("turnover_rate"), qd.get("turnover_rate"))
            item["updated_at"] = qd.get("updated_at") or item.get("updated_at")
            if not item.get("name"):
                item["name"] = stock_name_map.get(code, code)

    # A股 ROE 兜底：当大部分为空时，拉最近财报期 ROE 快照按 ts_code 合并
    if merged_map:
        missing_roe_count = sum(1 for v in merged_map.values() if _safe_num(v.get("roe")) is None)
        if missing_roe_count >= max(20, int(len(merged_map) * 0.3)):
            try:
                roe_map = await asyncio.to_thread(fetch_latest_roe_map)
            except Exception:
                roe_map = {}
            if isinstance(roe_map, dict) and roe_map:
                for code, item in merged_map.items():
                    if _safe_num(item.get("roe")) is not None:
                        continue
                    ts_code = code_to_ts_map.get(code)
                    if not ts_code:
                        if code.startswith(("60", "68", "90")):
                            ts_code = f"{code}.SH"
                        elif code.startswith(("00", "30", "20")):
                            ts_code = f"{code}.SZ"
                    if not ts_code:
                        continue
                    roe_doc = roe_map.get(ts_code)
                    if not isinstance(roe_doc, dict):
                        continue
                    roe_val = _safe_num(roe_doc.get("roe"))
                    if roe_val is not None:
                        item["roe"] = roe_val

    # 真实财报 ROE 补齐：本地财报表 + Tushare 财报接口
    if merged_map:
        # 优先给低估值候选补，减少接口压力
        ranked_for_roe = sorted(
            list(merged_map.values()),
            key=lambda x: (
                _safe_num(x.get("pe")) is None,
                (_safe_num(x.get("pe")) or 1e9),
                _safe_num(x.get("pb")) is None,
                (_safe_num(x.get("pb")) or 1e9),
            ),
        )
        roe_candidates = [str(x.get("code") or x.get("symbol") or "") for x in ranked_for_roe[:200]]
        try:
            real_roe_map = await asyncio.to_thread(_fetch_cn_real_roe_map_sync, roe_candidates)
        except Exception:
            real_roe_map = {}
        if isinstance(real_roe_map, dict) and real_roe_map:
            for code, item in merged_map.items():
                roe_val = _safe_num(real_roe_map.get(code))
                if roe_val is not None:
                    item["roe"] = roe_val

    items = list(merged_map.values())
    if not items:
        return [], 0, {
            "strategy_label": strategy_label,
            "level": 3,
            "fallback_used": True,
            "data_source_desc": "外部数据源不可用",
        }

    for level in (0, 1, 2):
        matched = [it for it in items if _is_match_strategy(it, strategy_label, level=level)]
        sorted_items = _sort_market_items(matched, strategy_label)
        if sorted_items:
            source_desc = " + ".join([x for x in [basic_source, quote_source, stock_source] if x]) or "external"
            return sorted_items[:limit], len(sorted_items), {
                "strategy_label": strategy_label,
                "level": level,
                "fallback_used": True,
                "trade_date": trade_date,
                "data_source_desc": source_desc,
                "latest_updated_at": _latest_updated_at(sorted_items),
            }

    # 最后一层：只要有可排序数据就返回，避免空卡片
    ranked = _sort_market_items(items, strategy_label)
    if ranked:
        source_desc = " + ".join([x for x in [basic_source, quote_source, stock_source] if x]) or "external"
        return ranked[:limit], len(ranked), {
            "strategy_label": strategy_label,
            "level": 3,
            "fallback_used": True,
            "trade_date": trade_date,
            "data_source_desc": source_desc,
            "latest_updated_at": _latest_updated_at(ranked),
        }
    return [], 0, {
        "strategy_label": strategy_label,
        "level": 3,
        "fallback_used": True,
        "trade_date": trade_date,
        "data_source_desc": "外部数据源",
    }


async def _screen_hk_us_from_sources(market: str, strategy: str, limit: int) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    strategy_label = _normalize_screen_strategy(strategy)
    source_symbols = HK_SCREEN_FALLBACK_SYMBOLS if market == "HK" else US_SCREEN_FALLBACK_SYMBOLS
    pick_n = min(len(source_symbols), max(limit + 4, 10))
    symbols = source_symbols[:pick_n]
    if not symbols:
        return [], 0, {"strategy_label": strategy_label, "level": 3, "fallback_used": True}

    # 美股优先走 yfinance，避免 alpha_vantage/finnhub 未配置时产生日志噪音
    if market == "US":
        def _fetch_us_by_yfinance(symbol: str) -> Optional[Dict[str, Any]]:
            try:
                import yfinance as yf
                ticker = yf.Ticker(symbol)
                info = ticker.info or {}
                hist = ticker.history(period="5d")
                pct_chg = None
                if hist is not None and not hist.empty:
                    last_close = _safe_num(hist.iloc[-1].get("Close"))
                    prev_close = _safe_num(hist.iloc[-2].get("Close")) if len(hist) >= 2 else None
                    if last_close is not None and prev_close not in (None, 0, 0.0):
                        pct_chg = (last_close / prev_close - 1.0) * 100.0
                return {
                    "code": symbol.upper(),
                    "symbol": symbol.upper(),
                    "name": str(info.get("longName") or info.get("shortName") or symbol).strip() or symbol,
                    "pe": _coalesce_num(info.get("trailingPE")),
                    "pb": _coalesce_num(info.get("priceToBook")),
                    "roe": _normalize_roe_percent(info.get("returnOnEquity")),
                    "pct_chg": pct_chg,
                    "turnover_rate": None,
                    "source": "yfinance",
                    "updated_at": datetime.utcnow(),
                }
            except Exception:
                return None

        yf_rows = await asyncio.gather(*[asyncio.to_thread(_fetch_us_by_yfinance, sym) for sym in symbols], return_exceptions=True)
        us_items = [row for row in yf_rows if isinstance(row, dict)]
        if us_items:
            for level in (0, 1, 2):
                matched = [it for it in us_items if _is_match_strategy(it, strategy_label, level=level)]
                sorted_items = _sort_market_items(matched, strategy_label)
                if sorted_items:
                    return sorted_items[:limit], len(sorted_items), {
                        "strategy_label": strategy_label,
                        "level": level,
                        "fallback_used": True,
                        "data_source_desc": "yfinance",
                        "latest_updated_at": _latest_updated_at(sorted_items),
                    }
            ranked = _sort_market_items(us_items, strategy_label)
            if ranked:
                return ranked[:limit], len(ranked), {
                    "strategy_label": strategy_label,
                    "level": 3,
                    "fallback_used": True,
                    "data_source_desc": "yfinance",
                    "latest_updated_at": _latest_updated_at(ranked),
                }

    try:
        db = get_mongo_db()
    except Exception:
        db = None
    service = ForeignStockService(db=db)
    semaphore = asyncio.Semaphore(4 if market == "US" else 2)

    async def _fetch_one(symbol: str) -> Optional[Dict[str, Any]]:
        async with semaphore:
            try:
                info, quote = await asyncio.gather(
                    service.get_basic_info(market, symbol, force_refresh=False),
                    service.get_quote(market, symbol, force_refresh=False),
                    return_exceptions=True,
                )
            except Exception:
                return None

            info_doc = info if isinstance(info, dict) else {}
            quote_doc = quote if isinstance(quote, dict) else {}
            if not info_doc and not quote_doc:
                return None

            normalized_code: str
            if market == "HK":
                normalized_code = _normalize_hk_code(info_doc.get("code") or quote_doc.get("code") or symbol) or symbol
            else:
                normalized_code = str(info_doc.get("code") or quote_doc.get("code") or symbol).upper().replace(".US", "")

            name = str(info_doc.get("name") or quote_doc.get("name") or normalized_code).strip() or normalized_code
            data_sources = list(
                dict.fromkeys(
                    [str(x).strip() for x in [info_doc.get("source"), quote_doc.get("source")] if str(x or "").strip()]
                )
            )
            quote_updated_at = quote_doc.get("updated_at")
            basic_updated_at = info_doc.get("updated_at")

            return {
                "code": normalized_code,
                "symbol": normalized_code,
                "name": name,
                "pe": _coalesce_num(info_doc.get("pe"), info_doc.get("pe_ttm")),
                "pb": _coalesce_num(info_doc.get("pb")),
                "roe": _coalesce_num(info_doc.get("roe")),
                "pct_chg": _calc_pct_chg_from_quote_fields(quote_doc),
                "turnover_rate": _coalesce_num(info_doc.get("turnover_rate"), quote_doc.get("turnover_rate")),
                "source": "+".join(data_sources) if data_sources else "external",
                "updated_at": quote_updated_at or basic_updated_at,
            }

    fetched = await asyncio.gather(*[_fetch_one(sym) for sym in symbols], return_exceptions=True)
    dedup: Dict[str, Dict[str, Any]] = {}
    for row in fetched:
        if isinstance(row, dict):
            key = str(row.get("code") or row.get("symbol") or "").strip()
            if key and key not in dedup:
                dedup[key] = row

    # 港股涨跌补齐：优先尝试长桥实时行情（仅补缺失项）
    if market == "HK":
        missing_pct_codes = [
            str(v.get("code") or v.get("symbol") or "").strip()
            for v in dedup.values()
            if _safe_num(v.get("pct_chg")) is None
        ]
        if missing_pct_codes:
            longport_pct_map = await asyncio.to_thread(_fetch_hk_pct_chg_from_longport_sync, missing_pct_codes)
            for code_raw, pct in longport_pct_map.items():
                code = _normalize_hk_code(code_raw)
                if not code:
                    continue
                item = dedup.get(code)
                if item is None:
                    continue
                if _safe_num(item.get("pct_chg")) is None:
                    item["pct_chg"] = pct
                    src = str(item.get("source") or "external")
                    if "longport" not in src:
                        item["source"] = f"{src}+longport"

    items = list(dedup.values())

    if not items:
        return [], 0, {"strategy_label": strategy_label, "level": 3, "fallback_used": True}

    for level in (0, 1, 2):
        matched = [it for it in items if _is_match_strategy(it, strategy_label, level=level)]
        sorted_items = _sort_market_items(matched, strategy_label)
        if sorted_items:
            return sorted_items[:limit], len(sorted_items), {
                "strategy_label": strategy_label,
                "level": level,
                "fallback_used": True,
                "data_source_desc": "external",
                "latest_updated_at": _latest_updated_at(sorted_items),
            }

    ranked = _sort_market_items(items, strategy_label)
    if ranked:
        return ranked[:limit], len(ranked), {
            "strategy_label": strategy_label,
            "level": 3,
            "fallback_used": True,
            "data_source_desc": "external",
            "latest_updated_at": _latest_updated_at(ranked),
        }
    return [], 0, {"strategy_label": strategy_label, "level": 3, "fallback_used": True}


async def _screen_cn_with_relax(strategy: str, limit: int) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    screening_service = get_enhanced_screening_service()
    last_meta: Dict[str, Any] = {}
    for level in (0, 1, 2):
        conditions, order_by, strategy_label = _build_screening_preset(strategy, level=level)
        result = await screening_service.screen_stocks(
            conditions=conditions,
            market="CN",
            date=None,
            adj="qfq",
            limit=limit,
            offset=0,
            order_by=order_by,
            use_database_optimization=True,
        )
        items = result.get("items") or []
        total = int(result.get("total") or 0)
        last_meta = {
            "strategy_label": strategy_label,
            "level": level,
            "took_ms": result.get("took_ms"),
            "optimization_used": result.get("optimization_used"),
            "fallback_used": False,
            "data_source_desc": "本地库",
            "latest_updated_at": _latest_updated_at(items if isinstance(items, list) else []),
        }
        if items:
            return items, total, last_meta
    fallback_items, fallback_total, fallback_meta = await _screen_cn_from_sources(strategy, limit)
    if fallback_items:
        return fallback_items, fallback_total, fallback_meta
    return [], 0, last_meta


async def _screen_hk_us_with_relax(market: str, strategy: str, limit: int) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    try:
        db = get_mongo_db()
        if market == "HK":
            basic_collection = db["stock_basic_info_hk"]
            quote_collection = db["market_quotes_hk"]
        else:
            basic_collection = db["stock_basic_info_us"]
            quote_collection = db["market_quotes_us"]

        raw_docs = await basic_collection.find(
            {},
            {"_id": 0, "code": 1, "symbol": 1, "name": 1, "pe": 1, "pb": 1, "roe": 1, "turnover_rate": 1, "source": 1, "updated_at": 1},
        ).sort("updated_at", -1).limit(1500).to_list(length=1500)

        # 去重（按 code/symbol）
        dedup: Dict[str, Dict[str, Any]] = {}
        for d in raw_docs:
            key = str(d.get("code") or d.get("symbol") or "").strip()
            if key and key not in dedup:
                dedup[key] = d
        candidates = list(dedup.values())
        if not candidates:
            fallback_items, fallback_total, fallback_meta = await _screen_hk_us_from_sources(market, strategy, limit)
            if fallback_items:
                return fallback_items, fallback_total, fallback_meta
            return [], 0, {"strategy_label": _normalize_screen_strategy(strategy), "level": 3, "fallback_used": True}

        codes = [str(x.get("code") or x.get("symbol") or "").strip() for x in candidates if (x.get("code") or x.get("symbol"))]
        quotes = await quote_collection.find(
            {"code": {"$in": codes}},
            {"_id": 0, "code": 1, "pct_chg": 1, "turnover_rate": 1, "source": 1, "updated_at": 1},
        ).to_list(length=len(codes) or 1)
        qmap = {str(q.get("code")): q for q in quotes}

        merged: List[Dict[str, Any]] = []
        for d in candidates:
            code = str(d.get("code") or d.get("symbol") or "").strip()
            q = qmap.get(code, {})
            source_values = [
                str(x).strip()
                for x in [d.get("source"), q.get("source")]
                if str(x or "").strip()
            ]
            item = {
                "code": code,
                "symbol": code,
                "name": d.get("name"),
                "pe": d.get("pe"),
                "pb": d.get("pb"),
                "roe": d.get("roe"),
                "pct_chg": q.get("pct_chg"),
                "turnover_rate": d.get("turnover_rate") if d.get("turnover_rate") is not None else q.get("turnover_rate"),
                "source": "+".join(dict.fromkeys(source_values)) if source_values else (d.get("source") or "local"),
                "updated_at": q.get("updated_at") or d.get("updated_at"),
            }
            merged.append(item)
    except Exception:
        fallback_items, fallback_total, fallback_meta = await _screen_hk_us_from_sources(market, strategy, limit)
        if fallback_items:
            return fallback_items, fallback_total, fallback_meta
        return [], 0, {"strategy_label": _normalize_screen_strategy(strategy), "level": 3, "fallback_used": True}

    strategy_label = _normalize_screen_strategy(strategy)
    for level in (0, 1, 2):
        matched = [it for it in merged if _is_match_strategy(it, strategy_label, level=level)]
        sorted_items = _sort_market_items(matched, strategy_label)
        if sorted_items:
            return sorted_items[:limit], len(sorted_items), {
                "strategy_label": strategy_label,
                "level": level,
                "fallback_used": False,
                "data_source_desc": "本地库",
                "latest_updated_at": _latest_updated_at(sorted_items),
            }

    fallback_items, fallback_total, fallback_meta = await _screen_hk_us_from_sources(market, strategy, limit)
    if fallback_items:
        return fallback_items, fallback_total, fallback_meta
    return [], 0, {"strategy_label": strategy_label, "level": 3, "fallback_used": True}


async def _collect_stock_screening_result(
    strategy: str,
    limit: int = 10,
    markets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    limit = max(1, min(30, int(limit)))
    target_markets = markets or ["CN"]
    target_markets = [m for m in target_markets if m in {"CN", "HK", "US"}] or ["CN"]

    results: List[Dict[str, Any]] = []
    for mk in target_markets:
        market_label = _market_label(mk)
        try:
            if mk == "CN":
                items, total, meta = await _screen_cn_with_relax(strategy, limit)
            else:
                items, total, meta = await _screen_hk_us_with_relax(mk, strategy, limit)
        except Exception as e:
            results.append(
                {
                    "market": mk,
                    "market_label": market_label,
                    "strategy_label": _normalize_screen_strategy(strategy),
                    "relax_level": 0,
                    "relax_text": "严格",
                    "items": [],
                    "total": 0,
                    "error": str(e),
                    "meta": {},
                }
            )
            continue

        strategy_label = str(meta.get("strategy_label") or _normalize_screen_strategy(strategy))
        relax_level = int(meta.get("level") or 0)
        if relax_level == 0:
            relax_text = "严格"
        elif relax_level == 1:
            relax_text = "中等放宽"
        elif relax_level == 2:
            relax_text = "宽松"
        else:
            relax_text = "回源兜底"
        results.append(
            {
                "market": mk,
                "market_label": market_label,
                "strategy_label": strategy_label,
                "relax_level": relax_level,
                "relax_text": relax_text,
                "items": items[:limit],
                "total": total,
                "error": None,
                "meta": meta,
            }
        )

    return {
        "strategy_label": _normalize_screen_strategy(strategy),
        "limit": limit,
        "markets": results,
    }


def _build_screening_result_text(result: Dict[str, Any]) -> str:
    market_results = result.get("markets") if isinstance(result, dict) else []
    if not isinstance(market_results, list) or not market_results:
        return "选股执行完成，但没有可展示的结果。"

    lines: List[str] = []
    for sec in market_results:
        market_label = str(sec.get("market_label") or sec.get("market") or "-")
        strategy_label = str(sec.get("strategy_label") or result.get("strategy_label") or "价值")
        relax_text = str(sec.get("relax_text") or "严格")
        err = sec.get("error")
        if err:
            lines.append(f"{market_label}: 选股失败 - {err}")
            continue

        items = sec.get("items") if isinstance(sec.get("items"), list) else []
        total = int(sec.get("total") or 0)
        meta = sec.get("meta") if isinstance(sec.get("meta"), dict) else {}
        source_desc = str(meta.get("data_source_desc") or "").strip()
        latest_updated = _format_updated_at_display(meta.get("latest_updated_at"))
        if not items:
            if latest_updated != "-":
                lines.append(f"{market_label}: 选股完成（{strategy_label}，{relax_text}），最新更新时间 {latest_updated}。")
            else:
                lines.append(f"{market_label}: 选股完成（{strategy_label}，{relax_text}），未找到符合条件的股票。")
            continue

        lines.append(
            f"{market_label} 选股结果（{strategy_label}，{relax_text}）: 共 {total} 只，展示前 {len(items)} 只"
        )
        if source_desc:
            lines.append(f"数据来源: {source_desc}")
        if latest_updated != "-":
            lines.append(f"最新更新时间: {latest_updated}")
        for idx, it in enumerate(items, 1):
            code = str(it.get("symbol") or it.get("code") or "-")
            name = str(it.get("name") or "-")
            pe = _format_optional_number(it.get("pe"), 2)
            pb = _format_optional_number(it.get("pb"), 2)
            roe = _format_optional_number(it.get("roe"), 2)
            pct = _format_optional_number(it.get("pct_chg"), 2)
            item_source = str(it.get("source") or source_desc or "-")
            item_updated = _format_updated_at_display(it.get("updated_at") or meta.get("latest_updated_at"))
            lines.append(f"{idx}. {code} {name} | PE:{pe} PB:{pb} ROE:{roe}% 涨跌:{pct}%")
            lines.append(f"   来源:{item_source} 更新时间:{item_updated}")

        if str(sec.get("market")) == "CN" and (meta.get("took_ms") is not None or meta.get("optimization_used")):
            lines.append(
                f"{market_label} 耗时: {meta.get('took_ms', '-')}ms | 方式: {meta.get('optimization_used', '-')}"
            )

    if not lines:
        return "选股执行完成，但没有可展示的结果。"
    lines.append("可用命令：/screen 价值 10 A股,港股,美股")
    return "\n".join(lines)


def _build_screening_result_card(result: Dict[str, Any], actionable_per_market: int = 5) -> Dict[str, Any]:
    strategy_label = str(result.get("strategy_label") or "价值")
    limit = int(result.get("limit") or 10)
    market_results = result.get("markets") if isinstance(result, dict) else []

    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**策略**: {strategy_label}  |  **每市场目标**: Top {limit}\n"
                    "点击下方按钮可直接加入自选或发起个股分析。"
                ),
            },
        }
    ]

    if not isinstance(market_results, list) or not market_results:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "暂无可展示结果。"},
            }
        )
    else:
        for sec in market_results:
            market_label = str(sec.get("market_label") or sec.get("market") or "-")
            market_code = str(sec.get("market") or "")
            relax_text = str(sec.get("relax_text") or "严格")
            total = int(sec.get("total") or 0)
            err = sec.get("error")
            items = sec.get("items") if isinstance(sec.get("items"), list) else []
            meta = sec.get("meta") if isinstance(sec.get("meta"), dict) else {}
            source_desc = str(meta.get("data_source_desc") or "").strip()
            market_updated = _format_updated_at_display(meta.get("latest_updated_at"))

            elements.append({"tag": "hr"})
            if err:
                elements.append(
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**{market_label}**\n❌ 选股失败: {err}"},
                    }
                )
                continue

            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{market_label}**  共 {total} 只（条件强度：{relax_text}）"
                            + (f"\n数据来源：`{source_desc}`" if source_desc else "")
                            + (f"\n最新更新时间：`{market_updated}`" if market_updated != "-" else "")
                        ),
                    },
                }
            )
            if not items:
                elements.append(
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "暂无符合条件的股票。"},
                    }
                )
                continue

            show_n = max(1, min(actionable_per_market, len(items)))
            for idx, it in enumerate(items[:show_n], 1):
                symbol = str(it.get("symbol") or it.get("code") or "-")
                name = str(it.get("name") or symbol)
                pe = _format_optional_number(it.get("pe"), 2)
                pb = _format_optional_number(it.get("pb"), 2)
                roe = _format_optional_number(it.get("roe"), 2)
                pct = _format_optional_number(it.get("pct_chg"), 2)
                item_source = str(it.get("source") or source_desc or "-")
                item_updated = _format_updated_at_display(it.get("updated_at") or meta.get("latest_updated_at"))

                elements.append(
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"{idx}. `{symbol}` **{name}**\n"
                                f"PE:{pe}  PB:{pb}  ROE:{roe}%  涨跌:{pct}%\n"
                                f"来源：`{item_source}`  更新时间：`{item_updated}`"
                            ),
                        },
                    }
                )
                elements.append(
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "加入自选"},
                                "type": "default",
                                "value": {
                                    "intent": "screen_add_watchlist",
                                    "symbol": symbol,
                                    "stock_name": name,
                                    "market": market_label,
                                    "market_code": market_code,
                                },
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "发起分析"},
                                "type": "primary",
                                "value": {
                                    "intent": "screen_run_analysis",
                                    "symbol": symbol,
                                    "stock_name": name,
                                    "market": market_label,
                                    "market_code": market_code,
                                    "research_depth": "全面",
                                },
                            },
                        ],
                    }
                )
            if len(items) > show_n:
                elements.append(
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"_该市场仅展示前 {show_n} 只可操作标的（实际命中 {len(items)} 只）_",
                        },
                    }
                )

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"选股结果 · {strategy_label}"},
        },
        "elements": elements,
    }


async def _run_stock_screening(strategy: str, limit: int = 10, markets: Optional[List[str]] = None) -> str:
    result = await _collect_stock_screening_result(strategy=strategy, limit=limit, markets=markets)
    return _build_screening_result_text(result)


async def _run_stock_screening_with_card(
    *,
    user_id: str,
    chat_id: Optional[str],
    strategy: str,
    limit: int = 10,
    markets: Optional[List[str]] = None,
) -> Tuple[str, bool]:
    result = await _collect_stock_screening_result(strategy=strategy, limit=limit, markets=markets)
    summary_text = _build_screening_result_text(result)
    if not chat_id:
        return summary_text, False

    card = _build_screening_result_card(result)
    sent = await feishu_push_service.send_custom_card(
        chat_id=chat_id,
        card=card,
        action="send_screening_result_card",
    )
    if sent.get("success"):
        return summary_text, True
    return summary_text, False


async def _mark_inbound_message_once(message_id: str) -> bool:
    """
    Returns True if this message_id is first seen; False if duplicated.
    """
    mid = str(message_id or "").strip()
    if not mid:
        return True
    db = get_mongo_db()
    now = datetime.utcnow()
    result = await db.feishu_inbound_dedupe.update_one(
        {"message_id": mid},
        {
            "$setOnInsert": {
                "message_id": mid,
                "created_at": now,
                "expires_at": now + timedelta(days=2),
            }
        },
        upsert=True,
    )
    return result.upserted_id is not None


def _extract_message_id_from_send_result(result: Dict[str, Any]) -> Optional[str]:
    data = (result or {}).get("response", {}) if isinstance(result, dict) else {}
    payload = data.get("data") if isinstance(data, dict) else None
    if isinstance(payload, dict):
        msg_id = payload.get("message_id")
        if msg_id:
            return str(msg_id)
    return None


def _extract_card_action_value(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    action = payload.get("action")
    if isinstance(action, dict):
        val = action.get("value")
        if isinstance(val, dict):
            return val
    event = payload.get("event")
    if isinstance(event, dict):
        event_action = event.get("action")
        if isinstance(event_action, dict):
            val = event_action.get("value")
            if isinstance(val, dict):
                return val
    return None


def _extract_card_action_chat_id(payload: Dict[str, Any]) -> Optional[str]:
    if isinstance(payload.get("open_chat_id"), str) and payload.get("open_chat_id"):
        return str(payload.get("open_chat_id"))
    ctx = payload.get("context")
    if isinstance(ctx, dict) and isinstance(ctx.get("open_chat_id"), str):
        return str(ctx.get("open_chat_id"))
    event = payload.get("event")
    if isinstance(event, dict):
        event_ctx = event.get("context")
        if isinstance(event_ctx, dict) and isinstance(event_ctx.get("open_chat_id"), str):
            return str(event_ctx.get("open_chat_id"))
    return None


def _extract_card_action_user_id(payload: Dict[str, Any]) -> str:
    op = payload.get("operator")
    if isinstance(op, dict):
        return str(op.get("open_id") or op.get("user_id") or op.get("union_id") or "default")
    event = payload.get("event")
    if isinstance(event, dict):
        op = event.get("operator")
        if isinstance(op, dict):
            return str(op.get("open_id") or op.get("user_id") or op.get("union_id") or "default")
    return "default"


async def _send_report_module_message(
    *,
    chat_id: str,
    report_ctx: Dict[str, Any],
    intro_prefix: Optional[str] = None,
) -> str:
    report_id = str(report_ctx.get("report_id") or "")
    stock_name = str(report_ctx.get("stock_name") or report_ctx.get("stock_symbol") or "")
    section_label = str(report_ctx.get("selected_section_label") or "摘要")
    text = str(report_ctx.get("summary") or "").strip()
    header = f"已加载报告上下文: {stock_name} ({report_id})\n模块: {section_label}"
    if intro_prefix:
        header = f"{intro_prefix}\n{header}"
    body = f"{header}\n\n{text}" if text else header
    await feishu_push_service.send_text(chat_id, body[:REPORT_TEXT_MAX_CHARS])
    return body[:REPORT_TEXT_MAX_CHARS]


async def _send_report_selector_card_if_needed(chat_id: str, report_ctx: Dict[str, Any]) -> None:
    sections_raw = report_ctx.get("report_sections") or []
    sections: List[Tuple[str, str]] = []
    for it in sections_raw:
        if isinstance(it, dict):
            key = str(it.get("key") or "").strip()
            label = str(it.get("label") or "").strip() or _section_label(key)
            if key:
                sections.append((key, label))
    if len(sections) <= 1:
        return
    card = _build_report_selector_card(
        report_id=str(report_ctx.get("report_id") or ""),
        stock_name=str(report_ctx.get("stock_name") or ""),
        stock_symbol=str(report_ctx.get("stock_symbol") or ""),
        sections=sections,
        selected=str(report_ctx.get("selected_section") or ""),
    )
    sent = await feishu_push_service.send_custom_card(chat_id, card, action="send_report_selector_card")
    if sent.get("success"):
        return

    report_id = str(report_ctx.get("report_id") or "")
    lines = ["报告模块按钮发送失败，可用以下命令查看指定模块："]
    for key, label in sections:
        lines.append(f"- `/report {report_id} {label}`")
    await feishu_push_service.send_text(chat_id, "\n".join(lines))


async def _handle_screen_add_watchlist_action(
    *,
    user_id: str,
    symbol: str,
    stock_name: str,
    market: str,
) -> str:
    s = str(symbol or "").strip()
    if not s:
        raise ValueError("缺少股票代码")
    name = str(stock_name or s).strip() or s
    market_label = _normalize_market_type_hint(market) or str(market or "").strip() or "A股"

    exists = await favorites_service.is_favorite(user_id, s)
    if exists:
        return f"已在自选股中: {s} {name}"
    await favorites_service.add_favorite(
        user_id=user_id,
        stock_code=s,
        stock_name=name,
        market=market_label,
        tags=[],
        notes="由飞书选股卡片添加",
    )
    return f"已加入自选股: {s} {name}"


async def _handle_screen_run_analysis_action(
    *,
    user_id: str,
    chat_id: str,
    symbol: str,
    stock_name: str,
    market: str,
    research_depth: str = "全面",
) -> Tuple[str, bool]:
    s = str(symbol or "").strip()
    if not s:
        raise ValueError("缺少股票代码")
    market_hint = _normalize_market_type_hint(market) or str(market or "").strip() or "A股"
    resolved_symbol, resolved_name, market_type = await _resolve_stock_for_analysis(
        s,
        market_hint=market_hint,
        user_id=user_id,
    )
    final_name = str(stock_name or resolved_name or resolved_symbol).strip() or resolved_symbol
    conv_id = f"chat:{chat_id}:user:{user_id}"
    return await _start_or_reuse_stock_analysis(
        user_id=user_id,
        chat_id=chat_id,
        conversation_id=conv_id,
        symbol=resolved_symbol,
        stock_name=final_name,
        market_type=market_type,
        research_depth=_normalize_research_depth(research_depth, default="全面"),
        force_refresh=False,
    )


async def handle_feishu_card_action_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = _extract_card_action_value(payload) or {}
    intent = str(value.get("intent") or "").strip()

    if intent == "screen_add_watchlist":
        chat_id = _extract_card_action_chat_id(payload)
        user_id = _extract_card_action_user_id(payload)
        symbol = str(value.get("symbol") or "")
        stock_name = str(value.get("stock_name") or value.get("name") or symbol)
        market = str(value.get("market") or value.get("market_code") or "A股")
        try:
            msg = await _handle_screen_add_watchlist_action(
                user_id=user_id,
                symbol=symbol,
                stock_name=stock_name,
                market=market,
            )
            if chat_id:
                await feishu_push_service.send_text(chat_id, msg)
            return {"toast": {"type": "success", "content": msg[:60]}}
        except Exception as exc:
            if chat_id:
                await feishu_push_service.send_text(chat_id, f"加入自选失败: {exc}")
            return {"toast": {"type": "error", "content": "加入自选失败"}}

    if intent == "screen_run_analysis":
        chat_id = _extract_card_action_chat_id(payload)
        user_id = _extract_card_action_user_id(payload)
        symbol = str(value.get("symbol") or "")
        stock_name = str(value.get("stock_name") or value.get("name") or symbol)
        market = str(value.get("market") or value.get("market_code") or "A股")
        research_depth = str(value.get("research_depth") or "全面")
        if not chat_id:
            return {"toast": {"type": "error", "content": "缺少会话ID"}}
        try:
            reply, already_sent = await _handle_screen_run_analysis_action(
                user_id=user_id,
                chat_id=chat_id,
                symbol=symbol,
                stock_name=stock_name,
                market=market,
                research_depth=research_depth,
            )
            if chat_id and not already_sent:
                await feishu_push_service.send_text(chat_id, reply)
            await _upsert_conversation(
                conversation_id=f"chat:{chat_id}:user:{user_id}",
                user_id=user_id,
                chat_id=chat_id,
                push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
            )
            return {"toast": {"type": "success", "content": "已启动分析任务"}}
        except Exception as exc:
            await feishu_push_service.send_text(chat_id, f"启动分析失败: {exc}")
            return {"toast": {"type": "error", "content": "启动分析失败"}}

    if intent != "show_report_section":
        return {"toast": {"type": "info", "content": "未识别的卡片操作，已忽略"}}

    report_id = str(value.get("report_id") or "").strip()
    section = str(value.get("section") or "").strip()
    chat_id = _extract_card_action_chat_id(payload)
    user_id = _extract_card_action_user_id(payload)
    if not report_id:
        return {"toast": {"type": "error", "content": "缺少报告ID"}}
    if not chat_id:
        return {"toast": {"type": "error", "content": "缺少会话ID"}}

    try:
        ctx = await _resolve_report_context(report_id, section=section)
        conv_id = f"chat:{chat_id}:user:{user_id}"
        await _upsert_conversation(
            conversation_id=conv_id,
            user_id=user_id,
            chat_id=chat_id,
            set_context=ctx,
        )
        await _send_report_module_message(
            chat_id=chat_id,
            report_ctx=ctx,
            intro_prefix="已根据你的选择返回对应报告模块。",
        )
        return {"toast": {"type": "success", "content": f"已返回模块：{ctx.get('selected_section_label') or '报告'}"}}
    except Exception as exc:
        await feishu_push_service.send_text(chat_id, f"加载报告模块失败: {exc}")
        return {"toast": {"type": "error", "content": "加载失败，请稍后重试"}}


def _build_analysis_progress_card(
    *,
    stock_name: str,
    symbol: str,
    task_id: str,
    status: str,
    progress: int,
    step: str,
    message: str,
) -> tuple[str, list[str], str]:
    st = str(status or "").lower()
    if st in {"completed"}:
        template = "green"
    elif st in {"failed", "cancelled"}:
        template = "red"
    elif st in {"pending"}:
        template = "orange"
    else:
        template = "blue"

    title = f"个股分析进度 · {stock_name} ({symbol})"
    lines = [
        f"- **任务ID**: `{task_id}`",
        f"- **状态**: **{status}**",
        f"- **进度**: **{progress}%**",
        f"- **阶段**: {step or '-'}",
        "",
        f"> {message}" if message else "> -",
    ]
    return title, lines, template


async def _resolve_stock_for_analysis(
    stock_keyword: str,
    market_hint: Optional[str] = None,
    user_id: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    返回: (symbol, display_name, market_type)
    symbol 传给分析任务；market_type 传给 AnalysisParameters.market_type
    """
    q = str(stock_keyword or "").strip()
    if not q:
        raise ValueError("股票名称或代码不能为空")

    market_type = _normalize_market_type_hint(market_hint) or str(market_hint or "").strip() or None
    q_clean = feishu_nl_agent_service._strip_market_suffix(q) or q

    # 兼容 hk00981 / usAAPL 直传代码
    m_hk_pref = re.fullmatch(r"(?i)hk[\s\-_\.]*([0-9]{1,5})", q)
    if m_hk_pref:
        digits = m_hk_pref.group(1).zfill(5)
        return f"{digits}.HK", f"{digits}.HK", market_type or "港股"
    m_us_pref = re.fullmatch(r"(?i)us[\s\-_\.]*([A-Z][A-Z0-9.\-]{0,9})", q)
    if m_us_pref:
        sym = m_us_pref.group(1).upper().replace(".US", "")
        return sym, sym, market_type or "美股"

    if re.fullmatch(r"\d{6}", q):
        symbol, name = await _resolve_a_share_symbol(q)
        return symbol, name, market_type or "A股"

    # 港股代码（5位）支持补 .HK
    if re.fullmatch(r"\d{1,5}", q) and (market_type == "港股" or len(q) == 5):
        digits = q.zfill(5)
        return f"{digits}.HK", digits, market_type or "港股"

    # 已带市场后缀
    if re.fullmatch(r"[A-Za-z0-9]+\\.(HK|US|SH|SZ)", q, flags=re.IGNORECASE):
        suffix = q.split(".")[-1].upper()
        inferred_market = "港股" if suffix == "HK" else ("美股" if suffix == "US" else "A股")
        return q.upper(), q.upper(), market_type or inferred_market

    # 按名称查询
    db = get_mongo_db()
    query: Dict[str, Any] = {"name": {"$regex": re.escape(q_clean), "$options": "i"}}
    if market_type in {"港股", "美股", "A股"}:
        query["$or"] = [{"market_type": market_type}, {"market": market_type}]

    candidates = await db.stock_basic_info.find(
        query,
        {"_id": 0, "name": 1, "code": 1, "symbol": 1, "market_type": 1, "market": 1, "updated_at": 1},
    ).sort([("updated_at", -1)]).to_list(length=20)

    doc: Optional[Dict[str, Any]] = None
    if len(candidates) == 1:
        doc = candidates[0]
    elif len(candidates) > 1:
        ai_candidates: List[Dict[str, str]] = []
        for item in candidates:
            ai_candidates.append(
                {
                    "symbol": str(item.get("symbol") or item.get("code") or ""),
                    "name": str(item.get("name") or ""),
                    "market": str(item.get("market_type") or item.get("market") or ""),
                }
            )

        decision_text = q_clean if not market_type else f"{q_clean}（{market_type}）"
        picked = feishu_nl_agent_service.choose_stock_candidate(decision_text, ai_candidates)
        if picked:
            picked_symbol = str(picked.get("symbol") or "").strip()
            if picked_symbol:
                for item in candidates:
                    symbol = str(item.get("symbol") or item.get("code") or "").strip()
                    if symbol.upper() == picked_symbol.upper():
                        doc = item
                        break

        if doc is None:
            options: List[str] = []
            for item in candidates[:5]:
                name = str(item.get("name") or "-")
                symbol = str(item.get("symbol") or item.get("code") or "-")
                mk = str(item.get("market_type") or item.get("market") or "-")
                options.append(f"{name}({symbol},{mk})")
            raise ValueError(
                "匹配到多个同名标的，AI未能确定唯一结果。"
                f"请明确市场或代码后重试。候选: {'; '.join(options)}"
            )
    if not doc and market_type in {"港股", "美股"}:
        # 先从最新持仓快照中尝试反查（优先用户自己的标的）
        snap = await db.portfolio_snapshots.find_one(
            {"user_id": user_id or "default"},
            {"_id": 0, "positions": 1},
            sort=[("created_at", -1)],
        )
        positions = snap.get("positions") if isinstance(snap, dict) else []
        market_alias = "HK" if market_type == "港股" else "US"
        for p in positions or []:
            p_market = str(p.get("market") or "").upper()
            p_symbol = str(p.get("symbol") or "").strip()
            p_name = str(p.get("name") or "").strip()
            if p_market != market_alias:
                continue
            if (q_clean in p_name) or (q_clean.upper() in p_symbol.upper()):
                return p_symbol, (p_name or q_clean), market_type

    if not doc and market_type == "港股":
        # 港股名称反查代码（AKShare）
        hk = await _resolve_hk_symbol_by_name(q_clean)
        if hk:
            return hk[0], hk[1], "港股"

    if not doc and market_type in {"港股", "美股"}:
        inferred_symbol = feishu_nl_agent_service.infer_symbol_from_name(q_clean, market_type)
        if inferred_symbol:
            return inferred_symbol, q_clean, market_type

    if not doc:
        raise ValueError(f"未找到股票: {q_clean}")

    name = str(doc.get("name") or q)
    symbol = str(doc.get("symbol") or doc.get("code") or "").strip()
    if not symbol:
        raise ValueError(f"股票标识缺失: {q}")

    # 归一化 A股代码
    if re.fullmatch(r"\d{6}", symbol):
        return symbol, name, market_type or "A股"
    # 港股/美股保留原始 symbol
    mt = str(doc.get("market_type") or doc.get("market") or "").strip() or market_type or "A股"
    return symbol, name, mt


async def _resolve_hk_symbol_by_name(name_keyword: str) -> Optional[tuple[str, str]]:
    q = str(name_keyword or "").strip()
    if not q:
        return None

    def _search() -> Optional[tuple[str, str]]:
        try:
            import akshare as ak
        except Exception:
            return None
        try:
            df = ak.stock_hk_spot()
        except Exception:
            return None
        if df is None or df.empty:
            return None

        code_col = "代码" if "代码" in df.columns else None
        name_col = "中文名称" if "中文名称" in df.columns else ("名称" if "名称" in df.columns else None)
        if not code_col or not name_col:
            return None

        names = df[name_col].astype(str)
        exact = df[names == q]
        chosen = exact if not exact.empty else df[names.str.contains(re.escape(q), case=False, na=False)]
        if chosen.empty:
            return None

        row = chosen.iloc[0]
        raw_code = str(row.get(code_col, "")).strip()
        code_digits = re.sub(r"\D", "", raw_code)
        if not code_digits:
            return None
        symbol = f"{code_digits.zfill(5)[-5:]}.HK"
        name = str(row.get(name_col, "")).strip() or q
        return symbol, name

    try:
        return await asyncio.wait_for(asyncio.to_thread(_search), timeout=8)
    except Exception:
        return None


async def _push_analysis_progress_loop(
    chat_id: str,
    progress_message_id: Optional[str],
    task_id: str,
    symbol: str,
    stock_name: Optional[str] = None,
) -> None:
    service = get_simple_analysis_service()
    display_name = str(stock_name or symbol)
    last_progress = -1
    last_step = ""
    last_status = ""
    last_message = ""
    last_push_at = 0.0
    loop_started_at = time.monotonic()
    use_message_update = bool(progress_message_id)
    update_degraded_notified = False

    poll_seconds = int(settings.FEISHU_ANALYSIS_PROGRESS_POLL_SECONDS)
    min_push_interval = int(settings.FEISHU_ANALYSIS_PROGRESS_MIN_PUSH_INTERVAL_SECONDS)
    min_progress_increment = int(settings.FEISHU_ANALYSIS_PROGRESS_MIN_INCREMENT)
    heartbeat_seconds = int(settings.FEISHU_ANALYSIS_PROGRESS_HEARTBEAT_SECONDS)
    max_track_minutes = int(settings.FEISHU_ANALYSIS_PROGRESS_MAX_TRACK_MINUTES)
    max_track_seconds = max_track_minutes * 60
    try:
        while True:
            now = time.monotonic()
            if now - loop_started_at > max_track_seconds:
                await feishu_push_service.send_text(
                    chat_id,
                    f"分析任务跟踪超时（>{max_track_minutes}分钟），任务仍在后台执行。\n"
                    f"任务ID: {task_id}\n"
                    "你可以稍后使用 /report <analysis_id> 或重新发起分析。",
                )
                break

            status = await service.get_task_status(task_id)
            if not status:
                await asyncio.sleep(poll_seconds)
                continue

            st = str(status.get("status") or "")
            progress = int(status.get("progress") or 0)
            step = str(status.get("current_step_name") or status.get("current_step") or "")
            message = str(status.get("message") or "")

            status_changed = st != last_status
            step_changed = step != last_step
            progress_jump = progress - last_progress >= min_progress_increment
            message_changed = message != last_message
            reached_final = st in {"completed", "failed", "cancelled"}
            heartbeat_due = (now - last_push_at) >= heartbeat_seconds
            min_interval_ok = (now - last_push_at) >= min_push_interval

            should_push = reached_final or (
                min_interval_ok and (status_changed or step_changed or progress_jump or (heartbeat_due and message_changed))
            )
            if should_push:
                title, lines, template = _build_analysis_progress_card(
                    stock_name=display_name,
                    symbol=symbol,
                    task_id=task_id,
                    status=st,
                    progress=progress,
                    step=step,
                    message=message,
                )

                if use_message_update and progress_message_id:
                    upd = await feishu_push_service.update_card_message(
                        message_id=progress_message_id,
                        title=title,
                        lines=lines,
                        template=template,
                    )
                    if upd.get("success"):
                        pass
                    else:
                        use_message_update = False
                        await feishu_push_service.send_card(chat_id, title=title, lines=lines, template=template)
                        if not update_degraded_notified:
                            await feishu_push_service.send_text(
                                chat_id,
                                "当前会话消息更新失败，已切换为阶段性卡片消息推送。",
                            )
                            update_degraded_notified = True
                else:
                    await feishu_push_service.send_card(chat_id, title=title, lines=lines, template=template)

                last_progress = progress
                last_step = step
                last_status = st
                last_message = message
                last_push_at = now

            if reached_final:
                break
            await asyncio.sleep(poll_seconds)
    except Exception as exc:
        await feishu_push_service.send_text(chat_id, f"进度跟踪异常: {exc}")


async def _run_stock_analysis_and_push_report(
    user_id: str,
    chat_id: str,
    conversation_id: str,
    symbol: str,
    stock_name: str,
    market_type: str,
    research_depth: str,
    progress_message_id: Optional[str],
) -> None:
    service = get_simple_analysis_service()
    request = SingleAnalysisRequest(
        symbol=symbol,
        parameters=AnalysisParameters(
            market_type=market_type,
            research_depth=research_depth,
            selected_analysts=["market", "fundamentals", "news", "social"],
        ),
    )
    create_res = await service.create_analysis_task(user_id, request)
    task_id = str(create_res.get("task_id"))

    # 先把任务ID写入进度消息
    init_title, init_lines, init_template = _build_analysis_progress_card(
        stock_name=stock_name,
        symbol=symbol,
        task_id=task_id,
        status="pending",
        progress=0,
        step="📋 准备阶段",
        message=f"正在执行 {stock_name} ({symbol}) 个股分析（{research_depth}），稍后将持续更新进度...",
    )
    if progress_message_id:
        await feishu_push_service.update_card_message(
            message_id=progress_message_id,
            title=init_title,
            lines=init_lines,
            template=init_template,
        )

    exec_task = asyncio.create_task(service.execute_analysis_background(task_id, user_id, request))
    progress_task = asyncio.create_task(
        _push_analysis_progress_loop(
            chat_id=chat_id,
            progress_message_id=progress_message_id,
            task_id=task_id,
            symbol=symbol,
            stock_name=stock_name,
        )
    )
    await exec_task
    await progress_task

    status = await service.get_task_status(task_id) or {}
    if str(status.get("status")) == "completed":
        result = status.get("result_data") or {}
        summary = _select_report_text(
            summary=str(result.get("summary") or result.get("recommendation") or "").strip(),
            reports=result.get("reports") or {},
            max_chars=REPORT_TEXT_MAX_CHARS,
        )
        if not summary:
            summary = "分析已完成，但摘要为空。可稍后使用 /report <task_id> 查看。"

        # 自动切换报告上下文，便于继续问答
        try:
            ctx = await _resolve_report_context(task_id)
            await _upsert_conversation(
                conversation_id=conversation_id,
                user_id=user_id,
                chat_id=chat_id,
                set_context=ctx,
            )
        except Exception:
            pass

        try:
            ctx = await _resolve_report_context(task_id)
            await _send_report_selector_card_if_needed(chat_id, ctx)
            await _send_report_module_message(
                chat_id=chat_id,
                report_ctx=ctx,
                intro_prefix=f"个股分析完成：{stock_name} ({symbol})\n任务ID: {task_id}",
            )
        except Exception:
            final_text = (
                f"个股分析完成：{stock_name} ({symbol})\n"
                f"任务ID: {task_id}\n\n"
                f"{summary[:REPORT_TEXT_MAX_CHARS]}\n\n"
                f"如需加载上下文继续追问：/report {task_id}"
            )
            await feishu_push_service.send_text(chat_id, final_text)
    else:
        err = str(status.get("error_message") or status.get("message") or "任务失败")
        await feishu_push_service.send_text(chat_id, f"个股分析失败：{stock_name} ({symbol})\n任务ID: {task_id}\n原因: {err}")


async def _start_or_reuse_stock_analysis(
    *,
    user_id: str,
    chat_id: Optional[str],
    conversation_id: str,
    symbol: str,
    stock_name: str,
    market_type: str,
    research_depth: str,
    force_refresh: bool = False,
) -> Tuple[str, bool]:
    # 1) 默认复用当日同标的已完成报告
    if not force_refresh:
        # 1.1) 若已有运行中的任务，直接提示等待
        running_task = await _find_running_task_for_symbol(user_id, symbol)
        if running_task:
            running_task_id = str(running_task.get("task_id") or "")
            msg = (
                f"该股票分析任务正在执行中：{stock_name} ({symbol})\n"
                f"任务ID: {running_task_id}\n"
                "请稍候，完成后会自动推送结果。"
            )
            if chat_id:
                await feishu_push_service.send_text(chat_id, msg)
            return msg, bool(chat_id)

        # 1.2) 复用当日已完成报告
        reusable = await _find_today_reusable_report(symbol)
        if reusable:
            report_id = str(reusable.get("analysis_id") or reusable.get("task_id") or reusable.get("_id"))
            ctx = await _resolve_report_context(report_id)
            await _upsert_conversation(
                conversation_id=conversation_id,
                user_id=user_id,
                chat_id=chat_id,
                set_context=ctx,
            )
            if chat_id:
                await _send_report_selector_card_if_needed(chat_id, ctx)
                await _send_report_module_message(
                    chat_id=chat_id,
                    report_ctx=ctx,
                    intro_prefix=(
                        "检测到今日已有该股票分析报告，已直接复用。"
                        "如需重算请使用：/report 股票代码 深度 force"
                    ),
                )
            return f"已复用今日报告: {stock_name} ({symbol})", bool(chat_id)

    # 3) 创建新任务
    start_title, start_lines, start_template = _build_analysis_progress_card(
        stock_name=stock_name,
        symbol=symbol,
        task_id="-",
        status="pending",
        progress=0,
        step="📋 准备阶段",
        message=f"正在执行 {stock_name} ({symbol}) 的个股分析（{research_depth}），稍后将持续更新进度...",
    )
    sent = (
        await feishu_push_service.send_card(
            chat_id=chat_id,
            title=start_title,
            lines=start_lines,
            template=start_template,
        )
        if chat_id
        else {"success": False}
    )
    progress_message_id = _extract_message_id_from_send_result(sent)
    reply_already_sent = bool(sent.get("success"))

    if chat_id:
        async def _delayed_analysis_start() -> None:
            await asyncio.sleep(0.2)
            await _run_stock_analysis_and_push_report(
                user_id=user_id,
                chat_id=chat_id,
                conversation_id=conversation_id,
                symbol=symbol,
                stock_name=stock_name,
                market_type=market_type,
                research_depth=research_depth,
                progress_message_id=progress_message_id,
            )

        asyncio.create_task(_delayed_analysis_start())
    return f"已启动个股分析任务: {stock_name} ({symbol})", reply_already_sent


async def handle_feishu_event_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = _extract_text(payload)
    if not text:
        return ok({"ignored": True}, "empty message")

    parts = text.split()
    command = parts[0].strip().lower()
    args = parts[1:]
    user_id = _extract_user_id(payload)
    chat_id = _extract_chat_id(payload)
    msg_meta = _extract_message_meta(payload)
    reply_already_sent = False
    if not await _mark_inbound_message_once(msg_meta.get("message_id", "")):
        return ok({"ignored": True}, "duplicate message")
    conv_id = _conversation_id(user_id, chat_id, msg_meta)

    # 确认/取消命令（全局优先）
    if command in {"/confirm", "/cancel"}:
        if not args:
            reply = "用法: /confirm <编号> 或 /cancel <编号>"
            if chat_id:
                await feishu_push_service.send_text(chat_id, reply)
            return ok({"reply": reply})

        action_id = args[0].strip()
        pending = await _load_pending_action(user_id, action_id)
        if not pending:
            reply = f"未找到待确认操作或已过期: {action_id}"
            if chat_id:
                await feishu_push_service.send_text(chat_id, reply)
            return ok({"reply": reply})

        if command == "/cancel":
            await _finalize_pending_action(action_id, "cancelled")
            reply = f"已取消操作: {action_id}"
            await _upsert_conversation(
                conversation_id=conv_id,
                user_id=user_id,
                chat_id=chat_id,
                push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
            )
            if chat_id:
                await feishu_push_service.send_text(chat_id, reply)
            return ok({"reply": reply})

        # /confirm
        try:
            reply = await _execute_action(user_id, pending.get("action", ""), pending.get("params", {}) or {})
            await _finalize_pending_action(action_id, "executed")
        except Exception as exc:
            await _finalize_pending_action(action_id, "failed")
            reply = f"执行失败: {exc}"
        if chat_id:
            await feishu_push_service.send_text(chat_id, reply)
        await _upsert_conversation(
            conversation_id=conv_id,
            user_id=user_id,
            chat_id=chat_id,
            push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
        )
        return ok({"reply": reply})

    # AI代理统一入口：命令/自然语言都先让AI路由，再执行动作或返回对话。
    await _upsert_conversation(
        conversation_id=conv_id,
        user_id=user_id,
        chat_id=chat_id,
        push_turn={"role": "user", "content": text, "ts": datetime.utcnow().isoformat()},
    )
    # 连续线程对话默认关闭 LLM 路由，减少每轮额外 token 消耗
    enable_llm_route = not conv_id.startswith("thread:")
    intent = feishu_nl_agent_service.parse_user_intent(text, enable_llm_route=enable_llm_route)
    mode = intent.get("mode")
    if mode == "action":
        action = intent.get("action")
        params = intent.get("params", {}) or {}
        instruction = str(intent.get("instruction") or "").strip()
        if chat_id and instruction:
            await feishu_push_service.send_text(chat_id, f"AI决策指令: `{instruction}`")
        try:
            if str(action) == "run_stock_analysis":
                stock_keyword = str(params.get("stock_name_or_code") or params.get("stock") or text).strip()
                market_hint = params.get("market")
                research_depth = _normalize_research_depth(str(params.get("research_depth") or ""), default="全面")
                force_refresh = bool(params.get("force_refresh")) or _is_force_refresh_token(str(params.get("force") or "")) or _is_force_refresh_token(text)
                symbol, stock_name, market_type = await _resolve_stock_for_analysis(
                    stock_keyword,
                    market_hint,
                    user_id=user_id,
                )
                reply, reply_already_sent = await _start_or_reuse_stock_analysis(
                    user_id=user_id,
                    chat_id=chat_id,
                    conversation_id=conv_id,
                    symbol=symbol,
                    stock_name=stock_name,
                    market_type=market_type,
                    research_depth=research_depth,
                    force_refresh=force_refresh,
                )

                # 回执优先：避免等待后续网络/IO，先返回 webhook 响应。
                if chat_id and not reply_already_sent:
                    asyncio.create_task(feishu_push_service.send_text(chat_id, reply))
                asyncio.create_task(
                    _upsert_conversation(
                        conversation_id=conv_id,
                        user_id=user_id,
                        chat_id=chat_id,
                        push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
                    )
                )
                return ok({"reply": reply, "mode": "action", "instruction": instruction})
            # 读类动作立即执行；写类动作需要确认
            elif str(action) in {
                "help",
                "daily_brief",
                "daily_picks",
                "rebalance_suggestions",
                "positions_snapshot",
                "list_watchlist",
                "set_report_context",
                "run_stock_screening",
            }:
                if str(action) == "set_report_context":
                    ctx = await _resolve_report_context(str(params.get("report_keyword") or ""))
                    await _upsert_conversation(
                        conversation_id=conv_id,
                        user_id=user_id,
                        chat_id=chat_id,
                        set_context=ctx,
                    )
                    if chat_id:
                        await _send_report_selector_card_if_needed(chat_id, ctx)
                        await _send_report_module_message(chat_id=chat_id, report_ctx=ctx)
                        reply_already_sent = True
                    reply = f"已切换到报告上下文：{ctx.get('stock_name') or ctx.get('stock_symbol')}（{ctx.get('report_id')}）"
                elif str(action) == "run_stock_screening":
                    strategy, limit, markets = _parse_screen_args([
                        str(params.get("strategy") or ""),
                        str(params.get("limit") or ""),
                        ",".join(params.get("markets") or []) if isinstance(params.get("markets"), list) else str(params.get("markets") or ""),
                    ])
                    reply, reply_already_sent = await _run_stock_screening_with_card(
                        user_id=user_id,
                        chat_id=chat_id,
                        strategy=strategy,
                        limit=limit,
                        markets=markets,
                    )
                else:
                    reply = await _execute_action(user_id, str(action), params)
            else:
                action_id = await _create_pending_action(
                    user_id=user_id,
                    chat_id=chat_id,
                    action=str(action),
                    params=params,
                )
                action_name = str(action)
                reply = (
                    f"识别到待执行操作: {action_name}\n"
                    f"参数: {json.dumps(params, ensure_ascii=False)}\n"
                    f"请确认: /confirm {action_id}\n"
                    f"取消: /cancel {action_id}\n"
                    "（10分钟内有效）"
                )
        except Exception as exc:
            reply = f"执行失败: {exc}"

        if chat_id and not reply_already_sent:
            await feishu_push_service.send_text(chat_id, reply)
        await _upsert_conversation(
            conversation_id=conv_id,
            user_id=user_id,
            chat_id=chat_id,
            push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
        )
        return ok({"reply": reply, "mode": "action", "instruction": instruction})

    if not command.startswith("/"):
        conv = await _get_conversation(conv_id)
        use_thread_context = conv_id.startswith("thread:")
        chat_history = (conv.get("history") or []) if use_thread_context else []
        prev_response_id = (
            str(((conv.get("llm_chat_state") or {}).get("previous_response_id") or "")).strip() or None
        ) if use_thread_context else None
        llm_state_update: Dict[str, Any] = {}
        if intent.get("reply"):
            reply = intent.get("reply")
        else:
            reply, llm_state_update = feishu_nl_agent_service.chat_reply_with_state(
                text=text,
                context=conv.get("context"),
                history=chat_history,
                previous_response_id=prev_response_id,
            )
        if chat_id and not reply_already_sent:
            await feishu_push_service.send_text(chat_id, reply)
        await _upsert_conversation(
            conversation_id=conv_id,
            user_id=user_id,
            chat_id=chat_id,
            push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
            set_fields=(
                {"llm_chat_state": llm_state_update}
                if llm_state_update
                else ({"llm_chat_state": None} if not use_thread_context else None)
            ),
        )
        return ok({"reply": reply, "mode": mode or "chat"})

    strict_ai_routing = str(os.getenv("FEISHU_AGENT_STRICT_ROUTING", "true")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # 显式斜杠命令总是允许走命令分支，避免对 LLM 路由产生硬依赖
    if strict_ai_routing and not command.startswith("/"):
        conv = await _get_conversation(conv_id)
        use_thread_context = conv_id.startswith("thread:")
        chat_history = (conv.get("history") or []) if use_thread_context else []
        prev_response_id = (
            str(((conv.get("llm_chat_state") or {}).get("previous_response_id") or "")).strip() or None
        ) if use_thread_context else None
        llm_state_update: Dict[str, Any] = {}
        if intent.get("reply"):
            reply = intent.get("reply")
        else:
            reply, llm_state_update = feishu_nl_agent_service.chat_reply_with_state(
                text=text,
                context=conv.get("context"),
                history=chat_history,
                previous_response_id=prev_response_id,
            )
        if chat_id and not reply_already_sent:
            await feishu_push_service.send_text(chat_id, reply)
        await _upsert_conversation(
            conversation_id=conv_id,
            user_id=user_id,
            chat_id=chat_id,
            push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
            set_fields=(
                {"llm_chat_state": llm_state_update}
                if llm_state_update
                else ({"llm_chat_state": None} if not use_thread_context else None)
            ),
        )
        return ok({"reply": reply, "mode": "chat", "strict_ai_routing": True})

    if command in {"/help", "help"}:
        reply = _help_text()
        if chat_id:
            await feishu_push_service.send_text(chat_id, reply)
        await _upsert_conversation(
            conversation_id=conv_id,
            user_id=user_id,
            chat_id=chat_id,
            push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
        )
        return ok({"reply": reply})

    if command not in {"/daily", "/pick", "/rebalance", "/position", "/wl", "/report", "/screen"}:
        reply = "支持命令: /help /daily /pick /rebalance /position /report /screen /wl /confirm /cancel"
        if chat_id:
            await feishu_push_service.send_text(chat_id, reply)
        return ok({"reply": reply})

    if command == "/daily":
        brief = await advisor_service.get_daily_brief(user_id)
        reply = brief.summary
    elif command == "/pick":
        picks = await advisor_service.generate_daily_picks(user_id)
        lines = [f"{x.symbol} {x.action} 分数:{x.score_total}" for x in picks[:10]]
        reply = "\n".join(lines) if lines else "暂无候选"
    elif command == "/rebalance":
        actions = await advisor_service.generate_rebalance(user_id)
        lines = [f"{x.symbol} {x.action} {x.current_weight:.0%}->{x.target_weight:.0%}" for x in actions[:10]]
        reply = "\n".join(lines) if lines else "暂无调仓建议"
    elif command == "/position":
        positions = await portfolio_service.get_unified_positions(user_id)
        lines = [f"{x.market} {x.symbol} 持仓:{x.quantity}" for x in positions[:20]]
        reply = "\n".join(lines) if lines else "暂无持仓"
    elif command == "/report":
        if not args:
            reply = "用法: /report 股票代码|股票名称 [快速|基础|标准|深度|全面] [force] 或 /report analysis_id [模块]"
        else:
            target = str(args[0] or "").strip()
            depth, force_refresh, requested_section = _parse_report_args(args[1:], default_depth="全面")

            # 明确使用报告ID时，保留“加载上下文”能力
            if _looks_like_report_id(target):
                try:
                    ctx = await _resolve_report_context(target, section=requested_section)
                    await _upsert_conversation(
                        conversation_id=conv_id,
                        user_id=user_id,
                        chat_id=chat_id,
                        set_context=ctx,
                    )
                    if chat_id:
                        await _send_report_selector_card_if_needed(chat_id, ctx)
                        await _send_report_module_message(chat_id=chat_id, report_ctx=ctx)
                        reply_already_sent = True
                    reply = f"已加载报告上下文: {ctx.get('stock_name') or ctx.get('stock_symbol')} ({ctx.get('report_id')})"
                except Exception as exc:
                    reply = f"加载报告上下文失败: {exc}"
            else:
                try:
                    symbol, stock_name, market_type = await _resolve_stock_for_analysis(
                        target,
                        user_id=user_id,
                    )
                    reply, reply_already_sent = await _start_or_reuse_stock_analysis(
                        user_id=user_id,
                        chat_id=chat_id,
                        conversation_id=conv_id,
                        symbol=symbol,
                        stock_name=stock_name,
                        market_type=market_type,
                        research_depth=depth,
                        force_refresh=force_refresh,
                    )
                except Exception as exc:
                    reply = f"启动个股分析失败: {exc}"
    elif command == "/screen":
        strategy, limit, markets = _parse_screen_args(args)
        try:
            reply, reply_already_sent = await _run_stock_screening_with_card(
                user_id=user_id,
                chat_id=chat_id,
                strategy=strategy,
                limit=limit,
                markets=markets,
            )
        except Exception as exc:
            reply = f"选股执行失败: {exc}"
    else:
        # /wl add 贵州茅台 [数量] [成本]
        # /wl del 贵州茅台|600519
        # /wl list
        if not args:
            reply = "用法: /wl add 贵州茅台 [数量] [成本] | /wl del 600519 | /wl list"
        elif args[0].lower() == "list":
            positions = get_a_share_sqlite_service().list_positions()
            if not positions:
                reply = "A股持仓列表为空"
            else:
                lines = [
                    f"{p.symbol} {p.name} 数量:{p.quantity:g} 成本:{(p.avg_cost if p.avg_cost is not None else '-')}"
                    for p in positions[:30]
                ]
                reply = "\n".join(lines)
        elif args[0].lower() in {"del", "remove", "rm"}:
            if len(args) < 2:
                reply = "用法: /wl del 股票代码或名称"
            else:
                try:
                    symbol, name = await _resolve_a_share_symbol(args[1])
                    ok_del = get_a_share_sqlite_service().delete_position(symbol)
                    reply = f"已删除: {symbol} {name}" if ok_del else f"未找到持仓: {symbol} {name}"
                except Exception as exc:
                    reply = f"删除失败: {exc}"
        elif args[0].lower() == "add":
            if len(args) < 2:
                reply = "用法: /wl add 贵州茅台 [数量] [成本]"
            else:
                try:
                    symbol, name = await _resolve_a_share_symbol(args[1])
                    qty = 100.0
                    if len(args) >= 3:
                        qty = float(args[2])
                    if qty <= 0:
                        raise ValueError("数量必须大于0")

                    if len(args) >= 4:
                        avg_cost = float(args[3])
                    else:
                        avg_cost = await _latest_a_share_price(symbol)

                    market_value = avg_cost * qty if avg_cost is not None else None
                    get_a_share_sqlite_service().upsert_position(
                        symbol=symbol,
                        name=name,
                        quantity=qty,
                        avg_cost=avg_cost,
                        market_value=market_value,
                    )
                    cost_text = f"{avg_cost:.3f}" if avg_cost is not None else "-"
                    reply = f"已添加A股持仓: {symbol} {name} 数量:{qty:g} 成本:{cost_text}"
                except Exception as exc:
                    reply = f"添加失败: {exc}"
        else:
            reply = "用法: /wl add 贵州茅台 [数量] [成本] | /wl del 600519 | /wl list"

    if chat_id and not reply_already_sent:
        await feishu_push_service.send_text(chat_id, reply)
    await _upsert_conversation(
        conversation_id=conv_id,
        user_id=user_id,
        chat_id=chat_id,
        push_turn={"role": "assistant", "content": reply, "ts": datetime.utcnow().isoformat()},
    )

    return ok({"reply": reply})


@router.post("/webhook")
async def webhook(
    request: Request,
    x_lark_request_timestamp: Optional[str] = Header(default=None),
    x_lark_signature: Optional[str] = Header(default=None),
):
    payload = await request.json()

    # URL验证
    if payload.get("type") == "url_verification" and payload.get("challenge"):
        return {"challenge": payload["challenge"]}

    # 简化校验：若配置了verification token，则要求匹配
    token = payload.get("token")
    if not token and isinstance(payload.get("event"), dict):
        token = payload.get("event", {}).get("token")
    if settings.FEISHU_BOT_VERIFICATION_TOKEN and token != settings.FEISHU_BOT_VERIFICATION_TOKEN:
        raise HTTPException(status_code=401, detail="invalid verification token")

    # 卡片按钮回调
    if _extract_card_action_value(payload):
        return await handle_feishu_card_action_payload(payload)
    return await handle_feishu_event_payload(payload)
