from app.services.feishu_nl_agent_service import FeishuNLAgentService


def test_parse_add_watchlist_rule():
    svc = FeishuNLAgentService()
    svc._llm_route = lambda text: None  # type: ignore[method-assign]
    out = svc.parse_user_intent("把贵州茅台加入自选股")
    assert out["mode"] == "action"
    assert out["action"] == "add_watchlist"
    assert out["params"]["stock_name_or_code"] == "贵州茅台"
    assert out.get("instruction", "").startswith("/wl add")


def test_parse_non_command_chat_fallback():
    svc = FeishuNLAgentService()
    svc._llm_route = lambda text: None  # type: ignore[method-assign]
    out = svc.parse_user_intent("今天市场怎么样")
    assert out["mode"] in {"chat", "action"}
    if out["mode"] == "chat":
        assert isinstance(out.get("reply"), str)


def test_extract_stock_from_analysis_with_market_prefix():
    svc = FeishuNLAgentService()
    stock, market, depth, force_refresh = svc._extract_stock_from_analysis("帮我分析下港股中芯国际")
    assert stock == "中芯国际"
    assert market == "港股"
    assert depth is None
    assert force_refresh is False


def test_extract_stock_from_analysis_with_market_parentheses():
    svc = FeishuNLAgentService()
    stock, market, depth, force_refresh = svc._extract_stock_from_analysis("帮我深度分析下中芯国际（港股）")
    assert stock == "中芯国际"
    assert market == "港股"
    assert depth == "深度"
    assert force_refresh is False


def test_action_to_instruction_for_analysis():
    svc = FeishuNLAgentService()
    instr = svc._action_to_instruction(
        "run_stock_analysis",
        {"stock_name_or_code": "中芯国际", "market": "港股", "research_depth": "深度", "force_refresh": True},
    )
    assert instr == "/report 中芯国际 港股 深度 force"


def test_normalize_llm_intent_market_hk_to_cn_label():
    svc = FeishuNLAgentService()
    out = svc._normalize_llm_intent(
        "帮我分析下中芯国际",
        {
            "mode": "action",
            "action": "run_stock_analysis",
            "params": {"stock_name_or_code": "中芯国际", "market": "hk", "research_depth": "深度"},
        },
    )
    assert out is not None
    assert out["params"]["market"] == "港股"


def test_normalize_llm_intent_cleans_stock_param_suffix_hk():
    svc = FeishuNLAgentService()
    svc.infer_symbol_from_name = lambda stock_name, market: None  # type: ignore[method-assign]
    out = svc._normalize_llm_intent(
        "帮我深度分析下中芯国际hk",
        {
            "mode": "action",
            "action": "run_stock_analysis",
            "params": {"stock_name_or_code": "中芯国际hk", "market": "港股", "research_depth": "深度"},
        },
    )
    assert out is not None
    assert out["params"]["stock_name_or_code"] == "中芯国际"
    assert out["params"]["market"] == "港股"


def test_extract_stock_from_analysis_with_compact_hk_suffix():
    svc = FeishuNLAgentService()
    stock, market, depth, force_refresh = svc._extract_stock_from_analysis("帮我深度分析下中芯国际hk")
    assert stock == "中芯国际"
    assert market == "港股"
    assert depth == "深度"
    assert force_refresh is False


def test_extract_stock_from_analysis_with_compact_hk_prefix():
    svc = FeishuNLAgentService()
    stock, market, depth, force_refresh = svc._extract_stock_from_analysis("帮我分析下hk中芯国际")
    assert stock == "中芯国际"
    assert market == "港股"
    assert depth is None
    assert force_refresh is False


def test_normalize_symbol_for_market():
    svc = FeishuNLAgentService()
    assert svc._normalize_symbol_for_market("981", "港股") == "00981.HK"
    assert svc._normalize_symbol_for_market("00981.hk", "港股") == "00981.HK"
    assert svc._normalize_symbol_for_market("AAPL.US", "美股") == "AAPL"
    assert svc._normalize_symbol_for_market("aapl", "美股") == "AAPL"


def test_action_to_instruction_prefers_hk_code_token():
    svc = FeishuNLAgentService()
    instr = svc._action_to_instruction(
        "run_stock_analysis",
        {"stock_name_or_code": "00981.HK", "market": "港股", "research_depth": "深度"},
    )
    assert instr == "/report hk00981 深度"


def test_normalize_llm_intent_accepts_hk_prefixed_code():
    svc = FeishuNLAgentService()
    out = svc._normalize_llm_intent(
        "帮我深度分析下中芯国际hk",
        {
            "mode": "action",
            "action": "run_stock_analysis",
            "params": {"stock_name_or_code": "hk00981", "market": "港股", "research_depth": "深度"},
        },
    )
    assert out is not None
    assert out["params"]["stock_name_or_code"] == "00981.HK"
    assert out["params"]["market"] == "港股"


def test_normalize_llm_intent_infers_code_from_name(monkeypatch):
    svc = FeishuNLAgentService()
    monkeypatch.setattr(svc, "infer_symbol_from_name", lambda stock_name, market: "00981.HK")
    out = svc._normalize_llm_intent(
        "帮我深度分析下中芯国际港股",
        {
            "mode": "action",
            "action": "run_stock_analysis",
            "params": {"stock_name_or_code": "中芯国际", "market": "港股", "research_depth": "深度"},
        },
    )
    assert out is not None
    assert out["params"]["stock_name_or_code"] == "00981.HK"


def test_parse_capability_question_returns_chat_instead_of_screen_action():
    svc = FeishuNLAgentService()
    out = svc.parse_user_intent("你现在选股有哪些策略？")
    assert out["mode"] == "chat"
    reply = str(out.get("reply") or "")
    assert "价值" in reply
    assert "质量" in reply
    assert "动量" in reply
    assert "/screen" in reply


def test_sanitize_for_prompt_redacts_sensitive_fields_and_values():
    svc = FeishuNLAgentService()
    payload = {
        "OPENAI_API_KEY": "sk-abc1234567890xyz",
        "message": "Authorization: Bearer verysecrettokenvalue123456",
        "nested": {
            "access_token": "my_access_token_value",
            "normal": "hello",
        },
    }
    safe = svc._sanitize_for_prompt(payload)
    assert safe["OPENAI_API_KEY"] == "[REDACTED]"
    assert "[REDACTED]" in safe["message"]
    assert safe["nested"]["access_token"] == "[REDACTED]"
    assert safe["nested"]["normal"] == "hello"
