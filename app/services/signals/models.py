from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

SignalLevel = Literal["prewarn", "confirm", "risk"]


@dataclass(slots=True)
class RuleCondition:
    op: str
    left: Any
    right: Any = None
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuleDefinition:
    rule_id: str
    name: str
    timeframe: str = "1d"
    markets: List[str] = field(default_factory=list)
    conditions: List[RuleCondition] = field(default_factory=list)
    confirm: List[RuleCondition] = field(default_factory=list)
    risk: List[RuleCondition] = field(default_factory=list)
    cooldown_minutes: int = 120
    level_on_match: SignalLevel = "prewarn"
    action_hint: str = ""
    notify_template: str = "default_signal_card"


@dataclass(slots=True)
class RuleSet:
    version_hash: str
    rules: List[RuleDefinition]
    raw_rules: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class MarketSnapshot:
    ticker: str
    market: str
    timeframe: str
    values: Dict[str, Optional[float]]
    prev_values: Dict[str, Optional[float]]
    series: Dict[str, List[float]]


@dataclass(slots=True)
class SignalCandidate:
    ticker: str
    market: str
    rule_id: str
    rule_name: str
    level: SignalLevel
    snapshot: Dict[str, Optional[float]]
    evidence: Dict[str, Any]
    ruleset_version_hash: str
    action_hint: str = ""
    cooldown_minutes: int = 120


@dataclass(slots=True)
class DedupeDecision:
    emit: bool
    reason: str
    cooldown_until: Optional[datetime] = None


@dataclass(slots=True)
class ScanResult:
    scanned: int = 0
    matched: int = 0
    emitted: int = 0
    skipped_market_closed: int = 0
    skipped_dedupe: int = 0
    skipped_muted: int = 0
    failed: int = 0
    events: List[Dict[str, Any]] = field(default_factory=list)
