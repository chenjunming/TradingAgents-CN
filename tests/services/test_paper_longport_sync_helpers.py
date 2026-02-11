from app.routers.paper import _longport_market_to_internal, _normalize_longport_symbol


def test_normalize_longport_symbol():
    assert _normalize_longport_symbol("600000.SH", "CN") == "600000"
    assert _normalize_longport_symbol("000001.SZ", "CN") == "000001"
    assert _normalize_longport_symbol("700.HK", "HK") == "00700"
    assert _normalize_longport_symbol("AAPL.US", "US") == "AAPL"


def test_longport_market_to_internal_from_symbol_suffix():
    assert _longport_market_to_internal(None, "700.HK") == "HK"
    assert _longport_market_to_internal(None, "AAPL.US") == "US"
    assert _longport_market_to_internal(None, "600000.SH") == "CN"

