"""
PolyBuk - Supabase Database Client

All database operations go through this module. Uses the service_role key
for full access (bypasses RLS) and targets the 'polybuk' schema.

IMPORTANT: Before using this client, you must expose the 'polybuk' schema
in Supabase Dashboard → Settings → API → Exposed schemas.
"""

import logging
from typing import Any

from supabase import Client, create_client
from supabase.client import ClientOptions

from config.settings import settings

logger = logging.getLogger(__name__)


class SupabaseClient:
    """Wrapper around the Supabase Python SDK.

    Provides simplified insert/select methods that all target
    the polybuk schema automatically.

    Usage:
        from core.supabase_client import db
        db.initialize()
        db.insert("trades", {"strategy": "mm", "price": 0.50, ...})
        rows = db.select("trades", limit=10)
    """

    def __init__(self):
        self._client: Client | None = None

    def initialize(self) -> bool:
        """Connect to Supabase. Call once at startup.

        Returns True if successful, False on error.
        """
        try:
            url = settings.supabase.url
            key = settings.supabase.service_key
            schema = settings.supabase.schema  # "polybuk"

            if not url or not key:
                logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY is empty in .env")
                return False

            # ClientOptions sets the default schema for ALL queries.
            # This means .table("trades") targets polybuk.trades, not public.trades.
            self._client = create_client(
                url,
                key,
                options=ClientOptions(schema=schema),
            )
            logger.info(f"Supabase client initialized (schema: {schema})")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}")
            return False

    # ================================================================
    # Write Operations
    # ================================================================

    def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Insert a single row into a table.

        Args:
            table: Table name (e.g., "trades", "decisions")
            data: Dict of column names to values

        Returns the inserted row as a dict, or None on error.

        Example:
            db.insert("trades", {
                "strategy": "market_maker",
                "market_id": "0x1234...",
                "side": "BUY",
                "price": 0.4500,
                "quantity": 20,
                "pool": "mm_pool",
            })
        """
        try:
            resp = self._client.table(table).insert(data).execute()
            if resp.data:
                logger.debug(f"Inserted into {table}: {len(resp.data)} row(s)")
                return resp.data[0]
            return None
        except Exception as e:
            logger.error(f"Insert into {table} failed: {e}")
            return None

    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert multiple rows at once.

        More efficient than calling insert() in a loop because it
        makes a single API call instead of N calls.

        Returns list of inserted rows, or empty list on error.
        """
        if not rows:
            return []
        try:
            resp = self._client.table(table).insert(rows).execute()
            logger.debug(f"Inserted into {table}: {len(resp.data)} row(s)")
            return resp.data or []
        except Exception as e:
            logger.error(f"Insert many into {table} failed: {e}")
            return []

    # ================================================================
    # Read Operations
    # ================================================================

    def select(
        self,
        table: str,
        columns: str = "*",
        filters: dict[str, Any] | None = None,
        order_by: str | None = None,
        descending: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query rows from a table.

        Args:
            table: Table name
            columns: Comma-separated column names, or "*" for all
            filters: Dict of {column: value} for equality filters
            order_by: Column to sort by
            descending: Sort direction (True = newest first)
            limit: Max rows to return

        Returns list of row dicts, or empty list on error.

        Example:
            recent_trades = db.select(
                "trades",
                filters={"strategy": "market_maker"},
                order_by="created_at",
                limit=50,
            )
        """
        try:
            query = self._client.table(table).select(columns)

            if filters:
                for col, val in filters.items():
                    query = query.eq(col, val)

            if order_by:
                query = query.order(order_by, desc=descending)

            if limit:
                query = query.limit(limit)

            resp = query.execute()
            return resp.data or []

        except Exception as e:
            logger.error(f"Select from {table} failed: {e}")
            return []

    def select_one(
        self, table: str, filters: dict[str, Any], columns: str = "*"
    ) -> dict[str, Any] | None:
        """Get a single row matching filters.

        Returns the row dict, or None if not found or on error.
        Useful for lookups like "get the config snapshot with this ID".
        """
        try:
            query = self._client.table(table).select(columns)
            for col, val in filters.items():
                query = query.eq(col, val)
            resp = query.limit(1).execute()
            if resp.data:
                return resp.data[0]
            return None
        except Exception as e:
            logger.error(f"Select one from {table} failed: {e}")
            return None

    # ================================================================
    # Update Operations
    # ================================================================

    def update(
        self, table: str, filters: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update rows matching filters.

        Args:
            table: Table name
            filters: Dict of {column: value} to identify which rows to update
            data: Dict of {column: new_value} to set

        Returns the updated row, or None on error.
        """
        try:
            query = self._client.table(table).update(data)
            for col, val in filters.items():
                query = query.eq(col, val)
            resp = query.execute()
            if resp.data:
                return resp.data[0]
            return None
        except Exception as e:
            logger.error(f"Update {table} failed: {e}")
            return None

    # ================================================================
    # Utility
    # ================================================================

    def is_initialized(self) -> bool:
        """Check if the client was successfully initialized."""
        return self._client is not None

    def test_connection(self) -> bool:
        """Verify we can read from the database.

        Tries to select from human_decisions (lightest table).
        Returns True if the query succeeds, False otherwise.
        """
        try:
            self._client.table("human_decisions").select("id").limit(1).execute()
            logger.info("Supabase connection test: OK")
            return True
        except Exception as e:
            logger.error(f"Supabase connection test failed: {e}")
            return False


# Global instance — import this everywhere
# Call db.initialize() once at bot startup (in main.py)
db = SupabaseClient()
