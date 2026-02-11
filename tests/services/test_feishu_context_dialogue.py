from app.services.feishu_nl_agent_service import FeishuNLAgentService


def test_parse_report_context_intent():
    svc = FeishuNLAgentService()
    out = svc.parse_user_intent("基于贵州茅台的分析报告继续分析")
    assert out["mode"] == "action"
    assert out["action"] == "set_report_context"
    assert out["params"]["report_keyword"] == "贵州茅台"


def test_chat_reply_with_report_context_fallback():
    svc = FeishuNLAgentService()
    text = svc.chat_reply(
        text="这个股票风险点有哪些",
        context={"type": "report", "stock_symbol": "600519", "summary": "..."},
        history=[{"role": "user", "content": "先看报告"}],
    )
    assert isinstance(text, str)
    assert len(text) > 0
