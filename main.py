"""
PolyBuk Trading Framework — Entry Point

Usage:
    python main.py                    # Run both strategies
    python main.py --strategy mm      # Only market maker
    python main.py --strategy nc      # Only near-certainties
    python main.py --dry-run          # Initialize everything, run 1 cycle, then exit

The bot runs indefinitely until you stop it with:
- Ctrl+C in the terminal
- /kill command in Telegram
- Kill switch (total loss circuit breaker)
"""

import argparse
import asyncio
import logging
import signal
import sys
import os

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import settings
from core.alerts import alerts
from core.config_manager import config_manager
from core.journal import journal
from core.order_manager import order_manager
from core.polymarket_client import polymarket_client
from core.risk_manager import risk_manager
from core.supabase_client import db
from strategies.market_maker import MarketMakerStrategy
from strategies.near_certainties import NearCertaintiesStrategy

logger = logging.getLogger("polybuk")


def setup_logging() -> None:
    """Configure logging to console with timestamps."""
    level = getattr(logging, settings.general.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="PolyBuk Trading Framework")
    parser.add_argument(
        "--strategy",
        choices=["mm", "nc", "both"],
        default="both",
        help="Which strategy to run (default: both)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run 1 cycle of each strategy then exit",
    )
    return parser.parse_args()


async def initialize_clients() -> bool:
    """Initialize all API clients. Returns True if all succeeded."""
    success = True

    # Supabase (database)
    if not db.initialize():
        logger.error("Failed to initialize Supabase client")
        success = False
    elif not db.test_connection():
        logger.error("Supabase connection test failed")
        success = False

    # Polymarket (trading API)
    if not polymarket_client.initialize():
        logger.error("Failed to initialize Polymarket client")
        success = False

    # Telegram (alerts — optional, bot works without it)
    alerts.initialize()

    return success


async def run_strategy_loop(strategy, dry_run: bool = False) -> None:
    """Run a strategy in a loop with its configured interval.

    Each cycle:
    1. Check if kill switch is active → stop if yes
    2. Execute one cycle of the strategy
    3. Sleep for cycle_interval seconds
    4. Repeat

    Errors in a single cycle are caught and logged — the bot continues.
    """
    name = strategy.name
    interval = strategy.cycle_interval

    # Setup
    if not await strategy.setup():
        logger.warning(f"[{name}] Setup failed, skipping strategy")
        return

    strategy.is_running = True
    logger.info(f"[{name}] Starting loop (every {interval}s)")

    try:
        while strategy.is_running:
            # Check kill switch
            if risk_manager._kill_switch_active or risk_manager._all_stopped:
                logger.info(f"[{name}] Stopping: kill switch or all-stop active")
                break

            try:
                await strategy.execute_cycle()
            except Exception as e:
                logger.error(f"[{name}] Cycle error: {e}", exc_info=True)

            if dry_run:
                logger.info(f"[{name}] Dry run complete — exiting")
                break

            await asyncio.sleep(interval)

    finally:
        strategy.is_running = False
        await strategy.cleanup()
        logger.info(f"[{name}] Loop stopped")


async def main() -> None:
    """Main entry point."""
    args = parse_args()
    setup_logging()

    logger.info("=== PolyBuk Framework Starting [LIVE] ===")

    # Initialize all clients
    if not await initialize_clients():
        logger.error("Client initialization failed. Check your .env file.")
        sys.exit(1)

    # Save config snapshot
    config_manager.save_snapshot(
        changed_by="system",
        change_reason=f"Bot startup (strategy={args.strategy})",
    )

    # Send Telegram startup message
    await alerts.send_startup_message()

    # Log startup to journal
    journal.log_human(
        action="bot_startup",
        details=f"PolyBuk started. Strategy: {args.strategy}.",
        context={
            "strategy": args.strategy,
            "dry_run": args.dry_run,
        },
    )

    # Create strategies
    tasks = []

    if args.strategy in ("mm", "both"):
        mm = MarketMakerStrategy()
        tasks.append(run_strategy_loop(mm, dry_run=args.dry_run))

    if args.strategy in ("nc", "both"):
        nc = NearCertaintiesStrategy()
        tasks.append(run_strategy_loop(nc, dry_run=args.dry_run))

    if not tasks:
        logger.error("No strategies to run")
        sys.exit(1)

    # Start Telegram command listener (for /kill, /status)
    await alerts.start_polling()

    # Run all strategies concurrently
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled — shutting down")

    # Cleanup
    await alerts.stop_polling()
    logger.info("=== PolyBuk Framework Stopped ===")


def handle_shutdown(signum, frame):
    """Handle Ctrl+C gracefully."""
    logger.info("Shutdown signal received (Ctrl+C)")
    risk_manager.activate_kill_switch()


if __name__ == "__main__":
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    asyncio.run(main())
