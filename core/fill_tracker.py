"""
PolyBuk - Fill Tracker

Polls Polymarket for EXECUTED trades (fills) and logs them to
polybuk.trades. This is the ONLY path that writes to the trades table
in live mode.

Why this module exists: order_manager.place_order used to call
journal.log_trade on every successful placement — but Polymarket
replies with status='live' on placement, which only means the order
is resting in the book. It hasn't matched yet. Logging on placement
inflated the volume KPI with ghost rows (1,266 of them in 24h, none
representing real fills).

The fill tracker queries Polymarket for actual matches, deduplicates
against what we've already logged, and inserts only real fills.

Usage:
    from core.fill_tracker import fill_tracker
    await fill_tracker.poll_and_log()   # one-shot
    # OR (from main.py) run fill_tracker.loop() as a background task.
"""

import asyncio
import logging
from typing import Any

from core.journal import journal
from core.polymarket_client import polymarket_client

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


class FillTracker:
    """Polls Polymarket for executed trades and logs new ones to Supabase."""

    def __init__(self):
        self._seen_trade_ids: set[str] = set()
        self._bootstrapped: bool = False

    def _bootstrap_from_db(self) -> None:
        """Load already-logged trade IDs from polybuk.trades on startup.

        Prevents re-logging fills across bot restarts. We store the
        Polymarket trade_id as execution_time_ms=None + a context — but
        cleaner: keep trade_id in the market_category field? No —
        better: rely on Polymarket's dedup and just query recent fills.

        Current approach: pull last 500 trade rows from Supabase and
        extract the order_id we recorded. On next poll, skip any fill
        whose trade_id matches what's already stored.

        We store trade_id in the 'market_category' column via a prefix
        trick — actually no, that's hacky. Simplest robust path:
        on startup, query our own data-api trades once, put every ID
        into _seen_trade_ids without re-inserting. From there onward
        dedup within the set.
        """
        try:
            fills = polymarket_client.get_trades() or []
            for f in fills:
                tid = _trade_id(f)
                if tid:
                    self._seen_trade_ids.add(tid)
            logger.info(
                f"Fill tracker bootstrapped: {len(self._seen_trade_ids)} "
                f"existing fills marked as already-seen"
            )
        except Exception as e:
            logger.warning(f"Fill tracker bootstrap failed (safe to continue): {e}")
        self._bootstrapped = True

    def poll_and_log(self) -> int:
        """One-shot: fetch fills, log new ones, return number logged.

        Safe to call from async or sync contexts (no awaits internally).
        """
        if not self._bootstrapped:
            self._bootstrap_from_db()

        try:
            fills = polymarket_client.get_trades() or []
        except Exception as e:
            logger.error(f"Fill poll failed: {e}")
            return 0

        new_count = 0
        for f in fills:
            tid = _trade_id(f)
            if not tid or tid in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(tid)

            side = str(f.get("side") or "").upper()
            size = _as_int(f.get("size"))
            price = _as_float(f.get("price"))
            asset_id = str(f.get("asset_id") or f.get("market") or "")
            if not (side and size and price and asset_id):
                logger.warning(f"Skipping malformed fill: {f}")
                continue

            row = journal.log_trade(
                strategy="market_maker",
                market_id=asset_id,
                side=side,
                price=price,
                quantity=size,
                pool="mm_pool",
            )
            if row:
                new_count += 1
                logger.info(
                    f"FILL logged: {side} {size} @ ${price:.4f} on "
                    f"{asset_id[:16]}... trade_id={tid[:12]}..."
                )

        if new_count:
            logger.info(f"Fill tracker: {new_count} new fill(s) logged")
        return new_count

    async def loop(self) -> None:
        """Run the poll loop indefinitely. Spawn as an asyncio task."""
        logger.info(f"Fill tracker loop started (every {POLL_INTERVAL_SECONDS}s)")
        while True:
            try:
                self.poll_and_log()
            except Exception as e:
                logger.error(f"Fill tracker loop error: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _trade_id(fill: dict[str, Any]) -> str:
    """Extract a stable unique identifier for a Polymarket fill."""
    for key in ("trade_id", "id", "match_hash", "transaction_hash"):
        v = fill.get(key)
        if v:
            return str(v)
    return ""


def _as_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


fill_tracker = FillTracker()
