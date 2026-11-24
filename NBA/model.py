"""Chronological model selection, disjoint sigmoid calibration and held-out scoring."""
import hashlib
import json
import os
import pickle
import tempfile
from collections import Counter
from pathlib import Path

from .data import DATA, SCHEMA, atomic_json, instant, utcnow
from .features import FEATURE_NAMES, PRIORS, training_rows


def dependencies():
    try:
        import numpy as np
        import sklearn
    except ImportError as exc:
        raise ValueError('NBA ML requires: python3 -m pip install -r NBA/requirements.txt') from exc
    return np, sklearn


def matrix(rows,feature_names=None):
    np, _ = dependencies()
    names=feature_names or FEATURE_NAMES
    return np.array([[r['features'][f] for f in names] for r in rows], dtype=float)


def logit(probabilities):
    np, _ = dependencies()
    p = np.clip(probabilities, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p)).reshape(-1, 1)


def fit_pair(rows):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    x, y = matrix(rows), [r['y'] for r in rows]
    if len(set(y)) != 2:
        raise ValueError('Training block requires wins and losses')
    logistic = make_pipeline(StandardScaler(), LogisticRegression(C=.1, max_iter=2000))
    # No shuffled internal early-stopping holdout.
    boosting = HistGradientBoostingClassifier(max_iter=150, max_leaf_nodes=7, min_samples_leaf=50,
                                              learning_rate=.04, l2_regularization=10,
                                              early_stopping=False, random_state=42)
    return logistic.fit(x, y), boosting.fit(x, y)


def raw_probability(pair, weight, rows):
    x = matrix(rows)
    return weight * pair[0].predict_proba(x)[:, 1] + (1 - weight) * pair[1].predict_proba(x)[:, 1]


def fit_calibrator(pair, weight, rows):
    from sklearn.linear_model import LogisticRegression
    y = [r['y'] for r in rows]
    if len(set(y)) != 2:
        raise ValueError('Calibration block requires wins and losses')
    return LogisticRegression(C=1., max_iter=2000).fit(logit(raw_probability(pair, weight, rows)), y)


def calibrated(pair, weight, calibrator, rows):
    return calibrator.predict_proba(logit(raw_probability(pair, weight, rows)))[:, 1]


def scores(y, p):
    from sklearn.metrics import brier_score_loss, log_loss
    np, _ = dependencies()
    y, p = np.array(y), np.asarray(p)
    reliability = []
    for index in range(10):
        selected = (p >= index / 10) & (p < (index + 1) / 10 if index < 9 else p <= 1)
        n = int(selected.sum())
        if n:
            mean, rate = float(p[selected].mean()), float(y[selected].mean())
            # Wilson interval describes outcome frequency within a bin; it is not
            # a confidence interval for an individual game's fair probability.
            z = 1.96
            center = (rate + z*z/(2*n)) / (1 + z*z/n)
            half = z * ((rate*(1-rate)/n + z*z/(4*n*n)) ** .5) / (1 + z*z/n)
            reliability.append({'bin': index, 'n': n, 'mean_probability': mean, 'win_rate': rate,
                                'frequency_95_interval': [center - half, center + half]})
    return {'n': len(y), 'log_loss': float(log_loss(y, p, labels=[0, 1])),
            'brier': float(brier_score_loss(y, p)), 'accuracy': float(((p >= .5) == y).mean()),
            'ece': sum(b['n'] * abs(b['mean_probability'] - b['win_rate']) for b in reliability) / len(y),
            'reliability': reliability}


def block_comparison(rows, p, reference):
    """Paired log-loss improvement, resampling whole calendar days, fixed seed."""
    np, _ = dependencies()
    y = np.array([r['y'] for r in rows])
    def losses(pred):
        q = np.clip(pred, 1e-6, 1 - 1e-6)
        return -(y * np.log(q) + (1 - y) * np.log(1 - q))
    improvement = losses(reference) - losses(p)
    days = sorted({r['date'] for r in rows})
    sums = np.array([sum(improvement[i] for i, r in enumerate(rows) if r['date'] == d) for d in days])
    counts = np.array([sum(r['date'] == d for r in rows) for d in days])
    rng = np.random.default_rng(42)
    sampled = rng.integers(0, len(days), size=(500, len(days)))
    draws = sums[sampled].sum(axis=1) / counts[sampled].sum(axis=1)
    return {'mean_log_loss_improvement': float(improvement.mean()),
            'day_block_bootstrap_95_interval': [float(v) for v in np.quantile(draws, [.025, .975])],
            'caveat': 'Day blocks handle same-day dependence; cross-day/team dependence can remain.'}


