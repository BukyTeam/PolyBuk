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
from core.journal import journal
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
        pnl_hour: float,
        mm_pool: float,
        nc_pool: float,
        reserve: float,
    ) -> bool:
        """Send hourly status update.

        Cumulative volume / target / percent are fetched from the journal
        (single source of truth: polybuk.trades) so every reporting path
        shows the same KPI.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        progress = journal.get_volume_progress()

        msg = (
            f"Resumen horaria — {now}\n"
            f"{journal.format_volume_progress(progress)}\n"
            f"Volumen ultima hora: ${volume_hour:,.2f}\n"
            f"P&L hora: ${pnl_hour:+,.2f}\n"
            f"MM Pool: ${mm_pool:,.2f} | NC Pool: ${nc_pool:,.2f} | "
            f"Reserva: ${reserve:,.2f}"
        )
        return await self._send(msg)

    async def send_daily_report(
        self,
        date: str,
        volume_today: float,
        pnl_today: float,
        pnl_cumulative: float,
        trades_count: int,
        mm_trades: int,
        nc_trades: int,
        avg_spread: float,
    ) -> bool:
        """Send daily report. Volume KPI is pulled from the journal."""
        progress = journal.get_volume_progress()

        msg = (
            f"Reporte diario — {date}\n"
            f"{journal.format_volume_progress(progress)}\n"
            f"Volumen hoy: ${volume_today:,.2f}\n"
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
        progress = journal.get_volume_progress()
        msg = (
            f"PolyBuk iniciado [LIVE]\n"
            f"{journal.format_volume_progress(progress)}\n"
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
        """Handle /status command — show detailed state with markets."""
        if str(update.effective_chat.id) != self._chat_id:
            return

        from config.markets import get_mm_markets, get_nc_markets
        from core.inventory_manager import inventory_manager
        from core.supabase_client import db

        status = risk_manager.get_status()

        # --- Header with volume KPI (the #1 project metric) ---
        progress = journal.get_volume_progress()
        lines = [
            "PolyBuk [LIVE]",
            journal.format_volume_progress(progress),
            "",
        ]

        # --- Active Markets ---
        mm_markets = get_mm_markets()
        if mm_markets:
            lines.append("Mercados MM activos:")
            for m in mm_markets:
                inv = inventory_manager.get_net_inventory(m.token_id)
                inv_str = f" inv={inv:+d}" if inv != 0 else ""
                lines.append(f"  {m.name}{inv_str}")
            lines.append("")

        nc_markets = get_nc_markets()
        if nc_markets:
            lines.append("Mercados NC activos:")
            for m in nc_markets:
                lines.append(f"  {m.name}")
            lines.append("")

        # --- Last 3 trades from Supabase ---
        recent = db.select(
            "trades",
            columns="side, price, quantity, market_name, created_at",
            order_by="created_at",
            descending=True,
            limit=3,
        )
        if recent:
            lines.append("Ultimos trades:")
            for t in recent:
                name = (t.get("market_name") or "?")[:25]
                side = t.get("side", "?")
                price = float(t.get("price", 0))
                qty = t.get("quantity", 0)
                ts = str(t.get("created_at", ""))[11:19]
                lines.append(f"  {side} {qty}x ${price:.2f} {name} [{ts}]")
            lines.append("")

        # --- Pools & Risk ---
        lines.append("Pools:")
        mm_flag = " PAUSADO" if status["pool_paused"]["mm_pool"] else ""
        mm_flag = " DETENIDO" if status["pool_stopped"]["mm_pool"] else mm_flag
        nc_flag = " PAUSADO" if status["pool_paused"]["nc_pool"] else ""
        nc_flag = " DETENIDO" if status["pool_stopped"]["nc_pool"] else nc_flag
        lines.append(f"  MM: ${status['pool_balances']['mm_pool']:,.2f}{mm_flag}")
        lines.append(f"  NC: ${status['pool_balances']['nc_pool']:,.2f}{nc_flag}")
        lines.append(f"  Reserva: ${status['pool_balances']['reserve']:,.2f}")
        lines.append("")

        lines.append(
            f"P&L hoy: MM ${status['daily_pnl']['mm_pool']:+,.2f} | "
            f"NC ${status['daily_pnl']['nc_pool']:+,.2f}"
        )
        lines.append(f"P&L total: ${status['total_pnl']:+,.2f}")

        if status["nc_failures"] > 0:
            lines.append(f"NC fallos: {status['nc_failures']}/{settings.nc.max_failures}")
        if status["api_errors"] > 0:
            lines.append(f"API errors: {status['api_errors']}")
        if status["kill_switch"]:
            lines.append("KILL SWITCH ACTIVO")

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
