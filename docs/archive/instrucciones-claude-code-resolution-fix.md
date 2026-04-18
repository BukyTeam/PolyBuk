# Instrucciones para Claude Code — PolyBuk
## Fix: Circuit Breaker de Resolución + Validación de Mercados via Gamma API

---

## Contexto y problema

El bot tiene un circuit breaker implementado en `core/risk_manager.py` (`check_resolution_buffer()`) pero **nunca se llama desde ninguna estrategia**. Esto significa que el bot puede colocar órdenes en mercados que están a minutos de resolver, lo cual es el escenario de pérdida más severo para un market maker (el precio colapsa a 0 o sube a 1 y tus órdenes quedan atrapadas).

Adicionalmente, los mercados en `config/markets.py` se configuran manualmente con fechas de resolución que no se validan contra la API real. El bot no sabe si un mercado ya resolvió, está en estado "resolving", o cambió sus condiciones.

**La solución correcta** es que el bot consulte la Gamma API en cada ciclo para obtener el estado y fecha de resolución real de cada mercado, y use esa información para activar el circuit breaker dinámicamente.

---

## Alcance de los cambios

### Archivos a modificar:
1. `config/markets.py` — Simplificar dataclass (remover campo `resolution_datetime` si existe; la fecha ahora viene de la API)
2. `core/polymarket_client.py` — Agregar método `get_market_status()` robusto
3. `core/risk_manager.py` — Verificar que `check_resolution_buffer()` está correcto (no modificar lógica, solo confirmar)
4. `strategies/market_maker.py` — Conectar el circuit breaker en el ciclo principal
5. `strategies/near_certainties.py` — Ídem para NC
6. `scripts/validate_markets.py` — Script nuevo: valida todos los mercados configurados contra Gamma API

### Archivos nuevos:
- `scripts/validate_markets.py`

---

## Instrucciones detalladas

---

### CAMBIO 1 — `core/polymarket_client.py`

Agregar el siguiente método a la clase `PolymarketClient`, después del método existente `get_market_info()`:

```python
def get_market_status(self, condition_id: str) -> dict | None:
    """Fetch real-time market status from Gamma API.

    Returns a dict with the following keys (all can be None if API fails):
        - 'active': bool — True if market is still open for trading
        - 'closed': bool — True if market is closed/resolved
        - 'resolving': bool — True if market is in resolution window (UMA dispute period)
        - 'resolution_datetime': datetime | None — When the market resolves (UTC)
        - 'hours_to_resolution': float | None — Hours from now until resolution
        - 'outcome': str | None — 'YES', 'NO', 'INVALID' if resolved; None if still open
        - 'condition_id': str — The condition_id queried

    Returns None if the API call fails entirely (network error, 404, etc.).
    In that case, the caller should treat it as "unknown" and skip the market
    for safety (conservative default).
    """
    from datetime import datetime, timezone

    try:
        resp = self._http.get(f"/markets/{condition_id}")
        resp.raise_for_status()
        data = resp.json()

        # Gamma API may return a list (one market) or a dict
        if isinstance(data, list):
            if not data:
                logger.warning(f"get_market_status: empty list for {condition_id}")
                return None
            data = data[0]

        # Parse resolution datetime
        resolution_datetime = None
        hours_to_resolution = None
        end_date_iso = data.get("endDate") or data.get("end_date_iso")

        if end_date_iso:
            try:
                # Gamma API returns ISO 8601 strings, e.g. "2026-04-14T22:00:00Z"
                resolution_datetime = datetime.fromisoformat(
                    end_date_iso.replace("Z", "+00:00")
                )
                now_utc = datetime.now(timezone.utc)
                delta = resolution_datetime - now_utc
                hours_to_resolution = delta.total_seconds() / 3600
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse endDate '{end_date_iso}': {e}")

        # Parse status flags
        active = bool(data.get("active", False))
        closed = bool(data.get("closed", False))
        resolving = bool(data.get("resolving", False))
        outcome = data.get("outcome")  # 'Yes', 'No', 'Invalid', or None

        # Normalize outcome to uppercase
        if outcome:
            outcome = outcome.upper()

        return {
            "condition_id": condition_id,
            "active": active,
            "closed": closed,
            "resolving": resolving,
            "resolution_datetime": resolution_datetime,
            "hours_to_resolution": hours_to_resolution,
            "outcome": outcome,
        }

    except Exception as e:
        logger.error(f"get_market_status failed for {condition_id}: {e}")
        return None
```

