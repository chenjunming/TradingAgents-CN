from app.routers.feishu_bot import _build_report_selector_card
from app.services.feishu_push_service import FeishuPushService


def test_simple_card_uses_lark_md_div():
    svc = FeishuPushService()
    card = svc._build_simple_card("测试标题", ["**加粗**", "第二行"])
    assert "schema" not in card
    assert card["elements"][0]["tag"] == "div"
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    assert "**加粗**" in card["elements"][0]["text"]["content"]


def test_report_selector_card_contains_buttons_and_lark_md_intro():
    card = _build_report_selector_card(
        report_id="rid-001",
        stock_name="中芯国际",
        stock_symbol="688981",
        sections=[("final_trade_decision", "最终决策"), ("market_report", "市场技术分析")],
        selected="final_trade_decision",
    )
    assert "schema" not in card
    assert card["elements"][0]["tag"] == "div"
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    action_rows = [x for x in card["elements"] if x.get("tag") == "action"]
    assert len(action_rows) == 1
    assert len(action_rows[0]["actions"]) == 2
