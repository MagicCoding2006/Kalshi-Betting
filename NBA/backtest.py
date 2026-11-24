"""Season calibration from saved out-of-sample predictions, never retrospective refits."""
import json
import math
from collections import Counter
from pathlib import Path

from .data import DATA, instant, utcnow
from .quarters import load_quarters


def buckets(outcomes, lower=0):
    result = []
    for index in range(lower, 10):
        selected = [(p, won) for p, won in outcomes if min(9, int(p * 10)) == index]
        n = len(selected)
        wins = sum(won for _, won in selected)
        average = sum(p for p, _ in selected) / n if n else None
        rate = wins / n if n else None
        interval = None
        if n:
            z = 1.96
            center = (rate + z*z/(2*n)) / (1 + z*z/n)
            half = z * math.sqrt(rate*(1-rate)/n + z*z/(4*n*n)) / (1 + z*z/n)
            interval = [max(0, center-half), min(1, center+half)]
        result.append({'lower': index/10, 'upper': (index+1)/10, 'n': n,
                       'wins': wins, 'losses': n-wins, 'mean_probability': average,
                       'expected_wins': sum(p for p, _ in selected), 'win_rate': rate,
                       'calibration_gap': rate-average if n else None,
                       'frequency_95_interval': interval, 'small_sample': n < 20})
    return result


def checkpoint_buckets(outcomes, lower=0):
    """Full-game probability bins, evaluated for leading/behind/tied at a checkpoint."""
    result = []
    for index in range(lower, 10):
        selected = [(p, margin) for p, margin in outcomes if min(9, int(p*10)) == index]
        n = len(selected)
        result.append({'lower': index/10, 'upper': (index+1)/10, 'n': n,
                       'wins': sum(m > 0 for _, m in selected), 'losses': sum(m < 0 for _, m in selected),
                       'ties': sum(m == 0 for _, m in selected),
                       'mean_probability': sum(p for p, _ in selected)/n if n else None,
                       'win_rate': sum(m > 0 for _, m in selected)/n if n else None,
                       'calibration_gap': None, 'small_sample': n < 20})
    return result


def checkpoint_report(records, quarter_map, periods, label):
    scored, picks, teams = [], [], []
    for record in records:
        scores = quarter_map.get(record['game_id'])
        if scores is None or sum(scores['home']) != record['home_score'] or sum(scores['away']) != record['away_score']:
            continue
        home, away = sum(scores['home'][:periods]), sum(scores['away'][:periods])
        margin = home-away
        pick_margin = margin if record['pick'] == record['home'] else -margin
        point = {'home_score': home, 'away_score': away, 'pick_result': 'leading' if pick_margin > 0 else 'behind' if pick_margin < 0 else 'tied'}
        record['checkpoints'][label] = point
        picks.append((record['pick_probability'], pick_margin))
        teams.extend([(record['home_probability'], margin), (record['away_probability'], -margin)])
        scored.append(record['game_id'])
    n = len(scored)
    return {'evaluated_games': n, 'missing_quarter_scores': len(records)-n,
            'leading': sum(m > 0 for _, m in picks), 'behind': sum(m < 0 for _, m in picks),
            'tied': sum(m == 0 for _, m in picks), 'lead_rate': sum(m > 0 for _, m in picks)/n if n else None,
            'pick_buckets': checkpoint_buckets(picks, 5), 'team_buckets': checkpoint_buckets(teams),
            'interpretation': 'Pregame full-game picks checked against the cumulative score at this checkpoint. Ties remain separate; lead rate includes ties in its denominator.'}


