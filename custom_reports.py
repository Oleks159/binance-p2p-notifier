"""Configurable historical reports: payment method and top 1..15 per side."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
CENT = Decimal("0.01")
METHODS = {"sepa": "SEPA Instant", "instant": "SEPA Instant",
           "sepainstant": "SEPA Instant", "wise": "Wise",
           "all": None, "both": None}

def parse_options(text):
    """Options after /today, or trailing tokens after /range dates."""
    method, count = None, 5
    seen_method = seen_count = False
    for token in text.lower().split():
        if token in METHODS:
            if seen_method:
                raise ValueError("Choose one method: sepa, wise, or all.")
            method, seen_method = METHODS[token], True
        elif token.isascii() and token.isdigit():
            if seen_count:
                raise ValueError("Choose one count between 1 and 15.")
            count, seen_count = int(token), True
            if not 1 <= count <= 15:
                raise ValueError("Choose a count between 1 and 15.")
        else:
            raise ValueError("Options: [sepa|wise|all] [1..15]. Example: /today sepa 10")
    return method, count


def report(store, start_ms, end_ms, method=None, count=5, heading="RANGE"):
    """Ranks unique ads, one best observed price per ad per side, using saved snapshots."""
    rows = store.db.execute(
        "SELECT side,id,seen_at,snapshot FROM changes WHERE seen_at>=? AND seen_at<=? "
        "UNION ALL SELECT side,id,last_seen,snapshot FROM offers WHERE last_seen>=? AND last_seen<=?",
        (start_ms, end_ms, start_ms, end_ms))
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
            methods = [m for m in item["methods"] if m in ("SEPA Instant", "Wise")]
            if method and method not in methods:
                continue
            if not methods or not price.is_finite() or price <= 0 or low > high or high <= 0:
                continue
        except (ValueError, TypeError, KeyError, InvalidOperation, ArithmeticError):
            continue
        previous = best[side].get(ad_id)
        if previous is None or (price < previous["price"] if side == "BUY" else price > previous["price"]):
            best[side][ad_id] = dict(price=price, time=observed, merchant=str(item.get("merchant","Unknown")),
                                      low=low, high=high, methods=methods, id=ad_id)
    start = datetime.fromtimestamp(start_ms/1000, TZ)
    end = datetime.fromtimestamp(end_ms/1000, TZ)
    lines = [f"Binance P2P — {heading} TOP {count}",
             f"{start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M} (Berlin)",
             f"Payment: {method or 'SEPA Instant + Wise'} | EUR {store.rules.minimum:g}–{store.rules.maximum:g}",
             "BUY = lowest; SELL = highest.", ""]
    for side in ("BUY","SELL"):
        items = sorted(best[side].values(),key=lambda x: (x["price"] if side=="BUY" else -x["price"],x["id"]))[:count]
        lines.append(f"TOP {len(items)} {side}:")
        if not items:
            lines.append("No qualifying saved observations.")
        for i,item in enumerate(items,1):
            at = datetime.fromtimestamp(item["time"]/1000,TZ)
            lines.extend([f"{i}. EUR {item['price']:f}/USDT — {item['merchant'][:45]}",
                          f"   EUR {item['low']:.2f}–{item['high']:.2f} | {', '.join(item['methods'])} | {at:%d.%m %H:%M}",
                          f"   https://c2c.binance.com/en/adv?code={item['id']}"])
        lines.append("")
    lines.append("Saved observations only; missed scans and unchanged historical offers can leave gaps. "
                 "Historical prices are not guaranteed executable.")
    return "\n".join(lines)
