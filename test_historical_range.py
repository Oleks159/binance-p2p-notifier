"""Regression tests for historical range reports. Run: python -m unittest test_historical_range.py"""
import json
import sqlite3
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from historical_range import historical_best, parse_range, range_report


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE changes (side TEXT,id TEXT,seen_at INTEGER,snapshot TEXT)")
        self.db.execute("CREATE TABLE offers (side TEXT,id TEXT,first_seen INTEGER,last_seen INTEGER,snapshot TEXT)")
        self.store = SimpleNamespace(db=self.db, rules=SimpleNamespace(
            minimum=Decimal("500"), maximum=Decimal("10000")))
        self.start, self.end = parse_range("2026-11-12 14:00 18:00")

    def add(self, side, ad_id, price, minute, method="Wise", minimum="500", maximum="5000"):
        item = dict(price=str(price), minimum_crypto=str(Decimal(minimum)/Decimal(str(price))),
                    maximum_crypto=str(Decimal(maximum)/Decimal(str(price))),
                    available="100000", methods=[method], merchant=ad_id)
        self.db.execute("INSERT INTO changes VALUES (?,?,?,?)",
                        (side, ad_id, self.start + minute*60000, json.dumps(item)))

    def test_parsing_and_invalid_windows(self):
        a,b = parse_range("12.11 23:00 13.11 01:00")
        self.assertEqual(b-a, 2*3600*1000)\n        c,d = parse_range("13.11 00:00 10:00")\n        self.assertEqual(d-c, 10*3600*1000)
        with self.assertRaises(ValueError):
            parse_range("2026-11-12 18:00 14:00")
        with self.assertRaises(ValueError):
            parse_range("2026-11-12 14:00 18:00", self.start-1)

    def test_ranking_dedup_and_bounds(self):
        self.add("BUY", "cheap", "0.906", 1)
        self.add("BUY", "cheap", "0.910", 2)
        self.add("BUY", "other", "0.908", 3)
        self.add("SELL", "high", "0.940", 4)
        self.add("SELL", "high", "0.925", 5)
        self.add("BUY", "too-small", "0.700", 6, minimum="1", maximum="100")
        self.add("BUY", "outside", "0.800", -1)
        ranked = historical_best(self.store, self.start, self.end)
        self.assertEqual([str(x["price"]) for x in ranked["BUY"]], ["0.906", "0.908"])
        self.assertEqual(str(ranked["SELL"][0]["price"]), "0.940")
        self.assertIn("cheap", range_report(self.store, self.start, self.end))


if __name__ == "__main__":
    unittest.main()
