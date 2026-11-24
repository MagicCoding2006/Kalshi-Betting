"""Launching twice must not touch portfolios or start a second engine."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from desk.server import LocalServer

ROOT = Path(__file__).resolve().parents[1]


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = self.server.responses.get(self.path, {})
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = LocalServer(('127.0.0.1', 0), StubHandler)
        self.server.responses = {}
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.btc = Path(self.temp.name) / 'btc' / 'paper.sqlite3'
        self.gold = Path(self.temp.name) / 'gold' / 'paper.sqlite3'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def launch(self, launcher='server.py'):
        result = subprocess.run([sys.executable, str(ROOT / launcher), '--port', str(self.port),
                                 '--db', str(self.btc), '--gold-db', str(self.gold)],
                                cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertFalse(self.btc.parent.exists(), 'Duplicate start must not open or initialize BTC storage')
        self.assertFalse(self.gold.parent.exists(), 'Duplicate start must not open or initialize Gold storage')
        self.assertNotIn('Traceback', result.stderr)
        return result

    def test_existing_current_desk_prints_link_and_exits_successfully(self):
        self.server.responses['/api/health'] = {'app': 'signal-desk', 'running': True, 'paper_only': True}
        result = self.launch()
        self.assertEqual(result.returncode, 0)
        self.assertIn('already running', result.stdout)
        self.assertIn(f'http://127.0.0.1:{self.port}', result.stdout)

    def test_legacy_running_desk_and_btc_launcher_are_recognized(self):
        self.server.responses = {'/api/health': {'running': True, 'paper_only': True},
                                 '/api/assets': {'assets': [{'id': 'btc'}, {'id': 'gold'}]}}
        result = self.launch('BTC/server.py')
        self.assertEqual(result.returncode, 0)
        self.assertIn('already running', result.stdout)

    def test_unrelated_service_reports_conflict_without_touching_portfolios(self):
        self.server.responses['/api/health'] = {'status': 'healthy'}
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn('another program', result.stderr)
        self.assertIn(str(self.port), result.stderr)

    def test_similar_health_without_both_strategies_is_not_our_desk(self):
        self.server.responses = {'/api/health': {'running': True, 'paper_only': True},
                                 '/api/assets': {'assets': [{'id': 'unrelated'}]}}
        self.assertEqual(self.launch().returncode, 1)


if __name__ == '__main__':
    unittest.main()
