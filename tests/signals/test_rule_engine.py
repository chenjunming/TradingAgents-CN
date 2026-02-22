from __future__ import annotations

from app.services.signals.models import MarketSnapshot, RuleCondition, RuleDefinition
from app.services.signals.rule_engine import RuleEngine


def test_rule_engine_cross_and_confirm():
    snapshot = MarketSnapshot(
        ticker="NVDA",
        market="US",
        timeframe="1d",
        values={"close": 101.0, "ma50": 100.0, "rsi14": 58.0, "vol_ratio_20d": 1.8},
        prev_values={"close": 99.0, "ma50": 100.0, "rsi14": 52.0, "vol_ratio_20d": 1.2},
        series={"close": [95, 96, 97, 98, 99, 101]},
    )
    rule = RuleDefinition(
        rule_id="ma50_reclaim",
        name="站回MA50",
        markets=["US"],
        conditions=[
            RuleCondition(op="cross_above", left="close", right="ma50"),
            RuleCondition(op="gte", left="vol_ratio_20d", right=1.5),
        ],
        confirm=[RuleCondition(op="gte", left="rsi14", right=55)],
    )

    events = RuleEngine().evaluate(snapshot=snapshot, rules=[rule], ruleset_version_hash="sha256:test")

    assert len(events) == 1
    assert events[0].level == "confirm"
    assert events[0].rule_id == "ma50_reclaim"