**Por qué así:** La Gamma API devuelve `endDate` en ISO 8601. Calculamos `hours_to_resolution` en tiempo real para que el circuit breaker siempre use datos frescos, no una fecha hardcodeada en el config.

---

### CAMBIO 2 — `strategies/market_maker.py`

Dentro del método `_process_market(self, market: Market)`, **inmediatamente después del Step 1 (GET STATE)** y antes del Step 2 (CALCULATE), insertar el siguiente bloque completo:

```python
# === STEP 1B: VALIDATE MARKET STATUS VIA GAMMA API ===
market_status = polymarket_client.get_market_status(market.condition_id)

if market_status is None:
    # API failed — conservative default: skip this market
    journal.log_decision(
        strategy=self.name,
        market_id=market.token_id,
        action="skip_cycle",
        reason="Could not fetch market status from Gamma API — skipping for safety",
        paper_trade=settings.paper.enabled,
    )
    return

# Market already closed or resolved
if market_status["closed"] or market_status["outcome"] is not None:
    journal.log_decision(
        strategy=self.name,
        market_id=market.token_id,
        action="skip_cycle",
        reason=(
            f"Market is closed or already resolved. "
            f"Outcome: {market_status['outcome']}. "
            f"Removing from active cycle."
        ),
        paper_trade=settings.paper.enabled,
    )
    # Alert operator to remove this market from config/markets.py
    import asyncio
    asyncio.create_task(
        alerts.send_alert(
            f"MERCADO RESUELTO — remover de config/markets.py:\n"
            f"{market.name}\n"
            f"Outcome: {market_status['outcome']}"
        )
    )
    return

# Market in UMA resolution window — do not trade
if market_status["resolving"]:
    journal.log_decision(
        strategy=self.name,
        market_id=market.token_id,
        action="skip_cycle",
        reason="Market is in 'resolving' state (UMA dispute window) — skipping",
        paper_trade=settings.paper.enabled,
    )
    return

# Resolution buffer circuit breaker
if market_status["hours_to_resolution"] is not None:
    buffer_ok, buffer_reason = risk_manager.check_resolution_buffer(
        market_status["hours_to_resolution"]
    )
    if not buffer_ok:
        journal.log_decision(
            strategy=self.name,
            market_id=market.token_id,
            action="resolution_buffer_triggered",
            reason=buffer_reason,
            context={
                "hours_to_resolution": market_status["hours_to_resolution"],
                "resolution_datetime": str(market_status["resolution_datetime"]),
            },
            paper_trade=settings.paper.enabled,
        )
        # Cancel all open orders for this market immediately
        order_manager.cancel_stale_orders(market_id=market.token_id, max_age_seconds=0)
        await alerts.send_alert(
            f"BUFFER RESOLUCIÓN activado: {market.name}\n"
            f"Resuelve en {market_status['hours_to_resolution']:.1f}h\n"
            f"Órdenes canceladas."
        )
        return
```

**Nota importante:** Asegurarse de que `alerts` está importado en `market_maker.py`. Si no lo está, agregar al bloque de imports:
```python
from core.alerts import alerts
```

---

### CAMBIO 3 — `strategies/near_certainties.py`

En el método `_evaluate_and_buy(self, market: Market)`, **al inicio del método** (antes de verificar si ya tiene posición), agregar:

