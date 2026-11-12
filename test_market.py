from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from market import MarketStore, Offer, PublicMarket, Rules, parse_offer, rank
from notifier import ServiceError


def offer(number, price="0.91", side="BUY", minimum="100", maximum="20000", available="20000", methods=("Wise",)):
    return Offer(str(number), side, D(price), D(minimum), D(maximum), D(available),
                 f"merchant-{number}", methods, D("0.99"), 100)


def snapshot(buy=(), sell=()):
    return {"BUY": list(buy), "SELL": list(sell)}


class FilteringTests(unittest.TestCase):
    def test_limits_are_asset_units_converted_to_eur(self):
        result = offer(1, "0.91", "SELL", minimum="8790.10", maximum="8791.20", available="8791.21").usable(Rules())
        self.assertEqual(result, (D("7999.00"), D("7999.99")))

    def test_overlapping_range_and_available_balance(self):
        rules = Rules()
        self.assertEqual(offer(1, "1", minimum="100", maximum="20000", available="900").usable(rules), (D("500.00"), D("900.00")))
        self.assertIsNone(offer(1, "1", minimum="11000").usable(rules))
        self.assertIsNone(offer(1, "1", maximum="499").usable(rules))
        self.assertIsNone(offer(1, "1", available="499").usable(rules))
        self.assertEqual(offer(1, "1", minimum="10000", maximum="10000").usable(rules), (D("10000.00"), D("10000.00")))

    def test_buy_lowest_and_sell_highest_with_stable_price_ties(self):
        items = [offer(1, "0.93"), offer(2, "0.91"), offer(3, "0.92")]
        self.assertEqual([item.id for item in rank(items, Rules())], ["2", "3", "1"])
        self.assertEqual([item.id for item in rank([replace(item, side="SELL") for item in items], Rules())], ["1", "3", "2"])
        self.assertEqual([item.id for item in rank([offer(2), offer(1)], Rules(), ["2", "1"])], ["2", "1"])

    def test_top_five_and_only_requested_methods(self):
        items = [offer(i, str(D("0.90") + D(i) / 1000)) for i in range(10)]
        items.append(offer(100, "0.5", methods=()))
        self.assertEqual([item.id for item in rank(items, Rules())], ["0", "1", "2", "3", "4"])


class PublicApiTests(unittest.TestCase):
    def test_discovers_identifiers_queries_both_sides_and_deduplicates(self):
        queries = []
        def transport(url, **kwargs):
            self.assertNotIn("X-MBX-APIKEY", kwargs.get("headers", {}))
            query = parse_qs(urlsplit(url).query)
            queries.append((urlsplit(url).path.rsplit("/", 1)[-1], query))
            if "trade-methods?" in url:
                data = [{"identifier": "foundInstant", "tradeMethodName": "SEPA Instant"},
                        {"identifier": "foundWise", "tradeMethodName": "Wise"},
                        {"identifier": "notInstant", "tradeMethodName": "SEPA"}]
            else:
                data = {"items": [{"adNo": "123", "asset": "USDT", "fiat": "EUR", "price": "0.91",
                                   "minTransAmount": "100", "maxTransAmount": "10000", "tradableAmount": "3000",
                                   "tradeMethods": ["foundInstant", "foundWise"], "advertiser": {"nickName": "seller"}}]}
            return {"code": "000000", "data": data, "success": True}
        result = PublicMarket(transport).scan()
        self.assertEqual(len(result["BUY"]), 1)
        self.assertEqual(len(result["SELL"]), 1)
        self.assertEqual(result["BUY"][0].methods, ("SEPA Instant", "Wise"))
        self.assertEqual(len(queries), 5)
        for name, query in queries[1:]:
            self.assertEqual(query["limit"], ["20"])
            self.assertIn(query["tradeMethodIdentifiers"][0], ("foundInstant", "foundWise"))
        self.assertEqual({query["tradeType"][0] for name, query in queries[1:]}, {"BUY", "SELL"})

    def test_missing_payment_method_fails_instead_of_partial_success(self):
        market = PublicMarket(lambda *a, **k: {"code": "000000", "data": [{"identifier": "Wise", "tradeMethodName": "Wise"}]})
        with self.assertRaises(ServiceError):
            market.scan()

    def test_invalid_market_and_numeric_schema_are_rejected(self):
        row = {"adNo": "123", "asset": "USDT", "fiat": "EUR", "price": "0.91",
               "minTransAmount": "100", "maxTransAmount": "10000", "tradableAmount": "3000",
               "tradeMethods": ["Wise"]}
        for changes in ({"fiat": "USD"}, {"price": "NaN"}, {"price": "0"}, {"maxTransAmount": "1"}, {"tradeMethods": [12]}):
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                parse_offer({**row, **changes}, "BUY", {"Wise": "Wise"})


class HistoryAndAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "market.sqlite3"
        self.store = MarketStore(self.path, Rules())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def deliver(self):
        messages = []
        self.store.deliver(messages.append)
        return messages

    def test_initial_snapshot_saved_then_unchanged_does_not_repeat(self):
        market = snapshot([offer(1), offer(2, "0.92")], [offer(3, side="SELL")])
        self.store.observe(market, 1)
        self.assertEqual(len(self.deliver()), 3)
        self.store.observe(market, 2)
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.store.history_count(), 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 3)

    def test_previous_second_is_promoted_after_leader_disappears(self):
        self.store.observe(snapshot([offer(1, "0.90"), offer(2, "0.91")]), 1)
        self.deliver()
        self.store.observe(snapshot([offer(2, "0.91")]), 2)
        messages = self.deliver()
        self.assertEqual(len(messages), 1)
        self.assertIn("BECAME BEST NOW", messages[0])
        self.assertIn("merchant-2", messages[0])
        self.store.observe(snapshot([offer(2, "0.91")]), 3)
        self.assertEqual(self.deliver(), [])

    def test_sell_successor_is_alerted_even_with_lower_price(self):
        self.store.observe(snapshot(sell=[offer(1, "0.95", "SELL"), offer(2, "0.94", "SELL")]), 1)
        self.deliver()
        self.store.observe(snapshot(sell=[offer(2, "0.94", "SELL")]), 2)
        messages = self.deliver()
        self.assertEqual(len(messages), 1)
        self.assertIn("BECAME BEST NOW", messages[0])

    def test_new_offer_outside_top_five_is_saved_but_not_notified(self):
        items = [offer(i, str(D("0.90") + D(i) / 1000)) for i in range(5)]
        self.store.observe(snapshot(items), 1)
        self.deliver()
        self.store.observe(snapshot(items + [offer(9, "0.99")]), 2)
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.store.history_count(), 6)

    def test_new_top_five_entry_and_price_improvement_alert(self):
        self.store.observe(snapshot([offer(1, "0.90"), offer(2, "0.95")]), 1)
        self.deliver()
        self.store.observe(snapshot([offer(1, "0.90"), offer(2, "0.94"), offer(3, "0.92")]), 2)
        messages = self.deliver()
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("NEW OFFER IN TOP 5" in message for message in messages))
        self.assertTrue(any("BETTER PRICE IN TOP 5" in message for message in messages))

    def test_history_records_changes_and_survives_restart(self):
        self.store.observe(snapshot([offer(1)]), 1)
        self.deliver()
        self.store.observe(snapshot([replace(offer(1), available=D("15000"))]), 2)
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 2)
        self.store.close()
        self.store = MarketStore(self.path, Rules())
        self.store.observe(snapshot([replace(offer(1), available=D("15000"))]), 3)
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.store.history_count(), 1)

    def test_atomic_rollback_does_not_save_partial_market(self):
        with self.assertRaises(KeyError):
            self.store.observe({"BUY": [offer(1)]}, 1)
        self.assertEqual(self.store.history_count(), 0)
        self.assertEqual(self.deliver(), [])

    def test_failed_notifications_retry_but_expired_offers_do_not(self):
        self.store.observe(snapshot([offer(1, "0.90"), offer(2, "0.91")]), 1)
        def fail(message):
            raise ServiceError("offline")
        with self.assertRaises(ServiceError):
            self.store.deliver(fail)
        self.store.observe(snapshot([offer(2, "0.91")]), 2)
        messages = self.deliver()
        self.assertEqual(len(messages), 1)
        self.assertIn("merchant-2", messages[0])
        self.assertNotIn("merchant-1", messages[0])
        self.store.observe(snapshot([offer(2, "0.91")]), 3)
        self.assertEqual(self.deliver(), [])

    def test_best_five_tie_order_survives_response_reordering(self):
        self.store.observe(snapshot([offer(1), offer(2)]), 1)
        self.deliver()
        ranked = self.store.observe(snapshot([offer(2), offer(1)]), 2)
        self.assertEqual(ranked["BUY"][0].id, "1")
        self.assertEqual(self.deliver(), [])


if __name__ == "__main__":
    unittest.main()
