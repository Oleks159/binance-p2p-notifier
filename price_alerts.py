"""Persistent configurable Telegram price thresholds and deduplicated alerts."""
from decimal import Decimal, InvalidOperation
import time

def parse_price(value):
    try:
        price = Decimal(value.replace(",", "."))
        if not price.is_finite() or price <= 0 or price >= 1000:
            raise ValueError()
        return price
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError("Invalid price. Example: /buy 0.905") from None

class PriceAlerts:
    def __init__(self, store):
        self.db = store.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS price_alert_settings (
            side TEXT PRIMARY KEY, threshold TEXT, enabled INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS price_alert_sent (
            side TEXT NOT NULL, ad_id TEXT NOT NULL, price TEXT NOT NULL,
            sent_at INTEGER NOT NULL, PRIMARY KEY(side,ad_id))""")
        self.db.commit()

    def settings(self):
        return {side: (Decimal(threshold) if threshold is not None else None, bool(enabled))
                for side,threshold,enabled in self.db.execute(
                    "SELECT side,threshold,enabled FROM price_alert_settings")}

    def configure(self, side, price):
        with self.db:
            self.db.execute("""INSERT INTO price_alert_settings(side,threshold,enabled)
                VALUES(?,?,1) ON CONFLICT(side) DO UPDATE SET threshold=excluded.threshold,enabled=1""",
                (side, str(price)))
            # A changed threshold starts a fresh alert session.
            self.db.execute("DELETE FROM price_alert_sent WHERE side=?", (side,))

    def disable(self):
        with self.db:
            self.db.execute("UPDATE price_alert_settings SET enabled=0")
            self.db.execute("DELETE FROM price_alert_sent")

    def describe(self):
        settings = self.settings()
        lines = ["Binance P2P price alerts:"]
        for side in ("BUY","SELL"):
            threshold, enabled = settings.get(side, (None, False))
            condition = "at or below" if side == "BUY" else "at or above"
            lines.append(f"{side}: {condition} EUR {threshold}/USDT" if enabled else f"{side}: OFF")
        lines.append("Set: /buy 0.905 or /sell 0.930 | Disable: /alerts off")
        return "\n".join(lines)

    def check(self, snapshot, rules, send):
        settings = self.settings()
        active = set()
        for side in ("BUY", "SELL"):
            threshold, enabled = settings.get(side, (None, False))
            for offer in snapshot[side]:
                eligible = offer.usable(rules)
                if not enabled or threshold is None or not eligible:
                    continue
                if not (offer.price <= threshold if side == "BUY" else offer.price >= threshold):
                    continue
                key = (side, offer.id)
                active.add(key)
                row = self.db.execute(
                    "SELECT price FROM price_alert_sent WHERE side=? AND ad_id=?", key).fetchone()
                previous = Decimal(row[0]) if row else None
                improved = previous is not None and (
                    offer.price < previous if side == "BUY" else offer.price > previous)
                if previous is not None and not improved:
                    continue
                low, high = eligible
                message = (
                    f"Binance P2P — {'BETTER PRICE' if improved else 'NEW MATCH'}\\n"
                    f"{side} USDT | EUR {offer.price:f}/USDT\\n"
                    f"Your threshold: EUR {threshold:f}\\n"
                    f"Merchant: {offer.merchant}\\n"
                    f"Trade amount: EUR {low:,.2f}–{high:,.2f}\\n"
                    f"Payment: {', '.join(offer.methods)}\\n"
                    f"Seen: {time.strftime('%H:%M:%S', time.localtime())}\\n"
                    f"https://c2c.binance.com/en/adv?code={offer.id}\\n"
                    "Verify availability and terms in Binance."
                ).replace("\\n", "\n")
                send(message)  # Persist only after successful Telegram delivery.
                with self.db:
                    self.db.execute("""INSERT INTO price_alert_sent(side,ad_id,price,sent_at)
                        VALUES(?,?,?,?) ON CONFLICT(side,ad_id)
                        DO UPDATE SET price=excluded.price,sent_at=excluded.sent_at""",
                        (side, offer.id, str(offer.price), int(time.time()*1000)))
        # Clear offers no longer qualifying, so re-entry can trigger a fresh alert.
        with self.db:
            for side, ad_id in self.db.execute("SELECT side,ad_id FROM price_alert_sent").fetchall():
                if (side,ad_id) not in active:
                    self.db.execute("DELETE FROM price_alert_sent WHERE side=? AND ad_id=?", (side,ad_id))
