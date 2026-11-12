"""Binance P2P -> Telegram. Python standard library only; no trading actions."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.binance.com"
ORDER_PATH = "/sapi/v1/c2c/agent/orderMatch/listOrders"


class ServiceError(Exception):
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = max(10, min(int(retry_after), 86400))


class NoRedirects(HTTPRedirectHandler):
    # Never forward credentials to a redirected host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, *, headers=None, body=None):
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = Request(url, data=data, headers=headers or {})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with build_opener(NoRedirects()).open(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        retry = error.headers.get("Retry-After", "30")
        retry = int(retry) if retry.isdigit() else 30
        # Do not print HTTPError: its URL can contain signatures or bot tokens.
        if error.code in (401, 403):
            detail = "Access denied. Check API permissions, IP restrictions and account eligibility."
        elif error.code in (418, 429):
            detail = "Rate limited. Waiting before retrying."
            retry = max(retry, 120 if error.code == 418 else 30)
        else:
            detail = "Check the connection settings and service availability."
        raise ServiceError(f"HTTP {error.code}. {detail}", retry) from None
    except (URLError, TimeoutError, OSError):
        raise ServiceError("Network connection failed or timed out. Check your internet connection.") from None
    except (ValueError, UnicodeError):
        raise ServiceError("The service returned an unreadable response.") from None


def api_code(value):
    # Error codes only: never echo arbitrary response data or credentials.
    code = str(value)
    return code if re.fullmatch(r"-?[A-Za-z0-9_]{1,40}", code) else "unknown"


def timestamp_ms(value):
    if isinstance(value, bool) or value is None:
        raise ServiceError("An order has no valid creation time; checkpoint was not advanced.")
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = int(value)
            if number < 1_000_000_000_000:
                number *= 1000
            return number
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # Do not guess Binance's timezone for an undocumented date format.
            raise ValueError()
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError, TypeError):
        raise ServiceError("Unsupported order creation time; checkpoint was not advanced.") from None


def order_message(order):
    def field(name, fallback="?"):
        return str(order.get(name, fallback))[:160].replace("\r", " ").replace("\n", " ")

    trade = field("tradeType")
    trade = {"0": "BUY", "1": "SELL"}.get(trade, trade)
    status = field("orderStatus")
    status = {"1": "Unpaid", "2": "Paid", "3": "Appealing", "4": "Completed",
              "6": "Cancelled", "7": "Cancelled by system"}.get(status, status)
    created = datetime.fromtimestamp(timestamp_ms(order["createTime"]) / 1000, timezone.utc)
    return (f"New Binance P2P order\nOrder: {field('orderNumber')}\n"
            f"Type: {trade}\nAmount: {field('amount')} {field('asset')}\n"
            f"Total: {field('totalPrice')} {field('fiat')}\nStatus: {status}\n"
            f"Created: {created:%Y-%m-%d %H:%M:%S} UTC\n\n"
            "Open Binance P2P to review the order.")


class Binance:
    def __init__(self, key, secret, transport=request_json):
        self.key, self.secret, self.transport = key, secret, transport
        self.offset = 0

    def now(self):
        return int(time.time() * 1000) + self.offset

    def sync_time(self):
        before = int(time.time() * 1000)
        response = self.transport(API + "/api/v3/time")
        after = int(time.time() * 1000)
        try:
            self.offset = int(response["serverTime"]) - (before + after) // 2
        except (TypeError, ValueError, KeyError):
            raise ServiceError("Could not synchronize with Binance server time.") from None

    def page(self, start, end, page):
        query = urlencode([("recvWindow", 10000), ("timestamp", self.now())], quote_via=quote)
        signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        result = self.transport(
            f"{API}{ORDER_PATH}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": self.key, "User-Agent": "binance-wallet/1.0.0 (Skill)"},
            body={"startDate": start, "endDate": end, "page": page, "rows": 20})
        if not isinstance(result, dict):
            raise ServiceError("Unexpected Binance response.")
        if result.get("success") is False or str(result.get("code", "000000")) not in ("0", "000000"):
            code = api_code(result.get("code"))
            hint = ("Check your PC clock." if code == "-1021" else
                    "Check P2P API eligibility, API key permissions and IP restrictions.")
            raise ServiceError(f"Binance rejected the order query ({code}). {hint}")
        rows, total = result.get("data"), result.get("total")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ServiceError("Unexpected Binance order list; checkpoint was not advanced.")
        if total is not None:
            try:
                total = int(total)
                if total < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ServiceError("Invalid Binance pagination total.") from None
        return rows, total

    def orders(self, start, end):
        found = {}
        count = 0
        for page in range(1, 10001):
            rows, total = self.page(start, end, page)
            for order in rows:
                number = order.get("orderNumber")
                if not number or not isinstance(number, (str, int)):
                    raise ServiceError("An order has no valid ID; checkpoint was not advanced.")
                created = timestamp_ms(order.get("createTime"))
                if not start <= created <= end:
                    raise ServiceError("Binance returned orders outside the requested time window.")
                if str(number) in found:
                    raise ServiceError("Binance pagination repeated an order; retrying without advancing.")
                found[str(number)] = order
            count += len(rows)
            if total is not None and count >= total:
                break
            if len(rows) < 20:
                if total is not None and count < total:
                    raise ServiceError("Incomplete Binance pagination; checkpoint was not advanced.")
                break
        else:
            raise ServiceError("Too many pages; checkpoint was not advanced. Contact the app maintainer.")
        return sorted(found.values(), key=lambda row: (timestamp_ms(row["createTime"]), str(row["orderNumber"])))


class Telegram:
    def __init__(self, token, chat_id, transport=request_json):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ServiceError("Enter the bot token from Telegram @BotFather.")
        self.token, self.chat_id, self.transport = token, chat_id, transport

    def call(self, method, body):
        response = self.transport(f"https://api.telegram.org/bot{self.token}/{method}", body=body)
        if not isinstance(response, dict) or response.get("ok") is not True:
            retry = response.get("parameters", {}).get("retry_after", 30) if isinstance(response, dict) else 30
            raise ServiceError("Telegram rejected the request. Check your bot token, chat ID and bot chat.", retry)
        return response.get("result")

    def find_private_chat(self, report=lambda text: None):
        identity = self.call("getMe", {})
        username = identity.get("username", "") if isinstance(identity, dict) else ""
        if not re.fullmatch(r"[A-Za-z0-9_]+", username):
            raise ServiceError("Telegram returned no valid bot username. Check your bot token.")
        report(f"Token verified: @{username}. Make sure this is your intended bot.")
        webhook = self.call("getWebhookInfo", {})
        if not isinstance(webhook, dict):
            raise ServiceError("Telegram returned an unexpected webhook status.")
        if webhook.get("url"):
            raise ServiceError(
                f"@{username} is connected to another service through a webhook. "
                "Use a separate bot for this notifier, or disconnect it from that service first.")
        report(f"Waiting for a message. Open https://t.me/{username} and send hello now.")
        try:
            result = self.call("getUpdates", {"timeout": 10, "limit": 100, "allowed_updates": ["message"]})
        except ServiceError as error:
            if "HTTP 409" in str(error):
                raise ServiceError("Another program is reading this bot's messages. Stop it or use a dedicated bot.") from None
            raise
        if not isinstance(result, list):
            raise ServiceError("Telegram returned an unexpected message list.")
        chats = set()
        for update in result or []:
            chat = update.get("message", {}).get("chat", {})
            if chat.get("type") == "private":
                chats.add(str(chat["id"]))
        if not chats:
            raise ServiceError(
                f"No messages received for @{username}. Open https://t.me/{username}, "
                "send a NEW hello message, then click Find my chat again. "
                "If that is a different bot, replace the token with the one from your bot's BotFather page.")
        if len(chats) > 1:
            raise ServiceError("Multiple private chats found. Enter your own numeric chat ID manually, or use a new dedicated bot.")
        return chats.pop()

    def send(self, message):
        if not re.fullmatch(r"-?[0-9]+", self.chat_id):
            raise ServiceError("Enter your numeric Telegram chat ID, or click Find my chat.")
        self.call("sendMessage", {"chat_id": self.chat_id, "text": message})


class Store:
    def __init__(self, path: Path, account: str, start: int):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, since INTEGER, cursor INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS alerts (account TEXT, id TEXT, message TEXT, sent INTEGER DEFAULT 0, PRIMARY KEY(account,id))")
        self.account = account
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO accounts VALUES (?,?,?)", (account, start, start))

    def times(self):
        return self.db.execute("SELECT since,cursor FROM accounts WHERE id=?", (self.account,)).fetchone()

    def record(self, orders, end):
        since, _ = self.times()
        # New alerts and the checkpoint commit together, even after a crash.
        with self.db:
            for order in orders:
                if timestamp_ms(order["createTime"]) >= since:
                    self.db.execute("INSERT OR IGNORE INTO alerts(account,id,message) VALUES (?,?,?)",
                                    (self.account, str(order["orderNumber"]), order_message(order)))
            self.db.execute("UPDATE accounts SET cursor=MAX(cursor,?) WHERE id=?", (end, self.account))

    def deliver(self, send, stopped=lambda: False):
        count = 0
        while not stopped():
            row = self.db.execute("SELECT id,message FROM alerts WHERE account=? AND sent=0 ORDER BY rowid LIMIT 1", (self.account,)).fetchone()
            if row is None:
                break
            send(row[1])
            with self.db:
                self.db.execute("UPDATE alerts SET sent=1 WHERE account=? AND id=?", (self.account, row[0]))
            count += 1
        return count

    def close(self):
        self.db.close()


def monitor(config, folder, stop, report):
    client = Binance(config["api_key"], config["api_secret"])
    telegram = Telegram(config["bot_token"], config["chat_id"])
    store = None
    errors = 0
    sync_at = 0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() >= sync_at:
                    client.sync_time()
                    sync_at = time.monotonic() + 300
                if store is None:
                    account = hashlib.sha256(config["api_key"].encode()).hexdigest()
                    store = Store(folder / "orders.sqlite3", account, client.now())
                    report("Connected. Monitoring orders created after the first start for this API key.")
                sent = store.deliver(telegram.send, stop.is_set)
                _, cursor = store.times()
                now = client.now()
                # Catch up one hour at a time, with a five-minute overlap for delayed visibility.
                end = min(now, cursor + 3_600_000)
                if end >= cursor and not stop.is_set():
                    orders = client.orders(max(0, cursor - 300_000), end)
                    store.record(orders, end)
                    sent += store.deliver(telegram.send, stop.is_set)
                if sent:
                    report(f"Sent {sent} new order alert(s).")
                report("Monitoring. Last successful check: " + datetime.now().strftime("%H:%M:%S"))
                errors = 0
                if end < now:
                    stop.wait(1)
                else:
                    stop.wait(config["poll_seconds"])
            except ServiceError as error:
                errors += 1
                delay = max(error.retry_after, min(300, 15 * 2 ** min(errors - 1, 5)))
                report(f"{error} Retrying in {delay}s.")
                stop.wait(delay)
    finally:
        if store is not None:
            store.close()
