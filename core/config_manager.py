"""
PolyBuk - Config Manager

Saves snapshots of the full configuration to Supabase every time
the bot starts or parameters change. This creates an audit trail
so you can correlate performance changes with parameter changes.

Usage:
    from core.config_manager import config_manager
    config_manager.save_snapshot("bot_startup")
    config_manager.save_snapshot("parameter_change", "Increased MM order size to 30")
"""

import logging
from dataclasses import asdict
from typing import Any

from config.settings import settings
from core.supabase_client import db

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manages configuration versioning in Supabase."""

    def save_snapshot(
        self,
        changed_by: str = "system",
        change_reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Save current configuration to polybuk.config_history.

        Called automatically at bot startup, and manually when you
        change parameters in settings.py.

        The snapshot captures ALL settings (risk, MM, NC, general, etc.)
        as a single JSON blob. Credentials are excluded for security.

        Args:
            changed_by: Who triggered the change ("system", "operator", etc.)
            change_reason: Why the change was made (optional but recommended)
        """
        try:
            # Convert all settings to a dict, excluding credentials
            snapshot = {
                "risk": asdict(settings.risk),
                "market_maker": asdict(settings.mm),
                "near_certainties": asdict(settings.nc),
                "general": asdict(settings.general),
            }

            data = {
                "config_snapshot": snapshot,
                "changed_by": changed_by,
                "change_reason": change_reason,
            }

            row = db.insert("config_history", data)
            if row:
                logger.info(
                    f"Config snapshot saved (by: {changed_by}, "
                    f"reason: {change_reason or 'none'})"
                )
            return row

        except Exception as e:
            logger.error(f"Failed to save config snapshot: {e}")
            return None

# Global instance
config_manager = ConfigManager()
