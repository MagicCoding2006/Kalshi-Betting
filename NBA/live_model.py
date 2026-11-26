"""Historical, point-in-time NBA in-game win probability research.

This model estimates the game winner from score, clock, and a frozen pregame
prior. It does not estimate Kalshi fills or guarantee a profitable entry/exit.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from .data import DATA, atomic_json, load_dataset, utcnow

SEASONS = (2023, 2024, 2025, 2026)
FEATURES = ('margin_scaled', 'remaining_fraction', 'pregame_logit_remaining', 'late_margin')
TRAIN_SEASONS = (2023, 2024)
CALIBRATION_SEASONS = (2025,)
TEST_SEASONS = (2026,)


def features(margin: float, remaining: float, prior: float) -> list[float]:
    remaining = max(0., min(2880., float(remaining)))
    prior = max(.001, min(.999, float(prior)))
    return [float(margin) / math.sqrt(1 + remaining / 60), remaining / 2880,
            math.log(prior / (1 - prior)) * remaining / 2880,
            float(margin) * max(0., 1 - remaining / 180)]


def _weighted(rows):
    counts = Counter(row['game_id'] for row in rows)
    return [1 / counts[row['game_id']] for row in rows]


def _metrics(rows, probabilities):
    n = len(rows)
    if not n:
        return {'samples': 0, 'games': 0, 'brier': None, 'log_loss': None,
                'accuracy': None, 'ece': None}
    weights = _weighted(rows)
    total = sum(weights)
    labels = [row['y'] for row in rows]
    clipped = [max(1e-7, min(1 - 1e-7, float(p))) for p in probabilities]
    brier = sum(w * (p-y)**2 for w, p, y in zip(weights, clipped, labels)) / total
    logloss = -sum(w * (y*math.log(p) + (1-y)*math.log1p(-p))
                   for w, p, y in zip(weights, clipped, labels)) / total
    accuracy = sum(w * (int(p >= .5) == y) for w, p, y in zip(weights, clipped, labels)) / total
    bins=[]
    for low in (i/10 for i in range(10)):
        selected=[(p, y, w) for p, y, w in zip(clipped, labels, weights)
                  if low <= p < low+.1 or (low == .9 and p == 1.)]
        mass=sum(w for _, _, w in selected)
        if mass:
            bins.append({'from':low,'to':low+.1,'weight':mass/total,
                         'mean_probability':sum(p*w for p,_,w in selected)/mass,
                         'win_rate':sum(y*w for _,y,w in selected)/mass,'games':len({rows[i]['game_id'] for i,(p,_,_) in enumerate(zip(clipped,labels,weights)) if low <= p < low+.1})})
    ece=sum(b['weight']*abs(b['mean_probability']-b['win_rate']) for b in bins)
    return {'samples':n,'games':len({r['game_id'] for r in rows}),'brier':brier,
            'log_loss':logloss,'accuracy':accuracy,'ece':ece,'reliability':bins}


def _fit(rows, key):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    x=np.asarray([r[key] for r in rows],dtype=float);y=np.asarray([r['y'] for r in rows],dtype=int)
    weights=np.asarray(_weighted(rows),dtype=float)
    scaler=StandardScaler().fit(x,sample_weight=weights)
    model=LogisticRegression(C=.1,max_iter=1000).fit(scaler.transform(x),y,sample_weight=weights)
    return {'means':scaler.mean_.tolist(),'scales':scaler.scale_.tolist(),
            'coefficients':model.coef_[0].tolist(),'intercept':float(model.intercept_[0]),
            'feature_min':x.min(axis=0).tolist(),'feature_max':x.max(axis=0).tolist()}


def _raw(model, x):
    value=model['intercept']+sum(c*(v-m)/s for c,v,m,s in zip(
        model['coefficients'],x,model['means'],model['scales']))
    return 1/(1+math.exp(-max(-35,min(35,value))))


def _calibrate(model, calibration_rows, key):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    raw=np.asarray([[_raw(model,r[key])] for r in calibration_rows])
    labels=np.asarray([r['y'] for r in calibration_rows])
    weights=np.asarray(_weighted(calibration_rows),dtype=float)
    eps=1e-7;logits=np.log(np.clip(raw,eps,1-eps)/(1-np.clip(raw,eps,1-eps)))
    calibrator=LogisticRegression(C=.1,max_iter=1000).fit(logits,labels,sample_weight=weights)
    model['calibration']={'coefficient':float(calibrator.coef_[0,0]),'intercept':float(calibrator.intercept_[0])}


def _probability(model, x):
    raw=_raw(model,x);cal=model.get('calibration')
    if cal:
        logit=math.log(max(1e-7,min(1-1e-7,raw))/(1-max(1e-7,min(1-1e-7,raw))))
        value=cal['coefficient']*logit+cal['intercept']
        return 1/(1+math.exp(-max(-35,min(35,value))))
    return raw


def _load_prior(dataset_dir):
    """Create each game's pregame Elo prior before adding that game's result."""
    from .features import training_rows
    dataset=load_dataset(dataset_dir)
    return {str(row['game_id']):row['elo_probability'] for row in training_rows(dataset['games'])}


def _build_rows(data_dir=DATA, seasons=SEASONS, interval_seconds=120):
    import pandas as pd
    data_dir=Path(data_dir);prior=_load_prior(data_dir)
    labels={}
    for game in load_dataset(data_dir)['games']:
        labels[str(game['game_id'])]=int(game['home_stats']['pts']>game['away_stats']['pts'])
    rows=[];source=[]
    for season in seasons:
        path=data_dir/'raw'/'live_pbp'/f'play_by_play_{season}.parquet'
        if not path.exists():continue
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        source.append({'season':season,'path':str(path),'bytes':path.stat().st_size,'sha256':digest})
        frame=pd.read_parquet(path,columns=['game_id','season','season_type','home_team_abbrev','away_team_abbrev',
            'period_number','end_game_seconds_remaining','home_score','away_score','game_play_number'])
        frame=frame[(frame['season_type']==2)&frame['game_id'].notna()].copy()
        frame['game_id']=frame['game_id'].astype(str)
        frame['remaining']=pd.to_numeric(frame['end_game_seconds_remaining'],errors='coerce')
        frame['home_score']=pd.to_numeric(frame['home_score'],errors='coerce')
        frame['away_score']=pd.to_numeric(frame['away_score'],errors='coerce')
        frame=frame.dropna(subset=['remaining','home_score','away_score','period_number'])
        for gid,group in frame.sort_values(['game_id','game_play_number']).groupby('game_id',sort=False):
            if gid not in labels or gid not in prior:continue
            group=group[(group['period_number']<=4)&(group['remaining']>=0)&(group['remaining']<=2880)]
            if group.empty:continue
            points=group[['remaining','home_score','away_score']].drop_duplicates(keep='last').sort_values('remaining',ascending=False)
            if points.empty:continue
            final=points.iloc[-1]
            if int(final.home_score)<0 or int(final.away_score)<0:continue
            # One observation per two elapsed minutes, selected only from plays
            # that have happened by that point. This prevents dense games from
            # dominating the sample while preserving clock/score chronology.
            seen=set()
            for point in points.itertuples(index=False):
                remaining=float(point.remaining)
                bucket=min(23,int((2880-remaining)//interval_seconds))
                if bucket in seen:continue
                seen.add(bucket)
                margin=float(point.home_score-point.away_score)
                x=features(margin,remaining,prior[gid])
                baseline=features(margin,remaining,.5)
                rows.append({'game_id':gid,'season':season,'bucket':bucket,'remaining':remaining,
                             'x':x,'baseline_x':baseline,'prior':prior[gid],'y':labels[gid]})
    return rows,source


def train_live_model(directory=DATA, seasons=SEASONS):
    """Train on earlier seasons, calibrate on a later season, hold out newest.

    Does not promote signals: entry/exit quality needs independent market replay.
    """
    from .data import atomic_json
    directory=Path(directory)
    rows,sources=_build_rows(directory,seasons)
    train=[r for r in rows if r['season'] in TRAIN_SEASONS]
    calibration=[r for r in rows if r['season'] in CALIBRATION_SEASONS]
    test=[r for r in rows if r['season'] in TEST_SEASONS]
    report={'schema':1,'kind':'live_score_clock_win_probability','generated_at':utcnow().isoformat(),
        'seasons':list(seasons),'train_seasons':list(TRAIN_SEASONS),'calibration_seasons':list(CALIBRATION_SEASONS),
        'test_seasons':list(TEST_SEASONS),'snapshot_interval_seconds':120,'feature_names':list(FEATURES),
        'sources':sources,'promoted':False,'status':'insufficient_data',
        'limitations':['Score/clock model only; no live lineup, shot-quality, fatigue, possession or in-game hustle inputs.',
          'Historical play-by-play source is community-maintained and can be corrected or delayed.',
          'No historical Kalshi order-book replay; probability quality does not establish entry/exit profitability.',
          'No exchange orders are submitted.']}
    if not train or not calibration or not test:
        report.update(train_games=len({r['game_id'] for r in train}),calibration_games=len({r['game_id'] for r in calibration}),
                      test_games=len({r['game_id'] for r in test}),reason='Missing one or more chronological season partitions.')
    else:
        models={}
        for key in ('x','baseline_x'):
            model=_fit(train,key);_calibrate(model,calibration,key);models[key]=model
        test_model=[_probability(models['x'],r['x']) for r in test]
        test_score=[_probability(models['baseline_x'],r['baseline_x']) for r in test]
        test_prior=[r['prior'] for r in test]
        primary=_metrics(test,test_model);score_baseline=_metrics(test,test_score);pregame=_metrics(test,test_prior)
        improves=(primary['brier']<score_baseline['brier'] and primary['log_loss']<score_baseline['log_loss'])
        report.update(status='evaluated',train_games=len({r['game_id'] for r in train}),
            calibration_games=len({r['game_id'] for r in calibration}),test_games=len({r['game_id'] for r in test}),
            train_observations=len(train),calibration_observations=len(calibration),test_observations=len(test),
            test=primary,score_time_baseline=score_baseline,pregame_baseline=pregame,
            improves_both_metrics=improves,
            signal_eligibility='probability_candidate_only' if improves else 'WAIT',
            test_period={'from_season':min(TEST_SEASONS),'through_season':max(TEST_SEASONS)})
        # The serving model only uses the fit/calibration seasons; untouched test
        # outcomes are never used for training, calibration, or threshold choice.
        artifact={'schema':1,'trained_at':utcnow().isoformat(),'model_id':hashlib.sha256(json.dumps(models['x'],sort_keys=True).encode()).hexdigest()[:16],
            'feature_names':list(FEATURES),'model':models['x'],'train_seasons':list(TRAIN_SEASONS),
            'calibration_seasons':list(CALIBRATION_SEASONS),'promoted':False,
            'supported_ranges':{'min':models['x']['feature_min'],'max':models['x']['feature_max']}}
        atomic_json(directory/'live-model.json',artifact)
    atomic_json(directory/'live-model-report.json',report)
    return report


def load_live_model(directory=DATA):
    path=Path(directory)/'live-model.json'
    if not path.exists():
        path=Path(__file__).resolve().parent/'models'/'live-model.json'
    return json.loads(path.read_text()) if path.exists() else None


def predict_live(artifact, margin, remaining, prior):
    if not artifact or not artifact.get('model'):
        return {'status':'unavailable','probability':None,'reason':'Live score/clock model has not been trained.'}
    x=features(margin,remaining,prior);model=artifact['model']
    if len(x)!=len(model['means']) or any(not math.isfinite(v) for v in x):
        return {'status':'abstain','probability':None,'reason':'Invalid live game state.'}
    for v,lo,hi in zip(x,model['feature_min'],model['feature_max']):
        if not lo-max(.02,(hi-lo)*.25)<=v<=hi+max(.02,(hi-lo)*.25):
            return {'status':'abstain','probability':None,'reason':'Game state is outside the historical training range.'}
    return {'status':'experimental','probability':_probability(model,x),'as_of':utcnow().isoformat(),
        'model_id':artifact.get('model_id'),'scope':'Regulation-game score and clock only; no live player/possession context.'}
