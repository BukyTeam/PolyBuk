"""
PolyBuk — Market Validator

Run this script BEFORE starting the bot to verify all configured markets
are valid, active, and have enough time before resolution.

Usage:
    python scripts/validate_markets.py

Exits with code 1 if any market has a CRITICAL issue, so this can gate
bot startup. Run every morning when rotating markets.
"""

import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.markets import get_mm_markets, get_nc_markets
from config.settings import settings
from core.polymarket_client import polymarket_client

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


def check_geoblock() -> bool:
    """Hit Polymarket's own geoblock endpoint as the first pre-flight check.

    This is the authoritative answer on whether the outbound IP is
    permitted to place orders. A passing Gamma/CLOB read does NOT prove
    the region is allowed — Polymarket only enforces the geoblock on
    write endpoints. On 2026-04-13 we learned this the expensive way by
    provisioning a Frankfurt droplet (Germany is blocked) and only
    noticing when the first live order got 403.

    Returns True if safe to proceed, False otherwise.
    """
    print(f"\n{BOLD}=== Polymarket Geoblock Pre-Check ==={RESET}")
    try:
        resp = httpx.get("https://polymarket.com/api/geoblock", timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  {RED}CRITICAL: Could not reach Polymarket geoblock endpoint: {e}{RESET}")
        return False

    ip = data.get("ip", "?")
    country = data.get("country", "?")
    region = data.get("region", "?")
    blocked = bool(data.get("blocked"))

    print(f"  IP: {ip}  Country: {country}  Region: {region}")
    if blocked:
        print(
            f"  {RED}CRITICAL: Polymarket reports this IP as BLOCKED. "
            f"Orders will be rejected with 403. Migrate the VPS to an "
            f"allowed region before starting the bot.{RESET}"
        )
        return False

    print(f"  {GREEN}OK — Polymarket says this IP is NOT blocked.{RESET}")
    return True


def _best_prices(book) -> tuple[float | None, float | None]:
    """Extract best bid and best ask from a py-clob-client book.

    py-clob-client sorts bids ASCENDING and asks DESCENDING, so the BEST
    price on each side is the LAST element. This matches the logic in
    strategies/market_maker.py:_extract_book_data.
    """
    try:
        bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
        asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
        if not bids or not asks:
            return None, None
        best_bid = float(bids[-1].price if hasattr(bids[-1], "price") else bids[-1].get("price", 0))
        best_ask = float(asks[-1].price if hasattr(asks[-1], "price") else asks[-1].get("price", 0))
        return best_bid, best_ask
    except (IndexError, KeyError, TypeError, ValueError):
        return None, None


def validate_market(market, strategy: str) -> tuple[bool, list[str]]:
    """Validate a single market. Returns (is_ok, list_of_issues)."""
    issues: list[str] = []
    is_critical = False

    print(f"\n  Checking: {market.name}")
    print(f"  token_id: {market.token_id[:24]}...")
    print(f"  condition_id: {market.condition_id[:24]}...")

    status = polymarket_client.get_market_status(market.condition_id)

    if status is None:
        issues.append(
            f"{RED}CRITICAL: Could not fetch market from Gamma API. "
            f"Check condition_id is correct.{RESET}"
        )
        return False, issues

    if status["outcome"] is not None:
        issues.append(
            f"{RED}CRITICAL: Market already resolved. "
            f"Outcome: {status['outcome']}. Remove from config.{RESET}"
        )
        return False, issues

    if status["closed"]:
        issues.append(f"{RED}CRITICAL: Market is closed. Remove from config.{RESET}")
        return False, issues

    if status["resolving"]:
        issues.append(
            f"{YELLOW}WARNING: Market is in 'resolving' state "
            f"(UMA dispute window). Bot will skip it.{RESET}"
        )

    hours = status["hours_to_resolution"]
    if hours is None:
        issues.append(f"{YELLOW}WARNING: Could not determine resolution time.{RESET}")
    else:
        print(f"  Hours to resolution: {hours:.1f}h")

        if strategy == "mm":
            buffer = settings.mm.resolution_buffer_hours
            if hours < buffer:
                issues.append(
                    f"{RED}CRITICAL: Only {hours:.1f}h to resolution. "
                    f"MM requires >{buffer}h buffer. Remove from config.{RESET}"
                )
                is_critical = True
            elif hours < buffer + 2:
                issues.append(
                    f"{YELLOW}WARNING: {hours:.1f}h to resolution. "
                    f"Bot will stop trading this market soon.{RESET}"
                )
            elif hours > 168:
                issues.append(
                    f"{YELLOW}WARNING: {hours:.1f}h to resolution (>7 days). "
                    f"Capital may be tied up long-term.{RESET}"
                )

        elif strategy == "nc":
            if hours < settings.nc.min_resolution_hours:
                issues.append(
                    f"{RED}CRITICAL: Only {hours:.1f}h to resolution. "
                    f"NC requires >{settings.nc.min_resolution_hours}h.{RESET}"
                )
                is_critical = True
            elif hours > settings.nc.max_resolution_hours:
                issues.append(
                    f"{YELLOW}INFO: {hours:.1f}h to resolution. "
                    f"NC max is {settings.nc.max_resolution_hours}h. "
                    f"Bot will skip this market.{RESET}"
                )

    book = polymarket_client.get_order_book(market.token_id)
    if book is None:
        issues.append(
            f"{RED}CRITICAL: Could not fetch order book for token_id. "
            f"Token ID may be incorrect.{RESET}"
        )
        is_critical = True
    else:
        best_bid, best_ask = _best_prices(book)
        if best_bid is None or best_ask is None:
            issues.append(
                f"{YELLOW}WARNING: Order book is empty (no bids or asks). "
                f"Market may be illiquid.{RESET}"
            )
        else:
            spread = round(best_ask - best_bid, 4)
            mid = round((best_bid + best_ask) / 2, 4)
            print(f"  Order book: bid={best_bid} ask={best_ask} spread={spread} mid={mid}")

            if strategy == "mm":
                if spread < settings.mm.min_spread:
                    issues.append(
                        f"{YELLOW}WARNING: Spread ${spread} < min ${settings.mm.min_spread}. "
                        f"Bot will skip this market each cycle.{RESET}"
                    )
                if spread > settings.mm.max_spread:
                    issues.append(
                        f"{YELLOW}WARNING: Spread ${spread} > max ${settings.mm.max_spread}. "
                        f"Market may be illiquid.{RESET}"
                    )
                if mid < settings.mm.min_price or mid > settings.mm.max_price:
                    issues.append(
                        f"{YELLOW}WARNING: Mid price ${mid} outside MM range "
                        f"[${settings.mm.min_price}, ${settings.mm.max_price}].{RESET}"
                    )

    return not is_critical, issues


def main() -> None:
    print(f"\n{BOLD}=== PolyBuk Market Validator ==={RESET}")

    # Geoblock check goes FIRST — if this fails, nothing else matters.
    if not check_geoblock():
        print(f"\n{RED}Aborting: outbound IP is blocked by Polymarket.{RESET}\n")
        sys.exit(1)

    print("Connecting to Polymarket APIs...")

    if not polymarket_client.initialize():
        print(f"{RED}ERROR: Failed to initialize Polymarket client. Check .env{RESET}")
        sys.exit(1)

    print(f"{GREEN}Connected.{RESET}")

    all_ok = True
    total_markets = 0

    mm_markets = get_mm_markets()
    print(f"\n{BOLD}--- Market Maker Markets ({len(mm_markets)}) ---{RESET}")

    if not mm_markets:
        print(f"  {YELLOW}No MM markets configured. Add markets to config/markets.py{RESET}")
    else:
        for market in mm_markets:
            total_markets += 1
            ok, issues = validate_market(market, "mm")
            if not ok:
                all_ok = False
            if issues:
                for issue in issues:
                    print(f"  {issue}")
            else:
                print(f"  {GREEN}OK{RESET}")

    nc_markets = get_nc_markets()
    print(f"\n{BOLD}--- Near-Certainties Markets ({len(nc_markets)}) ---{RESET}")

    if not nc_markets:
        print(f"  {YELLOW}No NC markets configured.{RESET}")
    else:
        for market in nc_markets:
            total_markets += 1
            ok, issues = validate_market(market, "nc")
            if not ok:
                all_ok = False
            if issues:
                for issue in issues:
                    print(f"  {issue}")
            else:
                print(f"  {GREEN}OK{RESET}")

    print(f"\n{BOLD}=== Summary ==={RESET}")
    print(f"Total markets checked: {total_markets}")

    if all_ok:
        print(f"{GREEN}All markets passed validation. Safe to start bot.{RESET}\n")
        sys.exit(0)
    else:
        print(
            f"{RED}One or more markets have CRITICAL issues. "
            f"Fix config/markets.py before starting bot.{RESET}\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
