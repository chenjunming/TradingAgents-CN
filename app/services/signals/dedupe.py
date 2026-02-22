from __future__ import annotations

from datetime import datetime, timedelta

from app.services.signals.models import DedupeDecision
from app.services.signals.store import SignalStore


class SignalDedupe:
    def __init__(self, store: SignalStore):
        self.store = store

    async def should_emit(
        self,
        user_id: str,
        ticker: str,
        rule_id: str,
        level: str,
        cooldown_minutes: int,
        force: bool = False,
    ) -> DedupeDecision:
        if force:
            return DedupeDecision(emit=True, reason="force")

        active_mute = await self.store.get_active_mute(user_id=user_id, ticker=ticker, rule_id=rule_id)
        if active_mute:
            return DedupeDecision(
                emit=False,
                reason="muted",
                cooldown_until=active_mute.get("mute_until"),
            )

        dedupe_key = f"{ticker.upper()}|{rule_id}|{level}"
        last = await self.store.latest_event_by_dedupe_key(user_id=user_id, dedupe_key=dedupe_key)
        now = datetime.utcnow()
        cooldown_until = now + timedelta(minutes=max(1, int(cooldown_minutes)))

        if not last:
            return DedupeDecision(emit=True, reason="new", cooldown_until=cooldown_until)

        prev_until = last.get("cooldown_until")
        if isinstance(prev_until, datetime) and now < prev_until:
            return DedupeDecision(emit=False, reason="cooldown", cooldown_until=prev_until)

        return DedupeDecision(emit=True, reason="cooldown_expired", cooldown_until=cooldown_until)
