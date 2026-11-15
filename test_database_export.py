import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database_export import export_database

class ExportTests(unittest.TestCase):
    def test_consistent_backup_and_private_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "live.sqlite3"
            db = sqlite3.connect(path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE changes (value TEXT)")
            db.execute("INSERT INTO changes VALUES ('snapshot')")
            db.commit()
            store = SimpleNamespace(db=db)
            telegram = SimpleNamespace(token="123:abc", chat_id="12345")
            captured = {}
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, *args): return b'{"ok":true}'
            class Opener:
                def open(self, request, timeout):
                    captured["url"] = request.full_url
                    captured["body"] = request.data
                    return Response()
            with patch("database_export.build_opener", return_value=Opener()):
                export_database(store, telegram)
            self.assertIn(b'12345', captured["body"])
            self.assertIn(b'SQLite format 3', captured["body"])
            self.assertIn(b'snapshot', captured["body"])
            self.assertIn("/sendDocument", captured["url"])
            db.close()

if __name__ == "__main__":
    unittest.main()
