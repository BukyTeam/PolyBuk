"""
PolyBuk - Base Strategy (Abstract)

Every trading strategy must inherit from this class and implement
its abstract methods. This ensures all strategies have a consistent
interface that main.py can use to run them.

To add a new strategy:
1. Create strategies/my_strategy.py
2. Create a class that inherits from BaseStrategy
3. Implement all abstract methods (setup, execute_cycle, cleanup)
4. Register it in main.py

Example:
    class MyStrategy(BaseStrategy):
        @property
        def name(self) -> str:
            return "my_strategy"

        @property
        def pool(self) -> str:
            return "mm_pool"

        @property
        def cycle_interval(self) -> int:
            return 30

        async def setup(self) -> bool:
            # Initialize strategy-specific state
            return True

        async def execute_cycle(self) -> None:
            # Run one iteration of the strategy
            ...

        async def cleanup(self) -> None:
            # Cancel orders, save state
            ...
"""

import logging
from abc import ABC, abstractmethod

from core.risk_manager import risk_manager

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Lifecycle:
        1. __init__() — created by main.py
        2. setup() — called once at startup (validate markets, etc.)
        3. execute_cycle() — called repeatedly at cycle_interval
        4. cleanup() — called on shutdown (cancel orders, save state)
    """

    def __init__(self):
        self._running: bool = False

    # ================================================================
    # Abstract Properties (must be implemented by each strategy)
    # ================================================================

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for logging and journal entries.

        e.g., "market_maker", "near_certainties"
        """
        ...

    @property
    @abstractmethod
    def pool(self) -> str:
        """Which capital pool this strategy uses.

        e.g., "mm_pool", "nc_pool"
        """
        ...

    @property
    @abstractmethod
    def cycle_interval(self) -> int:
        """Seconds between cycles.

        e.g., 30 for market maker, 300 for near-certainties
        """
        ...

    # ================================================================
    # Abstract Methods (must be implemented by each strategy)
    # ================================================================

    @abstractmethod
    async def setup(self) -> bool:
        """Initialize strategy. Called once at startup.

        Use this to validate that markets are configured, check
        initial balances, etc.

        Returns True if ready to trade, False if something is wrong.
        """
        ...

    @abstractmethod
    async def execute_cycle(self) -> None:
        """Run one cycle of the strategy.

        This is where the main trading logic lives:
        - Market Maker: get book → calculate prices → place orders
        - Near-Certainties: scan markets → evaluate → buy if good

        Called every cycle_interval seconds by main.py.
        Must handle its own errors (log and continue, don't crash).
        """
        ...

    @abstractmethod
    async def cleanup(self) -> None:
        """Shutdown gracefully. Called when the bot stops.

        Cancel open orders, save final state, etc.
        """
        ...

    # ================================================================
    # Shared Methods (available to all strategies)
    # ================================================================

    def is_pool_active(self) -> bool:
        """Check if this strategy's pool is still allowed to trade.

        Checks kill switch, circuit breakers, and pool state.
        Strategies should call this at the start of each cycle.
        """
        return risk_manager.is_pool_active(self.pool)

    def log_cycle_skip(self, reason: str) -> None:
        """Log when a cycle is skipped (pool paused, no markets, etc.)."""
        logger.info(f"[{self.name}] Cycle skipped: {reason}")

    @property
    def is_running(self) -> bool:
        return self._running

    @is_running.setter
    def is_running(self, value: bool) -> None:
        self._running = value
