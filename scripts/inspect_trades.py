"""Disposable inspection script — NOT committed.

Fetches recent trades from Polymarket data-api to inspect the real response
shape, so we can design the Bug 2 fix (NULL fields in polybuk.trades) against
real data instead of guesses.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings


ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def redact(value):
    """Redact ethereum addresses and long hashes inside any nested value."""
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


def fetch(url, params):
    qs = urllib.parse.urlencode(params)
    full = f"{url}?{qs}"
    print(f"GET {url}  params={params}")
    req = urllib.request.Request(full, headers={"User-Agent": "PolyBuk-Inspector/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"  HTTP {resp.status}")
        body = resp.read().decode("utf-8")
    return json.loads(body)


def analyze(trades):
    print(f"\nTotal trades returned: {len(trades)}")
    if not trades:
        return

    first = trades[0]
    print(f"\nKeys found (alphabetical): {sorted(first.keys())}")

    print("\n--- First trade (redacted) ---")
    print(json.dumps(redact(first), indent=2, default=str))

    print("\n--- Aggregate stats across returned trades ---")
    sides = Counter(t.get("side") for t in trades)
    print(f"side values: {dict(sides)}")

    for field in ("size", "matched_amount", "amount", "shares"):
        s = stat_field(trades, field)
        if s:
            print(f"{field}: {s}")

    for field in ("price", "usdc_amount", "usd_value"):
        s = stat_field(trades, field)
        if s:
            print(f"{field}: {s}")

    all_keys = set()
    for t in trades:
        all_keys.update(t.keys())
    always_present = sorted(k for k in all_keys if all(k in t for t in trades))
    optional = sorted(k for k in all_keys if k not in always_present)
    print(f"\nKeys present in ALL {len(trades)} trades: {always_present}")
    print(f"Optional keys (missing in at least one): {optional}")


def main():
    funder = settings.polymarket.funder_address
    if not funder:
        print("ERROR: POLYMARKET_FUNDER_ADDRESS not set in .env")
        sys.exit(1)

    print(f"Funder: {funder[:6]}...{funder[-4:]}")

    try:
        data = fetch(
            "https://data-api.polymarket.com/activity",
            {"user": funder, "type": "TRADE", "limit": 10},
        )
    except Exception as e:
        print(f"  ERROR on /activity: {type(e).__name__}: {e}")
        data = None

    trades = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])

    if not trades:
        print("\nPrimary endpoint returned empty/error. Trying fallback /trades...")
        try:
            data2 = fetch(
                "https://data-api.polymarket.com/trades",
                {"user": funder, "limit": 10},
            )
            trades = data2 if isinstance(data2, list) else (data2.get("data") if isinstance(data2, dict) else [])
            print("Fallback /trades responded.")
        except Exception as e:
            print(f"  ERROR on /trades: {type(e).__name__}: {e}")
            trades = []
    else:
        print("\nPrimary endpoint /activity worked.")

    analyze(trades)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"TOP-LEVEL ERROR: {type(e).__name__}: {e}")