def season_backtest(directory=DATA, now=None):
    """Join frozen holdout predictions to real results for the current NBA season."""
    directory, now = Path(directory), now or utcnow()
    paths = [directory/name for name in ('dataset.json', 'model-report.json', 'holdout-predictions.json')]
    if not all(p.exists() for p in paths):
        return {'status': 'not_ready', 'reason': 'Train the model to save held-out predictions for the season check.'}
    dataset, report, saved = [json.loads(p.read_text()) for p in paths]
    if not report.get('model_id') or report['model_id'] != saved.get('model_id'):
        return {'status': 'not_ready', 'reason': 'Model and saved holdout predictions do not match. Finish training before evaluating.'}
    split = report.get('split', {})
    cutoffs = [split.get(key, {}).get('through') for key in ('fit', 'selection', 'calibration')]
    if not all(cutoffs) or not split.get('test', {}).get('from') or not split['test'].get('through'):
        return {'status': 'not_ready', 'reason': 'The historical checkpoint has no complete evaluation-cutoff metadata.'}
    cutoff = max(cutoffs)
    season = now.year + int(now.month >= 9)
    completed = [g for g in dataset['games'] if g['season'] == season and instant(g['available_at']) <= now]
    by_id = {}
    for row in saved['rows']:
        if row['game_id'] in by_id:
            raise ValueError('Duplicate saved prediction for game ' + row['game_id'])
        by_id[row['game_id']] = row
    records, excluded = [], Counter()
    for game in sorted(completed, key=lambda g: (g['date'], g['game_id'])):
        row = by_id.get(game['game_id'])
        if row is None:
            excluded['no_saved_holdout_prediction'] += 1
            continue
        p = row.get('probability')
        if isinstance(p, bool) or not isinstance(p, (float, int)) or not math.isfinite(p) or not 0 <= p <= 1:
            excluded['invalid_probability'] += 1
            continue
        home_won = int(game['home_stats']['pts'] > game['away_stats']['pts'])
        if row.get('date') != game['date'] or row.get('y') != home_won:
            excluded['result_or_date_changed'] += 1
            continue
        if game['date'] <= cutoff or not split['test']['from'] <= game['date'] <= split['test']['through']:
            excluded['outside_untouched_test_period'] += 1
            continue
        pick_home = p >= .5
        records.append({'game_id': game['game_id'], 'date': game['date'], 'home': game['home'], 'away': game['away'],
                        'home_probability': p, 'away_probability': 1-p,
                        'pick': game['home'] if pick_home else game['away'],
                        'pick_probability': p if pick_home else 1-p,
                        'winner': game['home'] if home_won else game['away'],
                        'correct': bool(home_won == pick_home), 'home_won': home_won,
                        'home_score': game['home_stats']['pts'], 'away_score': game['away_stats']['pts'], 'checkpoints': {}})
    n = len(records)
    correct = sum(r['correct'] for r in records)
    favorites = [(r['pick_probability'], r['correct']) for r in records]
    teams = [(r['home_probability'], r['home_won']) for r in records]
    teams += [(r['away_probability'], 1-r['home_won']) for r in records]
    def loss(row):
        p = max(1e-15, min(1-1e-15, row['home_probability']))
        return -math.log(p if row['home_won'] else 1-p)
    quarters, quarter_exclusions = load_quarters(directory, season)
    checkpoints = {'q1': checkpoint_report(records, quarters, 1, 'q1'),
                   'halftime': checkpoint_report(records, quarters, 2, 'halftime')}
    return {'status': 'ready' if n else 'empty', 'generated_at': now.isoformat(),
            'season': season, 'season_label': f'{season-1}–{str(season)[-2:]}',
            'model_id': report['model_id'], 'evaluation_model': 'frozen_holdout_checkpoint',
            'evaluation_mode': 'historical_replay',
            'model_cutoff': cutoff, 'dataset_generated_at': dataset['generated_at'],
            'from': records[0]['date'] if n else None, 'through': records[-1]['date'] if n else None,
            'completed_games': len(completed), 'evaluated_games': n,
            'unscored_games': len(completed)-n, 'exclusion_reasons': dict(excluded),
            'summary': {'correct_picks': correct, 'incorrect_picks': n-correct,
                        'pick_accuracy': correct/n if n else None,
                        'mean_pick_probability': sum(p for p, _ in favorites)/n if n else None,
                        'log_loss': sum(loss(r) for r in records)/n if n else None,
                        'brier': sum((r['home_probability']-r['home_won'])**2 for r in records)/n if n else None},
            'pick_buckets': buckets(favorites, lower=5), 'team_buckets': buckets(teams),
            'results': records, 'checkpoints': checkpoints, 'quarter_source_exclusions': quarter_exclusions,
            'method': 'Historical replay using saved out-of-sample predictions built from past-only pregame features; no model is fitted on these outcomes.',
            'limitations': ['Earlier frozen evaluation checkpoint; live fair prices use a later serving refit.',
                            'Model-pick view counts one outcome per game. Every-team view counts two correlated outcomes per game.',
                            'Frequency intervals describe bucket outcomes, not certainty about the next game.',
                            'Historical quote prices are unavailable; this checks prediction calibration, not trading returns.',
                            'New completed games remain unscored until a matching held-out prediction is saved.']}
