"""CLOB SDK trades inspection — shows real shape returned by
polymarket_client.get_trades(), which is what fill_tracker actually consumes.

Run on the VPS where credentials are set:
    python scripts/inspect_clob_trades.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.polymarket_client import polymarket_client


ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def redact(value):
    if isinstance(value, str):
        if ADDR_RE.match(value):
            return "0x<REDACTED>"
        if value.startswith("0x") and len(value) > 50:
            return "0x<REDACTED>"
        if len(value) > 50 and re.match(r"^[a-fA-F0-9]+$", value):
            return "<REDACTED_HASH>"
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def stat_field(trades, field):
    vals = []
    for t in trades:
        v = t.get(field)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            pass
    if not vals:
        return None
    return {
        "count": len(vals),
        "min": min(vals),
        "max": max(vals),
        "avg": round(mean(vals), 6),
    }


def analyze(trades):
    print(f"\nTotal trades returned: {len(trades)}")
    if not trades:
        return

    sample = trades[:10]
    first = sample[0]

    if not isinstance(first, dict):
        print(f"First trade is not a dict: type={type(first).__name__}, value={first!r}")
        return

    print(f"\nKeys found (alphabetical): {sorted(first.keys())}")

    print("\n--- First trade (redacted) ---")
    print(json.dumps(redact(first), indent=2, default=str))

    print("\n--- Aggregate stats across first (up to) 10 trades ---")
    sides = Counter(t.get("side") for t in sample if isinstance(t, dict))
    print(f"side values: {dict(sides)}")

    for field in ("size", "matched_amount", "amount", "shares", "maker_amount_filled", "taker_amount_filled"):
        s = stat_field(sample, field)
        if s:
            print(f"{field}: {s}")

    for field in ("price", "usdc_amount", "usd_value"):
        s = stat_field(sample, field)
        if s:
            print(f"{field}: {s}")

    all_keys = set()
    for t in sample:
        if isinstance(t, dict):
            all_keys.update(t.keys())
    always_present = sorted(
        k for k in all_keys if all(isinstance(t, dict) and k in t for t in sample)
    )
    optional = sorted(k for k in all_keys if k not in always_present)
    print(f"\nKeys present in ALL {len(sample)} sampled trades: {always_present}")
    print(f"Optional keys (missing in at least one): {optional}")


def main():
    print("Initializing polymarket_client...")
    try:
        ok = polymarket_client.initialize()
    except Exception as e:
        print(f"ERROR during initialize(): {type(e).__name__}: {e}")
        return
    if not ok:
        print("ERROR: polymarket_client.initialize() returned falsy.")
        return
    print("Client initialized.")

    print("Calling polymarket_client.get_trades() (no args)...")
    try:
        resp = polymarket_client.get_trades()
    except Exception as e:
        print(f"ERROR during get_trades(): {type(e).__name__}: {e}")
        return

    print(f"Response type: {type(resp).__name__}")

    if resp is None:
        print("Response is None. Nothing to analyze.")
        return

    if not isinstance(resp, list):
        print(f"Response is not a list. Raw (truncated): {str(resp)[:500]}")
        return

    if not resp:
        print("Response is an empty list. No trades to show.")
        return

    analyze(resp)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"TOP-LEVEL ERROR: {type(e).__name__}: {e}")