def partition(rows):
    days = sorted({r['date'] for r in rows})
    boundaries = [days[int(len(days) * f)] for f in (.60, .75, .85)]
    blocks = [[], [], [], []]
    for row in rows:
        blocks[sum(row['date'] >= boundary for boundary in boundaries)].append(row)
    # At a boundary, purge any last-day label that would not yet have been
    # published under the availability convention at the next block's first tip.
    for i in range(3):
        cutoff = min(instant(r['tipoff']) for r in blocks[i + 1]) if blocks[i + 1] else None
        if cutoff:
            blocks[i] = [r for r in blocks[i] if instant(r['label_available_at']) < cutoff]
    if len(rows) < 2000 or min(map(len, blocks)) < 200:
        raise ValueError('Need at least 2,000 usable historical games and 200 in every chronological block')
    return blocks


def span(rows):
    return {'games': len(rows), 'from': min(r['date'] for r in rows), 'through': max(r['date'] for r in rows)}


def train(dataset, directory=DATA):
    np, sklearn = dependencies()
    from sklearn.metrics import log_loss
    directory = Path(directory)
    # A partially downloaded season must not contribute future/unavailable labels.
    now = utcnow()
    games = [g for g in dataset['games'] if instant(g['available_at']) <= now]
    rows = training_rows(games)
    fit, validation, calibration, test = partition(rows)
    pair = fit_pair(fit)
    candidates = [{'logistic_weight': w, 'validation_log_loss': float(log_loss(
        [r['y'] for r in validation], raw_probability(pair, w, validation), labels=[0, 1]))}
        for w in (1., .75, .5, .25, 0.)]
    weight = min(candidates, key=lambda c: c['validation_log_loss'])['logistic_weight']
    calibrator = fit_calibrator(pair, weight, calibration)
    p = calibrated(pair, weight, calibrator, test)
    y = [r['y'] for r in test]
    constant = np.full(len(test), np.mean([r['y'] for r in fit]))
    elo = np.array([r['elo_probability'] for r in test])
    report = {'schema': SCHEMA, 'created_at': now.isoformat(), 'dataset_fingerprint': dataset['fingerprint'],
              'source': 'Real SportsDataverse / ESPN final regular-season team box scores',
              'target': 'Home team full-game win, including overtime', 'feature_names': FEATURE_NAMES,
              'feature_priors': PRIORS, 'availability_policy': dataset['availability_policy'],
              'split': dict(zip(('fit', 'selection', 'calibration', 'test'), map(span, (fit, validation, calibration, test)))),
              'selection': candidates, 'logistic_weight': weight,
              'held_out': {'model': scores(y, p), 'constant_home_rate': scores(y, constant), 'elo': scores(y, elo),
                           'vs_constant': block_comparison(test, p, constant), 'vs_elo': block_comparison(test, p, elo)},
              'excluded_source_games': len(dataset['excluded']), 'usable_feature_rows': len(rows),
              'source_games_per_season': dict(sorted(Counter(g['season'] for g in games).items())),
              'exclusion_reasons': dict(Counter(r['reason'] for r in dataset['excluded'])),
              'excluded_schedule_entries': len(dataset.get('excluded_schedule', [])),
              'boundary_purged_rows': len(rows) - sum(map(len, (fit, validation, calibration, test))),
              'versions': {'numpy': np.__version__, 'sklearn': sklearn.__version__},
              'market_backtest': None,
              'limitations': ['No timestamped historical market quotes: market edge and trading P&L are unmeasured.',
                              'No injury, projected minutes, lineup, travel or tracking features in v1.',
                              'Historical stat publication times are assumed; revised final box scores can differ from original releases.',
                              'Team histories update with past test outcomes; fitted weights and calibrator stay frozen throughout test.',
                              'Single chronological holdout; walk-forward seasons and prospective paper evaluation remain necessary.',
                              'Deployment is a later refit with no new untouched performance estimate.']}
    # Fit the serving model on recent available labels AFTER the frozen evaluation.
    # Its calibration block remains disjoint from its estimator training block.
    deploy_cut = sorted({r['date'] for r in rows})[-60]
    deploy_cal = [r for r in rows if r['date'] >= deploy_cut]
    first_tip = min(instant(r['tipoff']) for r in deploy_cal)
    deploy_fit = [r for r in rows if r['date'] < deploy_cut and instant(r['label_available_at']) < first_tip]
    deploy_pair = fit_pair(deploy_fit)
    deploy_calibrator = fit_calibrator(deploy_pair, weight, deploy_cal)
    report['deployment'] = {'refit': True, 'fit': span(deploy_fit), 'calibration': span(deploy_cal),
                            'labels_available_through': max(r['label_available_at'] for r in rows),
                            'performance_estimate': 'See earlier frozen held_out checkpoint; not a test of this refit.'}
    identity = json.dumps({'fingerprint': dataset['fingerprint'], 'features': FEATURE_NAMES,
                           'created_at': report['created_at']}, sort_keys=True)
    report['model_id'] = hashlib.sha256(identity.encode()).hexdigest()[:16]
    bundle = {'schema': SCHEMA, 'feature_names': FEATURE_NAMES, 'pair': deploy_pair,
              'weight': weight, 'calibrator': deploy_calibrator, 'report': report}
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        pickle.dump(bundle, stream, protocol=pickle.HIGHEST_PROTOCOL)
        temporary = stream.name
    os.replace(temporary, directory / 'model.pkl')
    atomic_json(directory / 'model-report.json', report)
    # The exact once-held-out probabilities make evaluation independently reviewable.
    atomic_json(directory / 'holdout-predictions.json', {'model_id': report['model_id'], 'rows': [
        {'game_id': r['game_id'], 'date': r['date'], 'y': r['y'], 'probability': float(q),
         'elo_probability': r['elo_probability']} for r, q in zip(test, p)]})
    return report


