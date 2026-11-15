import json
import sqlite3
import unittest
from decimal import Decimal
from types import SimpleNamespace
from custom_reports import parse_options, report

class CustomReportTests(unittest.TestCase):
    def test_options(self):
        self.assertEqual(parse_options("sepa 10"), ("SEPA Instant",10))
        self.assertEqual(parse_options("15 wise"), ("Wise",15))
        self.assertEqual(parse_options(""), (None,5))
        with self.assertRaises(ValueError):
            parse_options("wise 16")

    def test_filter_and_rank(self):
        db=sqlite3.connect(":memory:")
        db.execute("CREATE TABLE changes(side TEXT,id TEXT,seen_at INTEGER,snapshot TEXT)")
        db.execute("CREATE TABLE offers(side TEXT,id TEXT,first_seen INTEGER,last_seen INTEGER,snapshot TEXT)")
        def add(side,id,price,methods):
            payload=dict(price=str(price),minimum_crypto="500",maximum_crypto="10000",
                         available="10000",merchant=id,methods=methods)
            db.execute("INSERT INTO changes VALUES(?,?,?,?,?)".replace("?,?,?,?,?","?,?,?,?"),
                       (side,id,1500,json.dumps(payload)))
        add("BUY","wisecheap","0.88",["Wise"])
        add("BUY","sepacheap","0.90",["SEPA Instant"])
        add("SELL","sepahigh","0.95",["SEPA Instant"])
        store=SimpleNamespace(db=db,rules=SimpleNamespace(minimum=Decimal("500"),maximum=Decimal("10000")))
        text=report(store,1000,2000,"SEPA Instant",10)
        self.assertIn("sepacheap",text)
        self.assertIn("sepahigh",text)
        self.assertNotIn("wisecheap",text)

if __name__=="__main__":
    unittest.main()
