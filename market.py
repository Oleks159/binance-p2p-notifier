"""Public USDT/EUR market watcher. BUY and SELL are the taker's perspective."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode

from notifier import ServiceError, Telegram, api_code, request_json
from daily_report import send_daily_if_due

PUBLIC = "https://www.binance.com/bapi/c2c/v1/public/c2c/agent/"
TARGET_METHODS = {"sepainstant": "SEPA Instant", "wise": "Wise"}
CENT = Decimal("0.01")


def decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ServiceError("An offer contains an invalid number. The market snapshot was not saved.") from None


@dataclass(frozen=True)
class Rules:
    minimum: Decimal = Decimal("500")
    maximum: Decimal = Decimal("10000")
    top: int = 5

    @classmethod
    def from_config(cls, config):
        minimum = decimal(config.get("min_eur", "500"))
        maximum = decimal(config.get("max_eur", "10000"))
        if minimum < 1 or maximum < minimum or maximum > 1_000_000_000:
            raise ServiceError("Choose a minimum of at least EUR 1 and a maximum at least as large as the minimum.")
        return cls(minimum, maximum)

    def key(self):
        # Different ranges have separate ranking state, history and notification queues.
        return f"USDT/EUR|{self.minimum.normalize()}|{self.maximum.normalize()}|SEPA Instant,Wise|top5"


@dataclass(frozen=True)
class Offer:
    id: str
    side: str
    price: Decimal
    minimum_crypto: Decimal
    maximum_crypto: Decimal
    available: Decimal
    merchant: str
    methods: tuple[str, ...]
    completion: Decimal | None
    order_count: int | None

    def usable(self, rules):
        # The public Agent API's minTransAmount/maxTransAmount are asset units.
        # Example verified against Binance's web listing: 8,790.10 USDT at
        # EUR 0.910 corresponds to an approximately EUR 7,999 minimum.
        low = max(rules.minimum, self.minimum_crypto * self.price)
        high = min(rules.maximum, self.maximum_crypto * self.price, self.available * self.price)
        low = low.quantize(CENT, rounding=ROUND_CEILING)
        high = high.quantize(CENT, rounding=ROUND_FLOOR)
        return (low, high) if low <= high and high > 0 else None

    def serialized(self):
        item = asdict(self)
        for name in ("price", "minimum_crypto", "maximum_crypto", "available", "completion"):
            item[name] = str(item[name]) if item[name] is not None else None
        return json.dumps(item, sort_keys=True, separators=(",", ":"))


def parse_offer(item, side, known_methods):
    if not isinstance(item, dict) or not str(item.get("adNo", "")).isdigit():
        raise ServiceError("Unexpected public offer format. The market snapshot was not saved.")
    if item.get("asset") != "USDT" or item.get("fiat") != "EUR":
        raise ServiceError("Binance returned a different market. The market snapshot was not saved.")
    price, minimum, maximum, available = [decimal(item.get(name)) for name in
                                          ("price", "minTransAmount", "maxTransAmount", "tradableAmount")]
    if price <= 0 or minimum < 0 or maximum < minimum or available < 0:
        raise ServiceError("An offer has invalid price or limits. The market snapshot was not saved.")
    method_ids = item.get("tradeMethods")
    if not isinstance(method_ids, list) or any(not isinstance(method, str) for method in method_ids):
        raise ServiceError("An offer has unsupported payment method data.")
    methods = tuple(sorted({known_methods[method] for method in method_ids if method in known_methods}))
    advertiser = item.get("advertiser") or {}
    if not isinstance(advertiser, dict):
        raise ServiceError("An offer has invalid advertiser data.")
    completion = decimal(advertiser["monthFinishRate"]) if advertiser.get("monthFinishRate") is not None else None
    count = advertiser.get("monthOrderCount")
    if count is not None:
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = None
    merchant = str(advertiser.get("nickName", "Unknown"))[:100].replace("\n", " ").replace("\r", " ")
    return Offer(str(item["adNo"]), side, price, minimum, maximum, available,
                 merchant, methods, completion, count)


class PublicMarket:
    def __init__(self, transport=request_json):
        self.transport = transport
        self.methods = None

    def get(self, endpoint, params):
        result = self.transport(PUBLIC + endpoint + "?" + urlencode(params),
                                headers={"User-Agent": "binance-wallet/1.0.0 (Skill)"})
        if not isinstance(result, dict) or result.get("success") is False or str(result.get("code")) not in ("000000", "0"):
            code = api_code(result.get("code")) if isinstance(result, dict) else "unknown"
            raise ServiceError(f"Binance public market query failed ({code}).")
        return result.get("data")

    def discover_methods(self):
        result = self.get("trade-methods", {"fiat": "EUR"})
        if not isinstance(result, list):
            raise ServiceError("Unexpected Binance payment method response.")
        methods = {}
        for item in result:
            if not isinstance(item, dict):
                continue
            normalized = "".join(char for char in str(item.get("tradeMethodName", "")).lower() if char.isalnum())
            if normalized in TARGET_METHODS and isinstance(item.get("identifier"), str):
                methods[item["identifier"]] = TARGET_METHODS[normalized]
        if set(methods.values()) != {"SEPA Instant", "Wise"}:
            raise ServiceError("Binance did not return both Wise and SEPA Instant for EUR. No partial scan was saved.")
        self.methods = methods
        return methods

    def scan(self, stopped=lambda: False):
        if self.methods is None:
            self.discover_methods()
        all_offers = {"BUY": {}, "SELL": {}}
        for side in ("BUY", "SELL"):
            for identifier in self.methods:
                if stopped():
                    return None
                data = self.get("ad-list", {"fiat": "EUR", "asset": "USDT", "tradeType": side,
                                            "limit": 20, "tradeMethodIdentifiers": identifier})
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ServiceError("Unexpected Binance public ad list. No partial scan was saved.")
                for item in data["items"]:
                    offer = parse_offer(item, side, self.methods)
                    # Validate the API's payment filter locally too.
                    if self.methods[identifier] in offer.methods:
                        all_offers[side][offer.id] = offer
        return {side: list(offers.values()) for side, offers in all_offers.items()}


def rank(offers, rules, previous=()):
    previous_order = {number: index for index, number in enumerate(previous)}
    eligible = [offer for offer in offers if offer.methods and offer.usable(rules)]
    # Stable ties avoid changing leaders just because equal-price API rows reorder.
    return sorted(eligible, key=lambda offer: (
        offer.price if offer.side == "BUY" else -offer.price,
        previous_order.get(offer.id, 1000000), offer.id))[:rules.top]


def money(value):
    return f"{value:,.2f}"


def alert_text(offer, rules, position, reason):
    low, high = offer.usable(rules)
    action = "BUY USDT with EUR" if offer.side == "BUY" else "SELL USDT for EUR"
    quality = ""
    if offer.completion is not None:
        quality += f"\nCompletion: {offer.completion * 100:.1f}%"
    if offer.order_count is not None:
        quality += f" | 30-day orders: {offer.order_count}"
    return (f"Binance P2P - {reason}\n{action} | Rank #{position} in watched offers\n"
            f"Price: EUR {offer.price:f} / USDT\n"
            f"Usable trade value: approx EUR {money(low)} - {money(high)}\n"
            f"Available: {offer.available:,.2f} USDT\n"
            f"Payment: {', '.join(offer.methods)}\nMerchant: {offer.merchant}{quality}\n"
            f"Offer: {offer.id}\nhttps://c2c.binance.com/en/adv?code={offer.id}\n\n"
            "Review exact limits, terms and account eligibility in Binance.")


class MarketStore:
    def __init__(self, path: Path, rules):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.rules = rules
        self.profile = rules.key()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS offers (
                side TEXT, id TEXT, first_seen INTEGER, last_seen INTEGER,
                snapshot TEXT, PRIMARY KEY(side,id));
            CREATE TABLE IF NOT EXISTS changes (
                number INTEGER PRIMARY KEY, side TEXT, id TEXT, seen_at INTEGER, snapshot TEXT);
            CREATE TABLE IF NOT EXISTS rankings (
                profile TEXT, side TEXT, snapshot TEXT, checked_at INTEGER, PRIMARY KEY(profile,side));
            CREATE TABLE IF NOT EXISTS alerts (
                number INTEGER PRIMARY KEY, profile TEXT, side TEXT, id TEXT,
                created_at INTEGER, reason TEXT, message TEXT, sent INTEGER DEFAULT 0,
                superseded INTEGER DEFAULT 0);
        """)

    def observe(self, snapshot, now=None, notify=False):
        now = int(time.time() * 1000) if now is None else now
        ranked = {}
        with self.db:
            for side in ("BUY", "SELL"):
                record = self.db.execute("SELECT snapshot FROM rankings WHERE profile=? AND side=?",
                                         (self.profile, side)).fetchone()
                previous = json.loads(record[0]) if record else []
                previous_ids = [item["id"] for item in previous]
                previous_prices = {item["id"]: decimal(item["price"]) for item in previous}
                known = set()
                for offer in snapshot[side]:
                    serialized = offer.serialized()
                    old = self.db.execute("SELECT snapshot FROM offers WHERE side=? AND id=?", (side, offer.id)).fetchone()
                    if old:
                        known.add(offer.id)
                    if old is None or old[0] != serialized:
                        self.db.execute("INSERT INTO changes(side,id,seen_at,snapshot) VALUES (?,?,?,?)",
                                        (side, offer.id, now, serialized))
                    self.db.execute("INSERT INTO offers VALUES (?,?,?,?,?) ON CONFLICT(side,id) DO UPDATE SET last_seen=excluded.last_seen,snapshot=excluded.snapshot",
                                    (side, offer.id, now, now, serialized))
                best = rank(snapshot[side], self.rules, previous_ids)
                ranked[side] = best
                current = [{"id": offer.id, "price": str(offer.price)} for offer in best]
                if notify and current != previous:
                    # An old unsent ranking alert must not arrive after its offer is obsolete.
                    self.db.execute("UPDATE alerts SET superseded=1 WHERE profile=? AND side=? AND sent=0",
                                    (self.profile, side))
                for position, offer in enumerate(best, 1):
                    reason = None
                    if record is None:
                        reason = "CURRENT BEST" if position == 1 else "CURRENT TOP 5"
                    elif position == 1 and (not previous_ids or offer.id != previous_ids[0]):
                        reason = "NEW BEST NOW" if offer.id not in known else "BECAME BEST NOW"
                    elif offer.id not in previous_ids:
                        reason = "NEW OFFER IN TOP 5" if offer.id not in known else "ENTERED TOP 5"
                    elif offer.price != previous_prices[offer.id]:
                        improved = offer.price < previous_prices[offer.id] if side == "BUY" else offer.price > previous_prices[offer.id]
                        if improved:
                            reason = "BETTER PRICE - BEST NOW" if position == 1 else "BETTER PRICE IN TOP 5"
                    if notify and reason:
                        self.db.execute("INSERT INTO alerts(profile,side,id,created_at,reason,message) VALUES (?,?,?,?,?,?)",
                                        (self.profile, side, offer.id, now, reason,
                                         alert_text(offer, self.rules, position, reason)))
                # Carry forward still-relevant notifications superseded by an unrelated rank change.
                for position, offer in enumerate(best, 1):
                    if not notify:
                        break
                    pending = self.db.execute("SELECT number,created_at,sent FROM alerts WHERE profile=? AND side=? AND id=? ORDER BY number DESC LIMIT 1",
                                              (self.profile, side, offer.id)).fetchone()
                    if pending and not pending[2]:
                        if pending[1] == now:
                            self.db.execute("UPDATE alerts SET superseded=0 WHERE number=?", (pending[0],))
                        else:
                            self.db.execute("UPDATE alerts SET superseded=0,message=? WHERE number=?",
                                            (alert_text(offer, self.rules, position, "CURRENT BEST" if position == 1 else "CURRENT TOP 5"), pending[0]))
                self.db.execute("INSERT INTO rankings VALUES (?,?,?,?) ON CONFLICT(profile,side) DO UPDATE SET snapshot=excluded.snapshot,checked_at=excluded.checked_at",
                                (self.profile, side, json.dumps(current), now))
        return ranked

    def deliver(self, send, stopped=lambda: False):
        sent = 0
        while not stopped():
            row = self.db.execute("SELECT number,message FROM alerts WHERE profile=? AND sent=0 AND superseded=0 ORDER BY number LIMIT 1", (self.profile,)).fetchone()
            if row is None:
                break
            send(row[1])
            with self.db:
                self.db.execute("UPDATE alerts SET sent=1 WHERE number=?", (row[0],))
            sent += 1
            # A dedicated bot may send at most one message a second to one chat.
            # The monitor supplies its Event.wait through the send wrapper.
        return sent

    def history_count(self):
        return self.db.execute("SELECT COUNT(*) FROM offers").fetchone()[0]

    def close(self):
        self.db.close()


