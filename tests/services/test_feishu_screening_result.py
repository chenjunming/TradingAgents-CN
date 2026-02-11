from app.routers.feishu_bot import _build_screening_result_card, _build_screening_result_text


def _sample_screen_result():
    return {
        "strategy_label": "价值",
        "limit": 10,
        "markets": [
            {
                "market": "HK",
                "market_label": "港股",
                "strategy_label": "价值",
                "relax_text": "严格",
                "items": [
                    {
                        "symbol": "00941",
                        "name": "中国移动",
                        "pe": 12.21,
                        "pb": 1.22,
                        "roe": 10.12,
                        "pct_chg": 1.52,
                        "source": "longport",
                        "updated_at": "2026-02-11T06:03:47Z",
                    }
                ],
                "total": 1,
                "error": None,
                "meta": {
                    "data_source_desc": "external",
                    "latest_updated_at": "2026-02-11T06:03:47Z",
                },
            }
        ],
    }


def test_screening_result_card_contains_source_and_updated_at():
    card = _build_screening_result_card(_sample_screen_result())
    contents = []
    for elem in card.get("elements", []):
        if elem.get("tag") != "div":
            continue
        text_obj = elem.get("text") or {}
        if text_obj.get("tag") == "lark_md":
            contents.append(str(text_obj.get("content") or ""))
    merged = "\n".join(contents)
    assert "数据来源" in merged
    assert "最新更新时间" in merged
    assert "来源：" in merged
    assert "更新时间：" in merged


def test_screening_result_text_contains_source_and_updated_at():
    text = _build_screening_result_text(_sample_screen_result())
    assert "数据来源: external" in text
    assert "最新更新时间:" in text
    assert "来源:longport 更新时间:" in text

