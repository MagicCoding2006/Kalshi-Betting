"""Gold feed correctness and cross-strategy isolation through the shared HTTP app."""
import json
import math
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from desk.server import Engine, Handler, LocalServer
from desk.model import normalize_book
from GOLD.feed import fetch, minute_candles


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def gold_payload(now, minutes=62):
    end = int(now // 60) * 60
    ticks = [{'t': stamp * 1000, 'v': 3600 * math.exp(.00003 * math.sin(stamp / 42))}
             for stamp in range(end - minutes * 60, int(now))]
    return {'live_data': {'type': 'commodity', 'details': {'asset': 'GOLD', 'timeseries': ticks}}}


class GoldFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name) / 'gold.sqlite3', 'gold')

    def tearDown(self):
        self.engine.pool.shutdown(wait=True)
        self.engine.close()
        self.temp.cleanup()

    def test_gold_uses_correct_reference_and_completed_minutes(self):
        now = time.time()
        data = gold_payload(now)
        reference = fetch(self.engine, {'event_ticker': 'KXGOLD15M-TEST'}, lambda url: data)
        self.assertEqual(reference['source'], 'Kalshi Gold reference (Pyth)')
        self.assertGreater(len(reference['candles']), 30)
        self.assertLess(reference['price'], 4000)
        self.assertTrue(all(c['time'] + 60 <= now for c in reference['candles']))
        minute = int(now // 60) * 60 - 60
        expected = next(p['v'] for p in reversed(data['live_data']['details']['timeseries']) if p['t'] // 1000 == minute + 59)
        self.assertEqual(reference['candles'][-1]['time'], minute)
        self.assertEqual(reference['candles'][-1]['close'], expected)
        self.assertLessEqual(reference['candles'][-1]['low'], expected)
        self.assertGreaterEqual(reference['candles'][-1]['high'], expected)

    def test_empty_stale_wrong_asset_and_historical_feeds_rejected(self):
        now = time.time()
        for data in [gold_payload(now - 100), {'live_data': {'type': 'crypto', 'details': {'asset': 'BTC'}}},
                     {'live_data': {'type': 'commodity', 'details': {'asset': 'GOLD', 'timeseries': []}}},
                     {**gold_payload(now), 'live_data': {**gold_payload(now)['live_data'], 'is_historical': True}}]:
            with self.assertRaises(ValueError):
                fetch(self.engine, {'event_ticker': 'KXGOLD15M-TEST'}, lambda url: data)

    def test_missing_coverage_and_partial_minutes_do_not_become_returns(self):
        minute = 1200
        complete = [{'time': minute + i, 'price': 3600 + i / 100} for i in range(60)]
        self.assertEqual(minute_candles(complete, 1260), [{'time': 1200, 'close': 3600.59, 'low': 3600.0, 'high': 3600.59}])
        self.assertEqual(minute_candles(complete, 1259), [])
        self.assertEqual(minute_candles(complete[:49], 1260), [])
        self.assertEqual(minute_candles(complete[10:], 1260), [])

    def test_tick_discovers_gold_and_never_calls_coinbase(self):
        e = self.engine
        now = time.time()
        market = {'ticker': 'KXGOLD15M-TEST', 'event_ticker': 'GOLD-EVENT', 'status': 'active',
                  'open_time': iso(now - 10), 'close_time': iso(now + 850),
                  'floor_strike': 3600., 'strike_type': 'greater_or_equal'}
        calls = []
        def public(url):
            calls.append(url)
            if '/markets?' in url:
                self.assertIn('series_ticker=KXGOLD15M', url)
                return {'markets': [market]}
            if '/live_data/' in url:
                return gold_payload(now)
            if '/orderbook?' in url:
                return {'orderbook_fp': {'yes_dollars': [['.49', '10000']], 'no_dollars': [['.49', '10000']]}}
            if '/events/' in url:
                return {'event': {}}
            if '/series/' in url:
                self.assertTrue(url.endswith('/series/KXGOLD15M'))
                return {'series': {'fee_type': 'quadratic', 'fee_multiplier': 1}}
            raise AssertionError('Unexpected URL ' + url)
        with patch('desk.server.get_json', side_effect=public):
            e.tick()
        self.assertFalse(any('coinbase' in url for url in calls))
        self.assertEqual(sum('/live_data/' in url for url in calls), 1)
        self.assertEqual(e.state()['asset']['id'], 'gold')
        self.assertGreater(e.return_count, 30)
        self.assertIsNotNone(e.decision['probability'])
        self.assertIn('Gold', e.input_status()['volatility']['source'])
        self.assertEqual(e.cash, 100)
        self.assertEqual(e.config['min_seconds'], 60)

    def test_database_cannot_be_rebound_to_another_strategy(self):
        with self.assertRaises(ValueError):
            Engine(Path(self.temp.name) / 'gold.sqlite3', 'btc')


class SharedHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engines = {key: Engine(Path(self.temp.name) / (key + '.sqlite3'), key) for key in ('btc', 'gold')}
        now = time.time()
        for key, engine in self.engines.items():
            engine.config.update({'drift_mode': 'manual', 'slippage_mode': 'manual'})
            engine.market = {'ticker': engine.spec['series'] + '-TEST', 'event_ticker': key + '-EVENT',
                             'status': 'active', 'open_time': iso(now - 10), 'close_time': iso(now + 850),
                             'floor_strike': 100., 'strike_type': 'greater_or_equal'}
            engine.reference = {'price': 100.1, 'time': now, 'source': engine.spec['source_label']}
            engine.book = normalize_book({'orderbook_fp': {'yes_dollars': [['.49', '10000']], 'no_dollars': [['.49', '10000']]}})
            engine.book_time, engine.volatility, engine.fee_supported = now, .6, True
            engine.compute()
            engine.log()
        self.http = LocalServer(('127.0.0.1', 0), Handler)
        self.http.engines = self.engines
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.http.server_address[1])

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        for e in self.engines.values():
            e.pool.shutdown(wait=True)
            e.close()
        self.temp.cleanup()

    def request(self, path, body=None):
        return urllib.request.urlopen(urllib.request.Request(self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'}), timeout=3)

    def test_both_states_exports_and_log_ids_are_scoped(self):
        for key, other in [('btc', 'gold'), ('gold', 'btc')]:
            with self.request('/api/state?asset=' + key) as response:
                state = json.load(response)
                self.assertEqual(state['asset']['id'], key)
                self.assertEqual(state['market']['ticker'], self.engines[key].spec['series'] + '-TEST')
            with self.request('/api/decision?id=1&asset=' + key) as response:
                self.assertEqual(json.load(response)['asset'], key)
            with self.request('/api/export.csv?asset=' + key) as response:
                data = response.read().decode()
                self.assertIn(key + '-decisions.csv', response.headers['Content-Disposition'])
                self.assertIn(self.engines[key].spec['series'], data)
                self.assertNotIn(self.engines[other].spec['series'], data)
        with self.request('/api/assets') as response:
            self.assertEqual({a['id'] for a in json.load(response)['assets']}, {'btc', 'gold'})
        with self.request('/api/state') as response:
            self.assertEqual(json.load(response)['asset']['id'], 'btc')

    def test_outcome_research_is_read_only_and_uses_isolated_database(self):
        balances={k:e.cash for k,e in self.engines.items()}
        with self.request('/btc-system') as response:
            self.assertIn('From BTC targets to executable trades',response.read().decode())
        with self.request('/api/btc-system') as response:
            data=json.load(response)
            self.assertEqual(data['stats']['observations'],0)
        system=self.engines['btc'].outcome_system
        self.assertTrue(str(system.directory).startswith(self.temp.name))
        self.assertIsNone(self.engines['gold']._outcome_system)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/btc-system/job',{'kind':'buy'})
        self.assertEqual(error.exception.code,400)
        req=urllib.request.Request(self.base+'/api/btc-system/job',data=b'{"kind":"train"}',headers={'Content-Type':'application/json','Origin':'https://example.com'})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req,timeout=3)
        self.assertEqual(error.exception.code,403)
        self.assertEqual(balances,{k:e.cash for k,e in self.engines.items()})

    def test_swing_target_api_validates_inputs_without_touching_paper_cash(self):
        balances={k:e.cash for k,e in self.engines.items()}
        for query in ['target=0','target=nan','target=inf','target=-1','target=100&target=101']:
            with self.assertRaises(urllib.error.HTTPError) as error:self.request('/api/btc-system/swing?'+query)
            self.assertEqual(error.exception.code,400)
        with self.request('/api/btc-system/swing?target=101') as response:
            result=json.load(response)
            self.assertEqual(result['target'],101)
            self.assertEqual(result['target_basis'],'trade_cost')
            self.assertEqual(result['settings']['objective'],'expected_log_growth')
            self.assertNotIn('profit_target',result['settings'])
        system=self.engines['btc'].outcome_system
        with patch.object(system.pool,'submit') as submit:
            with self.request('/api/btc-system/job',{'kind':'adaptive'}) as response:self.assertEqual(response.status,200)
            self.assertEqual(submit.call_args.args[1],'adaptive')
        self.assertEqual(balances,{k:e.cash for k,e in self.engines.items()})

    def test_settings_auto_buy_close_and_reset_do_not_cross_strategies(self):
        btc, gold = self.engines['btc'], self.engines['gold']
        with self.request('/api/config?asset=gold', {'kelly_fraction': .5}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(gold.config['kelly_fraction'], .5)
        self.assertEqual(btc.config['kelly_fraction'], .25)
        with self.request('/api/auto?asset=btc', {'enabled': True}):
            pass
        with self.request('/api/auto?asset=gold', {'enabled': True}):
            pass
        for key in ('btc', 'gold'):
            with self.request('/api/paper/buy?asset=' + key, {}):
                pass
        btc_cash = btc.cash
        # Both DBs contain position id=1. A Gold close must touch only Gold.
        with self.request('/api/paper/close?asset=gold', {'id': 1}):
            pass
        self.assertEqual(gold.positions()[0]['status'], 'closed')
        self.assertEqual(btc.positions()[0]['status'], 'open')
        self.assertEqual(btc.cash, btc_cash)
        with self.request('/api/paper/reset?asset=gold', {}):
            pass
        self.assertEqual(gold.cash, 100)
        self.assertEqual(gold.positions(), [])
        self.assertFalse(gold.auto)
        self.assertTrue(btc.auto)
        self.assertEqual(btc.cash, btc_cash)
        self.assertEqual(btc.positions()[0]['status'], 'open')
        self.assertEqual(btc.db.execute('SELECT COUNT(*) FROM paper_runs').fetchone()[0], 0)

    def test_unknown_or_duplicate_asset_cannot_reset_btc(self):
        for query in ('asset=wrong', 'asset=', 'asset=gold&asset=btc'):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.request('/api/paper/reset?' + query, {})
            self.assertEqual(cm.exception.code, 400)
        for engine in self.engines.values():
            self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM paper_runs').fetchone()[0], 0)

    def test_research_routes_are_separate_and_cannot_change_paper_cash(self):
        gold,btc=self.engines['gold'],self.engines['btc']
        with self.request('/api/research/settings?asset=gold',{'horizon':10,'range_filter':False}) as response:
            self.assertEqual(response.status,200)
        self.assertEqual(gold.research.settings['horizon'],10)
        self.assertEqual(btc.research.settings['horizon'],5)
        for key,engine in self.engines.items():
            engine.research.store.record(engine.book,engine.market['ticker'],time.time(),800,engine.reference,100,1)
            with self.request('/api/research/export.csv?asset='+key) as response:
                data=response.read().decode()
                self.assertIn(engine.spec['series'],data)
        with self.request('/api/research/job?asset=gold',{'kind':'markov'}) as response:
            self.assertEqual(response.status,200)
        for _ in range(100):
            if gold.research.status()['job']['status']!='running':break
            time.sleep(.01)
        with self.request('/api/research?asset=gold') as response:
            self.assertEqual(json.load(response)['reports'][0]['status'],'collecting')
        self.assertEqual(gold.cash,100)
        self.assertEqual(btc.cash,100)
        self.assertEqual(gold.positions(),[])
        self.assertEqual(btc.positions(),[])
        self.assertFalse(gold.auto)
        self.assertFalse(btc.auto)

    def test_gold_failure_does_not_block_btc_api_or_running_bot(self):
        self.engines['gold'].error = 'Gold feed unavailable'
        self.engines['btc'].auto = True
        with self.request('/api/state?asset=btc') as response:
            state = json.load(response)
            self.assertIsNone(state['error'])
            self.assertTrue(state['auto_paper'])
            self.assertEqual(state['decision']['action'], 'BUY YES')
        with self.request('/api/state?asset=gold') as response:
            self.assertEqual(json.load(response)['decision']['action'], 'WAIT')


if __name__ == '__main__':
    unittest.main()
