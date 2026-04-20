"""
PolyBuk Framework - Global Settings

All parameters from the spec live here. Each module gets its own dataclass
so parameters are grouped logically and typos cause immediate errors.

To change a parameter: edit the default value here, restart the bot.
The config_manager will snapshot changes to Supabase for audit trail.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (two levels up from config/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


# --- Helper to read env vars with type conversion ---

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ============================================================
# Polymarket API Credentials
# ============================================================

@dataclass(frozen=True)
class PolymarketCredentials:
    """Credentials for Polymarket CLOB and Gamma APIs.

    frozen=True means these can't be accidentally changed at runtime.
    The private key is used by py-clob-client to derive API L2 headers.

    funder_address: Polymarket accounts created via the web UI use a
    Gnosis Safe proxy. The EOA (derived from private_key) signs, but the
    USDC funds live at the Safe address. If set, the CLOB client runs in
    POLY_GNOSIS_SAFE mode (signature_type=2) and reports balances against
    the Safe. If empty, the client runs in direct EOA mode. You can find
    the Safe address in Polymarket UI → profile → deposit.
    """
    api_key: str = field(default_factory=lambda: _env("POLYMARKET_API_KEY"))
    api_secret: str = field(default_factory=lambda: _env("POLYMARKET_API_SECRET"))
    api_passphrase: str = field(default_factory=lambda: _env("POLYMARKET_API_PASSPHRASE"))
    private_key: str = field(default_factory=lambda: _env("POLYMARKET_PRIVATE_KEY"))
    funder_address: str = field(default_factory=lambda: _env("POLYMARKET_FUNDER_ADDRESS"))


# ============================================================
# Supabase Credentials
# ============================================================

@dataclass(frozen=True)
class SupabaseCredentials:
    """Supabase connection details.

    service_key has full access (used by the bot).
    anon_key is read-only (not used by bot, but kept for reference).
    Schema is always 'polybuk', never 'public'.
    """
    url: str = field(default_factory=lambda: _env("SUPABASE_URL"))
    anon_key: str = field(default_factory=lambda: _env("SUPABASE_ANON_KEY"))
    service_key: str = field(default_factory=lambda: _env("SUPABASE_SERVICE_KEY"))
    schema: str = "polybuk"


# ============================================================
# Telegram Credentials
# ============================================================

@dataclass(frozen=True)
class TelegramCredentials:
    """Telegram bot for alerts and commands."""
    bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))


# ============================================================
# Risk Settings (Spec Section 4)
# ============================================================

@dataclass(frozen=True)
class RiskSettings:
    """Capital pools and circuit breaker thresholds.

    Pools NEVER lend money to each other. Each pool is independent.
    Circuit breakers are safety nets that stop trading when losses exceed limits.

    Recalibrated 2026-04-20 to actual deposited capital of $102 after
    the Bangalore droplet restart. Only MM runs; NC stays disabled until
    MM proves stable. Hard kill at -$20 (~20% drawdown), daily pause at -$10.
    """
    # Capital pools (in USDC)
    total_capital: float = 102.0
    mm_pool: float = 80.0           # Market Maker only
    nc_pool: float = 0.0            # Disabled
    reserve: float = 22.0           # Buffer for gas / settlement slippage

    # Circuit breakers
    max_daily_loss_per_pool: float = 10.0    # Pause pool until tomorrow
    max_cumulative_loss_per_pool: float = 20.0  # Stop pool permanently (same threshold as total)
    max_total_loss: float = 20.0             # Stop EVERYTHING (~20% drawdown of $102)
    max_mm_exposure_contracts: int = 30      # Only allow reducing positions beyond this
    max_consecutive_api_errors: int = 3      # Pause all trading

    # Kill switch
    kill_switch_enabled: bool = True


# ============================================================
# Market Maker Settings (Spec Section 5)
# ============================================================

@dataclass(frozen=True)
class MarketMakerSettings:
    """Parameters for the Market Maker strategy.

    The MM places bid+ask orders around the mid price to capture the spread.
    It runs every 30 seconds, cancels stale orders, and adjusts prices
    based on inventory (skew function).
    """
    order_size: int = 7                # Contracts per order (sized so value stays under $5 cap at $0.665)
    half_spread_offset: float = 0.00   # Deprecated: pricing now joins best bid/ask
    max_exposure: int = 30             # Max net contracts in one direction
    stale_order_seconds: int = 60      # Cancel orders older than 1 minute (was 180)
    min_spread: float = 0.01           # Skip if spread too tight (Polymarket min tick is $0.01)
    max_spread: float = 0.15           # Skip if spread too wide (likely illiquid/risky)
    min_price: float = 0.10            # Don't operate at extremes
    max_price: float = 0.90            # Don't operate at extremes
    cycle_interval: int = 30           # Seconds between cycles
    resolution_buffer_hours: int = 2   # Close positions before market resolves
    max_order_value: float = 5.0       # Max USDC per single order


# ============================================================
# Near-Certainties Settings (Spec Section 6)
# ============================================================

@dataclass(frozen=True)
class NearCertaintiesSettings:
    """Parameters for the Near-Certainties strategy.

    NC buys outcomes priced at $0.93+ that are very likely to resolve YES.
    It's lower frequency (every 5 min) and more selective.
    After failures, it reduces size or stops entirely.
    """
    min_probability: float = 0.93      # Only buy if price >= this
    position_size: int = 30            # USDC per position
    max_positions: int = 3             # Max simultaneous open positions
    max_failures: int = 2              # Stop NC permanently after this many
    reduced_size: int = 20             # Size after first failure
    cycle_interval: int = 300          # 5 minutes between scans
    min_resolution_hours: int = 1      # Don't buy if resolves too soon
    max_resolution_hours: int = 24     # Don't buy if resolves too far out
    alert_price_drop: float = 0.85     # Alert if position drops below this


# ============================================================
# General Settings
# ============================================================

@dataclass(frozen=True)
class GeneralSettings:
    """Framework-wide settings."""
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    volume_target: float = 10_000.0    # USDC target to unlock Referral Program
    wallet_snapshot_interval: int = 3600  # Seconds (1 hour)


# ============================================================
# Master Settings Object
# ============================================================

@dataclass(frozen=True)
class Settings:
    """Single entry point for all configuration.

    Usage:
        from config.settings import settings
        print(settings.mm.order_size)       # 20
        print(settings.risk.mm_pool)        # 35.0
    """
    polymarket: PolymarketCredentials = field(default_factory=PolymarketCredentials)
    supabase: SupabaseCredentials = field(default_factory=SupabaseCredentials)
    telegram: TelegramCredentials = field(default_factory=TelegramCredentials)
    risk: RiskSettings = field(default_factory=RiskSettings)
    mm: MarketMakerSettings = field(default_factory=MarketMakerSettings)
    nc: NearCertaintiesSettings = field(default_factory=NearCertaintiesSettings)
    general: GeneralSettings = field(default_factory=GeneralSettings)


# Global instance — import this everywhere
settings = Settings()
