"""Customizable historical report options: 1–15 offers per side and payment filter."""
from datetime import datetime
from zoneinfo import ZoneInfo

from historical_range import historical_best

TZ = ZoneInfo("Europe/Berlin")
METHODS = {"sepa": "SEPA Instant", "sepainstant": "SEPA Instant",
           "wise": "Wise", "all": None, "both": None}

def parse_options(tokens):
    count, method = 5, None
    for token in tokens:
        normalized = token.lower().replace("-", "").replace("_", "")
        if normalized.isdigit():
            number = int(normalized)
            if not 1 <= number <= 15:
                raise ValueError("Choose a number from 1 to 15.")
            count = number
        elif normalized in METHODS:
            method = METHODS[normalized]
        else:
            raise ValueError("Options: [1-15] [sepa|wise|all]. Example: /today 10 sepa")
    return count, method

def report(store, start_ms, end_ms, count=5, method=None):
    # Retrieve up to 15 candidates per side after applying payment filtering.
    best = historical_best(store, start_ms, end_ms, count=count, method=method)
    start, end = (datetime.fromtimestamp(x/1000, TZ) for x in (start_ms, end_ms))
    lines = [
        f"Binance P2P — BEST {count} OBSERVED",
        f"{start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M} Berlin",
        f"Payment: {method or 'SEPA Instant + Wise'}",
        f"Trade size: EUR {store.rules.minimum:g}–{store.rules.maximum:g}",
        "BUY = lowest price; SELL = highest price.", "",
    ]
    for side in ("BUY", "SELL"):
        lines.append(f"TOP {count} {side}:")
        if not best[side]:
            lines.append("No matching saved observations.")
        for i, item in enumerate(best[side], 1):
            at = datetime.fromtimestamp(item["time"]/1000, TZ)
            lines.extend([
                f"{i}. EUR {item['price']:f}/USDT — {item['merchant'][:55]}",
                f"   EUR {item['low']:.2f}–{item['high']:.2f} | {', '.join(item['methods'])} | {at:%d.%m %H:%M}",
                f"   https://c2c.binance.com/en/adv?code={item['id']}",
            ])
        lines.append("")
    lines.append("Only saved API observations; not all Binance ads. Offers may no longer be available.")
    return "\n".join(lines)
