#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.signals.scanner import DEFAULT_SIGNALS_USER_ID, signal_scanner


async def _run(args):
    tickers = []
    if args.tickers:
        tickers = [x.strip() for x in args.tickers.split(",") if x.strip()]

    result = await signal_scanner.scan_once(
        user_id=args.user_id,
        force=args.force,
        rules_glob=args.rules,
        tickers_override=tickers or None,
    )

    print("=== Signal Scan Result ===")
    print(f"ruleset_version_hash: {result.get('ruleset_version_hash')}")
    print(f"scanned={result.get('scanned')} matched={result.get('matched')} emitted={result.get('emitted')}")
    print(
        "skipped_dedupe={} skipped_muted={} failed={}".format(
            result.get("skipped_dedupe"),
            result.get("skipped_muted"),
            result.get("failed"),
        )
    )

    for evt in result.get("events", []):
        print(
            "- {event_id} {ticker} {rule_id} {level} notify={notify}".format(
                event_id=evt.get("event_id"),
                ticker=evt.get("ticker"),
                rule_id=evt.get("rule_id"),
                level=evt.get("level"),
                notify=json.dumps(evt.get("notify", {}), ensure_ascii=False),
            )
        )


def main():
    parser = argparse.ArgumentParser(description="Run signal scanner once")
    parser.add_argument("--user-id", default=DEFAULT_SIGNALS_USER_ID)
    parser.add_argument("--rules", default="rules/*.yaml")
    parser.add_argument("--tickers", default="", help="comma separated tickers, optionally with market prefix like US:NVDA")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
