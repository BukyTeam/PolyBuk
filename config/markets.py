"""
PolyBuk Framework - Active Markets Configuration

This file is edited DAILY by the operator.
Each morning: remove expired markets, add new ones, restart bot.
Log every change in polybuk.human_decisions via Supabase dashboard.

How to find market info:
1. Go to polymarket.com, find a market
2. The URL has the slug: polymarket.com/event/slug-name
3. Use Gamma API to get condition_id and token_ids:
   GET https://gamma-api.polymarket.com/events?slug=slug-name
4. Each market has tokens: YES token and NO token
   - For MM: you need the token_id of the outcome you'll make markets on
   - For NC: you need the token_id of the outcome priced >= $0.93
"""

from dataclasses import dataclass


@dataclass
class Market:
    """A single market the bot can operate on.

    Attributes:
        token_id: The CLOB token ID (identifies YES or NO outcome).
                  This is what you pass to the API to place orders.
        condition_id: The market/question ID (identifies the overall market).
                      Used for metadata lookups.
        name: Human-readable name (for logs and alerts).
        category: e.g. "sports", "politics", "crypto". Used by NC to
                  ensure diversification (never 2 NC positions in same category).
        notes: Optional notes for the operator (why you picked this market).
    """
    token_id: str
    condition_id: str
    name: str
    category: str
    notes: str = ""


# ============================================================
# Market Maker Markets
# ============================================================
# Criteria (from spec section 9):
#   - Category: sports (most predictable volume patterns)
#   - Volume: >$50K total
#   - Spread: $0.02 - $0.10
#   - Price: $0.20 - $0.80 (avoid extremes)
#   - Resolution: 1-7 days out
#
# Start with 1 market. Add more after the live test validates execution.

MM_MARKETS: list[Market] = [
    # --- Active markets added 2026-04-12 ---
    # Champions League quarter-finals
    Market(
        token_id="98823714150622743440160382935670868763485312813237170221450878540264503721708",
        condition_id="0x368d8be588fc5dae12691b9d3fc5c757a7514e0fab86f4c18ec6b25eebddb7fe",
        name="FC Barcelona win 2026-04-14",
        category="sports",
        notes="Added 2026-04-12. YES=$0.545, 24h vol=$207K, liq=$193K. UCL QF.",
    ),
    Market(
        token_id="107465213618836390501776174005739694959470187392142407058085317714613067213535",
        condition_id="0xecc2bcecc21ad7d87925a1808b4657b22523996d4ab83e38ef970e3c25db1b11",
        name="PSG win 2026-04-14",
        category="sports",
        notes="Added 2026-04-12. YES=$0.395, 24h vol=$62K, liq=$781K. UCL QF vs Liverpool.",
    ),
    Market(
        token_id="107968591323106278367665655742307705452190612363508117328460265642342810950484",
        condition_id="0x7ee56fa66c1e16ca268f182716b63d8062b204229430724ad85ff5949f7d81d9",
        name="Bayern Munchen win 2026-04-15",
        category="sports",
        notes="Added 2026-04-12. YES=$0.625, 24h vol=$77K, liq=$729K. UCL QF.",
    ),
]


# ============================================================
# Near-Certainties Markets
# ============================================================
# Criteria (from spec section 9):
#   - Any category (but diversify — never 2 in same category)
#   - Price: >= $0.93
#   - Resolution: 1-24 hours
#   - Liquidity: enough to fill $30
#   - Max 3 positions open
#
# NC markets change frequently (every few hours).

NC_MARKETS: list[Market] = [
    # === ADD YOUR MARKETS HERE ===
    # Example (replace with real IDs):
    # Market(
    #     token_id="89432156789012.......rest-of-id",
    #     condition_id="0xabcdef1234567890.......rest-of-id",
    #     name="BTC above $60K on April 15",
    #     category="crypto",
    #     notes="Added 2026-04-15 10:00. Price $0.96, resolves in 8h",
    # ),
]


def get_mm_markets() -> list[Market]:
    """Returns active Market Maker markets."""
    return MM_MARKETS


def get_nc_markets() -> list[Market]:
    """Returns active Near-Certainties markets."""
    return NC_MARKETS


def get_all_markets() -> list[Market]:
    """Returns all active markets (both strategies)."""
    return MM_MARKETS + NC_MARKETS
