"""Season buckets must use only valid, untouched pregame predictions."""
import json
import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from NBA.backtest import buckets, season_backtest
from NBA.quarters import period_scores


class SeasonBacktestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.now = datetime(2026, 11, 21, 20, tzinfo=timezone.utc)
        self.games, self.rows = [], []
        probabilities = [.55, .65, .75, .85, .95, .2]
        outcomes = [1, 0, 1, 1, 0, 0]
        for i, (p, won) in enumerate(zip(probabilities, outcomes)):
            date = (datetime(2026, 10, 20, tzinfo=timezone.utc)+timedelta(days=i)).date().isoformat()
            self.games.append({'game_id': str(i), 'date': date, 'season': 2027, 'home': 'BOS', 'away': 'NYK',
                               'available_at': date+'T23:59:00+00:00',
                               'home_stats': {'pts': 110 if won else 100}, 'away_stats': {'pts': 100 if won else 110}})
            self.rows.append({'game_id': str(i), 'date': date, 'y': won, 'probability': p})
        self.report = {'model_id': 'test', 'split': {'fit': {'through': '2024-04-14'},
                       'selection': {'through': '2025-04-14'}, 'calibration': {'through': '2026-04-14'},
                       'test': {'from': '2026-10-20', 'through': '2026-11-20'}}}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self, identifier='test'):
        (self.path/'dataset.json').write_text(json.dumps({'generated_at': self.now.isoformat(), 'games': self.games}))
        (self.path/'model-report.json').write_text(json.dumps(self.report))
        (self.path/'holdout-predictions.json').write_text(json.dumps({'model_id': identifier, 'rows': self.rows}))

    def test_favorite_view_counts_games_once_and_team_view_counts_both_sides(self):
        r = season_backtest(self.path, self.now)
        self.assertEqual((r['evaluated_games'], r['summary']['correct_picks']), (6, 4))
        self.assertEqual(sum(b['n'] for b in r['pick_buckets']), 6)
        self.assertEqual(sum(b['n'] for b in r['team_buckets']), 12)
        self.assertEqual(sum(b['wins'] for b in r['team_buckets']), 6)
        self.assertEqual(r['results'][-1]['pick'], 'NYK')
        self.assertAlmostEqual(r['results'][-1]['pick_probability'], .8)
        self.assertEqual(r['pick_buckets'][0]['wins'], 1)

    def test_bucket_boundaries_and_empty_bins_are_explicit(self):
        b = buckets([(.5, 1), (.6, 0), (1., 1)], 5)
        self.assertEqual([x['n'] for x in b], [1, 1, 0, 0, 1])
        self.assertIsNone(b[2]['win_rate'])
        self.assertAlmostEqual(b[1]['calibration_gap'], -.6)
        self.assertTrue(all(0 <= x <= 1 for x in b[0]['frequency_95_interval']))

    def test_future_unknown_and_changed_results_are_not_scored(self):
        self.games[0]['available_at'] = '2026-12-01T12:00:00+00:00'
        self.rows[1]['y'] = 1
        self.rows.pop(2)
        self.save()
        r = season_backtest(self.path, self.now)
        self.assertEqual(r['completed_games'], 5)
        self.assertEqual(r['evaluated_games'], 3)
        self.assertEqual(r['unscored_games'], 2)
        self.assertEqual(r['exclusion_reasons']['result_or_date_changed'], 1)
        self.assertEqual(r['exclusion_reasons']['no_saved_holdout_prediction'], 1)

    def test_training_or_calibration_labels_cannot_be_counted_as_test_results(self):
        self.report['split']['calibration']['through'] = self.games[1]['date']
        self.save()
        r = season_backtest(self.path, self.now)
        self.assertEqual(r['evaluated_games'], 4)
        self.assertEqual(r['exclusion_reasons']['outside_untouched_test_period'], 2)

    def test_mismatched_model_and_duplicate_predictions_fail_closed(self):
        self.save(identifier='other')
        self.assertEqual(season_backtest(self.path, self.now)['status'], 'not_ready')
        self.rows.append(self.rows[0]); self.save()
        with self.assertRaisesRegex(ValueError, 'Duplicate saved prediction'):
            season_backtest(self.path, self.now)

    def test_halftime_is_cumulative_and_ties_are_neither_leads_nor_losses(self):
        raw = self.path/'raw'; raw.mkdir()
        fields = ['game_id', 'season_type', 'status_type_state', 'home_linescores', 'away_linescores', 'home_score', 'away_score']
        def scores(values):
            return '['+'\n '.join(str({'value': value, 'period': i+1}) for i, value in enumerate(values))+']'
        with (raw/'nba_schedule_2027.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
            writer.writerow({'game_id': '0', 'season_type': '2', 'status_type_state': 'post', 'home_score': 110, 'away_score': 100,
                             'home_linescores': scores([20, 30, 30, 30]), 'away_linescores': scores([30, 20, 25, 25])})
        report = season_backtest(self.path, self.now)
        self.assertEqual(report['checkpoints']['q1']['behind'], 1)
        half = report['checkpoints']['halftime']
        self.assertEqual((half['leading'], half['behind'], half['tied']), (0, 0, 1))
        self.assertEqual(half['lead_rate'], 0)
        self.assertEqual(half['missing_quarter_scores'], 5)
        self.assertEqual(report['results'][0]['checkpoints']['halftime']['home_score'], 50)

    def test_period_parser_rejects_missing_duplicate_and_executable_values(self):
        with self.assertRaises(ValueError):
            period_scores("[{'period':1, 'value':20}]")
        with self.assertRaises(ValueError):
            period_scores([{'period':1, 'value':20}]*4)
        with self.assertRaises((ValueError, SyntaxError)):
            period_scores("[__import__('os').system('false')]")


if __name__ == '__main__':
    unittest.main()
