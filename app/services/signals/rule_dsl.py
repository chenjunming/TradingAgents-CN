from __future__ import annotations

import glob
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from app.services.signals.models import RuleCondition, RuleDefinition, RuleSet


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except Exception as exc:
        raise RuntimeError("PyYAML is required for signals rules. Please install PyYAML>=6.0") from exc
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_condition(raw: Dict[str, Any]) -> RuleCondition:
    if not isinstance(raw, dict):
        raise ValueError(f"invalid condition type: {type(raw)}")
    op = str(raw.get("op") or "").strip()
    if not op:
        raise ValueError("condition op is required")
    left = raw.get("left")
    right = raw.get("right")
    params = {k: v for k, v in raw.items() if k not in {"op", "left", "right"}}
    return RuleCondition(op=op, left=left, right=right, params=params)


def _normalize_market(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in {"CN", "A股", "CHINA_A", "CHINA"}:
        return "CN"
    if s in {"HK", "港股", "HONG_KONG", "HONGKONG"}:
        return "HK"
    if s in {"US", "USA", "美股"}:
        return "US"
    return s


def _parse_rule(raw: Dict[str, Any]) -> RuleDefinition:
    if not isinstance(raw, dict):
        raise ValueError("rule must be object")
    rule_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or rule_id).strip()
    if not rule_id:
        raise ValueError("rule id is required")

    applies = raw.get("applies_to") or {}
    markets = [_normalize_market(x) for x in (applies.get("markets") or []) if str(x).strip()]
    if not markets:
        markets = ["CN", "HK", "US"]

    conditions = [_parse_condition(x) for x in (raw.get("conditions") or [])]
    if not conditions:
        raise ValueError(f"rule {rule_id} must provide conditions")

    confirm = [_parse_condition(x) for x in (raw.get("confirm") or [])]
    risk = [_parse_condition(x) for x in (raw.get("risk") or [])]

    notify = raw.get("notify") or {}
    level_on_match = str(raw.get("level_on_match") or "prewarn").strip().lower()
    if level_on_match not in {"prewarn", "confirm", "risk"}:
        level_on_match = "prewarn"

    return RuleDefinition(
        rule_id=rule_id,
        name=name,
        timeframe=str(raw.get("timeframe") or "1d").strip(),
        markets=markets,
        conditions=conditions,
        confirm=confirm,
        risk=risk,
        cooldown_minutes=int(raw.get("cooldown_minutes") or 120),
        level_on_match=level_on_match,
        action_hint=str(raw.get("action_hint") or "").strip(),
        notify_template=str(notify.get("template") or "default_signal_card"),
    )


def _canonical_rules_payload(raw_rules: List[Dict[str, Any]]) -> str:
    normalized = json.loads(json.dumps(raw_rules, ensure_ascii=False, sort_keys=True))
    normalized = sorted(normalized, key=lambda x: str(x.get("id") or ""))
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_ruleset_from_glob(pattern: str = "rules/*.yaml") -> RuleSet:
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no rule files matched: {pattern}")

    raw_rules: List[Dict[str, Any]] = []
    for f in files:
        path = Path(f)
        if path.suffix.lower() in {".yaml", ".yml"}:
            data = _load_yaml(path)
        elif path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            continue

        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            raw_rules.extend(data["rules"])
        elif isinstance(data, list):
            raw_rules.extend(data)
        elif isinstance(data, dict):
            raw_rules.append(data)

    if not raw_rules:
        raise ValueError(f"no valid rules loaded from: {pattern}")

    rules = [_parse_rule(x) for x in raw_rules]
    payload = _canonical_rules_payload(raw_rules)
    version_hash = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return RuleSet(version_hash=version_hash, rules=rules, raw_rules=raw_rules)
