import asyncio

from app.routers.feishu_bot import _normalize_market_type_hint, _parse_report_args, _select_report_section_text
from app.routers.feishu_bot import _resolve_stock_for_analysis
from app.services.feishu_nl_agent_service import FeishuNLAgentService


def test_parse_report_args_support_force_depth_and_section():
    depth, force_refresh, section = _parse_report_args(["深度", "force", "技术分析"], default_depth="全面")
    assert depth == "深度"
    assert force_refresh is True
    assert section == "market_report"


def test_select_report_section_prefers_final_decision_over_longest_text():
    reports = {
        "market_report": "M" * 5000,
        "final_trade_decision": "FINAL_DECISION_TEXT",
    }
    selected_key, selected_label, selected_text = _select_report_section_text(
        summary="short_summary",
        reports=reports,
        requested_section=None,
        max_chars=100_000,
    )
    assert selected_key == "final_trade_decision"
    assert selected_label == "最终决策"
    assert selected_text == "FINAL_DECISION_TEXT"


def test_nl_intent_extracts_force_refresh_for_reanalysis():
    svc = FeishuNLAgentService()
    svc._llm_route = lambda text: None  # type: ignore[method-assign]
    out = svc.parse_user_intent("帮我重新深度分析下中芯国际")
    assert out["mode"] == "action"
    assert out["action"] == "run_stock_analysis"
    assert out["params"].get("research_depth") == "深度"
    assert out["params"].get("force_refresh") is True


def test_normalize_market_type_hint_aliases():
    assert _normalize_market_type_hint("hk") == "港股"
    assert _normalize_market_type_hint("US") == "美股"
    assert _normalize_market_type_hint("sz") == "A股"


def test_resolve_stock_for_analysis_supports_hk_prefixed_code():
    symbol, name, market = asyncio.run(_resolve_stock_for_analysis("hk00981"))
    assert symbol == "00981.HK"
    assert market == "港股"
    assert name == "00981.HK"


def test_resolve_stock_for_analysis_supports_us_prefixed_code():
    symbol, name, market = asyncio.run(_resolve_stock_for_analysis("usAAPL"))
    assert symbol == "AAPL"
    assert market == "美股"
    assert name == "AAPL"
