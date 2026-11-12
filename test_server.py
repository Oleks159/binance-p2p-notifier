from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
import unittest

from notifier import ServiceError
from server import HealthState, handler_for, load_config


class ServerTests(unittest.TestCase):
    def test_environment_settings_and_validation(self):
        sample = {"TELEGRAM_BOT_TOKEN": "123:fake-token", "TELEGRAM_CHAT_ID": "456"}
        result = load_config(sample)
        self.assertEqual(result["poll_seconds"], 15)
        self.assertEqual(result["min_eur"], "500")
        self.assertEqual(result["max_eur"], "10000")
        for changes in ({"TELEGRAM_BOT_TOKEN": ""}, {"TELEGRAM_CHAT_ID": "@my_bot"},
                        {"POLL_SECONDS": "0"}, {"MAX_EUR": "10"}):
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                load_config({**sample, **changes})

    def test_health_endpoint_reports_worker_and_scan_without_secrets(self):
        state = HealthState()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertFalse(json.loads(response.read())["worker_running"])
            state.running = True
            state.update({})
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
            self.assertIsNotNone(body["last_successful_scan_utc"])
            self.assertEqual(set(body), {"service", "worker_running", "last_successful_scan_utc"})
            connection.request("GET", "/settings.dat")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