```python
# Validate market status before evaluating
market_status = polymarket_client.get_market_status(market.condition_id)

if market_status is None:
    logger.warning(f"Could not fetch status for {market.name} — skipping")
    return

if market_status["closed"] or market_status["resolving"] or market_status["outcome"] is not None:
    journal.log_decision(
        strategy=self.name,
        market_id=market.token_id,
        action="skip_cycle",
        reason=(
            f"Market not tradeable: closed={market_status['closed']}, "
            f"resolving={market_status['resolving']}, "
            f"outcome={market_status['outcome']}"
        ),
        paper_trade=settings.paper.enabled,
    )
    return

# Resolution buffer check for NC
if market_status["hours_to_resolution"] is not None:
    # NC minimum: market must resolve within max_resolution_hours AND after min_resolution_hours
    hours = market_status["hours_to_resolution"]
    if hours < settings.nc.min_resolution_hours:
        journal.log_rejected(
            strategy=self.name,
            market_id=market.token_id,
            market_name=market.name,
            opportunity_type="nc_high_prob",
            reason=f"Resolves too soon: {hours:.1f}h < {settings.nc.min_resolution_hours}h minimum",
        )
        return
    if hours > settings.nc.max_resolution_hours:
        journal.log_rejected(
            strategy=self.name,
            market_id=market.token_id,
            market_name=market.name,
            opportunity_type="nc_high_prob",
            reason=f"Resolves too far: {hours:.1f}h > {settings.nc.max_resolution_hours}h maximum",
        )
        return
```

También en el método `_monitor_positions()`, para cada posición abierta, agregar validación de resolución real:

```python
# Check real market status (not just price proxy)
market_status = polymarket_client.get_market_status(market.condition_id)
if market_status and market_status["outcome"] is not None:
    # Market resolved via API — settle at real outcome
    settlement = 1.0 if market_status["outcome"] == "YES" else 0.0
    won = market_status["outcome"] == "YES"
    self._close_position(token_id, settlement_price=settlement, won=won)
    continue
```

Este bloque va **antes** del bloque existente que detecta resolución por precio (`if current_price >= 0.99`).

---

### CAMBIO 4 — `scripts/validate_markets.py` (archivo nuevo)

Crear este archivo completo:

