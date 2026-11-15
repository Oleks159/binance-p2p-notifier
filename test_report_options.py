import unittest
from telegram_commands import report_options

class ReportOptionsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(report_options(""), (5, None))
    def test_payment_and_count(self):
        self.assertEqual(report_options("10 sepa"), (10, "SEPA Instant"))
        self.assertEqual(report_options("wise 15"), (15, "Wise"))
        self.assertEqual(report_options("5"), (5, None))
    def test_invalid(self):
        for value in ("16 sepa", "0", "wise sepa", "10 11"):
            with self.assertRaises(ValueError):
                report_options(value)

if __name__ == "__main__":
    unittest.main()
