import os
from typing import Dict


_PROFILE_LENGTH_HINTS: Dict[str, Dict[str, str]] = {
    "analyst": {
        "light": "建议长度 600-780 汉字",
        "balanced": "建议长度 700-900 汉字",
        "strong": "建议长度 500-680 汉字",
    },
    "researcher": {
        "light": "建议长度 320-480 汉字",
        "balanced": "建议长度 300-450 汉字",
        "strong": "建议长度 220-360 汉字",
    },
    "research_manager": {
        "light": "建议长度 850-1100 汉字",
        "balanced": "建议长度 800-1000 汉字",
        "strong": "建议长度 650-900 汉字",
    },
    "trader": {
        "light": "建议长度 500-760 汉字",
        "balanced": "建议长度 450-650 汉字",
        "strong": "建议长度 350-520 汉字",
    },
    "risk_manager": {
        "light": "建议长度 760-980 汉字",
        "balanced": "建议长度 700-900 汉字",
        "strong": "建议长度 560-780 汉字",
    },
}


_ROLE_SCHEMAS: Dict[str, str] = {
    "analyst": "结论 -> 3-5条关键证据 -> 风险提示 -> 建议",
    "researcher": "立场一句话 -> 3条最强论点 -> 1条反驳",
    "research_manager": "最终立场 -> 关键权衡 -> 执行要点",
    "trader": "动作 -> 仓位/价位 -> 风险控制",
    "risk_manager": "风险裁决 -> 触发条件 -> 监控指标",
}


_ROLE_ALIASES: Dict[str, str] = {
    "market_analyst": "analyst",
    "fundamentals_analyst": "analyst",
    "news_analyst": "analyst",
    "social_media_analyst": "analyst",
    "bull_researcher": "researcher",
    "bear_researcher": "researcher",
    "aggresive_debator": "researcher",
    "conservative_debator": "researcher",
    "neutral_debator": "researcher",
}


def is_concise_mode_enabled() -> bool:
    value = os.getenv("TA_PROMPT_CONCISE_MODE", "true").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _normalize_role(role: str) -> str:
    normalized = (role or "").strip().lower()
    return _ROLE_ALIASES.get(normalized, normalized if normalized in _ROLE_SCHEMAS else "analyst")


def _normalize_level(level: str) -> str:
    normalized = (level or "balanced").strip().lower()
    if normalized not in {"light", "balanced", "strong"}:
        return "balanced"
    return normalized


def get_output_schema(role: str) -> str:
    normalized_role = _normalize_role(role)
    return _ROLE_SCHEMAS[normalized_role]


def get_anti_repetition_rules() -> str:
    return (
        "反重复规则：\n"
        "1. 每段仅保留“观点+证据+影响”。\n"
        "2. 禁止复述上文和同义改写。\n"
        "3. 禁止客套话\n"
        "4. 引用数据只保留关键数字和时间点。\n"
        "5. 单句尽量不超过50个中文词。\n"
        "6. 必须保留关键风险和核心反证。\n"
        "7. 自检：若存在重复语义，请合并为一句并保留证据数字。"
    )


def get_concise_contract(role: str, level: str = "balanced") -> str:
    if not is_concise_mode_enabled():
        return ""

    normalized_role = _normalize_role(role)
    normalized_level = _normalize_level(level)
    schema = get_output_schema(normalized_role)
    length_hint = _PROFILE_LENGTH_HINTS[normalized_role][normalized_level]

    return (
        f"【精炼表达协议/{normalized_level}】\n"
        "先给结论，再给论证；只写关键信息。\n"
        f"输出结构：{schema}\n"
        f"{length_hint}\n"
        f"{get_anti_repetition_rules()}\n"
    )


def get_research_balance_rules() -> str:
    """研究员综合判断约束，避免单一技术面主导。"""
    return (
        "综合判断约束：\n"
        "1. 论证必须同时覆盖四个维度：基本面、新闻与宏观、情绪与资金、技术面。\n"
        "2. 三条最强论点中，至少两条不得以技术指标为主论据。\n"
        "3. 技术面仅用于择时与验证，不可单独作为结论依据。\n"
        "4. 每个维度至少给出一个可验证事实（数字、时间、事件或阈值）。\n"
        "5. 若维度间冲突，必须明确主导因子与失效条件。"
    )
