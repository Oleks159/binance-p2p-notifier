"""Run with python -m unittest test_scan_coverage.py."""
import unittest
from market import PublicMarket

def ad(number):
    return {"adNo":str(number),"asset":"USDT","fiat":"EUR","price":"0.95",
            "minTransAmount":"500","maxTransAmount":"5000","tradableAmount":"10000",
            "tradeMethods":["sepa"],"advertiser":{"nickName":"tester"}}

class CoverageTests(unittest.TestCase):
    def test_multiple_pages_and_duplicate_page_stop(self):
        calls = []
        def transport(url, headers=None):
            from urllib.parse import urlparse,parse_qs
            q = parse_qs(urlparse(url).query)
            if "trade-methods" in url:
                return {"code":"000000","data":[
                    {"tradeMethodName":"SEPA Instant","identifier":"sepa"},
                    {"tradeMethodName":"Wise","identifier":"wise"}]}
            calls.append(q)
            page = int(q.get("page",["1"])[0])
            # Page 1 is full; page 2 contains a new item; no third page.
            ids = range(1,21) if page == 1 else [21]
            return {"code":"000000","data":{"items":[ad(i) for i in ids]}}
        market = PublicMarket(transport)
        result = market.scan()
        self.assertEqual(len(result["BUY"]),21)
        self.assertEqual(len(result["SELL"]),21)
        self.assertEqual(len(calls),8)
        self.assertIn("21 ads/2 pages",market.last_scan_stats["SELL/SEPA Instant"])

if __name__ == "__main__":
    unittest.main()
