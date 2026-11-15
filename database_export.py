"""Private Telegram export of a consistent SQLite backup (stdlib only)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from notifier import NoRedirects, ServiceError

MAX_EXPORT_BYTES = 45 * 1024 * 1024


def export_database(store, telegram):
    """Back up the live SQLite connection without interrupting writes."""
    fd, filename = tempfile.mkstemp(prefix="p2p-history-", suffix=".sqlite3")
    os.close(fd)
    path = Path(filename)
    try:
        with sqlite3.connect(str(path)) as destination:
            store.db.backup(destination)
        size = path.stat().st_size
        if size > MAX_EXPORT_BYTES:
            raise ServiceError("Database exceeds the 45 MB Telegram export limit. Ask for a date-filtered export.")
        content = path.read_bytes()
        boundary = "p2p-export-" + uuid.uuid4().hex
        def part(name, value):
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
        header = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="document"; filename="market-history.sqlite3"\r\n'
            "Content-Type: application/vnd.sqlite3\r\n\r\n"
        ).encode()
        body = (part("chat_id", str(telegram.chat_id)) +
                part("caption", "Binance P2P historical database backup") +
                header + content + f"\r\n--{boundary}--\r\n".encode())
        request = Request(
            f"https://api.telegram.org/bot{telegram.token}/sendDocument",
            data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        try:
            with build_opener(NoRedirects()).open(request, timeout=120) as response:
                import json
                result = json.load(response)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ServiceError("Telegram rejected the database export.")
        except (HTTPError, URLError, TimeoutError, OSError):
            raise ServiceError("Could not send the database backup to Telegram. Check logs and try again.") from None
    finally:
        path.unlink(missing_ok=True)