def load_model(directory=DATA):
    _, sklearn = dependencies()
    path = Path(directory) / 'model.pkl'
    if not path.exists():
        raise ValueError('No NBA model. Run python3 -m NBA train first.')
    report_path = Path(directory) / 'model-report.json'
    if not report_path.exists():
        raise ValueError('NBA model metadata is missing; retrain')
    metadata = json.loads(report_path.read_text())
    if metadata['versions']['sklearn'] != sklearn.__version__:
        raise ValueError('NBA model requires scikit-learn ' + metadata['versions']['sklearn']
                         + '; use NBA/.venv/bin/python or retrain in this environment')
    # Only a locally trained artifact is loaded; never accept pickle uploads/URLs.
    with path.open('rb') as stream:
        bundle = pickle.load(stream)
    from .context import CONTEXT_NAMES
    if bundle.get('schema') != SCHEMA or bundle.get('feature_names') not in (FEATURE_NAMES,FEATURE_NAMES+CONTEXT_NAMES):
        raise ValueError('NBA feature schema changed; retrain the model')
    return bundle


def predict(bundle, features):
    return predict_many(bundle, [features])[0]


def predict_many(bundle, features):
    rows = [{'features': f} for f in features]
    x = matrix(rows,bundle.get('feature_names',FEATURE_NAMES))
    if bundle.get('kind')=='weighted_logistic':
        raw=bundle['classifier'].predict_proba(bundle['scaler'].transform(x)*bundle['feature_weights'])[:,1]
        probabilities=bundle['calibrator'].predict_proba(logit(raw))[:,1]
        return [{'home_win_probability':float(p),'away_win_probability':1-float(p),
                 'uncalibrated_components':{'weighted_logistic':float(q)},'component_disagreement':None}
                for p,q in zip(probabilities,raw)]
    components = [m.predict_proba(x)[:, 1] for m in bundle['pair']]
    raw = bundle['weight']*components[0]+(1-bundle['weight'])*components[1]
    probabilities = bundle['calibrator'].predict_proba(logit(raw))[:, 1]
    return [{'home_win_probability': float(p), 'away_win_probability': 1-float(p),
             'uncalibrated_components': {'logistic': float(a), 'gradient_boosting': float(b)},
             'component_disagreement': abs(float(a-b))} for p, a, b in zip(probabilities, *components)]
