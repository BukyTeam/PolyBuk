"""
PolyBuk - Order Manager

The ONLY module that places and cancels orders. All strategies go through
here. This enforces the flow:

    Strategy → Risk Check → Live Execution → Journal Logging

Never call polymarket_client.place_limit_order() directly from a strategy.
Always go through order_manager.

Usage:
    from core.order_manager import order_manager
    result = order_manager.place_order(
        strategy="market_maker", pool="mm_pool",
        token_id="0x...", side="BUY", price=0.45, size=20,
    )
"""

import logging
import time
from typing import Any

from config.settings import settings
from core.journal import journal
from core.polymarket_client import polymarket_client
from core.risk_manager import risk_manager

logger = logging.getLogger(__name__)


class OrderManager:
    """Manages order lifecycle: place, cancel, track."""

    def place_order(
        self,
        strategy: str,
        pool: str,
        token_id: str,
        side: str,
        price: float,
        size: int,
        market_name: str | None = None,
        market_category: str | None = None,
        net_exposure: int = 0,
    ) -> dict[str, Any] | None:
        """Place a limit order (with risk checks and logging).

        This is the main method strategies call. It:
        1. Calculates order value
        2. Asks risk_manager if the order is allowed
        3. Places the order on Polymarket
        4. Logs the trade and decision to journal

        Args:
            strategy: "market_maker" or "near_certainties"
            pool: "mm_pool" or "nc_pool"
            token_id: CLOB token ID
            side: "BUY" or "SELL"
            price: Price per contract
            size: Number of contracts
            net_exposure: Current net inventory (for MM exposure check)

        Returns:
            API response dict on success, None on failure/rejection.
        """
        order_value = round(price * size, 4)

        # --- Risk check ---
        allowed, reason = risk_manager.check_order(
            pool=pool,
            side=side,
            value=order_value,
            net_exposure=net_exposure,
        )

        if not allowed:
            journal.log_rejected(
                strategy=strategy,
                market_id=token_id,
                market_name=market_name,
                opportunity_type=f"{side.lower()}_order",
                reason=reason,
                details={
                    "price": price,
                    "size": size,
                    "value": order_value,
                    "pool": pool,
                },
            )
            logger.info(f"Order BLOCKED by risk: {reason}")
            return None

        # --- Execute order ---
        start_ms = int(time.time() * 1000)
        resp = polymarket_client.place_limit_order(
            token_id=token_id,
            side=side,
            price=price,
            size=float(size),
        )
        if resp is None:
            risk_manager.record_api_error()
            journal.log_decision(
                strategy=strategy,
                market_id=token_id,
                action="order_failed",
                reason="API call to place_limit_order returned None",
            )
            return None
        risk_manager.record_api_success()

        execution_time_ms = int(time.time() * 1000) - start_ms

        # --- Log trade ---
        journal.log_trade(
            strategy=strategy,
            market_id=token_id,
            market_name=market_name,
            market_category=market_category,
            side=side,
            price=price,
            quantity=size,
            pool=pool,
            execution_time_ms=execution_time_ms,
        )

        # --- Log decision ---
        journal.log_decision(
            strategy=strategy,
            market_id=token_id,
            action=f"place_{side.lower()}",
            reason=(
                f"{side} {size} contracts @ ${price:.4f} = ${order_value:.2f}. "
                f"Pool: {pool}."
            ),
            context={
                "price": price,
                "size": size,
                "value": order_value,
                "pool": pool,
                "net_exposure": net_exposure,
                "execution_time_ms": execution_time_ms,
            },
        )

        return resp

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID.

        Returns True if cancelled successfully, False otherwise.
        """
        resp = polymarket_client.cancel_order(order_id)
        if resp is not None:
            risk_manager.record_api_success()
            return True
        risk_manager.record_api_error()
        return False

    def cancel_all_orders(self) -> bool:
        """Cancel ALL open orders. Used by kill switch.

        Returns True if cancelled successfully.
        """
        resp = polymarket_client.cancel_all_orders()
        if resp is not None:
            risk_manager.record_api_success()
            logger.info("All orders cancelled")
            return True
        risk_manager.record_api_error()
        return False

    def get_open_orders(
        self, market_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get currently open orders."""
        orders = polymarket_client.get_open_orders(market_id)
        if orders:
            risk_manager.record_api_success()
        return orders

    def cancel_stale_orders(
        self,
        market_id: str | None = None,
        max_age_seconds: int | None = None,
    ) -> int:
        """Cancel orders older than max_age_seconds.

        The market maker calls this every cycle to remove orders that
        haven't filled. Stale orders use up capital without providing
        value — the market has moved past them.

        Args:
            market_id: Filter to specific market (optional)
            max_age_seconds: Override default (180s from settings)

        Returns number of orders cancelled.
        """
        if max_age_seconds is None:
            max_age_seconds = settings.mm.stale_order_seconds

        orders = self.get_open_orders(market_id)
        cancelled = 0
        now = time.time()

        for order in orders:
            # py-clob-client returns timestamp as string or int
            order_time = order.get("timestamp", order.get("createdAt", 0))
            if isinstance(order_time, str):
                try:
                    order_time = float(order_time)
                except ValueError:
                    continue

            age = now - order_time
            if age > max_age_seconds:
                order_id = order.get("id", order.get("orderID", ""))
                if order_id and self.cancel_order(order_id):
                    cancelled += 1
                    logger.debug(
                        f"Cancelled stale order {order_id} (age: {age:.0f}s)"
                    )

        if cancelled > 0:
            logger.info(f"Cancelled {cancelled} stale order(s)")
        return cancelled


# Global instance
order_manager = OrderManager()
