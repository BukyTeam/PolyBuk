"""
PolyBuk - Polymarket API Client

Wraps py-clob-client (CLOB API) and httpx (Gamma API) into a single
interface that the rest of the framework uses.

CLOB API (clob.polymarket.com): place/cancel orders, read orderbook
Gamma API (gamma-api.polymarket.com): discover markets, get metadata

Authentication: CLOB credentials are derived automatically from your
private key — no manual key generation needed.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BookParams,
    OpenOrderParams,
    OrderArgs,
    OrderType,
    TradeParams,
)
from py_clob_client.order_builder.constants import BUY, SELL

from config.settings import settings

logger = logging.getLogger(__name__)

# Polymarket endpoints
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
POLYGON_CHAIN_ID = 137


class PolymarketClient:
    """Unified client for all Polymarket API interactions.

    Usage:
        from core.polymarket_client import polymarket_client
        book = polymarket_client.get_order_book("token_id_here")
    """

    def __init__(self):
        self._clob: ClobClient | None = None
        self._http: httpx.Client | None = None

    def initialize(self) -> bool:
        """Connect to Polymarket APIs. Call once at startup.

        Returns True if successful, False if credentials are missing/invalid.

        Why separate from __init__: We want to create the global instance
        at import time (so other modules can import it), but only connect
        when the bot actually starts. This avoids connection errors during
        testing or when just importing config.
        """
        try:
            private_key = settings.polymarket.private_key
            if not private_key:
                logger.error("POLYMARKET_PRIVATE_KEY is empty in .env")
                return False

            # Step 1: Create client with private key.
            # If a funder_address is configured, the account is a Polymarket
            # proxy wallet — the EOA signs but funds live at the proxy.
            # Polymarket uses EIP-1167 minimal proxies (signature_type=1,
            # POLY_PROXY) for accounts created via their email/magic-link
            # Relayer flow — NOT Gnosis Safe. We verified the user's proxy
            # contract on-chain and it matches the EIP-1167 clone pattern.
            funder = settings.polymarket.funder_address.strip()
            if funder:
                self._clob = ClobClient(
                    host=CLOB_HOST,
                    key=private_key,
                    chain_id=POLYGON_CHAIN_ID,
                    signature_type=1,  # POLY_PROXY (EIP-1167)
                    funder=funder,
                )
                logger.info(
                    f"Polymarket CLOB client in POLY_PROXY mode "
                    f"(funder: {funder[:10]}...{funder[-6:]})"
                )
            else:
                self._clob = ClobClient(
                    host=CLOB_HOST,
                    key=private_key,
                    chain_id=POLYGON_CHAIN_ID,
                )
                logger.info("Polymarket CLOB client in EOA mode (no funder)")

            # Step 2: Derive CLOB API credentials from private key
            # This calls Polymarket's server to create/retrieve your CLOB keys.
            # It's idempotent — same key always gets same credentials.
            creds = self._clob.create_or_derive_api_creds()
            self._clob.set_api_creds(creds)
            logger.info("Polymarket CLOB client initialized (credentials derived)")

            # Step 3: Create HTTP client for Gamma API (market discovery)
            self._http = httpx.Client(
                base_url=GAMMA_HOST,
                timeout=15.0,
            )
            logger.info("Gamma API HTTP client initialized")

            return True

        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            return False

    # ================================================================
    # CLOB API — Order Book (read-only, no auth needed)
    # ================================================================

    def get_order_book(self, token_id: str) -> dict[str, Any] | None:
        """Get full order book for a token.

        Returns dict with 'bids', 'asks', 'market', 'asset_id', etc.
        Returns None on error.
        """
        try:
            book = self._clob.get_order_book(token_id)
            return book
        except Exception as e:
            logger.error(f"get_order_book failed for {token_id}: {e}")
            return None

    def get_midpoint(self, token_id: str) -> float | None:
        """Get mid price between best bid and best ask.

        This is the "fair price" the market maker uses as reference.
        Returns None on error.
        """
        try:
            mid = self._clob.get_midpoint(token_id)
            return float(mid)
        except Exception as e:
            logger.error(f"get_midpoint failed for {token_id}: {e}")
            return None

    def get_price(self, token_id: str, side: str = BUY) -> float | None:
        """Get best price on a given side.

        side: "BUY" (best bid) or "SELL" (best ask)
        """
        try:
            price = self._clob.get_price(token_id, side)
            return float(price)
        except Exception as e:
            logger.error(f"get_price failed for {token_id} {side}: {e}")
            return None

    def get_last_trade_price(self, token_id: str) -> float | None:
        """Get the most recent trade price for a token."""
        try:
            price = self._clob.get_last_trade_price(token_id)
            return float(price)
        except Exception as e:
            logger.error(f"get_last_trade_price failed for {token_id}: {e}")
            return None

    # ================================================================
    # CLOB API — Orders (authenticated)
    # ================================================================

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> dict[str, Any] | None:
        """Place a limit order (GTC = Good-Til-Cancelled).

        Args:
            token_id: The CLOB token ID (YES or NO outcome)
            side: "BUY" or "SELL"
            price: Price per contract (0.01 to 0.99)
            size: Number of contracts

        Returns the API response dict, or None on error.

        Why GTC: Our market maker cancels stale orders every 180 seconds.
        GTC means the order stays until we cancel it or it fills.
        """
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            signed_order = self._clob.create_order(order_args)
            resp = self._clob.post_order(signed_order, orderType=OrderType.GTC)
            logger.info(
                f"Order placed: {side} {size}x @ ${price:.4f} on {token_id[:16]}..."
            )
            return resp
        except Exception as e:
            logger.error(
                f"place_limit_order failed: {side} {size}x @ ${price:.4f} "
                f"on {token_id[:16]}...: {e}"
            )
            return None

    def cancel_order(self, order_id: str) -> dict[str, Any] | None:
        """Cancel a single order by ID."""
        try:
            resp = self._clob.cancel(order_id)
            logger.info(f"Order cancelled: {order_id}")
            return resp
        except Exception as e:
            logger.error(f"cancel_order failed for {order_id}: {e}")
            return None

    def cancel_all_orders(self) -> dict[str, Any] | None:
        """Cancel ALL open orders. Used by kill switch."""
        try:
            resp = self._clob.cancel_all()
            logger.info("All orders cancelled")
            return resp
        except Exception as e:
            logger.error(f"cancel_all_orders failed: {e}")
            return None

    def get_open_orders(
        self, market_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get all open orders, optionally filtered by market.

        Args:
            market_id: condition_id to filter by (optional)

        Returns list of order dicts, or empty list on error.
        """
        try:
            params = OpenOrderParams(market=market_id) if market_id else OpenOrderParams()
            resp = self._clob.get_orders(params)
            return resp if isinstance(resp, list) else []
        except Exception as e:
            logger.error(f"get_open_orders failed: {e}")
            return []

    def get_trades(
        self, market_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get executed trades (fills).

        Used by journal to log completed trades.
        """
        try:
            params = TradeParams(
                maker_address=self._clob.get_address(),
            )
            if market_id:
                params.market = market_id
            resp = self._clob.get_trades(params)
            return resp if isinstance(resp, list) else []
        except Exception as e:
            logger.error(f"get_trades failed: {e}")
            return []

    # ================================================================
    # Gamma API — Market Discovery (no auth needed)
    # ================================================================

    def get_markets(
        self,
        category: str | None = None,
        active: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search for markets via Gamma API.

        Used by Near-Certainties to scan for high-probability outcomes.

        Args:
            category: Filter by category (e.g., "sports", "crypto")
            active: Only return active (unresolved) markets
            limit: Max results to return
        """
        try:
            params: dict[str, Any] = {"limit": limit, "active": active}
            if category:
                params["tag"] = category
            resp = self._http.get("/markets", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"get_markets failed: {e}")
            return []

    def get_market_info(self, condition_id: str) -> dict[str, Any] | None:
        """Get detailed info about a specific market.

        Used to check resolution date, outcome, metadata.

        Gamma API lookup by condition_id is done via query param
        (/markets?condition_ids=0x...), not path param. The path form
        /markets/{id} only accepts Gamma's internal numeric id and
        returns 422 for condition_ids.
        """
        try:
            resp = self._http.get("/markets", params={"condition_ids": condition_id})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data[0] if data else None
            return data
        except Exception as e:
            logger.error(f"get_market_info failed for {condition_id}: {e}")
            return None

    def get_market_status(self, condition_id: str) -> dict[str, Any] | None:
        """Fetch real-time market status from Gamma API.

        Returns dict with: active, closed, resolving, accepting_orders,
        resolution_datetime, hours_to_resolution, outcome, condition_id.

        Returns None if the API call fails — caller should treat as
        "unknown" and skip the market for safety.

        Outcome detection: parses Gamma's outcomePrices/outcomes JSON arrays.
        A token with price >= 0.99 is the winning outcome.
        Resolving state is inferred from umaResolutionStatus, since Gamma
        does not return a direct 'resolving' boolean.
        """
        try:
            # Gamma only accepts condition_ids via query param; path form
            # /markets/{id} returns 422 for hex condition ids.
            resp = self._http.get("/markets", params={"condition_ids": condition_id})
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                if not data:
                    logger.warning(f"get_market_status: empty list for {condition_id}")
                    return None
                data = data[0]

            resolution_datetime = None
            hours_to_resolution = None
            end_iso = (
                data.get("endDate")
                or data.get("endDateIso")
                or data.get("end_date_iso")
            )
            if end_iso:
                try:
                    resolution_datetime = datetime.fromisoformat(
                        end_iso.replace("Z", "+00:00")
                    )
                    delta = resolution_datetime - datetime.now(timezone.utc)
                    hours_to_resolution = delta.total_seconds() / 3600
                except (ValueError, TypeError) as e:
                    logger.warning(f"Could not parse endDate '{end_iso}': {e}")

            active = bool(data.get("active", False))
            closed = bool(data.get("closed", False))
            accepting_orders = bool(data.get("acceptingOrders", True))

            uma_status = (data.get("umaResolutionStatus") or "").lower()
            # 'posed' / 'challenged' / 'disputed' = within UMA resolution window
            resolving = uma_status in ("posed", "challenged", "disputed")

            outcome = None
            if closed or uma_status == "resolved":
                outcome = self._parse_winning_outcome(
                    data.get("outcomes"), data.get("outcomePrices")
                )

            return {
                "condition_id": condition_id,
                "active": active,
                "closed": closed,
                "resolving": resolving,
                "accepting_orders": accepting_orders,
                "resolution_datetime": resolution_datetime,
                "hours_to_resolution": hours_to_resolution,
                "outcome": outcome,
            }

        except Exception as e:
            logger.error(f"get_market_status failed for {condition_id}: {e}")
            return None

    @staticmethod
    def _parse_winning_outcome(outcomes_raw: Any, prices_raw: Any) -> str | None:
        """Find the winning outcome name from Gamma's JSON-string arrays."""
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
            if not outcomes or not prices or len(outcomes) != len(prices):
                return None
            for name, price in zip(outcomes, prices):
                if float(price) >= 0.99:
                    return str(name).upper()
        except (ValueError, TypeError):
            pass
        return None

    def get_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get events (which contain multiple markets).

        An "event" is like "NBA Finals 2026" and has markets like
        "Lakers Win", "Celtics Win", etc.
        """
        try:
            resp = self._http.get("/events", params={"limit": limit})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"get_events failed: {e}")
            return []

    # ================================================================
    # Utility
    # ================================================================

    def get_address(self) -> str | None:
        """Get the wallet address derived from the private key."""
        try:
            return self._clob.get_address()
        except Exception as e:
            logger.error(f"get_address failed: {e}")
            return None

    def is_initialized(self) -> bool:
        """Check if the client was successfully initialized."""
        return self._clob is not None and self._http is not None


# Global instance — import this everywhere
# Call polymarket_client.initialize() once at bot startup (in main.py)
polymarket_client = PolymarketClient()
