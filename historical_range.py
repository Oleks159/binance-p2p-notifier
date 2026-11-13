"""Historical best BUY/SELL offers for a user-specified Berlin time interval."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
CENT = Decimal("0.01")
MAX_DAYS = 31


def parse_range(arguments, now=None):
    """Accept /range YYYY-MM-DD HH:MM HH:MM or two dated timestamps."""
    tokens = arguments.strip().split()
    current_year = datetime.fromtimestamp(now / 1000, TZ).year if now is not None else datetime.now(TZ).year

    def expand(day):
        if len(day) == 5 and day[2] == ".":
            return f"{current_year}-{day[3:5]}-{day[:2]}"
        return day

    if len(tokens) == 3:
        day, first, last = tokens
        start_s, end_s = f"{expand(day)} {first}", f"{expand(day)} {last}"
    elif len(tokens) == 4:
        start_s, end_s = f"{expand(tokens[0])} {tokens[1]}", f"{expand(tokens[2])} {tokens[3]}"
    else:
        raise ValueError("Usage: /range 13.11 00:00 10:00\\nOr: /range 12.11 00:00 13.11 12:00")
    try:
        start = datetime.strptime(start_s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        end = datetime.strptime(end_s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        raise ValueError("Invalid date/time. Use DD.MM HH:MM (current year, Berlin time).") from None
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if start_ms >= end_ms:
        raise ValueError("End time must be after start time.")
    if end_ms - start_ms > MAX_DAYS * 86_400_000:
        raise ValueError("Maximum interval is 31 days.")
    if now is not None and start_ms > now:
        raise ValueError("Start time cannot be in the future.")
    return start_ms, min(end_ms, now) if now is not None else end_ms


def historical_best(store, start_ms, end_ms):
    """Use historical changed snapshots plus offers whose last scan is in range.

    Changes only persist when a listing changes. Legacy data therefore cannot
    prove all unchanged observations inside a window; do not claim full coverage.
    """
    rows = store.db.execute(
        "SELECT side,id,seen_at,snapshot FROM changes WHERE seen_at>=? AND seen_at<=? "
        "UNION ALL SELECT side,id,last_seen,snapshot FROM offers "
        "WHERE last_seen>=? AND last_seen<=?",
        (start_ms, end_ms, start_ms, end_ms),
    )
    best = {"BUY": {}, "SELL": {}}
    for side, ad_id, observed, raw in rows:
        if side not in best:
            continue
        try:
            item = json.loads(raw)
            price = Decimal(str(item["price"]))
            low = max(store.rules.minimum, Decimal(str(item["minimum_crypto"])) * price)
            high = min(store.rules.maximum, Decimal(str(item["maximum_crypto"])) * price,
                       Decimal(str(item["available"])) * price)
            low = low.quantize(CENT, rounding=ROUND_CEILING)
            high = high.quantize(CENT, rounding=ROUND_FLOOR)
            methods = [x for x in item["methods"] if x in ("Wise", "SEPA Instant")]
            if not price.is_finite() or price <= 0 or not methods or low > high or high <= 0:
                continue
        except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
            continue
        old = best[side].get(ad_id)
        if old is None or (price < old["price"] if side == "BUY" else price > old["price"]):
            best[side][ad_id] = dict(id=ad_id, price=price, time=observed,
                                      merchant=str(item.get("merchant", "Unknown")),
                                      low=low, high=high, methods=methods)
    return {
        side: sorted(best[side].values(),
                     key=lambda x: (x["price"] if side == "BUY" else -x["price"], x["id"]))[:5]
        for side in ("BUY", "SELL")
    }


def range_report(store, start_ms, end_ms):
    best = historical_best(store, start_ms, end_ms)
    start = datetime.fromtimestamp(start_ms / 1000, TZ)
    end = datetime.fromtimestamp(end_ms / 1000, TZ)
    lines = [
        "Binance P2P — BEST OBSERVED IN RANGE",
        f"{start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M} (Berlin)",
        f"USDT/EUR | EUR {store.rules.minimum:g}–{store.rules.maximum:g} | SEPA Instant / Wise",
        "BUY = lowest price; SELL = highest price.", "",
    ]
    for side in ("BUY", "SELL"):
        lines.append(f"TOP 5 {side}:")
        if not best[side]:
            lines.append("No saved qualifying observations in this interval.")
        for i, offer in enumerate(best[side], 1):
            at = datetime.fromtimestamp(offer["time"] / 1000, TZ)
            lines.extend([
                f"{i}. EUR {offer['price']:f}/USDT — {offer['merchant'][:60]}",
                f"   EUR {offer['low']:.2f}–{offer['high']:.2f} | {', '.join(offer['methods'])}",
                f"   Observed {at:%d.%m %H:%M:%S}",
                f"   https://c2c.binance.com/en/adv?code={offer['id']}",
            ])
        lines.append("")
    lines.append("Historical observations only, not executable quotes. Older database records "
                 "track changes rather than every scan; unchanged offers and downtime can "
                 "leave gaps. Data lost before persistent storage cannot be reconstructed.")
    return "\n".join(lines)
