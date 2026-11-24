"""Full-slate visibility without turning historical or provisional values into live odds."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from NBA.board import build_board
from NBA.features import FEATURE_NAMES
from NBA.service import feature_catalog


class FakeState:
    def update(self, game):
        pass

    def features(self, fixture):
        return dict.fromkeys(FEATURE_NAMES, .5), {s: {'games': 30, 'rest': 1, 'b2b': 0, 'three_in_four': 0, 'last7': 3} for s in ('home', 'away')}


class BoardTests(unittest.TestCase):
    def test_full_season_keeps_started_games_and_previews_do_not_get_live_edges(self):
        now = datetime(2026, 11, 21, 20, tzinfo=timezone.utc)
        def fixture(key, day, hour, state='pre'):
            return {'game_id': key, 'date': day, 'tipoff': f'{day}T{hour}:00:00+00:00',
                    'season': 2027, 'home': 'BOS', 'away': 'NYK', 'neutral': False, 'state': state}
        fixtures = [fixture('past', '2026-11-20', '18', 'post'), fixture('started', '2026-11-21', '18'),
                    fixture('soon', '2026-11-21', '23'), fixture('unknown', '2026-11-22', '23'), fixture('later', '2026-12-01', '23')]
        dataset = {'schedule': fixtures, 'games': [{**fixtures[0], 'available_at': '2026-11-21T12:00:00+00:00',
                    'home_stats': {'pts': 110}, 'away_stats': {'pts': 100}}]}
        bundle = {'report': {'model_id': 'test', 'deployment': {'labels_available_through': '2026-11-21T12:00:00+00:00'},
                            'split': {s: {'through': '2026-04-14'} for s in ('fit', 'selection', 'calibration')}}}
        def blockers(f, *args):
            return ['preceding_game_result_unavailable:x'] if f['game_id'] == 'unknown' else []
        def predict(bundle, values):
            return [{'home_win_probability': .6, 'away_win_probability': .4} for _ in values]
        with tempfile.TemporaryDirectory() as folder, patch('NBA.board.state_at', return_value=FakeState()), patch('NBA.board.FeatureState', FakeState):
            path = Path(folder)
            (path/'pregame-predictions.json').write_text(json.dumps({'started': {'prediction': {'home_win_probability': .99},
                                                                    'as_of': '2026-11-21T19:00:00+00:00', 'model_id': 'test'}}))
            (path/'holdout-predictions.json').write_text(json.dumps({'model_id': 'test', 'rows': [
                {'game_id': 'past', 'date': '2026-11-20', 'probability': .65, 'y': 1}]}))
            rows, today = build_board(dataset, bundle, path, now, blockers, predict)
            self.assertEqual(len(rows), 5)
            mapped = {r['game_id']: r for r in rows}
            self.assertEqual(mapped['past']['prediction_kind'], 'historical_test')
            self.assertEqual(mapped['started']['prediction_kind'], 'pregame_replay', 'Post-tipoff archive must be rejected')
            self.assertIsNotNone(mapped['started']['pregame_prediction'])
            self.assertIsNone(mapped['started']['prediction'])
            self.assertIsNotNone(mapped['soon']['prediction'])
            for key in ('unknown', 'later'):
                self.assertIsNotNone(mapped[key]['preview_prediction'])
                self.assertIsNone(mapped[key]['prediction'])
                self.assertEqual(mapped[key]['status'], 'provisional')
            archive = json.loads((path/'pregame-predictions.json').read_text())
            self.assertIn('soon', archive)
            self.assertNotIn('later', archive)

    def test_catalog_matches_both_base_and_expanded_model_schema(self):
        from NBA.context import CONTEXT_NAMES
        for names in (FEATURE_NAMES,FEATURE_NAMES+CONTEXT_NAMES):
            with self.subTest(columns=len(names)),patch('NBA.service.model_report',return_value={'feature_names':names}):
                catalog = feature_catalog()
                flattened = [f for group in catalog['groups'] for f in group['features']]
                self.assertEqual(len(flattened),len(names))
                self.assertEqual(set(flattened),set(names))


if __name__ == '__main__':
    unittest.main()
