import unittest
import sqlite3
import json
from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo
from custom_reports import parse_options, report

class CustomReportsTests(unittest.TestCase):
    def test_options(self):
        self.assertEqual(parse_options(["10","sepa"]),(10,"SEPA Instant"))
        self.assertEqual(parse_options(["wise","15"]),(15,"Wise"))
        self.assertEqual(parse_options([]),(5,None))
        with self.assertRaises(ValueError):
            parse_options(["16"])

    def test_filter_before_ranking(self):
        db=sqlite3.connect(":memory:")
        db.execute("CREATE TABLE changes(side TEXT,id TEXT,seen_at INTEGER,snapshot TEXT)")
        db.execute("CREATE TABLE offers(side TEXT,id TEXT,first_seen INTEGER,last_seen INTEGER,snapshot TEXT)")
        start=int(datetime(2026,11,15,tzinfo=ZoneInfo("Europe/Berlin")).timestamp()*1000)
        for i in range(16):
            item=dict(price=str(Decimal("0.80")+Decimal(i)/1000),minimum_crypto="500",maximum_crypto="10000",available="10000",methods=["Wise" if i<10 else "SEPA Instant"],merchant=f"Merchant{i}")
            db.execute("INSERT INTO changes VALUES (?,?,?,?,?)" if False else "INSERT INTO changes VALUES (?,?,?,?)",("BUY",str(i),start+1000,json.dumps(item)))
        store=SimpleNamespace(db=db,rules=SimpleNamespace(minimum=Decimal("500"),maximum=Decimal("10000")))
        text=report(store,start,start+5000,15,"SEPA Instant")
        self.assertIn("Merchant10",text)
        self.assertNotIn("Merchant0\\n",text)
        self.assertIn("TOP 15 BUY",text)

if __name__=="__main__":
    unittest.main()
