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
import time
from typing import Any

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

POSITIONS_URL = "https://data-api.polymarket.com/positions"
CACHE_TTL_SECONDS = 20.0


class InventoryManager:
    """Tracks positions and calculates skew-adjusted prices.

    Source of truth: Polymarket's data-api positions endpoint, queried
    against the funder (proxy) address. Local state is a time-bounded
    cache (20s) — on cache miss we refresh from the API.

    Why we don't track locally: every strategy restart would reset
    in-memory positions to zero while the on-chain reality is tens of
    contracts. That silently disables the inventory-aware SELL guard
    and turns the market maker into a one-way buyer. Pulling from the
    API each cycle costs ~50ms and is always correct.
    """

    def __init__(self):
        self._positions: dict[str, int] = {}
        self._cache_ts: float = 0.0

    # ================================================================
    # Position Tracking (sourced from Polymarket data-api)
    # ================================================================

    def _refresh_from_api(self) -> None:
        """Pull live positions from Polymarket and update the cache.

        On failure we keep the last known cache to avoid falsely
        reporting inventory=0 (which would re-trigger the naked-short
        SELL bug we're defending against).
        """
        funder = settings.polymarket.funder_address.strip()
        if not funder:
            # EOA-mode account — positions held at the signer address,
            # not a proxy. data-api still accepts the EOA as 'user'.
            from core.polymarket_client import polymarket_client
            funder = polymarket_client.get_address() or ""
        if not funder:
            logger.warning("No funder address to query positions")
            return

        try:
            r = httpx.get(POSITIONS_URL, params={"user": funder}, timeout=5.0)
            r.raise_for_status()
            positions = r.json() or []
        except Exception as e:
            logger.error(f"Position refresh failed; keeping stale cache: {e}")
            return

        fresh: dict[str, int] = {}
        for p in positions:
            asset_id = str(p.get("asset") or "")
            try:
                size = int(float(p.get("size") or 0))
            except (TypeError, ValueError):
                continue
            if asset_id and size != 0:
                fresh[asset_id] = size

        self._positions = fresh
        self._cache_ts = time.time()
        logger.debug(f"Positions refreshed: {len(fresh)} non-zero")

    def _maybe_refresh(self) -> None:
        if time.time() - self._cache_ts > CACHE_TTL_SECONDS:
            self._refresh_from_api()

    def get_net_inventory(self, token_id: str) -> int:
        """Get net inventory for a token, refreshed from Polymarket."""
        self._maybe_refresh()
        return self._positions.get(token_id, 0)

    def get_all_positions(self) -> dict[str, int]:
        """Get all positions (refreshed). Used for wallet snapshots."""
        self._maybe_refresh()
        return self._positions.copy()

    def force_refresh(self) -> None:
        """Bypass the cache on the next read. Useful after manual trades."""
        self._cache_ts = 0.0

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
        """Get total absolute exposure across all positions."""
        self._maybe_refresh()
        return sum(abs(v) for v in self._positions.values())

    def get_position_summary(self) -> dict[str, Any]:
        """Get summary for Telegram status and wallet snapshots."""
        self._maybe_refresh()
        return {
            "positions": self._positions.copy(),
            "total_exposure": sum(abs(v) for v in self._positions.values()),
            "num_markets": len([v for v in self._positions.values() if v != 0]),
        }


# Global instance
inventory_manager = InventoryManager()
