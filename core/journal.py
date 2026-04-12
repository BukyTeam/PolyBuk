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
from datetime import datetime, timezone
from typing import Any

from core.supabase_client import db

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
        quantity: int,
        pool: str,
        market_name: str | None = None,
        market_category: str | None = None,
        order_type: str = "LIMIT",
        maker_rebate: float | None = None,
        fee_paid: float | None = None,
        execution_time_ms: int | None = None,
        paper_trade: bool = False,
    ) -> dict[str, Any] | None:
        """Log an executed trade.

        Called by order_manager after an order fills.
        The notional_value (price x quantity) is calculated automatically.

        Args:
            strategy: "market_maker" or "near_certainties"
            market_id: CLOB token_id
            side: "BUY" or "SELL"
            price: Execution price
            quantity: Number of contracts
            pool: "mm_pool" or "nc_pool"
            paper_trade: True if this was a simulated trade
        """
        data = {
            "strategy": strategy,
            "market_id": market_id,
            "market_name": market_name,
            "market_category": market_category,
            "side": side,
            "price": price,
            "quantity": quantity,
            "notional_value": round(price * quantity, 4),
            "order_type": order_type,
            "maker_rebate": maker_rebate,
            "fee_paid": fee_paid,
            "execution_time_ms": execution_time_ms,
            "pool": pool,
            "paper_trade": paper_trade,
        }
        row = db.insert("trades", data)
        if row:
            logger.info(
                f"Trade logged: {strategy} {side} {quantity}x @ ${price:.4f} "
                f"({pool}) {'[PAPER]' if paper_trade else '[LIVE]'}"
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
        paper_trade: bool = False,
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
            "paper_trade": paper_trade,
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


# Global instance — import this everywhere
journal = Journal()
