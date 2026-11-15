"""Tests: python -m unittest test_offer_visibility.py"""
import sqlite3
import unittest
from decimal import Decimal
from types import SimpleNamespace
from offer_visibility import setup, observe, extra_lines

class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        setup(self.db)
        self.db.execute("""CREATE TABLE offers
            (side TEXT,id TEXT,first_seen INTEGER,last_seen INTEGER,snapshot TEXT)""")

    def test_sessions_reappearance_and_legacy(self):
        offer = SimpleNamespace(id="123", serialized=lambda: '{"price":"0.92"}')
        observe(self.db, {"BUY": [], "SELL": [offer]}, 1000000)
        observe(self.db, {"BUY": [], "SELL": [offer]}, 1010000)
        observe(self.db, {"BUY": [], "SELL": []}, 1020000)
        observe(self.db, {"BUY": [], "SELL": [offer]}, 1030000)
        rows = self.db.execute("""SELECT first_seen,last_seen,missing_at FROM
            visibility_sessions WHERE side='SELL' ORDER BY id""").fetchall()
        self.assertEqual(rows, [(1000000,1010000,1020000),(1030000,1030000,None)])
        detail = extra_lines(self.db, "SELL", "123", 1005000, Decimal("3500"), Decimal("0.92"))
        self.assertTrue(any("3,500.00 USDT" in line for line in detail))
        self.assertTrue(any("10s" in line for line in detail))
        self.db.execute("INSERT INTO offers VALUES ('BUY','456',1000000,2000000,'{}')")
        legacy = extra_lines(self.db, "BUY", "456", 1500000)
        self.assertTrue(any("not continuous" in line for line in legacy))

if __name__ == "__main__":
    unittest.main()
