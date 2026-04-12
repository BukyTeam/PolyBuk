"""
PolyBuk - Paper Trading Engine

Simulates P&L for paper trades by tracking virtual fills and
comparing against real market prices.

When PAPER_MODE=true:
- Orders are "filled" instantly at the requested price
- P&L is calculated when positions are closed or markets resolve
- Everything is logged with paper_trade=true in Supabase

This module tracks the virtual portfolio. The actual order simulation
is handled by order_manager (it skips the API call in paper mode).

Usage:
    from core.paper_trading import paper_engine
    paper_engine.record_fill("token_id", "BUY", 0.45, 20)
    pnl = paper_engine.calculate_pnl("token_id", current_price=0.50)
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    """A virtual position from paper trading.

    Tracks the average entry price and total contracts so we can
    calculate P&L when the position is closed or market resolves.
    """
    token_id: str
    net_contracts: int = 0
    avg_entry_price: float = 0.0
    total_cost: float = 0.0        # Total USDC spent buying
    total_received: float = 0.0    # Total USDC received selling
    realized_pnl: float = 0.0     # P&L from closed portions


class PaperTradingEngine:
    """Tracks virtual portfolio for paper trading simulations."""

    def __init__(self):
        # token_id → PaperPosition
        self._positions: dict[str, PaperPosition] = {}
        self._total_volume: float = 0.0
        self._total_trades: int = 0

    def record_fill(
        self,
        token_id: str,
        side: str,
        price: float,
        quantity: int,
    ) -> None:
        """Record a simulated fill.

        Called by order_manager after a paper order is "placed".
        Updates the virtual position and volume tracking.

        Args:
            token_id: CLOB token ID
            side: "BUY" or "SELL"
            price: Simulated fill price
            quantity: Number of contracts
        """
        if token_id not in self._positions:
            self._positions[token_id] = PaperPosition(token_id=token_id)

        pos = self._positions[token_id]
        notional = price * quantity

        if side == "BUY":
            # Buying: increase position, update average price
            old_cost = pos.avg_entry_price * pos.net_contracts
            pos.net_contracts += quantity
            pos.total_cost += notional
            if pos.net_contracts > 0:
                pos.avg_entry_price = (old_cost + notional) / pos.net_contracts
        elif side == "SELL":
            # Selling: decrease position, realize P&L on sold portion
            if pos.net_contracts > 0:
                # Realize P&L = (sell price - avg entry) * quantity sold
                pnl = (price - pos.avg_entry_price) * quantity
                pos.realized_pnl += pnl
            pos.net_contracts -= quantity
            pos.total_received += notional

        self._total_volume += notional
        self._total_trades += 1

        logger.debug(
            f"[PAPER] Fill: {side} {quantity}x @ ${price:.4f} "
            f"on {token_id[:16]}... Net position: {pos.net_contracts}"
        )

    def calculate_unrealized_pnl(
        self, token_id: str, current_price: float
    ) -> float:
        """Calculate unrealized P&L for an open position.

        Unrealized = what you'd make if you closed the position now.
        """
        pos = self._positions.get(token_id)
        if not pos or pos.net_contracts == 0:
            return 0.0

        return (current_price - pos.avg_entry_price) * pos.net_contracts

    def close_position(
        self, token_id: str, settlement_price: float
    ) -> float:
        """Close a position at a given price (market resolution).

        Returns realized P&L for this position.
        For binary markets: settlement_price is 1.0 (YES) or 0.0 (NO).
        """
        pos = self._positions.get(token_id)
        if not pos or pos.net_contracts == 0:
            return 0.0

        # Settle remaining contracts
        pnl = (settlement_price - pos.avg_entry_price) * pos.net_contracts
        pos.realized_pnl += pnl
        total_pnl = pos.realized_pnl

        logger.info(
            f"[PAPER] Position closed: {token_id[:16]}... "
            f"@ ${settlement_price:.2f}. P&L: ${total_pnl:.4f}"
        )

        # Reset position
        pos.net_contracts = 0
        pos.avg_entry_price = 0.0

        return total_pnl

    def get_position(self, token_id: str) -> PaperPosition | None:
        """Get a specific paper position."""
        return self._positions.get(token_id)

    def get_all_positions(self) -> dict[str, PaperPosition]:
        """Get all paper positions."""
        return self._positions.copy()

    def get_stats(self) -> dict[str, Any]:
        """Get paper trading statistics."""
        total_realized = sum(p.realized_pnl for p in self._positions.values())
        open_positions = sum(
            1 for p in self._positions.values() if p.net_contracts != 0
        )

        return {
            "total_volume": round(self._total_volume, 2),
            "total_trades": self._total_trades,
            "total_realized_pnl": round(total_realized, 4),
            "open_positions": open_positions,
        }


# Global instance
paper_engine = PaperTradingEngine()
