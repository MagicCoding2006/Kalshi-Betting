"""Historical quote comparisons and the position exit conditions they explain."""
import csv
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from desk.server import Engine, Handler, LocalServer, market_quote
from desk.model import normalize_book


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def book(yes_bid=.49, yes_ask=.51):
    return normalize_book({'orderbook_fp': {'yes_dollars': [[str(yes_bid), '10000']],
                                           'no_dollars': [[str(1 - yes_ask), '10000']]}})


class QuoteTests(unittest.TestCase):
    def test_midpoint_uses_both_sides_without_treating_it_as_the_ask(self):
        quote = market_quote(book(.2, .4))
        self.assertAlmostEqual(quote['market_probability'], .3)
        self.assertEqual(quote['yes_bid'], .2)
        self.assertEqual(quote['yes_ask'], .4)

    def test_missing_one_sided_and_crossed_books_have_no_midpoint(self):
        for snapshot in ({}, {'yes_bids': [{'price': .4}]}, book(.8, .2),
                         {'yes_bids': [{'price': float('nan')}], 'yes_asks': [{'price': .4}]}):
            self.assertIsNone(market_quote(snapshot)['market_probability'])


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name) / 'paper.sqlite3')
        self.engine.config.update({'drift_mode': 'manual', 'slippage_mode': 'manual'})
        now = time.time()
        self.engine.market = {'ticker': 'KXBTC15M-TEST', 'status': 'active', 'open_time': iso(now - 10),
                              'close_time': iso(now + 850), 'floor_strike': 100., 'strike_type': 'greater_or_equal'}
        self.engine.reference = {'price': 100.1, 'time': now, 'source': 'Coinbase spot proxy'}
        self.engine.book = book()
        self.engine.book_time, self.engine.volatility, self.engine.fee_supported = now, .6, True
        self.engine.compute()

    def tearDown(self):
        self.engine.pool.shutdown(wait=True)
        self.engine.close()
        self.temp.cleanup()


class ExitTests(EngineFixture):
    def test_next_update_keeps_position_when_no_exit_condition_is_met(self):
        e = self.engine
        e.open_paper()
        for _ in range(2):
            e.compute()
            e.manage_positions()
        self.assertEqual(e.positions()[0]['status'], 'open')

    def test_spread_and_fees_can_trigger_stop_on_next_check_without_price_move(self):
        e = self.engine
        e.book = book(.35, .51)
        e.open_paper()
        e.compute()
        e.manage_positions()  # Same quotes as entry; no underlying price move.
        position = e.positions()[0]
        self.assertEqual(position['status'], 'closed')
        self.assertEqual(position['close_reason'], 'Stop-loss threshold reached')
        self.assertLessEqual(position['pnl'] / position['cost'], -.2)

    def test_take_profit_uses_net_liquidation_return(self):
        e = self.engine
        e.open_paper()
        e.book = book(.70, .71)
        e.compute()
        e.manage_positions()
        self.assertEqual(e.positions()[0]['close_reason'], 'Take-profit threshold reached')

    def test_fair_value_exit_uses_the_side_held(self):
        e = self.engine
        e.reference['price'] = 99.9
        e.open_paper()
        self.assertEqual(e.positions()[0]['side'], 'no')
        e.reference['price'] = 100.01
        e.compute()
        e.manage_positions()
        self.assertEqual(e.positions()[0]['close_reason'], 'Market bid has caught up with model fair value')

    def test_cutoff_disables_model_fair_exit_without_forcing_a_close(self):
        e = self.engine
        e.open_paper()
        e.market['close_time'] = iso(time.time() + 30)
        e.reference['price'] = 99.99
        e.compute()
        e.manage_positions()
        self.assertEqual(e.positions()[0]['status'], 'open')


class HistoricalComparisonTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.http = LocalServer(('127.0.0.1', 0), Handler)
        self.http.engine = self.engine
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.http.server_address[1])

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        super().tearDown()

    def read(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return response.read()

    def test_summary_details_chart_and_export_use_decision_time_quotes(self):
        e = self.engine
        e.book = book(.2, .4)
        e.log('WAIT', 'Snapshot test')
        e.book = book(.7, .8)  # A later live book must never rewrite history.
        state = json.loads(self.read('/api/state'))
        self.assertAlmostEqual(state['decisions'][0]['market_probability'], .3)
        self.assertAlmostEqual(state['probability_history'][0]['market'], .3)
        detail = json.loads(self.read('/api/decision?id=1'))
        self.assertAlmostEqual(detail['market_probability'], .3)
        self.assertEqual(detail['yes_ask'], .4)
        exported = list(csv.DictReader(io.StringIO(self.read('/api/export.csv').decode())))
        self.assertAlmostEqual(float(exported[0]['kalshi_p_yes_midpoint']), .3)
        self.assertEqual(float(exported[0]['kalshi_yes_bid']), .2)
        self.assertEqual(float(exported[0]['kalshi_yes_ask']), .4)

    def test_legacy_logs_derive_comparison_from_saved_books_without_migration(self):
        e = self.engine
        payload = {'time': time.time(), 'ticker': e.market['ticker'], 'action': 'WAIT', 'reason': 'Legacy record',
                   'probability': .65, 'book': book(.3, .5)}
        e.db.execute('INSERT INTO decisions(time,ticker,action,payload) VALUES(?,?,?,?)',
                     (payload['time'], payload['ticker'], payload['action'], json.dumps(payload)))
        e.db.commit()
        state = json.loads(self.read('/api/state'))
        self.assertAlmostEqual(state['decisions'][0]['market_probability'], .4)
        self.assertAlmostEqual(json.loads(self.read('/api/decision?id=1'))['market_probability'], .4)
        # Reading does not mutate the stored legacy audit record.
        self.assertNotIn('market_probability', json.loads(e.db.execute('SELECT payload FROM decisions').fetchone()[0]))


if __name__ == '__main__':
    unittest.main()
