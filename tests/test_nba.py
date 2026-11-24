"""NBA data/chronology, execution costs and API freshness invariants."""
import copy
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from NBA.data import BOX_FIELDS, atomic_json, games_from_rows
from NBA.features import FEATURE_NAMES, metrics, state_at, training_rows
from NBA.markets import compare, match_market, fixture_blockers
from NBA.service import public_snapshot
from desk.server import Handler, LocalServer


def fixture(index=0, **update):
    day = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    return {'game_id': str(index), 'date': day.date().isoformat(), 'tipoff': day.replace(hour=23).isoformat(),
            'season': 2025, 'home': 'BOS', 'away': 'NYK', 'neutral': False, 'state': 'post', **update}


def game(index=0):
    own = dict(pts=113., fgm=40., fga=85., fg3m=13., fg3a=35., ftm=20., fta=25., oreb=10., dreb=33., tov=13.)
    opp = dict(pts=106., fgm=38., fga=88., fg3m=10., fg3a=30., ftm=20., fta=25., oreb=11., dreb=30., tov=15.)
    f = fixture(index)
    available = datetime.fromisoformat(f['date']).replace(hour=12, tzinfo=timezone.utc) + timedelta(days=1)
    return {**f, 'available_at': available.isoformat(), 'home_stats': own, 'away_stats': opp}


class NBADataTests(unittest.TestCase):
    def test_possession_efficiency_and_four_factors(self):
        g = game()
        m = metrics(g['home_stats'], g['away_stats'])
        self.assertAlmostEqual(m['possessions'], (85 + 11 - 10 + 13 + 88 + 11 - 11 + 15) / 2)
        self.assertAlmostEqual(m['efg'], 46.5 / 85)
        self.assertAlmostEqual(m['orb_rate'], .25)
        self.assertAlmostEqual(m['ortg'], 11300 / m['possessions'])

    def test_future_and_own_final_scores_cannot_change_pregame_features(self):
        games = [game(i * 2) for i in range(24)]
        original = training_rows(games)
        altered = copy.deepcopy(games)
        altered[15]['home_stats']['pts'] = 40
        altered[-1]['away_stats']['pts'] = 180
        changed = training_rows(altered)
        self.assertEqual(original[5]['game_id'], games[15]['game_id'])
        self.assertEqual(original[5]['features'], changed[5]['features'])
        self.assertNotEqual(original[5]['y'], changed[5]['y'])
        self.assertEqual(original[0]['features'], changed[0]['features'])
        self.assertEqual(set(original[0]['features']), set(FEATURE_NAMES))

    def test_box_is_unavailable_until_declared_availability_time(self):
        g = game()
        at = datetime.fromisoformat(g['available_at'])
        self.assertEqual(len(state_at([g], at - timedelta(seconds=1)).history['BOS']), 0)
        self.assertEqual(len(state_at([g], at).history['BOS']), 1)

    def test_incomplete_or_corrupted_box_pair_is_excluded(self):
        g = game()
        rows = []
        for side, opponent, identifier in [('home', 'away', '1'), ('away', 'home', '2')]:
            stats = g[side + '_stats']
            row = {column: str(stats[key]) for key, column in BOX_FIELDS.items()}
            row.update(game_id='0', season_type='2', team_abbreviation=g[side], team_home_away=side,
                       team_id=identifier, opponent_team_id='2' if identifier == '1' else '1', game_date=g['date'],
                       opponent_team_score=str(g[opponent + '_stats']['pts']))
            rows.append(row)
        good, excluded = games_from_rows(rows, {'0': fixture()})
        self.assertEqual((len(good), len(excluded)), (1, 0))
        self.assertEqual(len(games_from_rows(rows[:1], {'0': fixture()})[0]), 0)
        rows[0]['team_score'] = '130'
        self.assertEqual(len(games_from_rows(rows, {'0': fixture()})[0]), 0)
        conflict = copy.deepcopy(rows[0]); conflict['team_score'] = '131'
        with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
            games_from_rows(rows + [conflict], {'0': fixture()})

    def test_unavailable_preceding_fixture_blocks_future_game(self):
        now = datetime(2025, 1, 10, 15, tzinfo=timezone.utc)
        dataset = {'generated_at': now.isoformat(), 'games': [], 'schedule': [fixture(9, state='pre')]}
        model = {'report': {'created_at': now.isoformat()}}
        profiles = {'home': {'games': 20}, 'away': {'games': 20}}
        reasons = fixture_blockers(fixture(10, state='pre'), dataset, model, profiles, now)
        self.assertTrue(any('preceding_game' in r for r in reasons))

    def test_chronological_blocks_purge_late_boundary_labels(self):
        from NBA.model import partition
        rows = []
        start = datetime(2010, 1, 1, tzinfo=timezone.utc)
        for i in range(2400):
            day = start + timedelta(days=i)
            rows.append({'date': day.date().isoformat(), 'tipoff': day.replace(hour=20).isoformat(),
                         'label_available_at': (day + timedelta(days=1)).replace(hour=12).isoformat()})
        rows[1439]['label_available_at'] = rows[1441]['tipoff']
        fit, selection, calibration, test = partition(rows)
        self.assertEqual(len(fit), 1439)
        self.assertLess(max(r['label_available_at'] for r in fit), min(r['tipoff'] for r in selection))
        self.assertLess(max(r['date'] for r in selection), min(r['date'] for r in calibration))
        self.assertLess(max(r['date'] for r in calibration), min(r['date'] for r in test))


