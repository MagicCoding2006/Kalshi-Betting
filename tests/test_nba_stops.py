"""Entry-specific stops must obey net payoffs, receipt chronology and exit depth."""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from NBA.risk import settings
from NBA.stops import adverse_moves, calculate_stop, stop_plan

NOW = datetime(2026, 11, 22, 5, tzinfo=timezone.utc)


def calculate(**updates):
    values = dict(entry=.6, quantity=100, target=.7, multiplier=1, config=settings({}), now=NOW)
    values.update(updates)
    return calculate_stop(**values)


def quote(side='yes', bid=.6, ask=.61, quantity=1000, age=0):
    return {'side': side, 'observed_at': (NOW-timedelta(seconds=age)).isoformat(), 'status': 'active',
            'book': {side+'_bids': [{'price': bid, 'quantity': quantity}],
                     side+'_asks': [{'price': ask, 'quantity': quantity}]}}


class StopTests(unittest.TestCase):
    def test_individual_entry_produces_a_cost_aware_stop_and_break_even(self):
        p = calculate()
        self.assertTrue(p['feasible'])
        self.assertEqual(p['stop_bid'], .59)
        self.assertEqual(p['break_even_bid'], .65)
        self.assertAlmostEqual(p['net_at_stop'], -5.38)
        self.assertAlmostEqual(p['net_at_target'], 5.85)
        self.assertAlmostEqual(p['two_wins_one_loss'], 6.32)
        self.assertGreaterEqual(p['net_reward_risk'], 1)
        self.assertLess(p['net_at_gap_stop'], p['net_at_stop'])

    def test_a_small_gross_scalp_can_fail_after_round_trip_costs(self):
        p = calculate(target=.65)
        self.assertGreater(p['net_at_target'], 0)
        self.assertFalse(p['feasible'])
        self.assertIsNone(p['stop_bid'])
        self.assertTrue(p['reasons'])
        self.assertFalse(calculate(target=.62)['feasible'])

    def test_risk_budget_cannot_be_met_by_claiming_a_profit_stop_above_entry(self):
        p = calculate(max_loss=5)
        self.assertFalse(p['feasible'])
        self.assertIsNone(p['stop_bid'])
        p = calculate(max_loss=6)
        self.assertTrue(p['feasible'])
        self.assertLessEqual(-p['net_at_stop'], 6)

    def test_quantity_changes_the_dollar_loss_and_actual_fees_are_respected(self):
        smaller = calculate(quantity=10)
        self.assertLess(-smaller['net_at_stop'], -calculate()['net_at_stop'])
        actual = calculate(entry_kind='filled', entry_fees=0)
        self.assertEqual(actual['entry_cost'], 60)
        self.assertEqual(actual['entry_fee_basis'], 'entered')
        self.assertLess(actual['break_even_bid'], calculate()['break_even_bid'])

    def test_a_wide_spread_rejects_an_artificially_tight_stop(self):
        p = calculate(quote=quote(bid=.56, ask=.6))
        self.assertFalse(p['feasible'])
        self.assertAlmostEqual(p['minimum_stop_distance'], .04)

    def test_a_tight_stop_cannot_override_the_global_full_loss_budget(self):
        p=calculate(config=settings({'max_loss_fraction':.02,'equity':1000}))
        self.assertFalse(p['feasible']);self.assertEqual(p['portfolio_loss_limit'],20)
        self.assertTrue(any('portfolio loss ceiling' in s for s in p['reasons']))

    def test_current_bid_already_at_stop_cannot_start_a_protected_entry(self):
        p = calculate(quote=quote(bid=.59, ask=.6))
        self.assertEqual(p['stop_bid'], .59)
        self.assertFalse(p['feasible'])
        self.assertTrue(any('already at or below' in s for s in p['reasons']))

    def test_a_good_top_bid_does_not_hide_a_bad_full_quantity_exit(self):
        q = quote(ask=.6)
        q['book']['yes_bids'] = [{'price': .6, 'quantity': 5}, {'price': .4, 'quantity': 100}]
        p = calculate(quote=q)
        self.assertFalse(p['feasible'])
        self.assertLess(p['current_bid_vwap'], p['stop_bid'])
        self.assertTrue(any('through current depth' in s for s in p['reasons']))

    def test_limited_exit_and_entry_depth_are_both_reported(self):
        p = calculate(quote=quote(bid=.6, ask=.61, quantity=50))
        self.assertFalse(p['feasible'])
        self.assertTrue(any('Full exit liquidity' in s for s in p['reasons']))
        self.assertTrue(any('not currently offered' in s for s in p['reasons']))

    def test_a_good_average_bid_does_not_hide_missing_depth_above_the_stop(self):
        q = quote(ask=.6)
        q['book']['yes_bids'] = [{'price': .6, 'quantity': 90}, {'price': .58, 'quantity': 10}]
        p = calculate(quote=q)
        self.assertGreater(p['current_bid_vwap'], p['stop_bid'])
        self.assertEqual(p['depth_at_stop'], 90)
        self.assertFalse(p['feasible'])
        self.assertTrue(any('Deeper fills' in s for s in p['reasons']))

    def test_stale_books_do_not_supply_current_liquidity_or_trigger_rejections(self):
        p = calculate(quote=quote(bid=.1, ask=.9, age=31))
        self.assertFalse(p['quote_fresh'])
        self.assertIsNone(p['spread'])
        self.assertIsNone(p['current_exit_pnl'])
        self.assertTrue(p['feasible'], 'The payoff calculation remains available with an explicit missing-book warning')
        self.assertTrue(any('No fresh' in s for s in p['warnings']))

    def test_no_contracts_use_their_own_bid_ask_and_full_depth(self):
        p = calculate(quote=quote(side='no', bid=.59, ask=.6))
        self.assertEqual(p['current_bid'], .59)
        self.assertFalse(p['feasible'])

    def test_recent_observed_adverse_change_can_make_the_stop_too_tight(self):
        p = calculate(history={'adverse_move_p95': .03, 'adjacent_pairs': 40, 'adverse_changes': 20})
        self.assertAlmostEqual(p['minimum_stop_distance'], .03)
        self.assertFalse(p['feasible'])

    def test_history_ignores_future_bad_quality_and_long_polling_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory)/'recordings.sqlite3')
            db.execute('CREATE TABLE books(id INTEGER PRIMARY KEY,ticker TEXT,observed_at TEXT,quality TEXT,yes_bid REAL,no_bid REAL)')
            rows = []
            for i in range(24):
                # Twelve valid 1c declines, alternating with rises.
                rows.append(('NBA-1', (NOW-timedelta(seconds=300-i*10)).isoformat(), 'ok', .6 if i%2 == 0 else .59, .4))
            rows.extend([('NBA-1', (NOW-timedelta(seconds=30)).isoformat(), 'ok', .1, .9),
                         ('NBA-1', (NOW-timedelta(seconds=20)).isoformat(), 'error', .01, .99),
                         ('NBA-1', (NOW+timedelta(seconds=10)).isoformat(), 'ok', .001, .999)])
            db.executemany('INSERT INTO books(ticker,observed_at,quality,yes_bid,no_bid) VALUES(?,?,?,?,?)', rows)
            db.commit(); db.close()
            p = adverse_moves(directory, 'NBA-1', 'yes', NOW)
            self.assertEqual(p['adverse_changes'], 12)
            self.assertAlmostEqual(p['adverse_move_p95'], .01)
            self.assertIsNone(adverse_moves(directory, 'NBA-1', 'no', NOW)['adverse_move_p95'])

    def test_manual_payoff_route_does_not_invent_model_evidence_or_known_fees(self):
        with tempfile.TemporaryDirectory() as d:
            p = stop_plan({'game_id': '1', 'entry_price': .6, 'quantity': 100}, d, NOW)
        self.assertFalse(p['entry_edge_evaluated'])
        self.assertFalse(p['stop_hit_probability_fitted'])
        self.assertTrue(p['fee_multiplier_assumed'])
        self.assertTrue(any('multiplier 1 is assumed' in s for s in p['warnings']))

    def test_invalid_values_and_mismatched_contracts_are_rejected(self):
        body = {'game_id': '1', 'entry_price': .6, 'quantity': 100}
        for update in [{'entry_price': True}, {'quantity': 1.5}, {'quantity': True}, {'target_bid': .5},
                       {'entry_fees': -1}, {'max_loss': 0}, {'min_reward_risk': float('nan')},
                       {'side': 'other'}, {'entry_kind': 'other'}, {'game_id': None}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                stop_plan({**body, **update}, now=NOW)
        with patch('NBA.stops.load_quotes', return_value=({}, {'NBA-1': {'game_id': 'other'}})):
            with self.assertRaises(ValueError):
                stop_plan({**body, 'ticker': 'NBA-1'}, now=NOW)

    def test_stop_http_calculation_never_selects_a_trading_engine(self):
        from desk.server import Handler, LocalServer
        server = LocalServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            with patch('NBA.stops.stop_plan', return_value={'feasible': True}) as calculate_mock, patch.object(Handler, 'select_engine', side_effect=AssertionError('Trading engine opened')):
                thread.start()
                body = {'game_id': '1', 'entry_price': .6, 'quantity': 100}
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/nba/stop-plan', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(req, timeout=3) as response:
                    self.assertTrue(json.load(response)['feasible'])
                calculate_mock.assert_called_once_with(body)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
