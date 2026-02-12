from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict


class FeishuNLAgentService:
    """
    自然语言代理服务：
    1) 先由 LLM 路由到已有命令/动作
    2) 未命中动作时走自然语言分析对话
    """

    API_CATALOG = [
        {
            "action": "help",
            "api": "internal:/help",
            "params": [],
            "desc": "查看机器人支持的全部命令",
        },
        {
            "action": "daily_brief",
            "api": "internal:/daily",
            "params": [],
            "desc": "查看今日投研摘要",
        },
        {
            "action": "daily_picks",
            "api": "internal:/pick",
            "params": [],
            "desc": "查看候选股票建议",
        },
        {
            "action": "rebalance_suggestions",
            "api": "internal:/rebalance",
            "params": [],
            "desc": "查看调仓建议",
        },
        {
            "action": "positions_snapshot",
            "api": "internal:/position",
            "params": [],
            "desc": "查看统一持仓",
        },
        {
            "action": "sync_longport_positions",
            "api": "POST /api/paper/sync/longport/positions",
            "params": ["symbols?", "replace_existing_longport_positions?", "sync_cash?"],
            "desc": "从长桥同步持仓到纸上账户",
        },
        {
            "action": "feedback_metrics",
            "api": "internal:/feedback [YYYY-MM]",
            "params": ["month_key?"],
            "desc": "查看持续评估统计（胜率/回撤/归因）",
        },
        {
            "action": "list_history_reports",
            "api": "internal:/history [股票代码|股票名称|analysis_id] [数量]",
            "params": ["report_keyword?", "limit?"],
            "desc": "查看历史报告列表",
        },
        {
            "action": "run_stock_analysis",
            "api": "internal:/report <代码|名称|analysis_id> (扩展为触发个股分析并回传报告)",
            "params": ["stock_name_or_code", "market?", "research_depth?", "force_refresh?"],
            "desc": "发起个股分析任务，返回进度和最终报告",
        },
        {
            "action": "run_positions_batch_analysis",
            "api": "internal:/reportpos [数量] [A股,港股,美股] [快速|基础|标准|深度|全面] [force]",
            "params": ["limit?", "markets?", "research_depth?", "force_refresh?"],
            "desc": "基于当前持仓批量发起个股分析",
        },
        {
            "action": "run_stock_screening",
            "api": "internal:/screen [价值|质量|动量] [数量] [A股,港股,美股]",
            "params": ["strategy?", "limit?", "markets?"],
            "desc": "触发多市场选股，返回各市场TopN结果",
        },
        {
            "action": "add_watchlist",
            "api": "POST /api/favorites",
            "params": ["stock_name_or_code"],
            "desc": "把股票加入自选股",
        },
        {
            "action": "remove_watchlist",
            "api": "DELETE /api/favorites/{stock_code}",
            "params": ["stock_name_or_code"],
            "desc": "把股票从自选股移除",
        },
        {
            "action": "list_watchlist",
            "api": "GET /api/favorites",
            "params": [],
            "desc": "查看自选股",
        },
        {
            "action": "add_a_share_position",
            "api": "POST /api/advisor/a-share/import-csv (内部改为upsert)",
            "params": ["stock_name_or_code", "quantity?", "avg_cost?"],
            "desc": "添加A股持仓",
        },
    ]
    COMMAND_CATALOG = [
        "/help",
        "/daily",
        "/pick",
        "/rebalance",
        "/position",
        "/syncpos [symbols可选]",
        "/feedback [YYYY-MM]",
        "/history [股票代码|股票名称|analysis_id] [数量]",
        "/report <代码|名称> [快速|基础|标准|深度|全面] [force]",
        "/reportpos [数量] [A股,港股,美股] [快速|基础|标准|深度|全面] [force]",
        "/report <analysis_id> [模块]",
        "/reportid <analysis_id> [模块]",
        "/screen [价值|质量|动量] [数量] [A股,港股,美股]",
        "/wl list",
        "/wl add <股票> [数量] [成本]",
        "/wl del <股票代码|名称>",
        "/confirm <编号>",
        "/cancel <编号>",
    ]
    ROUTABLE_ACTIONS = [
        "help",
        "daily_brief",
        "daily_picks",
        "rebalance_suggestions",
        "positions_snapshot",
        "sync_longport_positions",
        "feedback_metrics",
        "list_history_reports",
        "run_stock_analysis",
        "run_positions_batch_analysis",
        "run_stock_screening",
        "add_watchlist",
        "remove_watchlist",
        "list_watchlist",
        "add_a_share_position",
        "set_report_context",
    ]
    ACTION_PARAM_HINTS: Dict[str, Dict[str, list[str]]] = {
        "help": {"required": [], "optional": []},
        "daily_brief": {"required": [], "optional": []},
        "daily_picks": {"required": [], "optional": []},
        "rebalance_suggestions": {"required": [], "optional": []},
        "positions_snapshot": {"required": [], "optional": []},
        "sync_longport_positions": {
            "required": [],
            "optional": ["symbols", "replace_existing_longport_positions", "sync_cash"],
        },
        "feedback_metrics": {"required": [], "optional": ["month_key"]},
        "list_history_reports": {"required": [], "optional": ["report_keyword", "limit"]},
        "run_stock_analysis": {"required": ["stock_name_or_code"], "optional": ["market", "research_depth", "force_refresh"]},
        "run_positions_batch_analysis": {"required": [], "optional": ["limit", "markets", "research_depth", "force_refresh"]},
        "run_stock_screening": {"required": [], "optional": ["strategy", "limit", "markets"]},
        "add_watchlist": {"required": ["stock_name_or_code"], "optional": []},
        "remove_watchlist": {"required": ["stock_name_or_code"], "optional": []},
        "list_watchlist": {"required": [], "optional": []},
        "add_a_share_position": {"required": ["stock_name_or_code"], "optional": ["quantity", "avg_cost"]},
        "set_report_context": {"required": ["report_keyword"], "optional": []},
    }
    REDACTED_TEXT = "[REDACTED]"
    SENSITIVE_FIELD_RE = re.compile(
        r"(api[_-]?key|token|secret|password|passwd|authorization|cookie|private[_-]?key|client[_-]?secret)",
        flags=re.IGNORECASE,
    )
    SENSITIVE_VALUE_PATTERNS = [
        re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{12,}\b"),
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}\b"),
        re.compile(r"(?i)\b(?:xox[baprs]-[A-Za-z0-9-]{10,})\b"),
        re.compile(r"(?i)\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ]
    CAPABILITY_GUIDE = {
        "project": "TradingAgents-CN 飞书投研助手（多市场选股 + 个股分析 + 自选管理）",
        "markets": ["A股", "港股", "美股"],
        "features": [
            {
                "name": "选股",
                "usage": "/screen [价值|质量|动量] [数量] [A股,港股,美股]",
                "details": [
                    "价值：偏低估（低PE/低PB，且关注ROE）",
                    "质量：偏盈利能力（高ROE，兼顾估值约束）",
                    "动量：偏短期强势（涨跌幅/活跃度）",
                    "支持按市场分栏卡片展示，可一键加入自选/发起分析",
                ],
            },
            {
                "name": "个股分析",
                "usage": "/report <代码|名称> [快速|基础|标准|深度|全面] [force]",
                "details": [
                    "默认可复用当日已完成报告；加 force 强制重算",
                    "支持报告上下文连续问答与模块切换",
                ],
            },
            {
                "name": "报告上下文",
                "usage": "/report <analysis_id> [模块]",
                "details": [
                    "加载历史报告并切换模块（如市场技术、基本面、新闻、情绪）",
                ],
            },
            {
                "name": "自选股管理",
                "usage": "/wl list | /wl add <股票> [数量] [成本] | /wl del <股票代码|名称>",
                "details": [
                    "支持从选股卡片直接加入自选",
                    "写操作支持确认机制：/confirm /cancel",
                ],
            },
            {
                "name": "投研摘要与调仓",
                "usage": "/daily | /pick | /rebalance | /position | /syncpos",
                "details": [
                    "提供今日摘要、候选建议、调仓建议、持仓快照",
                    "支持从长桥同步持仓到纸上账户（默认同步现金并替换旧 longport 持仓）",
                ],
            },
        ],
        "security": [
            "不会在回答中返回任何密钥、Token、密码、Cookie、私钥",
            "涉及配置说明仅提供字段名和用途，不展示真实值",
        ],
    }
    ROUTE_SCHEMA = {"mode": "action|chat", "action": "action_name", "params": {}, "reply": "when mode=chat"}

    @staticmethod
    def _is_placeholder_key(value: str) -> bool:
        v = (value or "").strip().lower()
        if not v:
            return True
        placeholders = {
            "your_openai_api_key_here",
            "your-api-key",
            "your_api_key_here",
        }
        if v in placeholders:
            return True
        if "your" in v and "key" in v and ("here" in v or "api" in v):
            return True
        return False

    @staticmethod
    def _resolve_llm_credentials() -> tuple[str, str | None]:
        openai_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
        volc_key = (os.getenv("VOLCENGINE_API_KEY", "") or "").strip()

        if FeishuNLAgentService._is_placeholder_key(openai_key):
            openai_key = ""
        if FeishuNLAgentService._is_placeholder_key(volc_key):
            volc_key = ""

        api_key = openai_key or volc_key
        base_url = (
            os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or os.getenv("VOLCENGINE_BASE_URL")
        )
        # 使用火山 key 且未显式配置 base_url 时，自动回落到火山 OpenAI 兼容网关
        if not base_url and api_key and api_key == volc_key:
            base_url = "https://ark.cn-beijing.volces.com/api/v3"
        return api_key, base_url

    @classmethod
    def _sanitize_prompt_text(cls, text: str) -> str:
        out = str(text or "")
        for pattern in cls.SENSITIVE_VALUE_PATTERNS:
            out = pattern.sub(cls.REDACTED_TEXT, out)
        out = re.sub(
            r"(?im)\b([A-Z][A-Z0-9_]{1,64}(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|ACCESS[_-]?TOKEN|APP[_-]?SECRET|CLIENT[_-]?SECRET))\s*=\s*[^\s]+",
            lambda m: f"{m.group(1)}={cls.REDACTED_TEXT}",
            out,
        )
        out = re.sub(
            r"(?im)\b(authorization|cookie)\s*[:=]\s*[^\n]+",
            lambda m: f"{m.group(1)}: {cls.REDACTED_TEXT}",
            out,
        )
        return out

    @classmethod
    def _sanitize_for_prompt(cls, value: Any, parent_key: str = "") -> Any:
        if isinstance(value, dict):
            safe: Dict[str, Any] = {}
            for k, v in value.items():
                key = str(k)
                if cls.SENSITIVE_FIELD_RE.search(key):
                    safe[key] = cls.REDACTED_TEXT
                else:
                    safe[key] = cls._sanitize_for_prompt(v, parent_key=key)
            return safe
        if isinstance(value, list):
            return [cls._sanitize_for_prompt(x, parent_key=parent_key) for x in value]
        if isinstance(value, tuple):
            return [cls._sanitize_for_prompt(x, parent_key=parent_key) for x in value]
        if isinstance(value, str):
            if cls.SENSITIVE_FIELD_RE.search(parent_key):
                return cls.REDACTED_TEXT
            return cls._sanitize_prompt_text(value)
        return value

    @classmethod
    def _build_capability_knowledge_text(cls) -> str:
        guide = cls.CAPABILITY_GUIDE
        lines = [
            f"项目: {guide.get('project', '')}",
            f"支持市场: {', '.join(guide.get('markets', []))}",
            "核心功能:",
        ]
        for feature in guide.get("features", []):
            if not isinstance(feature, dict):
                continue
            name = str(feature.get("name") or "").strip()
            usage = str(feature.get("usage") or "").strip()
            lines.append(f"- {name}: {usage}")
            for detail in feature.get("details", []):
                lines.append(f"  - {detail}")
        lines.append("安全约束:")
        for sec in guide.get("security", []):
            lines.append(f"- {sec}")
        return "\n".join(lines).strip()

    @classmethod
    def _build_capability_reply(cls) -> str:
        guide = cls.CAPABILITY_GUIDE
        lines = [
            f"项目能力概览：{guide.get('project', '')}",
            f"支持市场：{', '.join(guide.get('markets', []))}",
            "",
            "可用功能与用法：",
        ]
        for feature in guide.get("features", []):
            if not isinstance(feature, dict):
                continue
            name = str(feature.get("name") or "").strip()
            usage = str(feature.get("usage") or "").strip()
            lines.append(f"1. {name}: `{usage}`")
            for detail in feature.get("details", []):
                lines.append(f"   {detail}")
        lines.extend(
            [
                "",
                "安全说明：",
                "1. 我不会返回任何 API Key/Token/Secret/Password/Cookie 等敏感信息。",
                "2. 若你问配置，我只会解释字段用途，不会展示真实值。",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _is_capability_question(text: str) -> bool:
        t = str(text or "").strip()
        if not t or t.startswith("/"):
            return False
        patterns = [
            r"(支持|可以|都能).*(功能|命令|能力|策略)",
            r"(功能|命令|能力|策略).*(有哪些|是什么|说明|介绍|区别)",
            r"(怎么用|使用说明|使用方法).*(选股|报告|自选|机器人|项目)",
            r"(选股).*(策略|方法|区别|说明)",
        ]
        return any(re.search(p, t, flags=re.IGNORECASE) for p in patterns)

    def parse_user_intent(self, text: str, enable_llm_route: bool = True) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"mode": "chat", "reply": "你可以告诉我你的投资操作需求。"}
        if self._is_history_report_query(text):
            params: Dict[str, Any] = {}
            history_kw = self._extract_history_report_keyword(text)
            if history_kw:
                params["report_keyword"] = history_kw
            return {
                "mode": "action",
                "action": "list_history_reports",
                "params": params,
                "instruction": self._action_to_instruction("list_history_reports", params),
            }

        # AI 驱动优先（默认开启），规则匹配仅作为可选兜底
        ai_driven_routing = self._is_true(os.getenv("FEISHU_AGENT_AI_DRIVEN_ROUTING", "true"), default=True)
        rule_fallback = self._is_true(os.getenv("FEISHU_AGENT_RULE_FALLBACK", "false"), default=False)

        if enable_llm_route and ai_driven_routing:
            llm_result = self._llm_route(text)
            if llm_result is not None:
                normalized = self._normalize_llm_intent(text, llm_result)
                if normalized is not None:
                    if normalized.get("mode") == "action":
                        action = str(normalized.get("action") or "")
                        params = normalized.get("params") if isinstance(normalized.get("params"), dict) else {}
                        normalized["instruction"] = self._action_to_instruction(action, params)
                    return normalized
            if not rule_fallback:
                return {"mode": "chat"}

        # 规则路由（非 AI 驱动模式或显式开启兜底时生效）
        rule_result = self._rule_route(text)
        if rule_result is not None and str(rule_result.get("action") or "") in {
            "set_report_context",
            "add_watchlist",
            "remove_watchlist",
            "list_watchlist",
            "run_stock_analysis",
            "run_stock_screening",
        }:
            normalized_rule = self._normalize_llm_intent(text, rule_result) or rule_result
            if normalized_rule.get("mode") == "action":
                action = str(normalized_rule.get("action") or "")
                params = normalized_rule.get("params") if isinstance(normalized_rule.get("params"), dict) else {}
                normalized_rule["instruction"] = self._action_to_instruction(action, params)
            return normalized_rule

        # 非 AI 驱动模式下，LLM 路由作为补充
        if enable_llm_route:
            llm_result = self._llm_route(text)
            if llm_result is not None:
                normalized = self._normalize_llm_intent(text, llm_result)
                if normalized is not None:
                    if normalized.get("mode") == "action":
                        action = str(normalized.get("action") or "")
                        params = normalized.get("params") if isinstance(normalized.get("params"), dict) else {}
                        normalized["instruction"] = self._action_to_instruction(action, params)
                    return normalized

        if rule_result is not None:
            normalized_rule = self._normalize_llm_intent(text, rule_result) or rule_result
            if normalized_rule.get("mode") == "action":
                action = str(normalized_rule.get("action") or "")
                params = normalized_rule.get("params") if isinstance(normalized_rule.get("params"), dict) else {}
                normalized_rule["instruction"] = self._action_to_instruction(action, params)
            return normalized_rule

        return {"mode": "chat"}

    def _normalize_llm_intent(self, text: str, llm_result: Dict[str, Any]) -> Dict[str, Any] | None:
        if not isinstance(llm_result, dict):
            return None

        mode = str(llm_result.get("mode") or "").strip()
        if mode == "chat":
            reply = llm_result.get("reply")
            if isinstance(reply, str) and reply.strip():
                return {"mode": "chat", "reply": reply.strip()}
            return {"mode": "chat"}

        if mode != "action":
            return None

        action = str(llm_result.get("action") or "").strip()
        if action not in self.ROUTABLE_ACTIONS:
            return None

        params = llm_result.get("params")
        if not isinstance(params, dict):
            params = {}
        if "market" in params:
            normalized_market = self._normalize_market_value(params.get("market"))
            if normalized_market:
                params["market"] = normalized_market

        report_keyword = self._extract_report_keyword(text)
        if report_keyword:
            return {
                "mode": "action",
                "action": "set_report_context",
                "params": {"report_keyword": report_keyword},
            }

        hints = self.ACTION_PARAM_HINTS.get(action, {"required": [], "optional": []})
        required_params = hints.get("required", [])

        # 参数补全（优先从用户原文提取），减少模型漏参导致的执行失败
        if action == "run_stock_analysis":
            raw_stock_param = str(params.get("stock_name_or_code") or "").strip()
            if raw_stock_param:
                cleaned = self._strip_market_suffix(raw_stock_param)
                if cleaned:
                    params["stock_name_or_code"] = cleaned
                inferred_market = self._infer_market_from_stock_token(raw_stock_param)
                if inferred_market and not params.get("market"):
                    params["market"] = inferred_market

            stock, market, depth, force_refresh = self._extract_stock_from_analysis(text)
            if stock:
                current = str(params.get("stock_name_or_code") or "").strip()
                if (not current) or (current == raw_stock_param) or (self._strip_market_suffix(current) == stock):
                    params["stock_name_or_code"] = stock
                if not params.get("market"):
                    inferred_market_from_stock = self._infer_market_from_stock_token(stock)
                    if inferred_market_from_stock:
                        params["market"] = inferred_market_from_stock
            if market and not params.get("market"):
                params["market"] = market
            if depth and not params.get("research_depth"):
                params["research_depth"] = depth
            if force_refresh and "force_refresh" not in params:
                params["force_refresh"] = True
            normalized_market = self._normalize_market_value(params.get("market"))
            if normalized_market:
                params["market"] = normalized_market

            stock_name_or_code = str(params.get("stock_name_or_code") or "").strip()
            if stock_name_or_code and normalized_market:
                normalized_symbol = self._normalize_symbol_for_market(stock_name_or_code, normalized_market)
                if normalized_symbol:
                    params["stock_name_or_code"] = normalized_symbol
                else:
                    inferred_symbol = self.infer_symbol_from_name(stock_name_or_code, normalized_market)
                    if inferred_symbol:
                        params["stock_name_or_code"] = inferred_symbol
        elif action == "add_watchlist" and not params.get("stock_name_or_code"):
            stock = self._extract_watchlist_target(text)
            if stock:
                params["stock_name_or_code"] = stock
        elif action == "remove_watchlist" and not params.get("stock_name_or_code"):
            m = re.search(r"(?:删除|移除|去掉)\s*(.+)", text)
            if m:
                params["stock_name_or_code"] = self._strip_market_suffix(m.group(1))
        elif action == "set_report_context" and not params.get("report_keyword"):
            kw = self._extract_report_keyword(text)
            if kw:
                params["report_keyword"] = kw
        elif action == "run_stock_screening":
            strategy = self._normalize_screening_strategy(params.get("strategy"))
            if not strategy:
                strategy = self._infer_screening_strategy(text)
            if strategy:
                params["strategy"] = strategy
            limit = self._normalize_screening_limit(params.get("limit"))
            if limit is None:
                limit = self._extract_screening_limit(text)
            if limit is not None:
                params["limit"] = limit
            markets = self._normalize_screening_markets(params.get("markets"))
            if not markets:
                markets = self._extract_screening_markets(text)
            if markets:
                params["markets"] = markets
        elif action == "list_history_reports":
            keyword = str(params.get("report_keyword") or "").strip()
            if keyword:
                params["report_keyword"] = keyword
            try:
                limit = int(float(str(params.get("limit")).strip())) if params.get("limit") is not None else None
            except Exception:
                limit = None
            if limit is not None:
                params["limit"] = max(1, min(30, int(limit)))

        missing = [k for k in required_params if not str(params.get(k) or "").strip()]
        if missing:
            # 关键参数缺失时不执行动作，回退自然对话避免误操作
            return {"mode": "chat"}

        return {"mode": "action", "action": action, "params": params}

    def _default_chat_reply(self) -> str:
        return (
            "当前未命中可执行命令，已转为自然语言分析。"
            f"你也可以直接使用命令：{'、'.join(self.COMMAND_CATALOG)}。"
            "也可以问我：这个机器人支持哪些功能/选股有哪些策略。"
        )

    def _action_to_instruction(self, action: str, params: Dict[str, Any]) -> str:
        a = str(action or "").strip()
        p = params if isinstance(params, dict) else {}

        if a == "help":
            return "/help"
        if a == "daily_brief":
            return "/daily"
        if a == "daily_picks":
            return "/pick"
        if a == "rebalance_suggestions":
            return "/rebalance"
        if a == "positions_snapshot":
            return "/position"
        if a == "sync_longport_positions":
            symbols = p.get("symbols")
            if isinstance(symbols, list):
                clean = [str(x).strip() for x in symbols if str(x).strip()]
                if clean:
                    return "/syncpos " + ",".join(clean)
            return "/syncpos"
        if a == "feedback_metrics":
            month_key = str(p.get("month_key") or "").strip()
            return f"/feedback {month_key}" if month_key else "/feedback"
        if a == "list_history_reports":
            keyword = str(p.get("report_keyword") or "").strip()
            limit = p.get("limit")
            parts = ["/history"]
            if keyword:
                parts.append(keyword)
            if limit is not None and str(limit).strip():
                parts.append(str(limit))
            return " ".join(parts)
        if a == "list_watchlist":
            return "/wl list"
        if a == "add_watchlist":
            target = str(p.get("stock_name_or_code") or "").strip()
            return f"/wl add {target}" if target else "/wl add <股票>"
        if a == "remove_watchlist":
            target = str(p.get("stock_name_or_code") or "").strip()
            return f"/wl del {target}" if target else "/wl del <股票>"
        if a == "add_a_share_position":
            target = str(p.get("stock_name_or_code") or "").strip() or "<股票>"
            qty = p.get("quantity")
            cost = p.get("avg_cost")
            parts = ["/wl add", target]
            if qty is not None and str(qty).strip():
                parts.append(str(qty))
            if cost is not None and str(cost).strip():
                parts.append(str(cost))
            return " ".join(parts)
        if a == "set_report_context":
            keyword = str(p.get("report_keyword") or "").strip()
            return f"/report {keyword}" if keyword else "/report <analysis_id|股票>"
        if a == "run_stock_analysis":
            market = self._normalize_market_value(p.get("market")) or str(p.get("market") or "").strip()
            stock_raw = str(p.get("stock_name_or_code") or "").strip()
            stock = stock_raw or "<代码|名称>"
            normalized_symbol = self._normalize_symbol_for_market(stock_raw, market) if stock_raw and market else None
            if normalized_symbol:
                instruction_token = self._symbol_to_instruction_token(normalized_symbol, market)
                stock = instruction_token or normalized_symbol
            elif stock_raw:
                stock = stock_raw
            depth = str(p.get("research_depth") or "").strip()
            force = bool(p.get("force_refresh"))
            parts = ["/report", stock]
            if market and not normalized_symbol:
                parts.append(market)
            if depth:
                parts.append(depth)
            if force:
                parts.append("force")
            return " ".join(parts)
        if a == "run_positions_batch_analysis":
            limit = self._normalize_screening_limit(p.get("limit")) or 5
            markets = self._normalize_screening_markets(p.get("markets")) or ["A股", "港股", "美股"]
            depth = self._normalize_research_depth(str(p.get("research_depth") or "")) or ""
            force = bool(p.get("force_refresh"))
            parts = ["/reportpos", str(limit), ",".join(markets)]
            if depth:
                parts.append(depth)
            if force:
                parts.append("force")
            return " ".join(parts)
        if a == "run_stock_screening":
            strategy = self._normalize_screening_strategy(p.get("strategy")) or "价值"
            limit = self._normalize_screening_limit(p.get("limit")) or 10
            markets = self._normalize_screening_markets(p.get("markets")) or ["A股"]
            market_text = ",".join(markets)
            return f"/screen {strategy} {limit} {market_text}"
        return a or "unknown_action"

    @staticmethod
    def _normalize_market_hint(text: str) -> str | None:
        raw = str(text or "")
        if (
            ("港股" in raw)
            or re.search(r"\(HK\)|（HK）|\.HK", raw, flags=re.IGNORECASE)
            or re.search(r"(?:^|[\s（(])hk(?:[\s）)]|$)", raw, flags=re.IGNORECASE)
            or re.search(r"^\s*hk(?=[\u4e00-\u9fa5\d])", raw, flags=re.IGNORECASE)
            or re.search(r"(?:[\u4e00-\u9fa5]|\d)\s*hk\s*$", raw, flags=re.IGNORECASE)
        ):
            return "港股"
        if (
            ("美股" in raw)
            or re.search(r"\(US\)|（US）|\.US", raw, flags=re.IGNORECASE)
            or re.search(r"(?:^|[\s（(])us(?:[\s）)]|$)", raw, flags=re.IGNORECASE)
            or re.search(r"^\s*us(?=[\u4e00-\u9fa5\d])", raw, flags=re.IGNORECASE)
            or re.search(r"(?:[\u4e00-\u9fa5]|\d)\s*us\s*$", raw, flags=re.IGNORECASE)
        ):
            return "美股"
        if ("A股" in raw) or ("a股" in raw):
            return "A股"
        return None

    @staticmethod
    def _normalize_market_value(value: Any) -> str | None:
        s = str(value or "").strip().lower()
        if not s:
            return None
        if s in {"hk", "hkex", "港股", "hongkong", "hong_kong"}:
            return "港股"
        if s in {"us", "usa", "nasdaq", "nyse", "美股"}:
            return "美股"
        if s in {"cn", "a", "a股", "ashare", "ashares", "sh", "sz"}:
            return "A股"
        return None

    @staticmethod
    def _infer_market_from_stock_token(token: str) -> str | None:
        raw = str(token or "").strip()
        if not raw:
            return None
        # 常见美股 ticker（如 AAPL/VRT）
        if re.fullmatch(r"[A-Za-z]{1,5}", raw):
            return "美股"
        # 常见港股数字代码（如 700/00700）
        if re.fullmatch(r"\d{4,5}", raw):
            return "港股"
        if re.search(r"^\s*hk(?=[\u4e00-\u9fa5\d])", raw, flags=re.IGNORECASE) or re.search(
            r"(?<=[\u4e00-\u9fa5\d])\s*hk\s*$",
            raw,
            flags=re.IGNORECASE,
        ):
            return "港股"
        if re.search(r"^\s*us(?=[\u4e00-\u9fa5\d])", raw, flags=re.IGNORECASE) or re.search(
            r"(?<=[\u4e00-\u9fa5\d])\s*us\s*$",
            raw,
            flags=re.IGNORECASE,
        ):
            return "美股"
        return None

    @staticmethod
    def _strip_market_suffix(stock: str) -> str:
        s = str(stock or "").strip()
        s = re.sub(r"[（(]\s*(港股|美股|A股|a股|HK|US)\s*[）)]", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"^\s*(港股|美股|A股|a股)\s*(的)?\s*", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"\s*(港股|美股|A股|a股)\s*$", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"^\s*(hk|us)\s*(?=[\u4e00-\u9fa5\d])", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"(?<=[\u4e00-\u9fa5\d])\s*(hk|us)\s*$", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"[，。！？?!].*$", "", s).strip()
        return s

    @staticmethod
    def _normalize_research_depth(text: str | None) -> str | None:
        raw = str(text or "").strip().lower()
        if not raw:
            return None
        aliases = {
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
        return aliases.get(raw)

    def _extract_research_depth(self, text: str) -> str | None:
        t = str(text or "").strip()
        if not t:
            return None
        for token in ("全面", "完整", "复杂", "深度", "标准", "基础", "快速"):
            if token in t:
                return self._normalize_research_depth(token)
        m = re.search(r"(?:深度|等级|级别)\s*[:：]?\s*([1-5])", t)
        if m:
            return self._normalize_research_depth(m.group(1))
        return None

    def _extract_force_refresh(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        patterns = ("强制", "重新", "重跑", "重算", "刷新", "重做")
        return any(p in t for p in patterns)

    def _extract_report_keyword(self, text: str) -> str | None:
        t = str(text or "").strip()
        if not t:
            return None
        patterns = [
            r"基于\s*(.+?)\s*的?\s*分析报告继续分析",
            r"基于\s*(.+?)\s*报告继续分析",
            r"基于\s*(.+?)\s*的?\s*报告继续分析",
            r"在\s*(.+?)\s*的?\s*分析报告基础上继续分析",
        ]
        for p in patterns:
            m = re.search(p, t)
            if m:
                kw = self._strip_market_suffix(m.group(1))
                if kw:
                    return kw
        if "报告" in t and "继续分析" in t:
            m = re.search(r"(.+?)\s*的?\s*分析报告", t)
            if m:
                kw = self._strip_market_suffix(m.group(1))
                if kw:
                    return kw
        return None

    @staticmethod
    def _is_history_report_query(text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        if not re.search(r"(报告|analysis)", t, flags=re.IGNORECASE):
            return False
        return bool(
            re.search(r"(历史|之前|过往|以前).*(分析)?报告", t)
            or re.search(r"(分析)?历史报告", t)
            or re.search(r"(查看|查|查询|找|列出).*(历史|之前|过往|以前).*(报告|analysis)", t)
        )

    def _extract_history_report_keyword(self, text: str) -> str | None:
        t = str(text or "").strip()
        if not t:
            return None

        m_symbol = re.search(r"\b([A-Za-z]{1,6}(?:\.HK)?)\b", t)
        if m_symbol:
            return m_symbol.group(1).upper()

        m_a = re.search(r"\b(\d{6})\b", t)
        if m_a:
            return m_a.group(1)

        patterns = [
            r"(?:查看|看看|查下|查询|找下|找找|列出|帮我查看下|帮我看下|帮我查下)\s*(.+?)\s*(?:之前|历史|过往|以前).*(?:分析)?报告",
            r"(?:查看|看看|查下|查询|找下|找找|列出)\s*(.+?)\s*(?:分析)?历史报告",
        ]
        for p in patterns:
            m = re.search(p, t, flags=re.IGNORECASE)
            if not m:
                continue
            kw = self._strip_market_suffix(str(m.group(1) or "").strip())
            kw = re.sub(r"^(?:一下|下|关于|有关)\s*", "", kw)
            if kw and kw not in {"我", "我们", "之前", "历史报告", "分析报告"}:
                return kw
        return None

    def _extract_stock_from_analysis(self, text: str) -> tuple[str | None, str | None, str | None, bool]:
        t = str(text or "").strip()
        market = self._normalize_market_hint(t)
        depth = self._extract_research_depth(t)
        force_refresh = self._extract_force_refresh(t)
        patterns = [
            r"(?:帮我)?(?:分析下|分析一下|分析|研究下|研究一下|研究|看下|看看)\s*([A-Za-z0-9.\-（）()\u4e00-\u9fa5]+)",
            r"(?:对|帮我对)\s*([A-Za-z0-9.\-（）()\u4e00-\u9fa5]+)\s*(?:做个股分析|进行分析)",
        ]
        stock_raw = None
        for p in patterns:
            m = re.search(p, t)
            if m:
                stock_raw = m.group(1)
                break
        if stock_raw is None:
            return None, market, depth, force_refresh
        raw_token = str(stock_raw).strip()
        if not market:
            if re.search(r"^\s*hk(?=[\u4e00-\u9fa5\d])", raw_token, flags=re.IGNORECASE) or re.search(
                r"(?<=[\u4e00-\u9fa5\d])\s*hk\s*$",
                raw_token,
                flags=re.IGNORECASE,
            ):
                market = "港股"
            elif re.search(r"^\s*us(?=[\u4e00-\u9fa5\d])", raw_token, flags=re.IGNORECASE) or re.search(
                r"(?<=[\u4e00-\u9fa5\d])\s*us\s*$",
                raw_token,
                flags=re.IGNORECASE,
            ):
                market = "美股"
        stock = self._strip_market_suffix(stock_raw)
        if not stock:
            return None, market, depth, force_refresh
        return stock, market, depth, force_refresh

    def _extract_watchlist_target(self, text: str) -> str | None:
        t = str(text or "").strip()
        patterns = [
            r"(?:把|将)?\s*(.+?)\s*(?:加入|加到|加入到)\s*(?:自选股|自选|关注池|观察池)",
            r"(?:添加|新增)\s*(.+?)\s*(?:到)?\s*(?:自选股|自选|关注池|观察池)",
        ]
        for p in patterns:
            m = re.search(p, t)
            if m:
                target = self._strip_market_suffix(m.group(1))
                if target:
                    return target
        return None

    @staticmethod
    def _normalize_screening_strategy(value: Any) -> str | None:
        s = str(value or "").strip().lower()
        if not s:
            return None
        mapping = {
            "价值": "价值",
            "value": "价值",
            "lowpe": "价值",
            "低估": "价值",
            "质量": "质量",
            "quality": "质量",
            "高roe": "质量",
            "roe": "质量",
            "动量": "动量",
            "momentum": "动量",
            "趋势": "动量",
        }
        return mapping.get(s)

    @staticmethod
    def _normalize_screening_limit(value: Any) -> int | None:
        try:
            n = int(float(str(value).strip()))
        except Exception:
            return None
        return max(1, min(30, n))

    def _infer_screening_strategy(self, text: str) -> str | None:
        t = str(text or "").lower()
        if any(k in t for k in ["动量", "趋势", "强势"]):
            return "动量"
        if any(k in t for k in ["质量", "高roe", "roe", "盈利能力"]):
            return "质量"
        if any(k in t for k in ["价值", "低估", "便宜", "低pe", "低pb"]):
            return "价值"
        return None

    def _extract_screening_limit(self, text: str) -> int | None:
        t = str(text or "")
        m = re.search(r"(?:top|前)\s*(\d{1,2})", t, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"(\d{1,2})\s*(?:只|个|条)", t)
        if not m:
            return None
        return self._normalize_screening_limit(m.group(1))

    @staticmethod
    def _normalize_screening_markets(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            raw_tokens = [str(x) for x in value]
        else:
            raw_tokens = re.split(r"[,\s/|，、]+", str(value))
        markets: list[str] = []
        for token in raw_tokens:
            t = str(token or "").strip().lower()
            if not t:
                continue
            if t in {"cn", "a", "a股", "ashare", "ashares"}:
                if "A股" not in markets:
                    markets.append("A股")
            elif t in {"hk", "港股", "hkex", "hongkong"}:
                if "港股" not in markets:
                    markets.append("港股")
            elif t in {"us", "美股", "usa", "nasdaq", "nyse"}:
                if "美股" not in markets:
                    markets.append("美股")
        return markets

    def _extract_screening_markets(self, text: str) -> list[str]:
        t = str(text or "")
        markets: list[str] = []
        if re.search(r"(a股|A股|ashare|cn)", t, flags=re.IGNORECASE):
            markets.append("A股")
        if re.search(r"(港股|hk|hong ?kong)", t, flags=re.IGNORECASE):
            markets.append("港股")
        if re.search(r"(美股|us|nasdaq|nyse)", t, flags=re.IGNORECASE):
            markets.append("美股")
        return markets

    def _rule_route(self, text: str) -> Dict[str, Any] | None:
        t = str(text or "").strip()
        if not t or t.startswith("/"):
            return None

        report_keyword = self._extract_report_keyword(t)
        if report_keyword:
            return {
                "mode": "action",
                "action": "set_report_context",
                "params": {"report_keyword": report_keyword},
            }

        add_target = self._extract_watchlist_target(t)
        if add_target:
            return {
                "mode": "action",
                "action": "add_watchlist",
                "params": {"stock_name_or_code": add_target},
            }

        if re.search(r"(?:查看|看看|列出).*(?:自选股|自选|关注池|观察池)", t):
            return {"mode": "action", "action": "list_watchlist", "params": {}}

        m_del = re.search(r"(?:把|将)?\s*(.+?)\s*(?:从)?\s*(?:自选股|自选|关注池|观察池).*(?:移除|删除|去掉)", t)
        if not m_del:
            m_del = re.search(r"(?:移除|删除|去掉)\s*(.+?)\s*(?:自选股|自选|关注池|观察池)", t)
        if m_del:
            target = self._strip_market_suffix(m_del.group(1))
            if target:
                return {
                    "mode": "action",
                    "action": "remove_watchlist",
                    "params": {"stock_name_or_code": target},
                }

        if re.search(r"(分析|研究|看下|看看|个股)", t):
            stock, market, depth, force_refresh = self._extract_stock_from_analysis(t)
            if stock:
                params: Dict[str, Any] = {"stock_name_or_code": stock}
                if market:
                    params["market"] = market
                if depth:
                    params["research_depth"] = depth
                if force_refresh:
                    params["force_refresh"] = True
                return {
                    "mode": "action",
                    "action": "run_stock_analysis",
                    "params": params,
                }

        if re.search(r"(持仓|仓位).*(批量|一键|全部).*(分析|研究)", t) or re.search(r"(批量|一键).*(分析|研究).*(持仓|仓位)", t):
            params: Dict[str, Any] = {}
            limit = self._extract_screening_limit(t)
            if limit is not None:
                params["limit"] = max(1, min(20, int(limit)))
            markets = self._extract_screening_markets(t)
            if markets:
                params["markets"] = markets
            depth = self._extract_research_depth(t)
            if depth:
                params["research_depth"] = depth
            if self._extract_force_refresh(t):
                params["force_refresh"] = True
            return {
                "mode": "action",
                "action": "run_positions_batch_analysis",
                "params": params,
            }

        if re.search(r"(选股|筛选|找股票|找标的|选.*股票|各选)", t):
            params: Dict[str, Any] = {}
            strategy = self._infer_screening_strategy(t)
            if strategy:
                params["strategy"] = strategy
            limit = self._extract_screening_limit(t)
            if limit is not None:
                params["limit"] = limit
            return {
                "mode": "action",
                "action": "run_stock_screening",
                "params": params,
            }

        if re.search(r"(同步|刷新|更新).*(长桥|longport).*(持仓|仓位)", t, flags=re.IGNORECASE):
            return {
                "mode": "action",
                "action": "sync_longport_positions",
                "params": {},
            }

        return None

    def chat_reply(
        self,
        text: str,
        context: Dict[str, Any] | None = None,
        history: list[Dict[str, str]] | None = None,
        previous_response_id: str | None = None,
    ) -> str:
        llm_reply, _ = self._llm_chat(
            text=text,
            context=context,
            history=history,
            previous_response_id=previous_response_id,
        )
        if llm_reply:
            return llm_reply
        if context and context.get("type") == "report":
            stock = context.get("stock_symbol", "")
            return f"当前在{stock}分析报告上下文中。你可以继续追问风险点、估值和仓位建议。"
        return (
            "我可以帮你做投研操作。"
            f"支持命令：{'、'.join(self.COMMAND_CATALOG)}。"
            "例如：把贵州茅台加入自选股。"
        )

    def chat_reply_with_state(
        self,
        text: str,
        context: Dict[str, Any] | None = None,
        history: list[Dict[str, str]] | None = None,
        previous_response_id: str | None = None,
    ) -> tuple[str, Dict[str, Any]]:
        llm_reply, llm_state = self._llm_chat(
            text=text,
            context=context,
            history=history,
            previous_response_id=previous_response_id,
        )
        if llm_reply:
            return llm_reply, llm_state
        if context and context.get("type") == "report":
            stock = context.get("stock_symbol", "")
            return f"当前在{stock}分析报告上下文中。你可以继续追问风险点、估值和仓位建议。", llm_state
        return (
            "我可以帮你做投研操作。"
            f"支持命令：{'、'.join(self.COMMAND_CATALOG)}。"
            "例如：把贵州茅台加入自选股。",
            llm_state,
        )

    @staticmethod
    def _is_true(value: str | None, default: bool = False) -> bool:
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _extract_responses_text(response: Any) -> str:
        text = getattr(response, "output_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()

        output = getattr(response, "output", None) or []
        if isinstance(output, list):
            chunks: list[str] = []
            for item in output:
                content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
                if isinstance(content, list):
                    for c in content:
                        t = c.get("text") if isinstance(c, dict) else getattr(c, "text", None)
                        if isinstance(t, str) and t.strip():
                            chunks.append(t.strip())
            if chunks:
                return "\n".join(chunks)
        return ""

    def _build_chat_system_prompt(
        self,
        user_text: str,
        safe_context: Dict[str, Any] | None,
        capability_text: str = "",
    ) -> str:
        # 默认走紧凑模板，避免每轮重复注入超长说明
        prompt = (
            "你是股票投研助手。非指令型消息请给出简洁结构化结论。"
            "优先输出：结论、核心理由、主要风险、触发条件。"
            "绝不输出任何密钥、token、密码、cookie 等敏感信息。"
        )
        # 报告上下文：追加报告型回答约束
        if isinstance(safe_context, dict) and safe_context.get("type") == "report":
            prompt += (
                "当前处于个股报告上下文。回答时优先围绕该报告的结论、风险、估值与仓位建议，"
                "避免偏离到无关话题。"
            )
        # 能力问答：按需注入能力说明，不在普通问答里重复消耗
        if self._is_capability_question(user_text):
            prompt += (
                f"支持命令: {json.dumps(self.COMMAND_CATALOG, ensure_ascii=False)}。"
                f"{('项目能力说明(安全版): ' + capability_text + '。') if capability_text else ''}"
            )
        else:
            prompt += "如用户询问功能说明，可建议使用 /help。"
        if safe_context:
            prompt += f" 当前上下文(已脱敏): {json.dumps(safe_context, ensure_ascii=False)}。"
        return prompt

    def _llm_route(self, text: str) -> Dict[str, Any] | None:
        api_key, base_url = self._resolve_llm_credentials()
        if not api_key:
            return None

        model = os.getenv("FEISHU_NL_AGENT_MODEL", "gpt-4o-mini")
        safe_text = self._sanitize_prompt_text(text)

        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
            action_manifest = [
                {
                    "action": item["action"],
                    "required_params": self.ACTION_PARAM_HINTS.get(item["action"], {}).get("required", []),
                    "optional_params": self.ACTION_PARAM_HINTS.get(item["action"], {}).get("optional", []),
                }
                for item in self.API_CATALOG
            ]
            route_schema = self.ROUTE_SCHEMA
            system_prompt = (
                "你是飞书股票机器人的指令路由器。"
                "你只输出 JSON，不要输出任何解释文字。"
                "你需要在已提供的命令/动作里做决策。"
                "若用户输入可映射到某个动作，输出 mode=action；否则输出 mode=chat。"
                "写操作将由后端二次确认，你只需正确给出动作和参数。"
                "涉及功能说明/策略介绍/使用教程类提问时，优先输出 mode=chat。"
                "绝不输出任何密钥、token、密码、cookie 等敏感信息。"
                f"输出格式: {json.dumps(route_schema, ensure_ascii=False)}。"
                f"动作清单: {json.dumps(action_manifest, ensure_ascii=False)}。"
                "参数必须只使用动作清单中定义的字段。"
                "当用户明确要求分析某只股票时，优先 action=run_stock_analysis。"
                "当用户要求选股/筛选股票时，优先 action=run_stock_screening。"
                "当 action=run_stock_analysis 时，params.stock_name_or_code 优先给股票代码，不要给名称。"
                "港股优先给 hk+5位数字（例如 hk00981），美股优先给 us+代码（例如 usAAPL），A股给6位代码。"
                "当用户说“基于某报告继续分析”时，优先 action=set_report_context。"
                "无法匹配动作时，返回 mode=chat 并给出简短 reply。"
            )
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": safe_text},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            content = content.removeprefix("```json").removesuffix("```").removeprefix("```").removesuffix("```").strip()
            data = json.loads(content)
            if not isinstance(data, dict):
                return None
            mode = data.get("mode")
            if mode not in {"action", "chat"}:
                return None
            if mode == "action":
                action = str(data.get("action") or "").strip()
                if action not in self.ROUTABLE_ACTIONS:
                    return {"mode": "chat"}
                params = data.get("params")
                if not isinstance(params, dict):
                    params = {}
                return {"mode": "action", "action": action, "params": params}
            if mode == "chat":
                reply = data.get("reply")
                if isinstance(reply, str) and reply.strip():
                    return {"mode": "chat", "reply": reply.strip()}
                return data
        except Exception:
            return None
        return None

    def _llm_chat(
        self,
        text: str,
        context: Dict[str, Any] | None = None,
        history: list[Dict[str, str]] | None = None,
        previous_response_id: str | None = None,
    ) -> tuple[str | None, Dict[str, Any]]:
        api_key, base_url = self._resolve_llm_credentials()
        if not api_key:
            return None, {}
        model = os.getenv("FEISHU_NL_AGENT_CHAT_MODEL", os.getenv("FEISHU_NL_AGENT_MODEL", "gpt-4o-mini"))
        safe_text = self._sanitize_prompt_text(text)
        safe_context = self._sanitize_for_prompt(context) if context else None
        safe_history = self._sanitize_for_prompt(history) if history else []
        need_capability_prompt = self._is_capability_question(safe_text)
        capability_text = self._build_capability_knowledge_text() if need_capability_prompt else ""
        next_state: Dict[str, Any] = {}
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
            prefer_native = self._is_true(os.getenv("FEISHU_NL_AGENT_USE_NATIVE_RESPONSES"), default=True)
            if prefer_native and hasattr(client, "responses"):
                try:
                    input_items: list[Dict[str, Any]] = []
                    if not previous_response_id:
                        # 首次进入原生会话时注入少量历史，便于从legacy多轮平滑迁移
                        for turn in (safe_history or [])[-8:]:
                            role = str(turn.get("role") or "").strip()
                            if role in {"user", "assistant"}:
                                input_items.append(
                                    {
                                        "role": role,
                                        "content": [{"type": "input_text", "text": str(turn.get("content", ""))}],
                                    }
                                )
                    input_items.append(
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": safe_text}],
                        }
                    )

                    req: Dict[str, Any] = {
                        "model": model,
                        "instructions": self._build_chat_system_prompt(
                            user_text=safe_text,
                            safe_context=safe_context,
                            capability_text=capability_text,
                        ),
                        "input": input_items,
                        "temperature": 0.5,
                        "store": True,
                    }
                    if previous_response_id:
                        req["previous_response_id"] = previous_response_id

                    response = client.responses.create(**req)
                    reply = self._extract_responses_text(response)
                    if reply:
                        next_state = {
                            "previous_response_id": str(getattr(response, "id", "") or ""),
                            "model": model,
                            "backend": "responses",
                            "updated_at": datetime.utcnow().isoformat(),
                        }
                        return reply, next_state
                except Exception:
                    if previous_response_id:
                        # previous_response_id失效时清空，避免后续每轮都失败
                        next_state = {"previous_response_id": None, "updated_at": datetime.utcnow().isoformat()}

            messages = [
                {
                    "role": "system",
                    "content": self._build_chat_system_prompt(
                        user_text=safe_text,
                        safe_context=safe_context,
                        capability_text=capability_text,
                    ),
                }
            ]
            for turn in (safe_history or [])[-8:]:
                role = turn.get("role")
                if role in {"user", "assistant"}:
                    messages.append({"role": role, "content": str(turn.get("content", ""))})
            messages.append({"role": "user", "content": safe_text})

            resp = client.chat.completions.create(
                model=model,
                temperature=0.5,
                messages=messages,
            )
            return (resp.choices[0].message.content or "").strip(), next_state
        except Exception:
            return None, next_state

    def choose_stock_candidate(self, text: str, candidates: list[Dict[str, str]]) -> Dict[str, str] | None:
        """
        在候选股票中由 LLM 决策最匹配项，避免硬编码映射。
        返回候选项原始 dict，失败返回 None。
        """
        if not candidates:
            return None

        api_key, base_url = self._resolve_llm_credentials()
        if not api_key:
            return None

        model = os.getenv("FEISHU_NL_AGENT_MODEL", "gpt-4o-mini")
        manifest = []
        for idx, c in enumerate(candidates):
            manifest.append(
                {
                    "index": idx,
                    "symbol": str(c.get("symbol") or ""),
                    "name": str(c.get("name") or ""),
                    "market": str(c.get("market") or ""),
                }
            )

        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
            system_prompt = (
                "你是股票代码消歧路由器。"
                "根据用户原话，从候选股票中选一个最匹配项。"
                "仅输出 JSON，如 {\"index\": 1}。"
                "如果无法判断，输出 {\"index\": -1}。"
            )
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({"text": text, "candidates": manifest}, ensure_ascii=False)},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            content = content.removeprefix("```json").removesuffix("```").removeprefix("```").removesuffix("```").strip()
            data = json.loads(content)
            idx = int(data.get("index", -1))
            if 0 <= idx < len(candidates):
                return candidates[idx]
        except Exception:
            return None
        return None

    @staticmethod
    def _normalize_symbol_for_market(symbol: str, market: str) -> str | None:
        s = str(symbol or "").strip().upper()
        m = str(market or "").strip()
        if not s or not m:
            return None

        if m == "港股":
            if re.fullmatch(r"\d{5}\.HK", s):
                return s
            digits = re.sub(r"\D", "", s)
            if not digits:
                return None
            if 1 <= len(digits) <= 5:
                return f"{digits.zfill(5)}.HK"
            return None

        if m == "美股":
            s = s.replace(".US", "").strip()
            if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
                return s
            return None

        if m == "A股":
            digits = re.sub(r"\D", "", s)
            if len(digits) == 6:
                return digits
            return None

        return None

    @staticmethod
    def _symbol_to_instruction_token(symbol: str, market: str) -> str | None:
        s = str(symbol or "").strip().upper()
        m = str(market or "").strip()
        if not s or not m:
            return None

        if m == "港股":
            digits = re.sub(r"\D", "", s)
            if 1 <= len(digits) <= 5:
                return f"hk{digits.zfill(5)}"
            return None

        if m == "美股":
            base = s.replace(".US", "").strip()
            if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", base):
                return f"us{base}"
            return None

        if m == "A股":
            digits = re.sub(r"\D", "", s)
            if len(digits) == 6:
                return digits
            return None

        return None

    def infer_symbol_from_name(self, stock_name: str, market: str) -> str | None:
        """
        当本地/数据源无法名称反查时，使用LLM做名称->代码兜底。
        """
        api_key, base_url = self._resolve_llm_credentials()
        if not api_key:
            return None

        market_cn = self._normalize_market_value(market) or str(market or "").strip()
        if market_cn not in {"港股", "美股", "A股"}:
            return None

        model = os.getenv("FEISHU_NL_AGENT_MODEL", "gpt-4o-mini")
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
            schema_hint = {"symbol": "string | empty"}
            system_prompt = (
                "你是股票代码解析器。"
                "根据股票名称和市场，输出最可能的交易代码。"
                "仅输出JSON，不要输出任何解释。"
                f"输出格式: {json.dumps(schema_hint, ensure_ascii=False)}。"
                "港股必须输出5位数字加 .HK（例如 00981.HK）；"
                "美股输出代码（例如 AAPL）。"
                "无法确定时输出空字符串。"
            )
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({"stock_name": stock_name, "market": market_cn}, ensure_ascii=False)},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            content = content.removeprefix("```json").removesuffix("```").removeprefix("```").removesuffix("```").strip()
            data = json.loads(content)
            if not isinstance(data, dict):
                return None
            symbol = str(data.get("symbol") or "").strip()
            if not symbol:
                return None
            return self._normalize_symbol_for_market(symbol, market_cn)
        except Exception:
            return None


feishu_nl_agent_service = FeishuNLAgentService()
