"""
PolyBuk - Journal System

Records every action the bot takes into Supabase. This is the bot's
"flight recorder" — if something goes wrong, you can trace exactly
what happened and why.

Every function returns the inserted row (dict) or None on failure.
Failures are logged but never crash the bot — trading continues
even if logging temporarily fails.

Usage:
    from core.journal import journal
    journal.log_trade(strategy="market_maker", market_id="0x...", ...)
"""

import logging
import time
from typing import Any

import httpx

from config.settings import settings
from core.supabase_client import db

POLYMARKET_ACTIVITY_URL = "https://data-api.polymarket.com/activity"
VOLUME_CACHE_TTL_SECONDS = 60.0

logger = logging.getLogger(__name__)


class Journal:
    """Centralized logging to Supabase tables."""

    # ================================================================
    # polybuk.trades — Every executed order
    # ================================================================

    def log_trade(
        self,
        strategy: str,
        market_id: str,
        side: str,
        price: float,
        quantity: float,
        pool: str,
        market_name: str | None = None,
        market_category: str | None = None,
        order_type: str = "LIMIT",
        trader_side: str | None = None,
        fee_rate_bps: float | None = None,
        maker_rebate: float | None = None,
        fee_paid: float | None = None,
        execution_time_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Log an executed trade.

        Called by fill_tracker after a real fill is detected on Polymarket.
        The notional_value (price x quantity) is calculated automatically.

        Args:
            strategy: "market_maker" or "near_certainties"
            market_id: CLOB token_id
            side: "BUY" or "SELL"
            price: Execution price
            quantity: Number of contracts (decimal allowed)
            pool: "mm_pool" or "nc_pool"
            trader_side: "MAKER" or "TAKER" — our role in the fill
            fee_rate_bps: Raw fee rate from CLOB SDK in basis points
        """
        data = {
            "strategy": strategy,
            "market_id": market_id,
            "market_name": market_name,
            "market_category": market_category,
            "side": side,
            "price": price,
            "quantity": quantity,
            "notional_value": round(price * quantity, 6),
            "order_type": order_type,
            "trader_side": trader_side,
            "fee_rate_bps": fee_rate_bps,
            "maker_rebate": maker_rebate,
            "fee_paid": fee_paid,
            "execution_time_ms": execution_time_ms,
            "pool": pool,
        }
        row = db.insert("trades", data)
        if row:
            logger.info(
                f"Trade logged: {strategy} {side} {quantity}x @ ${price:.4f} ({pool})"
            )
        return row

    # ================================================================
    # polybuk.decisions — Justification for every action
    # ================================================================

    def log_decision(
        self,
        strategy: str,
        market_id: str,
        action: str,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Log a decision with its justification.

        EVERY action the bot takes must have a reason recorded here.
        This is a hard requirement from the spec (section 10).

        Args:
            action: What the bot did, e.g. "place_bid", "cancel_stale",
                    "skip_wide_spread", "activate_circuit_breaker"
            reason: Human-readable explanation, e.g.
                    "Spread $0.08 within range, mid=$0.45, inventory=+10"
            context: Optional JSON with raw numbers for analysis
        """
        data = {
            "strategy": strategy,
            "market_id": market_id,
            "action": action,
            "reason": reason,
            "context": context,
        }
        row = db.insert("decisions", data)
        if row:
            logger.debug(f"Decision logged: {strategy} {action} — {reason}")
        return row

    # ================================================================
    # polybuk.rejected_opportunities — What the bot DIDN'T do
    # ================================================================

    def log_rejected(
        self,
        strategy: str,
        market_id: str,
        reason: str,
        market_name: str | None = None,
        opportunity_type: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Log an opportunity the bot chose NOT to take.

        This is gold for analysis: if the bot rejects too many good
        opportunities, the parameters need adjustment. If it correctly
        rejects bad ones, the filters are working.

        Args:
            opportunity_type: e.g. "mm_spread", "nc_high_prob"
            reason: Why it was rejected, e.g. "spread too tight ($0.01)",
                    "NC pool at max positions (3/3)"
            details: Raw market data at the time of rejection
        """
        data = {
            "strategy": strategy,
            "market_id": market_id,
            "market_name": market_name,
            "opportunity_type": opportunity_type,
            "details": details,
            "reason": reason,
        }
        row = db.insert("rejected_opportunities", data)
        if row:
            logger.debug(f"Rejected opportunity: {strategy} — {reason}")
        return row

    # ================================================================
    # polybuk.orderbook_snapshots — Order book state each cycle
    # ================================================================

    def log_snapshot(
        self,
        market_id: str,
        best_bid: float | None = None,
        best_ask: float | None = None,
        mid_price: float | None = None,
        spread: float | None = None,
        bid_depth_5: float | None = None,
        ask_depth_5: float | None = None,
        full_book: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Log a snapshot of the order book.

        Taken every MM cycle (30 seconds). Used to analyze market
        conditions and verify the bot's price calculations.

        bid_depth_5 / ask_depth_5: Total USDC in the top 5 price levels.
        Measures how much liquidity is available near the best price.
        """
        data = {
            "market_id": market_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "spread": spread,
            "bid_depth_5": bid_depth_5,
            "ask_depth_5": ask_depth_5,
            "full_book": full_book,
        }
        row = db.insert("orderbook_snapshots", data)
        if row:
            logger.debug(
                f"Snapshot: {market_id[:16]}... bid=${best_bid} ask=${best_ask} "
                f"spread=${spread}"
            )
        return row

    # ================================================================
    # polybuk.wallet_snapshots — Wallet state every hour
    # ================================================================

    def log_wallet(
        self,
        usdc_balance: float,
        total_equity: float,
        mm_pool_balance: float,
        nc_pool_balance: float,
        reserve_balance: float,
        open_positions: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Log a snapshot of the wallet and pool balances.

        Taken every 60 minutes. Tracks how capital moves between pools
        and whether the reserve is being touched (it shouldn't be).

        total_equity = usdc_balance + value of open positions
        """
        data = {
            "usdc_balance": usdc_balance,
            "open_positions": open_positions,
            "total_equity": total_equity,
            "mm_pool_balance": mm_pool_balance,
            "nc_pool_balance": nc_pool_balance,
            "reserve_balance": reserve_balance,
        }
        row = db.insert("wallet_snapshots", data)
        if row:
            logger.info(
                f"Wallet snapshot: equity=${total_equity:.2f} "
                f"MM=${mm_pool_balance:.2f} NC=${nc_pool_balance:.2f} "
                f"Reserve=${reserve_balance:.2f}"
            )
        return row

    # ================================================================
    # polybuk.human_decisions — Manual operator actions
    # ================================================================

    def log_human(
        self,
        action: str,
        details: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Log a manual decision by the operator (you).

        Called when you change markets, adjust parameters, or make
        any manual intervention. Keeps the audit trail complete.

        Example:
            journal.log_human(
                action="replace_market",
                details="Removed Lakers game (resolved), added Celtics vs Heat",
                context={"old_market": "0x...", "new_market": "0x..."},
            )
        """
        data = {
            "action": action,
            "details": details,
            "context": context,
        }
        row = db.insert("human_decisions", data)
        if row:
            logger.info(f"Human decision logged: {action} — {details}")
        return row

    # ================================================================
    # Volume KPI — Referral Program progress
    # ================================================================
    #
    # The $10K cumulative volume target is the #1 KPI of the project.
    # These helpers centralize the volume math so every reporting path
    # (/status, hourly summary, daily report, startup) shows the same
    # number derived from the same source of truth (polybuk.trades).

    # Cache state for Polymarket volume queries (module-level via class attrs
    # so every Journal instance / import shares the same cache window).
    _pm_vol_cache: float = 0.0
    _pm_vol_cache_ts: float = 0.0

    def _fetch_polymarket_volume(self) -> float | None:
        """Fetch total trading volume from Polymarket's data-api.

        Volume semantics match Polymarket's Referral Program widget:
        each TRADE contributes `size × $1` (notional binary — every
        contract pays $1 at resolution). This is different from the
        "cash volume" of size × price. We verified on 2026-04-15 that
        summing size across TRADE events equals the number shown in
        the Polymarket UI.

        Paginates the activity endpoint; returns None on error so the
        caller can fall back to the Supabase-derived value.
        """
        funder = settings.polymarket.funder_address.strip()
        if not funder:
            return None
        try:
            total_contracts = 0.0
            offset = 0
            page_size = 500
            while True:
                r = httpx.get(
                    POLYMARKET_ACTIVITY_URL,
                    params={
                        "user": funder,
                        "type": "TRADE",
                        "limit": page_size,
                        "offset": offset,
                    },
                    timeout=8.0,
                )
                r.raise_for_status()
                batch = r.json() or []
                if not batch:
                    break
                for t in batch:
                    try:
                        total_contracts += float(t.get("size") or 0)
                    except (TypeError, ValueError):
                        continue
                if len(batch) < page_size:
                    break
                offset += page_size
                # Safety cap — prevent runaway paging
                if offset > 50_000:
                    break
            return round(total_contracts, 2)
        except Exception as e:
            logger.error(f"Polymarket volume fetch failed: {e}")
            return None

    def get_cumulative_volume(self) -> float:
        """Total trading volume, sourced from Polymarket data-api.

        This is the SAME number Polymarket shows in the Referral
        Program widget. Cached for 60s to avoid hammering data-api.
        Falls back to a Supabase-derived sum of notional_value if the
        API call fails (legacy path — the value will be lower than
        the official number because Supabase only tracks fills our
        own fill_tracker has logged).
        """
        now = time.time()
        if now - self._pm_vol_cache_ts < VOLUME_CACHE_TTL_SECONDS:
            return self._pm_vol_cache

        fetched = self._fetch_polymarket_volume()
        if fetched is not None:
            Journal._pm_vol_cache = fetched
            Journal._pm_vol_cache_ts = now
            return fetched

        # Fallback: Supabase-derived (legacy, may undercount)
        try:
            resp = (
                db._client.table("trades")
                .select("notional_value")
                .execute()
            )
            total = sum(
                float(r.get("notional_value") or 0) for r in (resp.data or [])
            )
            return round(total, 2)
        except Exception as e:
            logger.error(f"Supabase volume fallback failed: {e}")
            return self._pm_vol_cache  # last known

    def get_volume_since(self, since_iso: str) -> float:
        """USDC live volume since a given ISO 8601 timestamp (UTC)."""
        try:
            resp = (
                db._client.table("trades")
                .select("notional_value")
                .gte("created_at", since_iso)
                .execute()
            )
            total = sum(
                float(r.get("notional_value") or 0) for r in (resp.data or [])
            )
            return round(total, 2)
        except Exception as e:
            logger.error(f"get_volume_since failed: {e}")
            return 0.0

    def get_volume_progress(self) -> dict[str, float]:
        """Return the volume KPI: cumulative, target, and percent.

        Used by every report to show a consistent headline:
            "Volumen acumulado: $X / $10,000 (Y%)"
        """
        cumulative = self.get_cumulative_volume()
        target = settings.general.volume_target
        percent = (cumulative / target * 100) if target > 0 else 0.0
        return {
            "cumulative": cumulative,
            "target": target,
            "percent": round(percent, 2),
        }

    @staticmethod
    def format_volume_progress(progress: dict[str, float]) -> str:
        """One-line formatter for the volume KPI."""
        return (
            f"Volumen acumulado: ${progress['cumulative']:,.2f} / "
            f"${progress['target']:,.0f} ({progress['percent']:.2f}%)"
        )


# Global instance — import this everywhere
journal = Journal()
