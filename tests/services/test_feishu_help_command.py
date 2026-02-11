from app.routers.feishu_bot import _help_text


def test_help_text_contains_core_commands():
    text = _help_text()
    assert "/help" in text
    assert "/daily" in text
    assert "/wl add" in text
    assert "/confirm" in text
