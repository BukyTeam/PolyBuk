"""
PolyBuk — One-shot cleanup of paper_trade=True rows in Supabase.

Paper trading was removed from the codebase on 2026-04-13 because it
never exercised Polymarket's real order API (the geoblock, proxy wallet,
and signature-type issues were all invisible in paper mode). The rows
left behind in polybuk.trades and polybuk.decisions from that era add
noise to every query and inflate the volume KPI if anything forgot to
filter them.

This script does a DESTRUCTIVE DELETE. Run once, confirm counts, done.

Usage:
    python scripts/cleanup_paper_trades.py          # prints counts, asks confirmation
    python scripts/cleanup_paper_trades.py --yes    # skip confirmation prompt
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.supabase_client import db

# Tables we know carry paper_trade. If future migrations add more, extend here.
PAPER_TABLES = ("trades", "decisions")


def count_paper(table: str) -> int:
    """Return the count of paper_trade=True rows in a table."""
    try:
        resp = (
            db._client.table(table)
            .select("id", count="exact")
            .eq("paper_trade", True)
            .execute()
        )
        return resp.count or 0
    except Exception as e:
        print(f"  ERROR counting {table}: {e}")
        return -1


def delete_paper(table: str) -> int:
    """Delete all paper_trade=True rows. Returns number deleted."""
    try:
        resp = (
            db._client.table(table)
            .delete()
            .eq("paper_trade", True)
            .execute()
        )
        return len(resp.data or [])
    except Exception as e:
        print(f"  ERROR deleting from {table}: {e}")
        return -1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args()

    if not db.initialize():
        print("ERROR: Supabase init failed. Check .env")
        sys.exit(1)

    print("\n=== Paper-trade cleanup ===\n")
    print("Counting paper rows in each table...")

    counts = {}
    total = 0
    for t in PAPER_TABLES:
        n = count_paper(t)
        counts[t] = n
        print(f"  polybuk.{t}: {n} paper_trade=True rows")
        if n > 0:
            total += n

    if total == 0:
        print("\nNothing to delete. Done.\n")
        return

    print(f"\nTotal to delete: {total} rows across {len(PAPER_TABLES)} tables.")
    print("This is IRREVERSIBLE.")

    if not args.yes:
        ans = input("\nType 'DELETE' to proceed: ").strip()
        if ans != "DELETE":
            print("Aborted.")
            sys.exit(0)

    print("\nDeleting...")
    for t in PAPER_TABLES:
        if counts.get(t, 0) > 0:
            n = delete_paper(t)
            print(f"  polybuk.{t}: deleted {n} rows")

    print("\nVerifying...")
    for t in PAPER_TABLES:
        n = count_paper(t)
        status = "clean" if n == 0 else f"{n} rows remain"
        print(f"  polybuk.{t}: {status}")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
