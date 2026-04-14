"""
PolyBuk - Risk Manager

Protects capital through pools, circuit breakers, and kill switch.
Every order attempt passes through this module BEFORE being placed.
If any check fails, the order is blocked and the reason is logged.

Circuit breakers (from spec section 4.4):
- Daily loss per pool > $20 → pause pool until tomorrow
- Cumulative loss per pool > $50 → stop pool permanently
- Total loss > $80 → stop EVERYTHING
- MM exposure > 100 contracts → only allow reducing positions
- 3 consecutive API errors → pause all trading
- Market <2h from resolution → close positions

Usage:
    from core.risk_manager import risk_manager
    allowed, reason = risk_manager.check_order("mm_pool", "BUY", 15.0, 20)
    if not allowed:
        journal.log_rejected(...)
"""

import logging
from datetime import datetime, timezone
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


class RiskManager:
    """Manages capital pools and enforces risk limits."""

    def __init__(self):
        # Pool balances — start with configured values
        self._pool_balances: dict[str, float] = {
            "mm_pool": settings.risk.mm_pool,
            "nc_pool": settings.risk.nc_pool,
            "reserve": settings.risk.reserve,
        }

        # P&L tracking
        self._daily_pnl: dict[str, float] = {"mm_pool": 0.0, "nc_pool": 0.0}
        self._cumulative_pnl: dict[str, float] = {"mm_pool": 0.0, "nc_pool": 0.0}
        self._total_pnl: float = 0.0

        # Circuit breaker states
        self._pool_paused: dict[str, bool] = {"mm_pool": False, "nc_pool": False}
        self._pool_stopped: dict[str, bool] = {"mm_pool": False, "nc_pool": False}
        self._all_stopped: bool = False
        self._kill_switch_active: bool = False

        # API error tracking
        self._consecutive_api_errors: int = 0
        self._api_paused: bool = False

        # NC failure tracking (spec section 4.3)
        self._nc_failures: int = 0

        # Date tracking for daily reset
        self._current_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ================================================================
    # Pre-Order Checks
    # ================================================================

    def check_order(
        self,
        pool: str,
        side: str,
        value: float,
        net_exposure: int = 0,
    ) -> tuple[bool, str]:
        """Check if an order is allowed by all risk rules.

        This is the MAIN entry point. Call before every order.

        Args:
            pool: "mm_pool" or "nc_pool"
            side: "BUY" or "SELL"
            value: USDC value of the order (price * quantity)
            net_exposure: Current net inventory in contracts (for MM only)

        Returns:
            (True, "ok") if allowed
            (False, "reason") if blocked
        """
        self._check_daily_reset()

        # Kill switch overrides everything
        if self._kill_switch_active:
            return False, "Kill switch is active"

        # All trading stopped (total loss > $80)
        if self._all_stopped:
            return False, f"All trading stopped: total loss ${abs(self._total_pnl):.2f} > ${settings.risk.max_total_loss}"

        # API pause (3 consecutive errors)
        if self._api_paused:
            return False, "Trading paused: too many consecutive API errors"

        # Pool doesn't exist
        if pool not in self._pool_balances:
            return False, f"Unknown pool: {pool}"

        # Pool permanently stopped
        if self._pool_stopped.get(pool, False):
            return False, f"{pool} permanently stopped: cumulative loss exceeded ${settings.risk.max_cumulative_loss_per_pool}"

        # Pool paused for today
        if self._pool_paused.get(pool, False):
            return False, f"{pool} paused until tomorrow: daily loss exceeded ${settings.risk.max_daily_loss_per_pool}"

        # Insufficient balance
        if value > self._pool_balances[pool]:
            return False, f"{pool} insufficient balance: ${self._pool_balances[pool]:.2f} < ${value:.2f} needed"

        # MM exposure check — only block orders that INCREASE exposure beyond limit
        if pool == "mm_pool":
            max_exp = settings.risk.max_mm_exposure_contracts
            if abs(net_exposure) >= max_exp:
                # Over limit: only allow orders that REDUCE exposure
                increasing = (net_exposure > 0 and side == "BUY") or (net_exposure < 0 and side == "SELL")
                if increasing:
                    return False, f"MM exposure limit: {abs(net_exposure)} contracts >= {max_exp} max. Only reducing positions allowed."

        # NC-specific checks
        if pool == "nc_pool":
            if self._nc_failures >= settings.nc.max_failures:
                return False, f"NC permanently stopped: {self._nc_failures} failures >= {settings.nc.max_failures} max"

        return True, "ok"

    def check_resolution_buffer(self, hours_to_resolution: float) -> tuple[bool, str]:
        """Check if a market is too close to resolution.

        Markets within 2 hours of resolution are dangerous because
        prices become volatile and liquidity dries up.
        """
        buffer = settings.mm.resolution_buffer_hours
        if hours_to_resolution < buffer:
            return False, f"Market resolves in {hours_to_resolution:.1f}h (< {buffer}h buffer)"
        return True, "ok"

    # ================================================================
    # Recording Results
    # ================================================================

    def record_trade_result(self, pool: str, pnl: float) -> None:
        """Record the P&L from a completed trade.

        Called after a trade executes.
        Updates daily and cumulative P&L, and triggers circuit breakers
        if thresholds are crossed.

        Args:
            pool: "mm_pool" or "nc_pool"
            pnl: Profit (positive) or loss (negative) in USDC
        """
        self._daily_pnl[pool] = self._daily_pnl.get(pool, 0.0) + pnl
        self._cumulative_pnl[pool] = self._cumulative_pnl.get(pool, 0.0) + pnl
        self._total_pnl += pnl
        self._pool_balances[pool] += pnl

        # Check circuit breakers after each trade
        self._check_circuit_breakers(pool)

    def record_nc_failure(self) -> None:
        """Record a Near-Certainties failure (position resolved NO).

        After 1 failure: reduce position size to $20 (handled by NC strategy).
        After 2 failures: stop NC permanently (handled by check_order).
        """
        self._nc_failures += 1
        logger.warning(
            f"NC failure #{self._nc_failures} recorded. "
            f"Max: {settings.nc.max_failures}"
        )

    def record_api_error(self) -> None:
        """Record a consecutive API error.

        After 3 in a row, all trading pauses.
        """
        self._consecutive_api_errors += 1
        if self._consecutive_api_errors >= settings.risk.max_consecutive_api_errors:
            self._api_paused = True
            logger.error(
                f"API PAUSE: {self._consecutive_api_errors} consecutive errors. "
                f"All trading paused."
            )

    def record_api_success(self) -> None:
        """Reset API error counter on successful call."""
        if self._consecutive_api_errors > 0:
            self._consecutive_api_errors = 0
        if self._api_paused:
            self._api_paused = False
            logger.info("API pause lifted: successful API call")

    # ================================================================
    # Kill Switch
    # ================================================================

    def activate_kill_switch(self) -> None:
        """Emergency stop. Triggered by Telegram /kill command.

        The order_manager is responsible for cancelling orders and
        closing positions. This just sets the flag.
        """
        self._kill_switch_active = True
        logger.critical("KILL SWITCH ACTIVATED — all trading stopped")

    def deactivate_kill_switch(self) -> None:
        """Re-enable trading after kill switch. Use with caution."""
        self._kill_switch_active = False
        logger.warning("Kill switch deactivated — trading re-enabled")

    # ================================================================
    # State Queries
    # ================================================================

    def get_pool_balance(self, pool: str) -> float:
        """Get current balance of a pool."""
        return self._pool_balances.get(pool, 0.0)

    def get_nc_failure_count(self) -> int:
        """Get number of NC failures so far."""
        return self._nc_failures

    def get_nc_position_size(self) -> int:
        """Get current NC position size (reduced after first failure)."""
        if self._nc_failures >= 1:
            return settings.nc.reduced_size  # $20
        return settings.nc.position_size  # $30

    def is_pool_active(self, pool: str) -> bool:
        """Check if a pool is active (not paused, stopped, or killed)."""
        if self._kill_switch_active or self._all_stopped or self._api_paused:
            return False
        if self._pool_stopped.get(pool, False):
            return False
        if self._pool_paused.get(pool, False):
            return False
        return True

    def get_status(self) -> dict[str, Any]:
        """Get full risk manager status. Used by Telegram /status."""
        return {
            "pool_balances": self._pool_balances.copy(),
            "daily_pnl": self._daily_pnl.copy(),
            "cumulative_pnl": self._cumulative_pnl.copy(),
            "total_pnl": self._total_pnl,
            "pool_paused": self._pool_paused.copy(),
            "pool_stopped": self._pool_stopped.copy(),
            "all_stopped": self._all_stopped,
            "kill_switch": self._kill_switch_active,
            "api_paused": self._api_paused,
            "api_errors": self._consecutive_api_errors,
            "nc_failures": self._nc_failures,
        }

    # ================================================================
    # Internal
    # ================================================================

    def _check_circuit_breakers(self, pool: str) -> None:
        """Evaluate all circuit breakers after a P&L change."""

        # Daily loss per pool > $20 → pause until tomorrow
        daily_loss = self._daily_pnl.get(pool, 0.0)
        if daily_loss < 0 and abs(daily_loss) > settings.risk.max_daily_loss_per_pool:
            self._pool_paused[pool] = True
            logger.warning(
                f"CIRCUIT BREAKER: {pool} paused until tomorrow. "
                f"Daily loss ${abs(daily_loss):.2f} > "
                f"${settings.risk.max_daily_loss_per_pool} limit"
            )

        # Cumulative loss per pool > $50 → stop permanently
        cum_loss = self._cumulative_pnl.get(pool, 0.0)
        if cum_loss < 0 and abs(cum_loss) > settings.risk.max_cumulative_loss_per_pool:
            self._pool_stopped[pool] = True
            logger.error(
                f"CIRCUIT BREAKER: {pool} PERMANENTLY STOPPED. "
                f"Cumulative loss ${abs(cum_loss):.2f} > "
                f"${settings.risk.max_cumulative_loss_per_pool} limit"
            )

        # Total loss > $80 → stop everything
        if self._total_pnl < 0 and abs(self._total_pnl) > settings.risk.max_total_loss:
            self._all_stopped = True
            logger.critical(
                f"CIRCUIT BREAKER: ALL TRADING STOPPED. "
                f"Total loss ${abs(self._total_pnl):.2f} > "
                f"${settings.risk.max_total_loss} limit"
            )

    def _check_daily_reset(self) -> None:
        """Reset daily P&L and unpaused pools at midnight UTC.

        Pools that were paused for the day get unpaused.
        Pools that were permanently stopped stay stopped.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._daily_pnl = {"mm_pool": 0.0, "nc_pool": 0.0}

            # Unpause daily-paused pools (not permanently stopped ones)
            for pool in self._pool_paused:
                if self._pool_paused[pool] and not self._pool_stopped[pool]:
                    self._pool_paused[pool] = False
                    logger.info(f"Daily reset: {pool} unpaused for new day")

            logger.info(f"Daily P&L reset for {today}")


# Global instance
risk_manager = RiskManager()
