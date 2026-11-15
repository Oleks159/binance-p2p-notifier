"""Regression test for Telegram duplicate-update suppression."""
import sqlite3
import unittest
from types import SimpleNamespace
from telegram_commands import Commands

class FakeTelegram:
    def __init__(self):
        self.sent = []
    def call(self, method, body):
        if method == "getUpdates":
            return [{"update_id": 42, "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"is_bot": False}, "text": "/help"}}]
    def send(self, message):
        self.sent.append(message)

class DedupTests(unittest.TestCase):
    def test_repeated_update_and_restart(self):
        db = sqlite3.connect(":memory:")
        store = SimpleNamespace(db=db)
        telegram = FakeTelegram()
        Commands(telegram, "123").poll(store, None, None, lambda _: None)
        Commands(telegram, "123").poll(store, None, None, lambda _: None)
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_processed_updates").fetchone()[0], 1)

if __name__ == "__main__":
    unittest.main()
