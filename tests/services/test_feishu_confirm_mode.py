from app.services.feishu_nl_agent_service import FeishuNLAgentService


def test_nl_agent_detects_action_for_watchlist_add():
    svc = FeishuNLAgentService()
    out = svc.parse_user_intent("把贵州茅台加入自选股")
    assert out["mode"] == "action"
    assert out["action"] == "add_watchlist"


def test_nl_agent_chat_for_non_action_text():
    svc = FeishuNLAgentService()
    out = svc.parse_user_intent("今天心情不错")
    assert out["mode"] in {"chat", "action"}
