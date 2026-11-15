"""Telegram command polling for the dedicated market watcher."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_report import build_daily_report
from custom_reports import parse_options, report as custom_report
from historical_range import parse_range, range_report
from price_alerts import parse_price
from search_diagnostics import diagnose
from market import rank
from notifier import ServiceError

TZ = ZoneInfo("Europe/Berlin")


def current_text(snapshot, rules):
    lines = ["Binance P2P — CURRENT observed top 5",
             f"Checked {datetime.now(TZ):%d.%m.%Y %H:%M:%S} Berlin",
             "USDT/EUR | SEPA Instant / Wise | observed API listings only", ""]
    for side in ("BUY", "SELL"):
        lines.append("TOP " + side + ":")
        offers = rank(snapshot[side], rules)
        if not offers:
            lines.append("No qualifying offers in the current scan.")
        for index, offer in enumerate(offers, 1):
            low, high = offer.usable(rules)
            lines.extend([
                f"{index}. EUR {offer.price:f}/USDT — {offer.merchant}",
                f"   EUR {low:,.2f}–{high:,.2f} | {', '.join(offer.methods)}",
                f"   https://c2c.binance.com/en/adv?code={offer.id}",
            ])
        lines.append("")
    lines.append("Prices can change. Check the live advertisement before trading.")
    return "\n".join(lines)


class Commands:
    def __init__(self, telegram, chat_id):
        self.telegram = telegram
        self.chat_id = str(chat_id)
        self.offset = None

    def poll(self, store, snapshot, rules, report, price_alerts=None, market=None):
        result = self.telegram.call("getUpdates", {
            "timeout": 0, "limit": 100,
            "allowed_updates": ["message"],
            **({"offset": self.offset} if self.offset is not None else {}),
        })
        if not isinstance(result, list):
            raise ServiceError("Unexpected Telegram updates response.")
        for update in result:
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            self.offset = max(self.offset or 0, update_id + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) != self.chat_id or chat.get("type") != "private":
                continue
            sender = message.get("from") or {}
            if sender.get("is_bot"):
                continue
            command = str(message.get("text") or "").split(maxsplit=1)[0].split("@")[0].lower()
            if command in ("/start", "/help"):
                response = ("Commands:\n/now — current top 5 BUY and SELL\n"
                            "/today [sepa|wise|all] [1..15] — today report\n"
                            "/range DD.MM HH:MM HH:MM [method] [count]\n"
                            "/range DD.MM HH:MM DD.MM HH:MM [method] [count]\n"
                            "/status — database and scan status\n"
                            "/buy 0.905 — BUY price ceiling\n"
                            "/sell 0.930 — SELL price floor\n"
                            "/alerts — view thresholds\n"
                            "/alerts off — disable alerts\n"
                            "/diagnose [ad ID] — test Binance search parameters\n/help — commands")
            elif command == "/now":
                response = current_text(snapshot, rules)
            elif command == "/today":
                try:
                    now = int(time.time() * 1000)
                    local = datetime.fromtimestamp(now/1000, TZ)
                    start = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
                    method, count = parse_options(str(message.get("text") or "").partition(" ")[2])
                    response = custom_report(store, start, now, method, count, "TODAY")
                except ValueError as error:
                    response = str(error)
            elif command == "/range":
                try:
                    tokens = str(message.get("text") or "").partition(" ")[2].split()
                    if len(tokens) >= 4 and (tokens[2].count(".") == 1 or tokens[2].count("-") == 2):
                        date_count = 4
                    else:
                        date_count = 3
                    start_ms, end_ms = parse_range(" ".join(tokens[:date_count]), int(time.time() * 1000))
                    method, count = parse_options(" ".join(tokens[date_count:]))
                    response = custom_report(store, start_ms, end_ms, method, count)
                except ValueError as error:
                    response = str(error)
            elif command in ("/buy", "/sell") and price_alerts is not None:
                try:
                    value = parse_price(str(message.get("text") or "").partition(" ")[2].strip())
                    price_alerts.configure(command[1:].upper(), value)
                    response = price_alerts.describe()
                except ValueError as error:
                    response = str(error)
            elif command == "/alerts" and price_alerts is not None:
                argument = str(message.get("text") or "").partition(" ")[2].strip().lower()
                if argument == "off":
                    price_alerts.disable()
                    response = "Price alerts disabled.\n" + price_alerts.describe()
                elif argument == "":
                    response = price_alerts.describe()
                else:
                    response = "Use /alerts or /alerts off"
            elif command == "/diagnose" and market is not None:
                target = str(message.get("text") or "").partition(" ")[2].strip()
                if target and (not target.isdigit() or len(target) > 32):
                    response = "Usage: /diagnose or /diagnose 12345678901234567890"
                else:
                    response = diagnose(market, rules, target or None)
            elif command == "/status":
                first = store.db.execute("SELECT MIN(seen_at),MAX(seen_at),COUNT(*) FROM changes").fetchone()
                response = (f"Watcher active. Stored price changes: {first[2]}.\n"
                            f"First saved change: {datetime.fromtimestamp(first[0]/1000, TZ):%d.%m %H:%M} Berlin\n"
                            if first[0] else "Watcher active. No historical changes saved yet.")
                response += "\nData may be incomplete after restarts without persistent storage."
            else:
                continue
            # Telegram sendMessage supports at most 4096 characters per message.
            # Split on line boundaries so long reports are delivered completely.
            chunk = ""
            for line in response.splitlines(keepends=True):
                if len(chunk) + len(line) > 3900 and chunk:
                    self.telegram.send(chunk)
                    chunk = ""
                chunk += line
            if chunk:
                self.telegram.send(chunk)
            report("Answered Telegram command " + command)
