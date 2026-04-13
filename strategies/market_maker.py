"""
PolyBuk - Market Maker Strategy

Captures the bid-ask spread by placing orders on both sides of the
order book. Runs the 8-step cycle from spec section 5.1 every 30 seconds.

How market making works (simplified):
- The order book has buyers (bids) and sellers (asks)
- The gap between best bid and best ask is the "spread"
- We place a buy order slightly below mid price and a sell order slightly above
- When both fill, we capture the spread as profit
- Example: buy at $0.44, sell at $0.46 → profit $0.02 per contract

Risks:
- If the price moves against us before both sides fill, we lose
- The skew function mitigates this by adjusting prices based on inventory
- Circuit breakers stop us if losses exceed limits
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any

from config.markets import Market, get_mm_markets
from config.settings import settings
from core.inventory_manager import inventory_manager
from core.journal import journal
from core.order_manager import order_manager
from core.paper_trading import paper_engine
from core.polymarket_client import polymarket_client
from core.risk_manager import risk_manager
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class MarketMakerStrategy(BaseStrategy):
    """Market maker bot — captures bid-ask spread."""

    def __init__(self):
        super().__init__()
        self._last_wallet_snapshot: float = 0.0
        self._cycle_count: int = 0

    @property
    def name(self) -> str:
        return "market_maker"

    @property
    def pool(self) -> str:
        return "mm_pool"

    @property
    def cycle_interval(self) -> int:
        return settings.mm.cycle_interval  # 30 seconds

    # ================================================================
    # Lifecycle
    # ================================================================

    async def setup(self) -> bool:
        """Validate that we have markets configured and pool is active."""
        markets = get_mm_markets()
        if not markets:
            logger.warning("No MM markets configured in config/markets.py")
            return False

        if not self.is_pool_active():
            logger.warning("MM pool is not active (paused/stopped/killed)")
            return False

        logger.info(
            f"Market Maker initialized with {len(markets)} market(s). "
            f"Pool: ${risk_manager.get_pool_balance('mm_pool'):.2f}"
        )
        return True

    async def cleanup(self) -> None:
        """Cancel all open orders on shutdown."""
        order_manager.cancel_all_orders()
        journal.log_decision(
            strategy=self.name,
            market_id="all",
            action="shutdown",
            reason="Market maker shutting down, all orders cancelled",
        )
        logger.info("Market Maker cleaned up")

    # ================================================================
    # Main Cycle (8 steps from spec)
    # ================================================================

    async def execute_cycle(self) -> None:
        """Run one complete MM cycle across all configured markets."""
        if not self.is_pool_active():
            self.log_cycle_skip("MM pool not active")
            return

        markets = get_mm_markets()
        if not markets:
            self.log_cycle_skip("No markets configured")
            return

        self._cycle_count += 1

        for market in markets:
            try:
                await self._process_market(market)
            except Exception as e:
                logger.error(f"Error processing market {market.name}: {e}")
                risk_manager.record_api_error()

        # Step 8: Wallet snapshot every 60 minutes
        await self._maybe_wallet_snapshot()

    async def _process_market(self, market: Market) -> None:
        """Process a single market through the 8-step cycle."""

        # === STEP 1: GET STATE ===
        book = polymarket_client.get_order_book(market.token_id)
        if book is None:
            risk_manager.record_api_error()
            journal.log_decision(
                strategy=self.name,
                market_id=market.token_id,
                action="skip_cycle",
                reason="Failed to fetch order book",
            )
            return

        risk_manager.record_api_success()
        inventory = inventory_manager.get_net_inventory(market.token_id)

        # === STEP 2: CALCULATE ===
        best_bid, best_ask, mid_price, spread = self._extract_book_data(book)

        if mid_price is None:
            journal.log_decision(
                strategy=self.name,
                market_id=market.token_id,
                action="skip_cycle",
                reason="Order book empty or invalid — no mid price",
            )
            return

        # === STEP 3: EVALUATE ===
        should_trade, eval_reason = self._evaluate_conditions(
            mid_price, spread, market
        )

        if not should_trade:
            journal.log_rejected(
                strategy=self.name,
                market_id=market.token_id,
                market_name=market.name,
                opportunity_type="mm_spread",
                reason=eval_reason,
                details={
                    "mid_price": mid_price,
                    "spread": spread,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                },
            )
            return

        # === STEP 4: CANCEL STALE ORDERS ===
        cancelled = order_manager.cancel_stale_orders(market_id=market.token_id)

        # === STEP 5: CALCULATE PRICES (with skew) ===
        my_bid, my_ask = inventory_manager.calculate_prices(
            mid_price=mid_price,
            inventory=inventory,
        )

        # Validate calculated prices are within MM operating range
        if my_bid < settings.mm.min_price or my_ask > settings.mm.max_price:
            journal.log_rejected(
                strategy=self.name,
                market_id=market.token_id,
                market_name=market.name,
                opportunity_type="mm_spread",
                reason=(
                    f"Calculated prices outside range: "
                    f"bid=${my_bid:.2f} ask=${my_ask:.2f} "
                    f"(range: ${settings.mm.min_price}-${settings.mm.max_price})"
                ),
            )
            return

        # === STEP 6: PLACE ORDERS ===
        order_size = settings.mm.order_size
        order_value = my_bid * order_size

        # Check max order value
        if order_value > settings.mm.max_order_value:
            order_size = int(settings.mm.max_order_value / my_bid)

        # Place BID (buy order)
        bid_result = order_manager.place_order(
            strategy=self.name,
            pool=self.pool,
            token_id=market.token_id,
            side="BUY",
            price=my_bid,
            size=order_size,
            market_name=market.name,
            market_category=market.category,
            net_exposure=inventory,
        )

        # Place ASK (sell order)
        ask_result = order_manager.place_order(
            strategy=self.name,
            pool=self.pool,
            token_id=market.token_id,
            side="SELL",
            price=my_ask,
            size=order_size,
            market_name=market.name,
            market_category=market.category,
            net_exposure=inventory,
        )

        # Update inventory for paper trades
        if settings.paper.enabled:
            if bid_result:
                inventory_manager.update_position(
                    market.token_id, "BUY", order_size
                )
                paper_engine.record_fill(
                    market.token_id, "BUY", my_bid, order_size
                )
            if ask_result:
                inventory_manager.update_position(
                    market.token_id, "SELL", order_size
                )
                paper_engine.record_fill(
                    market.token_id, "SELL", my_ask, order_size
                )

        # === STEP 7: LOG SNAPSHOT ===
        journal.log_snapshot(
            market_id=market.token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread=spread,
            bid_depth_5=self._calculate_depth(book, "bids", 5),
            ask_depth_5=self._calculate_depth(book, "asks", 5),
        )

        # Log the cycle decision with full context
        journal.log_decision(
            strategy=self.name,
            market_id=market.token_id,
            action="cycle_complete",
            reason=(
                f"Spread ${spread:.4f} in range. "
                f"Mid=${mid_price:.4f}, inv={inventory}. "
                f"Bid=${my_bid:.2f}, Ask=${my_ask:.2f}. "
                f"Stale cancelled: {cancelled}."
            ),
            context={
                "cycle": self._cycle_count,
                "mid_price": mid_price,
                "spread": spread,
                "inventory": inventory,
                "my_bid": my_bid,
                "my_ask": my_ask,
                "order_size": order_size,
                "stale_cancelled": cancelled,
                "bid_placed": bid_result is not None,
                "ask_placed": ask_result is not None,
            },
            paper_trade=settings.paper.enabled,
        )

    # ================================================================
    # Helpers
    # ================================================================

    def _extract_book_data(
        self, book: Any
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """Extract best bid, best ask, mid price, and spread from order book.

        Returns (best_bid, best_ask, mid_price, spread).
        Any value can be None if the book is empty on that side.
        """
        try:
            bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
            asks = book.asks if hasattr(book, "asks") else book.get("asks", [])

            if not bids or not asks:
                return None, None, None, None

            # py-clob-client sorts bids ASCENDING (lowest first) and asks DESCENDING
            # (highest first). So the BEST bid is the LAST bid, and the BEST ask
            # is the LAST ask.
            best_bid = float(bids[-1].price if hasattr(bids[-1], "price") else bids[-1].get("price", 0))
            best_ask = float(asks[-1].price if hasattr(asks[-1], "price") else asks[-1].get("price", 0))

            if best_bid <= 0 or best_ask <= 0:
                return None, None, None, None

            mid_price = round((best_bid + best_ask) / 2, 4)
            spread = round(best_ask - best_bid, 4)

            return best_bid, best_ask, mid_price, spread

        except (IndexError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Failed to extract book data: {e}")
            return None, None, None, None

    def _evaluate_conditions(
        self, mid_price: float, spread: float | None, market: Market
    ) -> tuple[bool, str]:
        """Check all conditions before trading.

        Returns (should_trade, reason_if_not).
        """
        # Spread too tight — no room for profit
        if spread is not None and spread < settings.mm.min_spread:
            return False, f"Spread too tight: ${spread:.4f} < ${settings.mm.min_spread} min"

        # Spread too wide — likely illiquid or risky market
        if spread is not None and spread > settings.mm.max_spread:
            return False, f"Spread too wide: ${spread:.4f} > ${settings.mm.max_spread} max"

        # Price at extremes — too risky (near 0 or 1)
        if mid_price < settings.mm.min_price:
            return False, f"Price too low: ${mid_price:.4f} < ${settings.mm.min_price} min"
        if mid_price > settings.mm.max_price:
            return False, f"Price too high: ${mid_price:.4f} > ${settings.mm.max_price} max"

        return True, "ok"

    def _calculate_depth(
        self, book: Any, side: str, levels: int
    ) -> float | None:
        """Calculate total USDC depth in the top N price levels.

        Depth = how much money is sitting in the orderbook near the
        best price. More depth = more liquid market = safer to trade.

        Since py-clob-client sorts bids ascending and asks descending,
        the best prices are at the END of each list. We take the last
        N levels (the ones closest to mid price).
        """
        try:
            orders = book.get(side, []) if isinstance(book, dict) else getattr(book, side, [])
            # Take the last N levels (best prices)
            best_levels = orders[-levels:] if len(orders) >= levels else orders
            total = 0.0
            for order in best_levels:
                price = float(order.price if hasattr(order, "price") else order.get("price", 0))
                size = float(order.size if hasattr(order, "size") else order.get("size", 0))
                total += price * size
            return round(total, 4)
        except Exception:
            return None

    async def _maybe_wallet_snapshot(self) -> None:
        """Take a wallet snapshot every 60 minutes."""
        now = time.time()
        interval = settings.general.wallet_snapshot_interval  # 3600

        if now - self._last_wallet_snapshot < interval:
            return

        self._last_wallet_snapshot = now

        journal.log_wallet(
            usdc_balance=risk_manager.get_pool_balance("mm_pool")
                + risk_manager.get_pool_balance("nc_pool")
                + risk_manager.get_pool_balance("reserve"),
            total_equity=risk_manager.get_pool_balance("mm_pool")
                + risk_manager.get_pool_balance("nc_pool")
                + risk_manager.get_pool_balance("reserve"),
            mm_pool_balance=risk_manager.get_pool_balance("mm_pool"),
            nc_pool_balance=risk_manager.get_pool_balance("nc_pool"),
            reserve_balance=risk_manager.get_pool_balance("reserve"),
            open_positions=inventory_manager.get_position_summary(),
        )