def watch(config, folder, stop, report, update=lambda ranked: None):
    rules = Rules.from_config(config)
    market = PublicMarket()
    telegram = Telegram(config["bot_token"], config["chat_id"])
    store = MarketStore(folder / "market.sqlite3", rules)
    from telegram_commands import Commands
    commands = Commands(telegram, config['chat_id'])
    errors = 0
    last_method_sync = 0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() - last_method_sync > 3600:
                    market.discover_methods()
                    last_method_sync = time.monotonic()
                snapshot = market.scan(stop.is_set)
                if snapshot is None or stop.is_set():
                    break
                ranked = store.observe(snapshot)
                update(ranked)
                report(f"Checked BUY and SELL. Best five: {len(ranked['BUY'])} BUY, {len(ranked['SELL'])} SELL. "
                       f"Saved offers: {store.history_count()}. " + time.strftime("%H:%M:%S"))

                # No automatic market alerts or daily summaries: respond only to commands.
                try:
                    commands.poll(store, snapshot, rules, report)
                except ServiceError as error:
                    report(f"Telegram command polling failed: {error}")
                errors = 0
                stop.wait(config["poll_seconds"])
            except ServiceError as error:
                errors += 1
                delay = max(error.retry_after, min(300, 15 * 2 ** min(errors - 1, 5)))
                report(f"Market check failed: {error} Retrying in {delay}s.")
                stop.wait(delay)
    finally:
        store.close()
