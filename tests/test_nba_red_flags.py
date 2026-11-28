"""Pregame red flags: shorthanded model side, late model drops, gaps and underdogs."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from NBA.red_flags import absences, red_flags, update_trail, load_trail

NOW = datetime(2026, 11, 22, 21, tzinfo=timezone.utc)


def player(pid, minutes, impact, status='unreported'):
    return {'id': pid, 'name': pid.upper(), 'baseline_minutes': minutes, 'projected_minutes': 0 if status == 'out' else minutes,
            'impact': impact, 'availability': status}


def rotation(*players):
    out = sum(p['baseline_minutes'] for p in players if p['availability'] == 'out')
    return {'players': list(players), 'out_minutes': out, 'injury_missing': 0}


def game(home_p=.64, okc_ask=.38, mia_ask=.63, okc_players=None, mia_players=None, quote_age=10):
    stamp = (NOW-timedelta(seconds=quote_age)).isoformat()
    healthy = [player('a%d' % i, 30-i*2, 1-i*.1) for i in range(8)]
    return {'game_id': '1', 'home': 'OKC', 'away': 'MIA', 'tipoff': (NOW+timedelta(hours=2)).isoformat(),
            'prediction': {'home_win_probability': home_p, 'away_win_probability': 1-home_p},
            'context': {'rotations': {'home': rotation(*(okc_players or healthy)), 'away': rotation(*(mia_players or healthy))}},
            'markets': [{'ticker': 'OKC', 'yes_team': 'OKC', 'quote_received_at': stamp,
                         'quotes': {'yes_ask': okc_ask, 'no_ask': mia_ask}, 'candidates': []}]}


INJURED_OKC = [player('sga', 34, 2.5, 'out'), player('jdub', 32, 1.8, 'out'), player('chet', 29, 1.6, 'out')] + \
              [player('b%d' % i, 26-i*2, .5-i*.1) for i in range(7)]


class RedFlagTests(unittest.TestCase):
    def test_model_side_missing_stars_is_a_skip(self):
        r = red_flags(game(okc_players=INJURED_OKC), NOW)
        self.assertEqual(r['verdict'], 'skip')
        self.assertEqual(r['model_team'], 'OKC')
        self.assertAlmostEqual(r['model_gap'], .26)
        top = r['flags'][0]
        self.assertEqual((top['level'], top['code'], top['team']), ('skip', 'model_side_missing_key_players', 'OKC'))
        self.assertIn('SGA', top['detail'])
        self.assertIn('no learned injury weight', top['detail'])
        self.assertEqual([p['name'] for p in r['teams']['OKC']['key_out']], ['SGA', 'JDUB', 'CHET'])
        self.assertTrue(r['teams']['OKC']['key_out'][0]['star'])

    def test_one_star_out_with_small_gap_is_caution_but_big_gap_is_skip(self):
        one = [player('sga', 34, 2.5, 'out')] + [player('b%d' % i, 28-i*2, .5-i*.1) for i in range(8)]
        r = red_flags(game(home_p=.70, okc_ask=.63, mia_ask=.39, okc_players=one), NOW)
        self.assertEqual(r['verdict'], 'caution')
        self.assertEqual(r['flags'][0]['code'], 'model_side_missing_key_players')
        self.assertIn('SGA', r['flags'][0]['detail'])
        r = red_flags(game(home_p=.70, okc_ask=.52, mia_ask=.50, okc_players=one), NOW)
        self.assertEqual(r['verdict'], 'skip')
        self.assertTrue(r['flags'][0]['title'].startswith('Major absence'))

    def test_injured_opponent_is_only_a_note(self):
        r = red_flags(game(home_p=.70, okc_ask=.62, mia_ask=.40, mia_players=INJURED_OKC), NOW)
        self.assertEqual(r['model_team'], 'OKC')
        self.assertEqual(r['verdict'], 'clear')
        self.assertEqual([f['code'] for f in r['flags']], ['opponent_missing_key_players'])

    def test_bench_absences_are_not_key(self):
        bench = [player('a%d' % i, 30-i*2, 1-i*.1) for i in range(8)] + [player('deep', 8, -.5, 'out')]
        a = absences(rotation(*bench))
        self.assertEqual(len(a['out']), 1)
        self.assertEqual(a['key_out'], [])
        r = red_flags(game(home_p=.70, okc_ask=.62, mia_ask=.40, okc_players=bench), NOW)
        self.assertEqual(r['verdict'], 'clear')

    def test_many_rotation_minutes_out_without_a_starter_is_caution(self):
        role = [player('a%d' % i, 30-i*2, 1-i*.1) for i in range(5)] + \
               [player('r1', 18, -.2, 'out'), player('r2', 16, -.3, 'out'), player('r3', 15, -.4)]
        r = red_flags(game(home_p=.70, okc_ask=.62, mia_ask=.40, okc_players=role), NOW)
        self.assertEqual(r['verdict'], 'caution')
        self.assertEqual(r['flags'][0]['code'], 'model_side_missing_key_players')

    def test_questionable_key_player_on_model_side(self):
        q = [player('star', 35, 2, 'questionable')] + [player('b%d' % i, 26-i*2, .5-i*.1) for i in range(7)]
        r = red_flags(game(home_p=.70, okc_ask=.62, mia_ask=.40, okc_players=q), NOW)
        self.assertEqual(r['verdict'], 'caution')
        self.assertIn('key_player_uncertain', [f['code'] for f in r['flags']])

    def test_late_model_drop(self):
        trail = [[(NOW-timedelta(hours=5)).isoformat(), .75], [(NOW-timedelta(hours=1)).isoformat(), .72]]
        r = red_flags(game(home_p=.70, okc_ask=.62, mia_ask=.40), NOW, trail)
        drop = next(f for f in r['flags'] if f['code'] == 'model_dropped_late')
        self.assertEqual(drop['level'], 'caution')
        self.assertIn('fell 5 pts', drop['title'])
        old = [[(NOW-timedelta(hours=20)).isoformat(), .90]]
        self.assertNotIn('model_dropped_late', [f['code'] for f in red_flags(game(home_p=.70, okc_ask=.62, mia_ask=.40), NOW, old)['flags']])

    def test_big_gap_and_underdog(self):
        r = red_flags(game(home_p=.45, okc_ask=.28, mia_ask=.74), NOW)
        self.assertEqual(r['model_team'], 'OKC')
        self.assertEqual([f['code'] for f in r['flags']], ['large_model_market_gap', 'underdog'])
        self.assertEqual(r['verdict'], 'caution')

    def test_stale_quotes_still_flag_injuries_without_a_side(self):
        r = red_flags(game(okc_players=INJURED_OKC, quote_age=200), NOW)
        self.assertIsNone(r['model_team'])
        self.assertEqual(r['verdict'], 'caution')
        self.assertEqual(r['flags'][0]['code'], 'missing_key_players')

    def test_without_an_injury_report_the_verdict_is_unchecked(self):
        g = game(home_p=.70, okc_ask=.62, mia_ask=.40)
        for side in ('home', 'away'):
            g['context']['rotations'][side]['injury_missing'] = 1
        r = red_flags(g, NOW)
        self.assertEqual(r['verdict'], 'unchecked')
        self.assertEqual(r['flags'][-1]['code'], 'injury_report_missing')
        g = game(home_p=.70, okc_ask=.62, mia_ask=.40, quote_age=500)
        self.assertEqual(red_flags(g, NOW)['verdict'], 'unchecked')
        g.pop('context')
        self.assertEqual(red_flags(g, NOW)['verdict'], 'unchecked')

    def test_trail_thins_and_drops_old_games(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [{'game_id': '1', 'tipoff': (NOW+timedelta(hours=2)).isoformat(), 'prediction': {'home_win_probability': .6}},
                    {'game_id': 'old', 'tipoff': (NOW-timedelta(days=2)).isoformat(), 'prediction': {'home_win_probability': .6}}]
            update_trail(d, rows, NOW, recordings=Path(d)/'missing.sqlite3')
            update_trail(d, rows, NOW+timedelta(seconds=30))
            rows[0]['prediction']['home_win_probability'] = .58
            update_trail(d, rows, NOW+timedelta(seconds=60))
            trail = load_trail(d)
            self.assertEqual(list(trail), ['1'])
            self.assertEqual([p for _, p in trail['1']], [.6, .58])
            self.assertEqual(json.loads((Path(d)/'forecast-trail.json').read_text()), trail)


if __name__ == '__main__':
    unittest.main()
