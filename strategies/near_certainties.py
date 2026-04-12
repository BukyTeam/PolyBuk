"""
PolyBuk - Near-Certainties Strategy

Buys outcomes priced at $0.93+ that are very likely to resolve YES.
Runs the 6-step cycle from spec section 6.1 every 5 minutes.

How it works (simplified):
- Scan markets for outcomes with price >= $0.93 (93%+ probability)
- Buy $30 worth and wait for resolution
- If YES: receive $1.00 per contract → small profit (~$0.90-$2.10)
- If NO: lose the investment (~$27.90-$29.10)

Risk management:
- Max 3 open positions at a time
- Never 2 positions in the same category (diversification)
- After 1 failure: reduce size from $30 to $20
- After 2 failures: stop NC permanently

The profit per trade is small, but the win rate should be very high
(93%+). Volume contribution: each position generates ~$30 in volume.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from config.markets import Market, get_nc_markets
from config.settings import settings
from core.journal import journal
from core.order_manager import order_manager
from core.paper_trading import paper_engine
from core.polymarket_client import polymarket_client
from core.risk_manager import risk_manager
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class NearCertaintiesStrategy(BaseStrategy):
    """Near-Certainties bot — buys high-probability outcomes."""

    def __init__(self):
        super().__init__()
        # Track open NC positions: token_id → {market info, entry price, etc.}
        self._open_positions: dict[str, dict[str, Any]] = {}
        # Track categories of open positions (for diversification)
        self._open_categories: set[str] = set()

    @property
    def name(self) -> str:
        return "near_certainties"

    @property
    def pool(self) -> str:
        return "nc_pool"

    @property
    def cycle_interval(self) -> int:
        return settings.nc.cycle_interval  # 300 seconds (5 min)

    # ================================================================
    # Lifecycle
    # ================================================================

    async def setup(self) -> bool:
        """Validate NC readiness."""
        if not self.is_pool_active():
            logger.warning("NC pool is not active")
            return False

        failures = risk_manager.get_nc_failure_count()
        if failures >= settings.nc.max_failures:
            logger.warning(
                f"NC permanently stopped: {failures} failures >= "
                f"{settings.nc.max_failures} max"
            )
            return False

        logger.info(
            f"Near-Certainties initialized. "
            f"Pool: ${risk_manager.get_pool_balance('nc_pool'):.2f}. "
            f"Failures: {failures}/{settings.nc.max_failures}"
        )
        return True

    async def cleanup(self) -> None:
        """Log shutdown. NC doesn't place limit orders, so nothing to cancel."""
        journal.log_decision(
            strategy=self.name,
            market_id="all",
            action="shutdown",
            reason=(
                f"NC shutting down. Open positions: {len(self._open_positions)}. "
                f"Failures: {risk_manager.get_nc_failure_count()}"
            ),
        )
        logger.info("Near-Certainties cleaned up")

    # ================================================================
    # Main Cycle (6 steps from spec)
    # ================================================================

    async def execute_cycle(self) -> None:
        """Run one NC cycle."""
        if not self.is_pool_active():
            self.log_cycle_skip("NC pool not active")
            return

        # === STEP 1: EVALUATE CAPACITY ===
        can_open, capacity_reason = self._check_capacity()

        # === STEP 5: MONITOR EXISTING POSITIONS ===
        # Always monitor, even if we can't open new ones
        await self._monitor_positions()

        if not can_open:
            logger.debug(f"NC capacity check: {capacity_reason}")
            return

        # === STEP 2: SCAN MARKETS ===
        markets = get_nc_markets()
        if not markets:
            self.log_cycle_skip("No NC markets configured")
            return

        # === STEP 3 & 4: EVALUATE AND EXECUTE ===
        for market in markets:
            # Re-check capacity (might have filled up during loop)
            can_open, _ = self._check_capacity()
            if not can_open:
                break

            try:
                await self._evaluate_and_buy(market)
            except Exception as e:
                logger.error(f"Error evaluating NC market {market.name}: {e}")
                risk_manager.record_api_error()

    # ================================================================
    # Step 1: Capacity Check
    # ================================================================

    def _check_capacity(self) -> tuple[bool, str]:
        """Check if we can open a new NC position.

        Checks: failure count, open position count, pool balance.
        """
        # Failures exceeded
        failures = risk_manager.get_nc_failure_count()
        if failures >= settings.nc.max_failures:
            return False, f"NC stopped: {failures} failures"

        # Max positions reached
        if len(self._open_positions) >= settings.nc.max_positions:
            return False, f"Max positions: {len(self._open_positions)}/{settings.nc.max_positions}"

        # Insufficient balance
        position_size = risk_manager.get_nc_position_size()
        balance = risk_manager.get_pool_balance("nc_pool")
        if balance < position_size:
            return False, f"Insufficient balance: ${balance:.2f} < ${position_size} needed"

        return True, "ok"

    # ================================================================
    # Steps 3-4: Evaluate and Buy
    # ================================================================

    async def _evaluate_and_buy(self, market: Market) -> None:
        """Evaluate a single market and buy if it passes all filters."""

        # Skip if already have a position in this market
        if market.token_id in self._open_positions:
            return

        # Skip if already have a position in this category (diversification)
        if market.category in self._open_categories:
            journal.log_rejected(
                strategy=self.name,
                market_id=market.token_id,
                market_name=market.name,
                opportunity_type="nc_high_prob",
                reason=f"Category '{market.category}' already has open position (diversification rule)",
            )
            return

        # Get current price
        price = polymarket_client.get_price(market.token_id, "SELL")
        if price is None:
            risk_manager.record_api_error()
            return
        risk_manager.record_api_success()

        # Check minimum probability
        if price < settings.nc.min_probability:
            journal.log_rejected(
                strategy=self.name,
                market_id=market.token_id,
                market_name=market.name,
                opportunity_type="nc_high_prob",
                reason=f"Price ${price:.4f} < ${settings.nc.min_probability} minimum",
                details={"current_price": price},
            )
            return

        # Determine position size (reduced after first failure)
        position_size = risk_manager.get_nc_position_size()
        quantity = int(position_size / price)

        if quantity <= 0:
            return

        # === EXECUTE BUY ===
        result = order_manager.place_order(
            strategy=self.name,
            pool=self.pool,
            token_id=market.token_id,
            side="BUY",
            price=price,
            size=quantity,
            market_name=market.name,
            market_category=market.category,
        )

        if result is not None:
            # Track open position
            self._open_positions[market.token_id] = {
                "market": market,
                "entry_price": price,
                "quantity": quantity,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            self._open_categories.add(market.category)

            # Record in paper engine
            if settings.paper.enabled:
                paper_engine.record_fill(
                    market.token_id, "BUY", price, quantity
                )

            logger.info(
                f"NC position opened: {market.name} — "
                f"{quantity} contracts @ ${price:.4f} = "
                f"${price * quantity:.2f}"
            )

    # ================================================================
    # Step 5: Monitor Positions
    # ================================================================

    async def _monitor_positions(self) -> None:
        """Monitor open NC positions for resolution or price drops."""
        if not self._open_positions:
            return

        # Iterate over a copy since we might remove items
        for token_id, pos_data in list(self._open_positions.items()):
            market: Market = pos_data["market"]
            entry_price: float = pos_data["entry_price"]

            # Check current price
            current_price = polymarket_client.get_price(token_id, "SELL")
            if current_price is None:
                continue
            risk_manager.record_api_success()

            # Alert if price dropped significantly
            if current_price < settings.nc.alert_price_drop:
                journal.log_decision(
                    strategy=self.name,
                    market_id=token_id,
                    action="price_alert",
                    reason=(
                        f"Price dropped to ${current_price:.4f} "
                        f"(alert threshold: ${settings.nc.alert_price_drop}). "
                        f"Entry was ${entry_price:.4f}"
                    ),
                    context={
                        "current_price": current_price,
                        "entry_price": entry_price,
                        "market_name": market.name,
                    },
                )

            # Check if market resolved (price = 1.0 or 0.0)
            if current_price >= 0.99:
                # Resolved YES — we win
                self._close_position(token_id, settlement_price=1.0, won=True)
            elif current_price <= 0.01:
                # Resolved NO — we lose
                self._close_position(token_id, settlement_price=0.0, won=False)

    def _close_position(
        self, token_id: str, settlement_price: float, won: bool
    ) -> None:
        """Close a position after market resolution."""
        pos_data = self._open_positions.get(token_id)
        if not pos_data:
            return

        market: Market = pos_data["market"]
        entry_price: float = pos_data["entry_price"]
        quantity: int = pos_data["quantity"]

        # Calculate P&L
        pnl = (settlement_price - entry_price) * quantity

        # Record in risk manager
        risk_manager.record_trade_result(self.pool, pnl)

        if not won:
            risk_manager.record_nc_failure()

        # Record in paper engine
        if settings.paper.enabled:
            paper_engine.close_position(token_id, settlement_price)

        # Log
        journal.log_decision(
            strategy=self.name,
            market_id=token_id,
            action="position_closed",
            reason=(
                f"{'WON' if won else 'LOST'}: {market.name}. "
                f"Entry=${entry_price:.4f}, Settlement=${settlement_price:.2f}. "
                f"P&L: ${pnl:+.4f}. "
                f"NC failures: {risk_manager.get_nc_failure_count()}/{settings.nc.max_failures}"
            ),
            context={
                "entry_price": entry_price,
                "settlement_price": settlement_price,
                "quantity": quantity,
                "pnl": pnl,
                "won": won,
            },
            paper_trade=settings.paper.enabled,
        )

        # Remove from tracking
        self._open_categories.discard(market.category)
        del self._open_positions[token_id]

        logger.info(
            f"NC position closed: {market.name} — "
            f"{'WON' if won else 'LOST'} ${pnl:+.4f}"
        )
