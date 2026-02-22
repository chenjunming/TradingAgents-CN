from __future__ import annotations

import asyncio

from app.services.signals.models import RuleSet, RuleDefinition, RuleCondition, SignalCandidate
from app.services.signals.scanner import SignalScanner


class _FakeStore:
    def __init__(self):
        self.saved_events = []

    async def ensure_indexes(self):
        return None

    async def save_ruleset(self, version_hash, rules):
        return None

    async def save_event(self, event):
        self.saved_events.append(event)

    async def get_active_mute(self, user_id, ticker, rule_id):
        return None

    async def latest_event_by_dedupe_key(self, user_id, dedupe_key):
        return None


class _FakeNotifier:
    async def send_feishu_card(self, event):
        return {"success": False, "reason": "test"}


class _FakeDataSource:
    def get_kline_with_fallback(self, code, period="day", limit=220):
        items = []
        for i in range(1, 80):
            items.append({"time": f"2026-01-{i:02d}", "close": 100 + i, "volume": 100000 + i * 100})
        return items, "fake"


class _FakeForeignStockService:
    def __init__(self):
        self.kline_calls = []
        self.quote_calls = []

    async def get_quote(self, market: str, code: str, force_refresh: bool = False):
        self.quote_calls.append((market, code, force_refresh))
        return {"price": 180.0}

    async def get_kline(self, market: str, code: str, period: str = "day", limit: int = 120, force_refresh: bool = False):
        self.kline_calls.append((market, code, period, limit, force_refresh))
        items = []
        for i in range(1, 90):
            items.append(
                {
                    "trade_date": f"2026-01-{(i % 28) + 1:02d}",
                    "close": 16.0 + i * 0.02,
                    "volume": 100000 + i * 200,
                }
            )
        return items[-limit:]


def test_scanner_scan_once_emits(monkeypatch):
    scanner = SignalScanner()
    scanner.store = _FakeStore()
    from app.services.signals.dedupe import SignalDedupe

    scanner.dedupe = SignalDedupe(scanner.store)
    scanner.notifier = _FakeNotifier()
    fake_foreign = _FakeForeignStockService()

    async def _targets(user_id, override_tickers=None):
        return [("NVDA", "US")]

    monkeypatch.setattr(scanner, "_collect_targets", _targets)
    monkeypatch.setattr(scanner, "_get_foreign_stock_service", lambda: fake_foreign)
    monkeypatch.setattr("app.services.signals.scanner.settings.SIGNALS_RESPECT_MARKET_HOURS", False)

    def _fake_load(_):
        return RuleSet(
            version_hash="sha256:test",
            rules=[
                RuleDefinition(
                    rule_id="r1",
                    name="r1",
                    markets=["US"],
                    conditions=[RuleCondition(op="gte", left="close", right=100)],
                )
            ],
            raw_rules=[{"id": "r1"}],
        )

    monkeypatch.setattr("app.services.signals.scanner.load_ruleset_from_glob", _fake_load)

    result = asyncio.run(scanner.scan_once(user_id="default", rules_glob="dummy"))

    assert result["emitted"] == 1
    assert scanner.store.saved_events
    assert fake_foreign.kline_calls
    assert fake_foreign.kline_calls[0][0] == "US"
    assert fake_foreign.kline_calls[0][1] == "NVDA"


def test_scanner_hk_uses_foreign_kline(monkeypatch):
    scanner = SignalScanner()
    scanner.store = _FakeStore()
    from app.services.signals.dedupe import SignalDedupe

    scanner.dedupe = SignalDedupe(scanner.store)
    scanner.notifier = _FakeNotifier()
    scanner.data_source = _FakeDataSource()
    fake_foreign = _FakeForeignStockService()

    async def _targets(user_id, override_tickers=None):
        return [("00883", "HK")]

    monkeypatch.setattr(scanner, "_collect_targets", _targets)
    monkeypatch.setattr(scanner, "_get_foreign_stock_service", lambda: fake_foreign)
    monkeypatch.setattr("app.services.signals.scanner.settings.SIGNALS_RESPECT_MARKET_HOURS", False)

    def _fake_load(_):
        return RuleSet(
            version_hash="sha256:test",
            rules=[
                RuleDefinition(
                    rule_id="hk_r1",
                    name="hk_r1",
                    markets=["HK"],
                    conditions=[RuleCondition(op="gte", left="close", right=10)],
                )
            ],
            raw_rules=[{"id": "hk_r1"}],
        )

    monkeypatch.setattr("app.services.signals.scanner.load_ruleset_from_glob", _fake_load)

    result = asyncio.run(scanner.scan_once(user_id="default", rules_glob="dummy"))

    assert result["emitted"] == 1
    assert fake_foreign.kline_calls
    assert fake_foreign.kline_calls[0][0] == "HK"
    assert fake_foreign.kline_calls[0][1] == "00883"
    assert scanner.store.saved_events[0]["ticker"] == "00883"
    assert scanner.store.saved_events[0]["market"] == "HK"


def test_scanner_skips_when_market_closed(monkeypatch):
    scanner = SignalScanner()
    scanner.store = _FakeStore()
    from app.services.signals.dedupe import SignalDedupe

    scanner.dedupe = SignalDedupe(scanner.store)
    scanner.notifier = _FakeNotifier()
    fake_foreign = _FakeForeignStockService()

    async def _targets(user_id, override_tickers=None):
        return [("NVDA", "US")]

    monkeypatch.setattr(scanner, "_collect_targets", _targets)
    monkeypatch.setattr(scanner, "_get_foreign_stock_service", lambda: fake_foreign)
    monkeypatch.setattr("app.services.signals.scanner.settings.SIGNALS_RESPECT_MARKET_HOURS", True)
    monkeypatch.setattr("app.services.signals.scanner.market_calendar_service.is_market_open", lambda market: False)

    def _fake_load(_):
        return RuleSet(
            version_hash="sha256:test",
            rules=[
                RuleDefinition(
                    rule_id="r1",
                    name="r1",
                    markets=["US"],
                    conditions=[RuleCondition(op="gte", left="close", right=100)],
                )
            ],
            raw_rules=[{"id": "r1"}],
        )

    monkeypatch.setattr("app.services.signals.scanner.load_ruleset_from_glob", _fake_load)

    result = asyncio.run(scanner.scan_once(user_id="default", rules_glob="dummy"))

    assert result["emitted"] == 0
    assert result["skipped_market_closed"] == 1
    assert not scanner.store.saved_events
    assert not fake_foreign.kline_calls
