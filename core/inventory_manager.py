"""
PolyBuk - Inventory Manager

Tracks positions and calculates prices adjusted for inventory (skew).

The skew function is the market maker's core pricing mechanism:
if you're holding too many contracts in one direction, it shifts
your quotes to encourage the market to take the other side.

This prevents the bot from accumulating dangerous one-sided exposure.

Usage:
    from core.inventory_manager import inventory_manager
    inv = inventory_manager.get_net_inventory("token_id")
    bid, ask = inventory_manager.calculate_prices(mid, inv)
"""

import logging
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


class InventoryManager:
    """Tracks positions and calculates skew-adjusted prices."""

    def __init__(self):
        # token_id → net contracts (positive = long, negative = short)
        self._positions: dict[str, int] = {}

    # ================================================================
    # Position Tracking
    # ================================================================

    def update_position(self, token_id: str, side: str, quantity: int) -> None:
        """Update inventory after a trade.

        Called by the strategy after each order execution.

        Args:
            token_id: CLOB token ID
            side: "BUY" (adds to position) or "SELL" (reduces position)
            quantity: Number of contracts
        """
        current = self._positions.get(token_id, 0)
        if side == "BUY":
            self._positions[token_id] = current + quantity
        elif side == "SELL":
            self._positions[token_id] = current - quantity

        logger.debug(
            f"Position updated: {token_id[:16]}... "
            f"{current} → {self._positions[token_id]}"
        )

    def get_net_inventory(self, token_id: str) -> int:
        """Get net inventory for a token.

        Positive = long (you own contracts, profit if price goes up)
        Negative = short (you owe contracts, profit if price goes down)
        Zero = flat (no directional risk)
        """
        return self._positions.get(token_id, 0)

    def get_all_positions(self) -> dict[str, int]:
        """Get all positions. Used for wallet snapshots."""
        return self._positions.copy()

    def reset_position(self, token_id: str) -> None:
        """Reset a position to zero. Used when a market resolves."""
        if token_id in self._positions:
            old = self._positions.pop(token_id)
            logger.info(
                f"Position reset: {token_id[:16]}... was {old}, now 0"
            )

    # ================================================================
    # Skew Function (Spec Section 5.2)
    # ================================================================

    def calculate_prices(
        self,
        mid_price: float,
        inventory: int,
        max_inventory: int | None = None,
        half_spread: float | None = None,
    ) -> tuple[float, float]:
        """Calculate skew-adjusted bid and ask prices.

        This is the market maker's core pricing algorithm from the spec.

        How skew works:
        - If inventory = 0: bid and ask are symmetric around mid price
        - If inventory > 0 (long): shift BOTH prices DOWN to encourage
          selling (we want to reduce our long position)
        - If inventory < 0 (short): shift BOTH prices UP to encourage
          buying (we want to reduce our short position)

        The shift amount is proportional to how full our inventory is
        relative to the max allowed.

        Args:
            mid_price: Current midpoint from order book
            inventory: Net contracts (from get_net_inventory)
            max_inventory: Override max exposure (default from settings)
            half_spread: Override half spread offset (default from settings)

        Returns:
            (bid_price, ask_price) — both clamped to [0.05, 0.95]
        """
        if max_inventory is None:
            max_inventory = settings.mm.max_exposure
        if half_spread is None:
            half_spread = settings.mm.half_spread_offset

        # Skew: proportional to inventory fullness, max shift of $0.02
        # When inventory is at max, skew = 0.02 (2 cents shift)
        skew = (inventory / max_inventory) * 0.02 if max_inventory > 0 else 0.0

        # Apply spread and skew
        my_bid = round(mid_price - half_spread - skew, 2)
        my_ask = round(mid_price + half_spread - skew, 2)

        # Clamp to safe range — never quote at extremes
        # (spec says 0.10-0.90 for MM, but we use 0.05-0.95 as hard floor/ceiling)
        my_bid = max(0.05, min(0.95, my_bid))
        my_ask = max(0.05, min(0.95, my_ask))

        logger.debug(
            f"Prices: mid=${mid_price:.4f} inv={inventory} skew={skew:.4f} "
            f"→ bid=${my_bid:.2f} ask=${my_ask:.2f}"
        )

        return my_bid, my_ask

    # ================================================================
    # Analysis Helpers
    # ================================================================

    def get_total_exposure(self) -> int:
        """Get total absolute exposure across all positions.

        Used by risk manager to check if we're over the limit.
        """
        return sum(abs(v) for v in self._positions.values())

    def get_position_summary(self) -> dict[str, Any]:
        """Get summary for Telegram status and wallet snapshots."""
        return {
            "positions": self._positions.copy(),
            "total_exposure": self.get_total_exposure(),
            "num_markets": len([v for v in self._positions.values() if v != 0]),
        }


# Global instance
inventory_manager = InventoryManager()
