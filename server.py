"""Headless server entry point; configuration comes only from environment variables."""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading

from market import Rules, watch
from notifier import ServiceError, Telegram


def load_config(environment):
    config = {
        "bot_token": environment.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": environment.get("TELEGRAM_CHAT_ID", "").strip(),
        "min_eur": environment.get("MIN_EUR", "500"),
        "max_eur": environment.get("MAX_EUR", "10000"),
    }
    if not config["bot_token"] or not config["chat_id"]:
        raise ServiceError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your hosting service's secret environment settings.")
    Telegram(config["bot_token"], config["chat_id"])
    if not config["chat_id"].lstrip("-").isdigit():
        raise ServiceError("TELEGRAM_CHAT_ID must be your numeric chat ID.")
    Rules.from_config(config)
    try:
        config["poll_seconds"] = int(environment.get("POLL_SECONDS", "15"))
        if not 10 <= config["poll_seconds"] <= 3600:
            raise ValueError()
    except ValueError:
        raise ServiceError("POLL_SECONDS must be between 10 and 3600.") from None
    return config


class HealthState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.last_scan = None

    def update(self, ranked):
        with self.lock:
            self.last_scan = datetime.now(timezone.utc).isoformat()

    def payload(self):
        with self.lock:
            return {"service": "binance-p2p-market-watcher", "worker_running": self.running,
                    "last_successful_scan_utc": self.last_scan}


def handler_for(state):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return
            payload = state.payload()
            body = json.dumps(payload).encode()
            self.send_response(200 if payload["worker_running"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200 if state.payload()["worker_running"] else 503)
            self.end_headers()

        def log_message(self, format, *args):
            # Avoid logging public request paths or query strings.
            pass

    return HealthHandler


def main():
    try:
        config = load_config(os.environ)
        folder = Path(os.environ.get("DATA_DIR", "data")).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        port = int(os.environ.get("PORT", "10000"))
    except ServiceError as error:
        print(str(error), flush=True)
        return 1
    except (ValueError, OSError):
        print("Check PORT and DATA_DIR configuration and write permissions.", flush=True)
        return 1
    stop = threading.Event()
    state = HealthState()
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_for(state))

    def worker():
        with state.lock:
            state.running = True
        try:
            watch(config, folder, stop, lambda text: print(text, flush=True), state.update)
        except Exception as error:
            print("Watcher stopped after a local error: " + type(error).__name__, flush=True)
        finally:
            with state.lock:
                state.running = False

    def shutdown(*args):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    thread = threading.Thread(target=worker, name="market-watcher", daemon=True)
    thread.start()
    print(f"Health endpoint listening on port {port}. Market data uses no Binance API key.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