class NBAMarketTests(unittest.TestCase):
    def setUp(self):
        self.book = {'orderbook_fp': {'yes_dollars': [['0.40', '20']], 'no_dollars': [['0.50', '4'], ['0.45', '20']]}}
        self.series = {'fee_type': 'quadratic_with_maker_fees', 'fee_multiplier': 1}

    def test_actual_depth_and_fees_can_erase_apparent_midpoint_edge(self):
        yes, no = compare(.58, self.book, self.series, quantity=10)
        self.assertAlmostEqual(yes['vwap'], .53)
        self.assertAlmostEqual(yes['fee_upper_estimate'], .18)
        self.assertAlmostEqual(yes['net_edge'], .58 - .53 - .018 - .005)
        self.assertAlmostEqual(yes['expected_profit_dollars'], 10 * yes['net_edge'])
        self.assertEqual(yes['status'], 'no_edge')
        self.assertAlmostEqual(no['probability'], .42)

    def test_missing_depth_unknown_fees_and_crossed_book_fail_closed(self):
        self.assertEqual(compare(.8, self.book, self.series, quantity=100)[0]['status'], 'WAIT')
        self.assertIsNone(compare(.8, self.book, {'fee_type': 'unknown'})[0]['net_edge'])
        crossed = {'orderbook_fp': {'yes_dollars': [['0.8', '20']], 'no_dollars': [['0.8', '20']]}}
        self.assertIn('crossed_book', compare(.8, crossed, self.series)[0]['blockers'])

    def test_market_join_requires_both_teams_date_and_yes_suffix(self):
        f = fixture(0, state='pre')
        market = {'ticker': 'KXNBAGAME-25JAN01NYBOS-BOS', 'event_ticker': 'KXNBAGAME-25JAN01NYBOS',
                  'market_type': 'binary', 'status': 'active'}
        self.assertEqual(match_market(market, [f]), (f, 'BOS'))
        self.assertIsNone(match_market({**market, 'ticker': market['event_ticker'] + '-LAL'}, [f]))
        self.assertIsNone(match_market(market, [fixture(1)]))
        self.assertIsNone(match_market(market, [fixture(0, away='LAL')]))
        self.assertIsNone(match_market(market, [f, {**f, 'game_id': 'duplicate'}]))


class NBAServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 21, 17, tzinfo=timezone.utc)
        atomic_json(self.directory / 'model-report.json', {'model_id': 'test'})
        self.payload = {'generated_at': self.now.isoformat(), 'model_id': 'test',
                        'quote_max_age_seconds': 90, 'estimate_max_age_seconds': 900,
                        'games': [{'tipoff': (self.now + timedelta(hours=2)).isoformat(), 'prediction': {'home_win_probability': .6},
                                   'status': 'research_estimate', 'blockers': [], 'markets': [
                                       {'quote_received_at': self.now.isoformat(), 'candidates': [
                                           {'status': 'research_edge', 'net_edge': .05, 'expected_profit_dollars': .5,
                                            'probability': .6, 'fair_price_cents': 60, 'blockers': []}]}]}]}
        atomic_json(self.directory / 'fair-prices.json', self.payload)

    def tearDown(self):
        self.temp.cleanup()

    def test_quote_expiry_removes_ev_and_game_start_removes_pregame_price(self):
        result = public_snapshot(self.directory, self.now + timedelta(seconds=100))
        self.assertIsNotNone(result['games'][0]['prediction'])
        self.assertIsNone(result['games'][0]['markets'][0]['candidates'][0]['net_edge'])
        result = public_snapshot(self.directory, self.now + timedelta(hours=3))
        self.assertIsNone(result['games'][0]['prediction'])
        self.assertIsNone(result['games'][0]['markets'][0]['candidates'][0]['fair_price_cents'])
        self.assertIn('game_started', result['games'][0]['blockers'])
        self.assertEqual(json.loads((self.directory / 'fair-prices.json').read_text()), self.payload)

    def test_new_model_invalidates_previously_scanned_prices(self):
        atomic_json(self.directory / 'model-report.json', {'model_id': 'new'})
        result = public_snapshot(self.directory, self.now)
        self.assertIsNone(result['games'][0]['prediction'])
        self.assertIn('model_changed_or_unavailable', result['games'][0]['blockers'])

    def test_read_only_http_route_works_without_portfolio_engines(self):
        server = LocalServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with patch('NBA.service.public_snapshot', return_value={'status': 'research_only', 'games': []}):
            thread.start()
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}/api/nba/fair-prices', timeout=3) as response:
                    self.assertEqual(json.load(response)['status'], 'research_only')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
