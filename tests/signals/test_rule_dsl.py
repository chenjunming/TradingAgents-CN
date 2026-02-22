from __future__ import annotations

from pathlib import Path

from app.services.signals.rule_dsl import load_ruleset_from_glob


def test_load_ruleset_from_yaml(tmp_path: Path):
    rule_file = tmp_path / "rules.yaml"
    rule_file.write_text(
        """
rules:
  - id: sample_rule
    name: Sample
    timeframe: 1d
    applies_to:
      markets: [CN, US]
    conditions:
      - op: gte
        left: rsi14
        right: 50
""".strip(),
        encoding="utf-8",
    )

    ruleset = load_ruleset_from_glob(str(rule_file))

    assert ruleset.version_hash.startswith("sha256:")
    assert len(ruleset.rules) == 1
    rule = ruleset.rules[0]
    assert rule.rule_id == "sample_rule"
    assert set(rule.markets) == {"CN", "US"}
