"""Daily digest of observed USDT/EUR offers at 20:00 Europe/Berlin.

Uses only historical snapshots actually observed by this watcher. No trading.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
DAY_MS = 86_400_000


def report_due(store, now):
    """Return today's Berlin date key when 20:00 has passed and not yet sent."""
    local = datetime.fromtimestamp(now / 1000, TZ)
    if local.hour < 20:
        return None
    key = local.date().isoformat()
    store.db.execute("CREATE TABLE IF NOT EXISTS daily_reports (profile TEXT, day TEXT, sent_at INTEGER, PRIMARY KEY(profile,day))")
    record = store.db.execute(
        "SELECT 1 FROM daily_reports WHERE profile=? AND day=?", (store.profile, key)
    ).fetchone()
    return None if record else key


def build_daily_report(store, now, today=False):
    """Rank best observed price for each unique ad over the previous 24h."""
    start = (int(datetime.fromtimestamp(now / 1000, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000) if today else now - DAY_MS)
    candidates = {"BUY": {}, "SELL": {}}

    # A change is stored only when offer data changes, so also include the most
    # recent snapshot of offers still present within the reporting window.
    rows = store.db.execute(
        "SELECT side,id,seen_at,snapshot FROM changes WHERE seen_at>=? AND seen_at<=? "
        "UNION ALL "
        "SELECT side,id,last_seen,snapshot FROM offers WHERE last_seen>=? AND last_seen<=?",
        (start, now, start, now),
    )
    for side, ad_id, seen_at, raw in rows:
        if side not in candidates:
            continue
        try:
            item = json.loads(raw)
            price = Decimal(item["price"])
            minimum = Decimal(item["minimum_crypto"])
            maximum = Decimal(item["maximum_crypto"])
            available = Decimal(item["available"])
            methods = item["methods"]
            low = max(store.rules.minimum, minimum * price)
            high = min(store.rules.maximum, maximum * price, available * price)
            if not methods or low > high or high <= 0 or price <= 0:
                continue
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue
        current = candidates[side].get(ad_id)
        is_better = current is None or (
            price < current["price"] if side == "BUY" else price > current["price"]
        )
        if is_better:
            candidates[side][ad_id] = {
                "id": ad_id, "price": price, "time": seen_at,
                "merchant": str(item.get("merchant", "Unknown")),
                "methods": methods, "low": low, "high": high,
            }

    local = datetime.fromtimestamp(now / 1000, TZ)
    start_local = datetime.fromtimestamp(start / 1000, TZ)
    lines = [
        "Binance P2P — today top 5 (observed offers)" if today else "Binance P2P — last 24h top 5 (observed offers)",
        f"{start_local:%d.%m %H:%M} – {local:%d.%m.%Y %H:%M} (Berlin)",
        f"Trade size: EUR {store.rules.minimum:g}–{store.rules.maximum:g}; SEPA Instant / Wise",
        "BUY = buy USDT cheaply; SELL = sell USDT expensively.",
        "",
    ]
    best = {}
    for side in ("BUY", "SELL"):
        ranked = sorted(
            candidates[side].values(),
            key=lambda item: (item["price"] if side == "BUY" else -item["price"], item["id"]),
        )[:5]
        best[side] = ranked
        lines.append(f"TOP {side}:")
        if not ranked:
            lines.append("No qualifying observations in the last 24h.")
        for position, item in enumerate(ranked, 1):
            at = datetime.fromtimestamp(item["time"] / 1000, TZ)
            lines.extend([
                f"{position}. EUR {item['price']:f}/USDT — {item['merchant'][:60]}",
                f"   EUR {item['low']:.2f}–{item['high']:.2f} | {', '.join(item['methods'])} | observed {at:%H:%M}",
                f"   https://c2c.binance.com/en/adv?code={item['id']}",
            ])
        lines.append("")
    if best["BUY"] and best["SELL"]:
        difference = best["SELL"][0]["price"] - best["BUY"][0]["price"]
        lines.append(f"Theoretical best-price difference: EUR {difference:f}/USDT (historical, not executable spread).")
    lines.append("Observed snapshots only, not all Binance listings. Missing scans or lost storage can omit better offers. Historical offers may no longer be available.")
    return "\n".join(lines)


def send_daily_if_due(store, now, send, report):
    key = report_due(store, now)
    if key is None:
        return False
    message = build_daily_report(store, now)
    send(message)
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO daily_reports(profile,day,sent_at) VALUES(?,?,?)",
            (store.profile, key, now),
        )
    report(f"Sent daily top-5 BUY/SELL digest for {key} (Europe/Berlin).")
    return True
