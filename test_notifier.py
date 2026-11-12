import hashlib
import hmac
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from notifier import Binance, ServiceError, Store, Telegram, monitor, request_json, timestamp_ms

BASE = 1_800_000_000_000


def order(number, created=BASE):
    return {"orderNumber": str(number), "createTime": created, "tradeType": "BUY",
            "asset": "USDT", "amount": "10.00", "totalPrice": "9.50", "fiat": "EUR",
            "orderStatus": 1}


class BinanceTests(unittest.TestCase):
    def test_signed_request_matches_wire_query_and_schema(self):
        calls = []

        def transport(url, **kwargs):
            calls.append((url, kwargs))
            return {"code": "000000", "success": True, "data": [], "total": 0}

        client = Binance("test-key", "test-secret", transport)
        client.page(BASE, BASE + 1000, 3)
        url, kwargs = calls[0]
        query, signature = urlsplit(url).query.rsplit("&signature=", 1)
        self.assertEqual(signature, hmac.new(b"test-secret", query.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(kwargs["headers"]["X-MBX-APIKEY"], "test-key")
        self.assertEqual(kwargs["body"], {"startDate": BASE, "endDate": BASE + 1000, "page": 3, "rows": 20})
        self.assertEqual(parse_qs(query)["recvWindow"], ["10000"])

    def test_pagination_reads_more_than_first_page(self):
        pages = []

        def transport(url, **kwargs):
            page = kwargs["body"]["page"]
            pages.append(page)
            data = [order(i) for i in range(20)] if page == 1 else [order(20)]
            return {"code": "000000", "data": data, "total": 21}

        self.assertEqual(len(Binance("k", "s", transport).orders(BASE, BASE + 1000)), 21)
        self.assertEqual(pages, [1, 2])

    def test_exact_page_without_total_reads_until_empty(self):
        def transport(url, **kwargs):
            data = [order(i) for i in range(20)] if kwargs["body"]["page"] == 1 else []
            return {"data": data}
        self.assertEqual(len(Binance("k", "s", transport).orders(BASE, BASE + 1000)), 20)

    def test_rejects_malformed_denied_incomplete_and_repeated_pages(self):
        for response in [
            {"code": "-1002", "success": False},
            {"data": {}}, {"data": [{"orderNumber": "1"}]},
            {"data": [order(1)], "total": 25},
            {"data": [order(1, BASE - 1)], "total": 1},
            {"data": [order(i) for i in range(20)], "total": 40},
        ]:
            with self.subTest(response=response), self.assertRaises(ServiceError):
                Binance("k", "s", lambda *a, **k: response).orders(BASE, BASE + 1000)

    def test_error_response_does_not_expose_upstream_message(self):
        client = Binance("k", "s", lambda *a, **k: {"code": "-1002", "message": "SECRET"})
        with self.assertRaises(ServiceError) as context:
            client.page(BASE, BASE, 1)
        self.assertNotIn("SECRET", str(context.exception))

    def test_normalizes_millisecond_second_and_iso_times(self):
        self.assertEqual(timestamp_ms(BASE), BASE)
        self.assertEqual(timestamp_ms(BASE // 1000), BASE)
        self.assertEqual(timestamp_ms("2027-01-15T08:00:00Z"), BASE)
        with self.assertRaises(ServiceError):
            timestamp_ms("2027-01-15 08:00:00")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path, "account", BASE)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_skips_old_orders_deduplicates_and_survives_restart(self):
        self.store.record([order("old", BASE - 1), order("new")], BASE + 1000)
        sent = []
        self.assertEqual(self.store.deliver(sent.append), 1)
        self.store.record([order("new")], BASE + 2000)
        self.assertEqual(self.store.deliver(sent.append), 0)
        self.store.close()
        self.store = Store(self.path, "account", BASE + 10000)
        self.assertEqual(self.store.times(), (BASE, BASE + 2000))
        self.assertEqual(self.store.deliver(sent.append), 0)
        self.assertEqual(len(sent), 1)

    def test_failed_delivery_stays_pending(self):
        self.store.record([order("new")], BASE + 1000)

        def fail(message):
            raise ServiceError("Telegram unavailable")

        with self.assertRaises(ServiceError):
            self.store.deliver(fail)
        self.store.close()
        self.store = Store(self.path, "account", BASE + 10000)
        self.assertEqual(self.store.deliver(lambda message: None), 1)

    def test_bad_batch_rolls_back_alerts_and_checkpoint(self):
        with self.assertRaises(ServiceError):
            self.store.record([order("valid"), order("invalid", "bad")], BASE + 1000)
        self.assertEqual(self.store.times(), (BASE, BASE))
        self.assertEqual(self.store.deliver(lambda message: None), 0)

    def test_account_keys_have_separate_boundaries(self):
        other = Store(self.path, "other-account", BASE + 10000)
        try:
            self.store.record([order("one")], BASE + 1000)
            self.assertEqual(other.deliver(lambda message: None), 0)
            self.assertEqual(other.times(), (BASE + 10000, BASE + 10000))
        finally:
            other.close()


class TelegramTests(unittest.TestCase):
    def test_private_chat_discovery_and_send(self):
        calls = []

        def transport(url, **kwargs):
            calls.append((url, kwargs["body"]))
            if url.endswith("/getMe"):
                return {"ok": True, "result": {"username": "my_test_bot"}}
            if url.endswith("/getWebhookInfo"):
                return {"ok": True, "result": {"url": ""}}
            return {"ok": True, "result": [{"message": {"chat": {"id": 12345, "type": "private"}}}]}

        telegram = Telegram("123:fake_token", "12345", transport)
        self.assertEqual(telegram.find_private_chat(), "12345")
        telegram.send("test")
        self.assertEqual(calls[-1][1], {"chat_id": "12345", "text": "test"})

    def test_does_not_choose_between_multiple_private_chats(self):
        result = [{"message": {"chat": {"id": number, "type": "private"}}} for number in [1, 2]]
        def transport(url, **kwargs):
            if url.endswith("/getMe"):
                return {"ok": True, "result": {"username": "my_test_bot"}}
            if url.endswith("/getWebhookInfo"):
                return {"ok": True, "result": {"url": ""}}
            return {"ok": True, "result": result}
        telegram = Telegram("123:fake_token", "", transport)
        with self.assertRaises(ServiceError):
            telegram.find_private_chat()

    def test_empty_updates_explain_which_bot_needs_a_new_message(self):
        def transport(url, **kwargs):
            if url.endswith("/getMe"):
                return {"ok": True, "result": {"username": "actual_token_bot"}}
            if url.endswith("/getWebhookInfo"):
                return {"ok": True, "result": {"url": ""}}
            self.assertEqual(kwargs["body"]["allowed_updates"], ["message"])
            return {"ok": True, "result": []}
        logs = []
        with self.assertRaises(ServiceError) as context:
            Telegram("123:fake_token", "", transport).find_private_chat(logs.append)
        self.assertIn("https://t.me/actual_token_bot", str(context.exception))
        self.assertIn("NEW hello", str(context.exception))
        self.assertTrue(any("Token verified: @actual_token_bot" in log for log in logs))

    def test_existing_webhook_is_explained_without_deleting_it(self):
        methods = []
        def transport(url, **kwargs):
            method = url.rsplit("/", 1)[-1]
            methods.append(method)
            result = {"username": "my_test_bot"} if method == "getMe" else {"url": "https://example.invalid/private-path"}
            return {"ok": True, "result": result}
        with self.assertRaises(ServiceError) as context:
            Telegram("123:fake_token", "", transport).find_private_chat()
        self.assertIn("webhook", str(context.exception))
        self.assertNotIn("private-path", str(context.exception))
        self.assertEqual(methods, ["getMe", "getWebhookInfo"])

    def test_another_polling_program_has_actionable_error(self):
        def transport(url, **kwargs):
            if url.endswith("/getMe"):
                return {"ok": True, "result": {"username": "my_test_bot"}}
            if url.endswith("/getWebhookInfo"):
                return {"ok": True, "result": {"url": ""}}
            raise ServiceError("HTTP 409. Check connection settings.")
        with self.assertRaises(ServiceError) as context:
            Telegram("123:fake_token", "", transport).find_private_chat()
        self.assertIn("Another program", str(context.exception))

    def test_telegram_failure_is_not_marked_as_success(self):
        telegram = Telegram("123:fake_token", "12345", lambda *a, **k: {"ok": False, "parameters": {"retry_after": 90}})
        with self.assertRaises(ServiceError) as context:
            telegram.send("test")
        self.assertEqual(context.exception.retry_after, 90)

    def test_http_error_does_not_log_token(self):
        from urllib.error import HTTPError
        from email.message import Message
        headers = Message()
        headers["Retry-After"] = "70"
        error = HTTPError("https://example.invalid/SECRET", 429, "SECRET", headers, None)
        with patch("notifier.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(ServiceError) as context:
                request_json("https://example.invalid/SECRET")
        self.assertNotIn("SECRET", str(context.exception))
        self.assertEqual(context.exception.retry_after, 70)


class MonitorTests(unittest.TestCase):
    def test_monitor_delivers_then_catches_up_after_restart(self):
        import threading
        stop = threading.Event()
        sent = []
        windows = []

        class FakeBinance:
            current = BASE

            def __init__(self, *args):
                pass

            def sync_time(self):
                pass

            def now(self):
                return self.current

            def orders(self, start, end):
                windows.append((start, end))
                return [order("first" if end == BASE else "second", end)]

        class FakeTelegram:
            def __init__(self, *args):
                pass

            def send(self, text):
                sent.append(text)
                stop.set()

        config = {"api_key": "k", "api_secret": "s", "bot_token": "123:fake", "chat_id": "1", "poll_seconds": 15}
        with tempfile.TemporaryDirectory() as temp, patch("notifier.Binance", FakeBinance), patch("notifier.Telegram", FakeTelegram):
            monitor(config, Path(temp), stop, lambda text: None)
            stop.clear()
            FakeBinance.current = BASE + 10000
            monitor(config, Path(temp), stop, lambda text: None)
        self.assertEqual(len(sent), 2)
        self.assertIn("first", sent[0])
        self.assertIn("second", sent[1])
        self.assertEqual(windows[1], (BASE - 300000, BASE + 10000))


if __name__ == "__main__":
    unittest.main()
