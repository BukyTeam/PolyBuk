"""
PolyBuk — Daily Report

Computes yesterday's trading stats and the cumulative volume KPI, then:
  - Prints a summary to stdout (for cron log capture / manual review)
  - Sends the report to Telegram via alerts.send_daily_report

Usage:
    python scripts/daily_report.py              # Report for yesterday (UTC)
    python scripts/daily_report.py --date 2026-04-13   # Report for a specific day

The volume KPI (cumulative / $10,000 target) is the project's #1 metric.
It is always derived from polybuk.trades via journal.get_volume_progress(),
so every reporting path (hourly summary, /status, this script) shows the
same number from the same source of truth.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.alerts import alerts
from core.journal import journal
from core.supabase_client import db


def _parse_date(value: str | None) -> datetime:
    """Return UTC midnight of the requested date (default: yesterday)."""
    if value:
        d = datetime.strptime(value, "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _trades_in_window(start: datetime, end: datetime) -> list[dict]:
    """Fetch trades with created_at in [start, end)."""
    try:
        resp = (
            db._client.table("trades")
            .select("strategy, side, price, quantity, notional_value, created_at")
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .execute()
        )
        return resp.data or []
    except Exception as e:
        print(f"ERROR fetching trades: {e}", file=sys.stderr)
        return []


def _pnl_cumulative() -> float:
    """Sum realized P&L recorded in decisions (context.pnl on position_closed).

    NC position closures emit a decisions row with action='position_closed'
    and context.pnl. MM realized P&L is not logged as a single number per
    cycle, so this is primarily NC P&L — good enough as a quick indicator
    until a dedicated pnl table is added.
    """
    try:
        resp = (
            db._client.table("decisions")
            .select("context")
            .eq("action", "position_closed")
            .execute()
        )
        total = 0.0
        for row in resp.data or []:
            ctx = row.get("context") or {}
            if isinstance(ctx, dict):
                total += float(ctx.get("pnl", 0) or 0)
        return round(total, 4)
    except Exception:
        return 0.0


async def run(date_arg: str | None) -> int:
    start = _parse_date(date_arg)
    end = start + timedelta(days=1)
    label = start.strftime("%Y-%m-%d")

    if not db.initialize():
        print("ERROR: Supabase init failed. Check .env", file=sys.stderr)
        return 1

    trades = _trades_in_window(start, end)

    volume_today = round(
        sum(float(t.get("notional_value") or 0) for t in trades), 2
    )
    mm_trades = sum(1 for t in trades if t.get("strategy") == "market_maker")
    nc_trades = sum(1 for t in trades if t.get("strategy") == "near_certainties")

    progress = journal.get_volume_progress()
    pnl_cum = _pnl_cumulative()

    # --- Console output ---
    print(f"\n=== PolyBuk Daily Report — {label} ===")
    print(journal.format_volume_progress(progress))
    print(f"Volumen del día: ${volume_today:,.2f}")
    print(f"Trades: {len(trades)} ({mm_trades} MM, {nc_trades} NC)")
    print(f"P&L acumulado (realized NC): ${pnl_cum:+,.2f}")
    print()

    # --- Telegram ---
    if not alerts.initialize():
        print("Telegram disabled (no credentials) — skipping send.")
        return 0

    await alerts.send_daily_report(
        date=label,
        volume_today=volume_today,
        pnl_today=0.0,  # Daily P&L requires per-cycle snapshots; not tracked yet
        pnl_cumulative=pnl_cum,
        trades_count=len(trades),
        mm_trades=mm_trades,
        nc_trades=nc_trades,
        avg_spread=0.0,  # Derivable from orderbook_snapshots; not tracked here
    )
    print("Report sent to Telegram.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="PolyBuk daily report")
    parser.add_argument(
        "--date",
        help="Report date YYYY-MM-DD (UTC). Defaults to yesterday.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.date)))


if __name__ == "__main__":
    main()
