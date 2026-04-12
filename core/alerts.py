"""
PolyBuk - Telegram Alerts & Commands

Sends alerts to your Telegram and handles commands (/kill, /status).

Three types of outgoing messages:
1. Immediate alerts — circuit breakers, errors, critical events
2. Hourly summary — volume, P&L, pool balances
3. Daily report — full day metrics

Two incoming commands:
- /kill — emergency stop (cancel all, stop bot)
- /status — show current state

Usage:
    from core.alerts import alerts
    await alerts.send_alert("Circuit breaker activated!")
    await alerts.start_command_listener()
"""

import asyncio
import logging
from typing import Any

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config.settings import settings
from core.risk_manager import risk_manager

logger = logging.getLogger(__name__)


class TelegramAlerts:
    """Sends alerts and receives commands via Telegram."""

    def __init__(self):
        self._bot: Bot | None = None
        self._app: Application | None = None
        self._chat_id: str = ""

    def initialize(self) -> bool:
        """Set up Telegram bot. Call once at startup.

        Returns True if credentials are present, False otherwise.
        """
        token = settings.telegram.bot_token
        self._chat_id = settings.telegram.chat_id

        if not token or not self._chat_id:
            logger.warning(
                "Telegram credentials missing — alerts disabled. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
            )
            return False

        self._bot = Bot(token=token)
        self._app = Application.builder().token(token).build()

        # Register command handlers
        self._app.add_handler(CommandHandler("kill", self._handle_kill))
        self._app.add_handler(CommandHandler("status", self._handle_status))

        logger.info("Telegram alerts initialized")
        return True

    # ================================================================
    # Sending Messages
    # ================================================================

    async def send_alert(self, message: str) -> bool:
        """Send an immediate alert message.

        Used for circuit breakers, errors, and critical events.
        """
        return await self._send(message)

    async def send_hourly_summary(
        self,
        volume_hour: float,
        volume_cumulative: float,
        volume_target: float,
        pnl_hour: float,
        mm_pool: float,
        nc_pool: float,
        reserve: float,
    ) -> bool:
        """Send hourly status update (spec format)."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        pct = (volume_cumulative / volume_target * 100) if volume_target > 0 else 0

        msg = (
            f"Resumen horaria — {now}\n"
            f"Volumen ultima hora: ${volume_hour:,.2f}\n"
            f"Volumen acumulado: ${volume_cumulative:,.2f} / "
            f"${volume_target:,.0f} ({pct:.1f}%)\n"
            f"P&L hora: ${pnl_hour:+,.2f}\n"
            f"MM Pool: ${mm_pool:,.2f} | NC Pool: ${nc_pool:,.2f} | "
            f"Reserva: ${reserve:,.2f}"
        )
        return await self._send(msg)

    async def send_daily_report(
        self,
        date: str,
        volume_today: float,
        volume_cumulative: float,
        volume_target: float,
        pnl_today: float,
        pnl_cumulative: float,
        trades_count: int,
        mm_trades: int,
        nc_trades: int,
        avg_spread: float,
    ) -> bool:
        """Send daily report (spec format)."""
        pct = (volume_cumulative / volume_target * 100) if volume_target > 0 else 0

        msg = (
            f"Reporte diario — {date}\n"
            f"Volumen hoy: ${volume_today:,.2f}\n"
            f"Acumulado: ${volume_cumulative:,.2f} / "
            f"${volume_target:,.0f} ({pct:.1f}%)\n"
            f"P&L hoy: ${pnl_today:+,.2f} | "
            f"Acumulado: ${pnl_cumulative:+,.2f}\n"
            f"Trades: {trades_count} ({mm_trades} MM, {nc_trades} NC)\n"
            f"Spread promedio: ${avg_spread:.3f}"
        )
        return await self._send(msg)

    async def send_circuit_breaker_alert(
        self,
        pool: str,
        trigger: str,
        action: str,
    ) -> bool:
        """Send circuit breaker alert (spec format)."""
        msg = (
            f"CIRCUIT BREAKER ACTIVADO\n"
            f"Pool: {pool}\n"
            f"Trigger: {trigger}\n"
            f"Accion: {action}"
        )
        return await self._send(msg)

    async def send_startup_message(self) -> bool:
        """Notify that the bot has started."""
        mode = "PAPER" if settings.paper.enabled else "LIVE"
        msg = (
            f"PolyBuk iniciado [{mode}]\n"
            f"MM Pool: ${settings.risk.mm_pool:,.2f}\n"
            f"NC Pool: ${settings.risk.nc_pool:,.2f}\n"
            f"Reserve: ${settings.risk.reserve:,.2f}"
        )
        return await self._send(msg)

    # ================================================================
    # Command Handlers
    # ================================================================

    async def _handle_kill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /kill command — emergency stop.

        Activates kill switch in risk manager. The main loop will
        detect this and cancel all orders + stop trading.
        """
        # Only respond to our chat
        if str(update.effective_chat.id) != self._chat_id:
            return

        risk_manager.activate_kill_switch()
        await update.message.reply_text(
            "KILL SWITCH ACTIVADO\n"
            "Cancelando ordenes y deteniendo bot..."
        )
        logger.critical("Kill switch activated via Telegram /kill")

    async def _handle_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /status command — show current state."""
        if str(update.effective_chat.id) != self._chat_id:
            return

        status = risk_manager.get_status()
        mode = "PAPER" if settings.paper.enabled else "LIVE"

        lines = [
            f"Estado PolyBuk [{mode}]",
            f"",
            f"Pools:",
            f"  MM: ${status['pool_balances']['mm_pool']:,.2f}"
            f" {'(pausado)' if status['pool_paused']['mm_pool'] else ''}"
            f" {'(DETENIDO)' if status['pool_stopped']['mm_pool'] else ''}",
            f"  NC: ${status['pool_balances']['nc_pool']:,.2f}"
            f" {'(pausado)' if status['pool_paused']['nc_pool'] else ''}"
            f" {'(DETENIDO)' if status['pool_stopped']['nc_pool'] else ''}",
            f"  Reserva: ${status['pool_balances']['reserve']:,.2f}",
            f"",
            f"P&L hoy: MM ${status['daily_pnl']['mm_pool']:+,.2f} | "
            f"NC ${status['daily_pnl']['nc_pool']:+,.2f}",
            f"P&L total: ${status['total_pnl']:+,.2f}",
            f"",
            f"NC fallos: {status['nc_failures']}/{settings.nc.max_failures}",
            f"API errors: {status['api_errors']}",
            f"Kill switch: {'ACTIVO' if status['kill_switch'] else 'inactivo'}",
        ]

        await update.message.reply_text("\n".join(lines))

    # ================================================================
    # Polling (for receiving commands)
    # ================================================================

    async def start_polling(self) -> None:
        """Start listening for Telegram commands.

        This runs in the background alongside the trading loop.
        Uses polling (not webhooks) because our VPS doesn't need
        to expose ports — simpler and more secure.
        """
        if self._app is None:
            logger.warning("Telegram not initialized, skipping polling")
            return

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram command listener started")

    async def stop_polling(self) -> None:
        """Stop listening for commands. Call on shutdown."""
        if self._app and self._app.updater.running:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram command listener stopped")

    # ================================================================
    # Internal
    # ================================================================

    async def _send(self, message: str) -> bool:
        """Send a message to the configured chat."""
        if not self._bot or not self._chat_id:
            logger.debug(f"Telegram not configured, would send: {message[:80]}...")
            return False

        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=message,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False


# Global instance
alerts = TelegramAlerts()
