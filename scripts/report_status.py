"""
PolyBuk - Scheduled Status Report

One-shot script that sends a state snapshot to Telegram. Designed to be
invoked by cron 4x/day (09/12/15/18 Colombia time = 14/17/20/23 UTC).

What the report includes:
- Volume KPI progress (the #1 metric for the Referral Program goal)
- Cash + position values from Polymarket data-api (ground truth)
- Open orders count
- Fills in the last window (hours since previous report)
- P&L from the risk manager's running accounting
- Circuit breaker state

Usage (cron):
    0 14,17,20,23 * * * cd /home/polybuk/PolyBuk && \\
        /home/polybuk/PolyBuk/venv/bin/python scripts/report_status.py \\
        >> /home/polybuk/PolyBuk/logs/reports.log 2>&1

Manual test:
    python scripts/report_status.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from config.settings import settings
from core.alerts import alerts
from core.journal import journal
from core.polymarket_client import polymarket_client
from core.risk_manager import risk_manager
from core.supabase_client import db

# Colombia is UTC-5. Reports fire at 09/12/15/18 COT, so the window
# between consecutive reports is 3 hours (midnight-to-09 is the overnight
# window and will show everything since the previous day's 18:00 report).
REPORT_WINDOW_HOURS = 3


def _fetch_polymarket_balance() -> tuple[float, float]:
    """Returns (cash_usdc, position_value) from data-api."""
    funder = settings.polymarket.funder_address.strip()
    if not funder:
        return 0.0, 0.0
    try:
        r = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder},
            timeout=8.0,
        )
        r.raise_for_status()
        positions = r.json() or []
        position_value = sum(float(p.get("currentValue") or 0) for p in positions)
    except Exception:
        position_value = 0.0

    # On-chain USDC.e balance at the Safe
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org", request_kwargs={"timeout": 8}))
        abi = [{"constant": True, "inputs": [{"name": "", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
        usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        c = w3.eth.contract(address=Web3.to_checksum_address(usdc_e), abi=abi)
        cash = c.functions.balanceOf(Web3.to_checksum_address(funder)).call() / 1e6
    except Exception:
        cash = 0.0

    return round(cash, 2), round(position_value, 2)


def _fills_in_window(since: datetime) -> tuple[int, float]:
    """Returns (fill_count, fill_volume) from polybuk.trades since `since`."""
    try:
        resp = (
            db._client.table("trades")
            .select("notional_value")
            .gte("created_at", since.isoformat())
            .execute()
        )
        rows = resp.data or []
        vol = round(sum(float(r.get("notional_value") or 0) for r in rows), 2)
        return len(rows), vol
    except Exception:
        return 0, 0.0


async def build_and_send() -> None:
    if not db.initialize():
        print("ERROR: Supabase init failed", file=sys.stderr)
        sys.exit(1)
    if not polymarket_client.initialize():
        print("ERROR: Polymarket init failed", file=sys.stderr)
        sys.exit(1)
    if not alerts.initialize():
        print("WARN: Telegram not configured — would not send report", file=sys.stderr)
        return

    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(hours=REPORT_WINDOW_HOURS)

    # Data gathering
    progress = journal.get_volume_progress()
    cash, pos_value = _fetch_polymarket_balance()
    open_orders = polymarket_client.get_open_orders() or []
    fills_count, fills_vol = _fills_in_window(window_start)
    status = risk_manager.get_status()

    # Colombia local time for header
    cot = now_utc - timedelta(hours=5)
    header_time = cot.strftime("%Y-%m-%d %H:%M COT")

    # Circuit breaker flags
    flags = []
    if status.get("pool_paused", {}).get("mm_pool"):
        flags.append("MM PAUSADO")
    if status.get("pool_stopped", {}).get("mm_pool"):
        flags.append("MM DETENIDO")
    if status.get("all_stopped"):
        flags.append("ALL STOPPED")
    if status.get("kill_switch"):
        flags.append("KILL SWITCH")
    if status.get("api_paused"):
        flags.append("API PAUSED")

    lines = [
        f"Reporte PolyBuk — {header_time}",
        journal.format_volume_progress(progress),
        "",
        f"Cartera Polymarket: ${cash + pos_value:,.2f}",
        f"  Efectivo: ${cash:,.2f}   Posiciones: ${pos_value:,.2f}",
        f"Órdenes abiertas: {len(open_orders)}",
        "",
        f"Últimas {REPORT_WINDOW_HOURS}h:",
        f"  Fills reales: {fills_count}",
        f"  Volumen fills: ${fills_vol:,.2f}",
        "",
        f"P&L hoy MM: ${status.get('daily_pnl', {}).get('mm_pool', 0):+,.2f}",
        f"P&L total: ${status.get('total_pnl', 0):+,.2f}",
    ]
    if flags:
        lines.append("")
        lines.append("⚠  " + " | ".join(flags))

    message = "\n".join(lines)
    print(message)
    print()

    ok = await alerts.send_alert(message)
    print(f"Telegram delivery: {'OK' if ok else 'FAILED'}")


if __name__ == "__main__":
    asyncio.run(build_and_send())
