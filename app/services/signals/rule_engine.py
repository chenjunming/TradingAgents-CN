from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.services.signals.indicators import trend_slope
from app.services.signals.models import MarketSnapshot, RuleCondition, RuleDefinition, SignalCandidate


class RuleEngine:
    def _get_value(self, snapshot: MarketSnapshot, operand: Any, use_prev: bool = False) -> Optional[float]:
        if isinstance(operand, (int, float)):
            return float(operand)
        key = str(operand or "").strip()
        if not key:
            return None
        source = snapshot.prev_values if use_prev else snapshot.values
        value = source.get(key)
        if value is None:
            return None
        return float(value)

    def _eval_condition(self, snapshot: MarketSnapshot, cond: RuleCondition) -> Tuple[bool, str]:
        op = cond.op
        left = self._get_value(snapshot, cond.left)
        right = self._get_value(snapshot, cond.right)

        if op == "gte":
            return (left is not None and right is not None and left >= right, f"gte({cond.left},{cond.right})")
        if op == "lte":
            return (left is not None and right is not None and left <= right, f"lte({cond.left},{cond.right})")
        if op == "between":
            bounds = cond.right if isinstance(cond.right, (list, tuple)) else [None, None]
            low = float(bounds[0]) if len(bounds) > 0 and bounds[0] is not None else None
            high = float(bounds[1]) if len(bounds) > 1 and bounds[1] is not None else None
            ok = left is not None and low is not None and high is not None and low <= left <= high
            return ok, f"between({cond.left},{low},{high})"
        if op == "within_pct":
            pct = float(cond.params.get("pct", cond.params.get("tolerance_pct", 2.0)))
            if left is None or right is None or right == 0:
                return False, f"within_pct({cond.left},{cond.right},{pct})"
            delta = abs(left - right) / abs(right) * 100.0
            return delta <= pct, f"within_pct({cond.left},{cond.right},{pct})"
        if op == "cross_above":
            lp = self._get_value(snapshot, cond.left, use_prev=True)
            rp = self._get_value(snapshot, cond.right, use_prev=True)
            ok = lp is not None and rp is not None and left is not None and right is not None and lp <= rp and left > right
            return ok, f"cross_above({cond.left},{cond.right})"
        if op == "cross_below":
            lp = self._get_value(snapshot, cond.left, use_prev=True)
            rp = self._get_value(snapshot, cond.right, use_prev=True)
            ok = lp is not None and rp is not None and left is not None and right is not None and lp >= rp and left < right
            return ok, f"cross_below({cond.left},{cond.right})"
        if op == "new_high_n":
            n = int(cond.right or cond.params.get("n") or 20)
            series = snapshot.series.get(str(cond.left), [])
            if len(series) < n + 1:
                return False, f"new_high_n({cond.left},{n})"
            ok = series[-1] > max(series[-(n + 1):-1])
            return ok, f"new_high_n({cond.left},{n})"
        if op == "new_low_n":
            n = int(cond.right or cond.params.get("n") or 20)
            series = snapshot.series.get(str(cond.left), [])
            if len(series) < n + 1:
                return False, f"new_low_n({cond.left},{n})"
            ok = series[-1] < min(series[-(n + 1):-1])
            return ok, f"new_low_n({cond.left},{n})"
        if op == "trend_up":
            window = int(cond.right or cond.params.get("window") or 5)
            series = snapshot.series.get(str(cond.left), [])
            slope = trend_slope(series, window)
            return slope is not None and slope > 0, f"trend_up({cond.left},{window})"
        if op == "trend_down":
            window = int(cond.right or cond.params.get("window") or 5)
            series = snapshot.series.get(str(cond.left), [])
            slope = trend_slope(series, window)
            return slope is not None and slope < 0, f"trend_down({cond.left},{window})"

        return False, f"unsupported_op({op})"

    def _match_all(self, snapshot: MarketSnapshot, conditions: List[RuleCondition]) -> Tuple[bool, List[str], List[str]]:
        matched: List[str] = []
        failed: List[str] = []
        for cond in conditions:
            ok, expr = self._eval_condition(snapshot, cond)
            if ok:
                matched.append(expr)
            else:
                failed.append(expr)
        return len(failed) == 0, matched, failed

    def evaluate(self, snapshot: MarketSnapshot, rules: List[RuleDefinition], ruleset_version_hash: str) -> List[SignalCandidate]:
        result: List[SignalCandidate] = []
        for rule in rules:
            if rule.timeframe != snapshot.timeframe:
                continue
            if rule.markets and snapshot.market not in rule.markets:
                continue

            cond_ok, cond_matched, cond_failed = self._match_all(snapshot, rule.conditions)
            if not cond_ok:
                continue

            level = rule.level_on_match
            matched_conditions = list(cond_matched)
            conflicts = list(cond_failed)

            if rule.risk:
                risk_ok, risk_matched, _ = self._match_all(snapshot, rule.risk)
                if risk_ok:
                    level = "risk"
                    matched_conditions.extend(risk_matched)

            if level != "risk" and rule.confirm:
                confirm_ok, confirm_matched, confirm_failed = self._match_all(snapshot, rule.confirm)
                if confirm_ok:
                    level = "confirm"
                    matched_conditions.extend(confirm_matched)
                else:
                    conflicts.extend(confirm_failed)

            result.append(
                SignalCandidate(
                    ticker=snapshot.ticker,
                    market=snapshot.market,
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    level=level,
                    snapshot={
                        "price": snapshot.values.get("close"),
                        "ma20": snapshot.values.get("ma20"),
                        "ma50": snapshot.values.get("ma50"),
                        "ma100": snapshot.values.get("ma100"),
                        "rsi14": snapshot.values.get("rsi14"),
                        "vwap": snapshot.values.get("vwap"),
                        "vol_ratio_20d": snapshot.values.get("vol_ratio_20d"),
                    },
                    evidence={
                        "matched_conditions": matched_conditions,
                        "conflicts": conflicts,
                    },
                    ruleset_version_hash=ruleset_version_hash,
                    action_hint=rule.action_hint,
                    cooldown_minutes=rule.cooldown_minutes,
                )
            )

        return result
