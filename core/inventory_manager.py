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
        best_bid: float,
        best_ask: float,
        inventory: int,
        max_inventory: int | None = None,
    ) -> tuple[float, float]:
        """Calculate prices that JOIN the top of the book, with inventory skew.

        Design: in a $0.01-tick market where the typical spread is also
        $0.01, placing at mid±offset puts us BEHIND the best prices and
        produces near-zero fills. We join the existing best bid/ask
        instead (same queue position, but actually fillable).

        Skew shifts BOTH sides down when long / up when short:
        - Inventory 0          → bid=best_bid, ask=best_ask (pure join)
        - Inventory > 0 (long) → both shifted down (encourage selling)
        - Inventory < 0 (short)→ both shifted up (encourage buying)

        Shift is proportional to inventory fullness vs max_exposure.
        At max inventory the shift is $0.02 (2 ticks), which in a $0.01
        spread means our SELL will cross the bid and fill as a taker —
        exactly what we want when we need to dump.

        Args:
            best_bid: Current best bid on the book
            best_ask: Current best ask on the book
            inventory: Net contracts we hold (from get_net_inventory)
            max_inventory: Override max exposure (default from settings)

        Returns:
            (bid_price, ask_price) — both clamped to [0.05, 0.95]
        """
        if max_inventory is None:
            max_inventory = settings.mm.max_exposure

        # Skew in ticks: 0 when flat, up to $0.02 when inventory == max_inventory
        skew = (inventory / max_inventory) * 0.02 if max_inventory > 0 else 0.0

        my_bid = round(best_bid - skew, 2)
        my_ask = round(best_ask - skew, 2)

        # Clamp to safe range — never quote at extremes
        my_bid = max(0.05, min(0.95, my_bid))
        my_ask = max(0.05, min(0.95, my_ask))

        # Guard: if skew pushed bid >= ask, widen by 1 tick
        if my_bid >= my_ask:
            my_ask = round(my_bid + 0.01, 2)

        logger.debug(
            f"Prices: bbid=${best_bid:.4f} bask=${best_ask:.4f} "
            f"inv={inventory} skew={skew:.4f} → bid=${my_bid:.2f} ask=${my_ask:.2f}"
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
