import pytest

from tradingagents.agents.utils.prompt_contract import (
    get_anti_repetition_rules,
    get_concise_contract,
    get_output_schema,
)


@pytest.mark.parametrize(
    "role,expected_schema",
    [
        ("market_analyst", "结论 -> 3-5条关键证据 -> 风险提示 -> 建议"),
        ("fundamentals_analyst", "结论 -> 3-5条关键证据 -> 风险提示 -> 建议"),
        ("news_analyst", "结论 -> 3-5条关键证据 -> 风险提示 -> 建议"),
        ("social_media_analyst", "结论 -> 3-5条关键证据 -> 风险提示 -> 建议"),
        ("bull_researcher", "立场一句话 -> 3条最强论点 -> 1条反驳"),
        ("bear_researcher", "立场一句话 -> 3条最强论点 -> 1条反驳"),
        ("aggresive_debator", "立场一句话 -> 3条最强论点 -> 1条反驳"),
        ("conservative_debator", "立场一句话 -> 3条最强论点 -> 1条反驳"),
        ("neutral_debator", "立场一句话 -> 3条最强论点 -> 1条反驳"),
        ("research_manager", "最终立场 -> 关键权衡 -> 执行要点"),
        ("trader", "动作 -> 仓位/价位 -> 风险控制"),
        ("risk_manager", "风险裁决 -> 触发条件 -> 监控指标"),
    ],
)
def test_get_output_schema_for_roles(role, expected_schema):
    assert get_output_schema(role) == expected_schema


def test_get_anti_repetition_rules_contains_core_rules():
    rules = get_anti_repetition_rules()
    assert "禁止复述上文和同义改写" in rules
    assert "关键数字和时间点" in rules
    assert "必须保留关键风险和核心反证" in rules


def test_get_concise_contract_balanced(monkeypatch):
    monkeypatch.setenv("TA_PROMPT_CONCISE_MODE", "true")
    contract = get_concise_contract("trader", "balanced")
    assert "精炼表达协议/balanced" in contract
    assert "动作 -> 仓位/价位 -> 风险控制" in contract
    assert "建议长度 450-650 汉字" in contract


def test_get_concise_contract_disabled(monkeypatch):
    monkeypatch.setenv("TA_PROMPT_CONCISE_MODE", "false")
    assert get_concise_contract("market_analyst", "balanced") == ""