```python
"""
PolyBuk — Market Validator

Run this script BEFORE starting the bot to verify all configured markets
are valid, active, and have enough time before resolution.

Usage:
    python scripts/validate_markets.py

Output:
    - Prints status of each configured market (MM and NC)
    - Warns if any market is close to resolution, closed, or has bad token_id
    - Exits with code 1 if any critical issue is found (so CI/CD can catch it)

Run every morning when rotating markets.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.markets import get_mm_markets, get_nc_markets
from config.settings import settings
from core.polymarket_client import polymarket_client

# ANSI colors for terminal output
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


def validate_market(market, strategy: str) -> tuple[bool, list[str]]:
    """Validate a single market. Returns (is_ok, list_of_issues)."""
    issues = []
    is_critical = False

    print(f"\n  Checking: {market.name}")
    print(f"  token_id: {market.token_id[:24]}...")
    print(f"  condition_id: {market.condition_id[:24]}...")

    # 1. Fetch market status from Gamma API
    status = polymarket_client.get_market_status(market.condition_id)

    if status is None:
        issues.append(f"{RED}CRITICAL: Could not fetch market from Gamma API. "
                      f"Check condition_id is correct.{RESET}")
        return False, issues

    # 2. Check if already resolved
    if status["outcome"] is not None:
        issues.append(f"{RED}CRITICAL: Market already resolved. "
                      f"Outcome: {status['outcome']}. Remove from config.{RESET}")
        return False, issues

    # 3. Check if closed
    if status["closed"]:
        issues.append(f"{RED}CRITICAL: Market is closed. Remove from config.{RESET}")
        return False, issues

    # 4. Check if in resolving state
    if status["resolving"]:
        issues.append(f"{YELLOW}WARNING: Market is in 'resolving' state "
                      f"(UMA dispute window). Bot will skip it.{RESET}")

    # 5. Check resolution time
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
            elif hours > 168:  # 7 days
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

    # 6. Verify token_id returns a valid order book
    book = polymarket_client.get_order_book(market.token_id)
    if book is None:
        issues.append(f"{RED}CRITICAL: Could not fetch order book for token_id. "
                      f"Token ID may be incorrect.{RESET}")
        is_critical = True
    else:
        # Check liquidity
        bids = getattr(book, "bids", None) or book.get("bids", [])
        asks = getattr(book, "asks", None) or book.get("asks", [])
        if not bids or not asks:
            issues.append(f"{YELLOW}WARNING: Order book is empty (no bids or asks). "
                          f"Market may be illiquid.{RESET}")
        else:
            best_bid = float(bids[0].price if hasattr(bids[0], "price") else bids[0].get("price", 0))
            best_ask = float(asks[0].price if hasattr(asks[0], "price") else asks[0].get("price", 0))
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


def main():
    print(f"\n{BOLD}=== PolyBuk Market Validator ==={RESET}")
    print(f"Connecting to Polymarket APIs...")

    if not polymarket_client.initialize():
        print(f"{RED}ERROR: Failed to initialize Polymarket client. Check .env{RESET}")
        sys.exit(1)

    print(f"{GREEN}Connected.{RESET}")

    all_ok = True
    total_markets = 0

    # Validate MM markets
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

    # Validate NC markets
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

    # Summary
    print(f"\n{BOLD}=== Summary ==={RESET}")
    print(f"Total markets checked: {total_markets}")

    if all_ok:
        print(f"{GREEN}All markets passed validation. Safe to start bot.{RESET}\n")
        sys.exit(0)
    else:
        print(f"{RED}One or more markets have CRITICAL issues. "
              f"Fix config/markets.py before starting bot.{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

## Orden de ejecución de los cambios

Implementar en este orden exacto (cada paso depende del anterior):

1. `core/polymarket_client.py` — agregar `get_market_status()`
2. `strategies/market_maker.py` — insertar Step 1B
3. `strategies/near_certainties.py` — insertar validación al inicio de `_evaluate_and_buy()` y en `_monitor_positions()`
4. `scripts/validate_markets.py` — crear archivo nuevo

---

## Verificación post-implementación

Una vez implementados los cambios, ejecutar en este orden:

```bash
# 1. Validar que los mercados configurados son válidos
python scripts/validate_markets.py

# 2. Dry-run para confirmar que el bot inicializa sin errores
python main.py --dry-run --strategy mm

# 3. Paper trading por mínimo 2 horas para verificar que el circuit breaker
#    se llama correctamente en cada ciclo (verificar en polybuk.decisions)
python main.py --paper --strategy mm
```

En Supabase, confirmar que aparecen registros en `polybuk.decisions` con `action = 'resolution_buffer_triggered'` o `action = 'skip_cycle'` con razones relacionadas al estado del mercado. Esto confirma que el circuit breaker está conectado.

---

## Comportamiento esperado tras el fix

| Situación | Comportamiento del bot |
|-----------|----------------------|
| Mercado activo, >2h para resolver | Opera normalmente |
| Mercado activo, <2h para resolver | Cancela órdenes, skippea ciclo, alerta Telegram |
| Mercado en estado `resolving` (UMA) | Skippea ciclo, no opera |
| Mercado ya resuelto | Cancela, alerta operador para remover de config |
| Gamma API no responde | Skippea mercado por seguridad (conservative default) |
| token_id incorrecto | `validate_markets.py` lo detecta antes de iniciar |

---

## Notas para el operador

- **Correr `validate_markets.py` cada mañana** al rotar mercados, antes de reiniciar el bot.
- El script sale con código 1 si hay problemas críticos — el bot NO debe iniciarse hasta que todos los mercados pasen.
- La llamada a Gamma API en cada ciclo tiene un costo de latencia (~100-200ms). Con 3 mercados y ciclos de 30s, esto es despreciable. Si en el futuro se operan 10+ mercados, evaluar caching con TTL de 5 minutos.
- El campo `condition_id` en `config/markets.py` es el que se usa para consultar Gamma API. El `token_id` es el que se usa para órdenes en CLOB API. Ambos deben ser correctos.
