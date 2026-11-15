"""Test durable Telegram command deduplication."""
import sqlite3
import unittest
from types import SimpleNamespace
from telegram_commands import Commands

class FakeTelegram:
    def __init__(self):
        self.messages=[]
        self.updates=[{"update_id": 10, "message": {"chat":{"id":42,"type":"private"},
                      "from":{"is_bot":False},"text":"/help"}}]
    def call(self, name, body):
        if name=="getUpdates":
            return [u for u in self.updates if u["update_id"] >= body.get("offset",0)]
        raise AssertionError(name)
    def send(self, text):
        self.messages.append(text)

class DedupTests(unittest.TestCase):
    def test_restart_does_not_repeat_commands(self):
        db=sqlite3.connect(":memory:")
        store=SimpleNamespace(db=db)
        bot=FakeTelegram()
        commands=Commands(bot,"42")
        commands.poll(store,{},None,lambda x:None)
        self.assertEqual(bot.messages,[])  # initial backlog skipped
        bot.updates.append({"update_id":11,"message":{"chat":{"id":42,"type":"private"},
                          "from":{"is_bot":False},"text":"/help"}})
        commands.poll(store,{},None,lambda x:None)
        self.assertEqual(len(bot.messages),1)
        Commands(bot,"42").poll(store,{},None,lambda x:None)
        self.assertEqual(len(bot.messages),1)

if __name__=="__main__":
    unittest.main()
