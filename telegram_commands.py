"""Telegram command polling for the dedicated market watcher."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_report import build_daily_report
from historical_range import parse_range, range_report
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

    def poll(self, store, snapshot, rules, report):
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
                            "/today — today's best observed top 5 BUY and SELL\n"
                            "/range DD.MM HH:MM HH:MM — same-day range\n"
                            "/range DD.MM HH:MM DD.MM HH:MM — across dates\n"
                            "/status — database and scan status\n/help — commands")
            elif command == "/now":
                response = current_text(snapshot, rules)
            elif command == "/today":
                response = build_daily_report(store, int(time.time() * 1000), today=True)
            elif command == "/range":
                try:
                    start_ms, end_ms = parse_range(str(message.get("text") or "").partition(" ")[2], int(time.time() * 1000))
                    response = range_report(store, start_ms, end_ms)
                except ValueError as error:
                    response = str(error)
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
