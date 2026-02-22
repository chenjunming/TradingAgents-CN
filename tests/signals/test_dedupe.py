from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.services.signals.dedupe import SignalDedupe


class _FakeStore:
    def __init__(self, mute=None, last=None):
        self._mute = mute
        self._last = last

    async def get_active_mute(self, user_id: str, ticker: str, rule_id: str):
        return self._mute

    async def latest_event_by_dedupe_key(self, user_id: str, dedupe_key: str):
        return self._last


def test_dedupe_blocks_in_cooldown():
    last = {"cooldown_until": datetime.utcnow() + timedelta(minutes=10)}
    dedupe = SignalDedupe(_FakeStore(last=last))

    decision = asyncio.run(dedupe.should_emit("u", "NVDA", "r1", "prewarn", 120))

    assert decision.emit is False
    assert decision.reason == "cooldown"


def test_dedupe_blocks_when_muted():
    mute = {"mute_until": datetime.utcnow() + timedelta(days=1)}
    dedupe = SignalDedupe(_FakeStore(mute=mute))

    decision = asyncio.run(dedupe.should_emit("u", "NVDA", "r1", "prewarn", 120))

    assert decision.emit is False
    assert decision.reason == "muted"
