"""Offline NBA research only. Never imports a trading engine or writes active artifacts.

Run from workspace root: NBA/.venv/bin/python -m NBA.research.exit_study
Results go into NBA/.data/research/exit-study-<UTC timestamp>.json.
All candidate definitions are fixed before examining their test metrics.
"""
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from NBA.data import DATA, instant, load_dataset
from NBA.experiments import ALL_NAMES, GROUPS, feature_weights, split_season
from NBA.features import metrics, training_rows
from NBA.markets import fee_terms, sweep
from NBA.model import block_comparison, logit, scores, span
from NBA.quarters import load_quarters
from desk.model import normalize_book


CANDIDATES=['incumbent','recency_730_days','nonlinear_context','equal_ensemble','margin_ridge','opponent_adjusted_form']
ADJUSTED=['adjusted_offense_diff','adjusted_defense_diff']


def fee(price,quantity=10,rounding='current_conservative',multiplier=1):
    scale=100 if rounding=='current_conservative' else 10000
    return math.ceil((.07*multiplier*quantity*price*(1-price)-1e-10)*scale)/scale/quantity


def payoff_tests():
    rows=[]
    for target,stop in [(.65,.45),(.65,.55),(.70,.55),(.70,.50)]:
        for rounding in ['current_conservative','published_centicent']:
            for slip in [0,.005]:
                win=target-.60-fee(.60,rounding=rounding)-fee(target,rounding=rounding)-2*slip
                loss=.60-stop+fee(.60,rounding=rounding)+fee(stop,rounding=rounding)+2*slip
                rows.append({'entry':.60,'target_bid':target,'stop_bid':stop,'quantity':10,
                             'fee_policy':rounding,'slippage_each_leg':slip,'net_gain':win,'net_loss':loss,
                             'break_even_target_before_stop_rate':loss/(win+loss) if win>0 else None,
                             'expected_pnl_at_two_thirds_target_rate':2*win/3-loss/3})
    assert abs(fee(.60)-.017)<1e-9
    assert abs(fee(.60,rounding='published_centicent')-.0168)<1e-9
    selected=next(r for r in rows if r['target_bid']==.65 and r['stop_bid']==.45 and r['fee_policy']=='current_conservative' and r['slippage_each_leg']==.005)
    assert abs(selected['net_gain']-.007)<1e-9
    assert abs(selected['net_loss']-.195)<1e-9
    return {'scenarios':rows,'interpretation':'These are hypothetical payoff calculations, not observed target-hit probabilities. Stop levels are assumed fills; jumps can be worse.'}


def market_audit(snapshot_files):
    count=0;after_tip=0;games=set();tickers=set();models=Counter();stamps=[];last=None
    for path in snapshot_files:
        raw=json.loads(path.read_text());scan=raw['scan'];last=raw
        models[scan['model_id']]+=1;stamps.append(scan['generated_at'])
        by_ticker={m['ticker']:g for g in scan['games'] for m in g.get('markets',[])}
        for ticker,record in raw.get('raw_books',{}).items():
            g=by_ticker.get(ticker)
            if g is None:continue
            count+=1;games.add(g['game_id']);tickers.add(ticker)
            if instant(record['received_at'])>=instant(g['tipoff']):after_tip+=1
    roundtrips=[]
    if last:
        multiplier=fee_terms(last['raw_series'])
        for g in last['scan']['games']:
            if not g.get('prediction'):continue
            for market in g.get('markets',[]):
                raw_book=last.get('raw_books',{}).get(market['ticker'])
                if raw_book is None:continue
                book=normalize_book(raw_book['payload'])
                for side in ('yes','no'):
                    buy=sweep(book[side+'_asks'],10,multiplier);sell=sweep(book[side+'_bids'],10,multiplier)
                    if buy is None or sell is None:continue
                    pnl=(sell['price_cost']-buy['price_cost']-sell['fee_upper_estimate']-buy['fee_upper_estimate'])/10-.01
                    roundtrips.append({'game_id':g['game_id'],'ticker':market['ticker'],'side':side,
                                       'buy_vwap':buy['vwap'],'sell_vwap':sell['vwap'],
                                       'immediate_roundtrip_net_per_contract':pnl})
    return {'snapshot_files':len(snapshot_files),'first_at':min(stamps),'last_at':max(stamps),
            'hours':(instant(max(stamps))-instant(min(stamps))).total_seconds()/3600,
            'book_observations':count,'distinct_games':len(games),'distinct_market_tickers':len(tickers),
            'post_tip_book_observations':after_tip,'models_seen':dict(models),
            'latest_quote_roundtrips':roundtrips,
            'median_immediate_roundtrip_net':float(np.median([r['immediate_roundtrip_net_per_contract'] for r in roundtrips])) if roundtrips else None,
            'limitations':['Repeated snapshots are not independent games.',
                           'The collector currently selects pregame books; this archive cannot validate a full in-game exit strategy.',
                           'Snapshot requests are sequential and do not establish simultaneous cross-contract fills.',
                           'Immediate roundtrips use displayed depth, conservative fees and 0.5 cent slippage each leg, with no real fills.']}


def adjusted_form(dataset,rows):
    """EWMA ratings adjusted for the opponent's earlier offense/defense estimate."""
    by_id={r['game_id']:r for r in rows};completed=sorted(dataset['games'],key=lambda g:(g['available_at'],g['game_id']))
    ratings={};cursor=0
    def rating(code,season):
        if code not in ratings:ratings[code]=[110.,110.,season]
        value=ratings[code]
        if value[2]!=season:
            value[:]=[110+.75*(value[0]-110),110+.75*(value[1]-110),season]
        return value
    for fixture in sorted(dataset['games'],key=lambda g:(g['tipoff'],g['game_id'])):
        cutoff=instant(fixture['tipoff'])
        while cursor<len(completed) and instant(completed[cursor]['available_at'])<=cutoff:
            g=completed[cursor];h=rating(g['home'],g['season']);a=rating(g['away'],g['season'])
            old_h,old_a=list(h),list(a);m=metrics(g['home_stats'],g['away_stats'])
            offense,defense=m['ortg'],m['drtg']
            h[:2]=[.9*old_h[0]+.1*(offense-old_a[1]+110),.9*old_h[1]+.1*(defense-old_a[0]+110)]
            a[:2]=[.9*old_a[0]+.1*(defense-old_h[1]+110),.9*old_a[1]+.1*(offense-old_h[0]+110)]
            cursor+=1
        if fixture['game_id'] in by_id:
            h,a=rating(fixture['home'],fixture['season']),rating(fixture['away'],fixture['season'])
            by_id[fixture['game_id']]['features'].update({ADJUSTED[0]:h[0]-a[0],ADJUSTED[1]:h[1]-a[1]})


def array(rows,names):return np.array([[r['features'][f] for f in names] for r in rows],float)


def fit_candidates(fit,cal,test,config,margins):
    x,xc,xt=[array(rows,ALL_NAMES) for rows in (fit,cal,test)]
    y,yc=[np.array([r['y'] for r in rows]) for rows in (fit,cal)]
    weights=np.array(feature_weights(config));scaler=StandardScaler().fit(x)
    z,zc,zt=[scaler.transform(v)*weights for v in (x,xc,xt)]
    result={}
    def calibrated(raw_c,raw_t,logits=True):
        c,t=(logit(raw_c),logit(raw_t)) if logits else (np.asarray(raw_c).reshape(-1,1),np.asarray(raw_t).reshape(-1,1))
        fitted=LogisticRegression(C=1,max_iter=1200).fit(c,yc)
        return fitted.predict_proba(t)[:,1]
    lr=LogisticRegression(C=config['C'],max_iter=1200).fit(z,y)
    result['incumbent']=calibrated(lr.predict_proba(zc)[:,1],lr.predict_proba(zt)[:,1])
    newest=max(instant(r['tipoff']) for r in fit)
    recency=np.array([2**(-(newest-instant(r['tipoff'])).total_seconds()/86400/730) for r in fit]);recency/=recency.mean()
    recent=LogisticRegression(C=config['C'],max_iter=1200).fit(z,y,sample_weight=recency)
    result['recency_730_days']=calibrated(recent.predict_proba(zc)[:,1],recent.predict_proba(zt)[:,1])
    usable=[i for i,n in enumerate(ALL_NAMES) if n not in GROUPS['referees']+GROUPS['availability']]
    nonlinear=HistGradientBoostingClassifier(max_iter=150,max_leaf_nodes=7,min_samples_leaf=60,
        learning_rate=.04,l2_regularization=10,early_stopping=False,random_state=42).fit(x[:,usable],y)
    result['nonlinear_context']=calibrated(nonlinear.predict_proba(xc[:,usable])[:,1],nonlinear.predict_proba(xt[:,usable])[:,1])
    result['equal_ensemble']=(result['incumbent']+result['nonlinear_context'])/2
    ridge=Ridge(alpha=100).fit(scaler.transform(x)[:,usable],[margins[r['game_id']] for r in fit])
    result['margin_ridge']=calibrated(ridge.predict(scaler.transform(xc)[:,usable]),ridge.predict(scaler.transform(xt)[:,usable]),False)
    names=ALL_NAMES+ADJUSTED
    adjusted_scaler=StandardScaler().fit(array(fit,names));w=np.r_[weights,[1.,1.]]
    za,zca,zta=[adjusted_scaler.transform(array(v,names))*w for v in (fit,cal,test)]
    adjusted=LogisticRegression(C=config['C'],max_iter=1200).fit(za,y)
    result['opponent_adjusted_form']=calibrated(adjusted.predict_proba(zca)[:,1],adjusted.predict_proba(zta)[:,1])
    return result


def summary(rows,p):
    r=scores([g['y'] for g in rows],p)
    r['correct']=int(sum((p>=.5)==np.array([g['y'] for g in rows])))
    return r


def paired_diagnostic(rows,p,baseline):
    labels=np.array([r['y'] for r in rows]);a=(p>=.5)==labels;b=(baseline>=.5)==labels
    dates=sorted({r['date'] for r in rows});counts=[];sums=[]
    for day in dates:
        mask=np.array([r['date']==day for r in rows]);counts.append(int(mask.sum()));sums.append(int((a[mask].astype(int)-b[mask].astype(int)).sum()))
    rng=np.random.default_rng(42);indices=rng.integers(0,len(dates),size=(1000,len(dates)))
    draws=np.array(sums)[indices].sum(axis=1)/np.array(counts)[indices].sum(axis=1)
    return {**block_comparison(rows,p,baseline),'accuracy_difference':float(a.mean()-b.mean()),
            'accuracy_day_block_bootstrap_95_interval':[float(v) for v in np.quantile(draws,[.025,.975])],
            'baseline_wrong_challenger_right':int((a & ~b).sum()),'baseline_right_challenger_wrong':int((b & ~a).sum())}


def challenger_tests(dataset,history):
    rows=training_rows(dataset['games'])
    for r in rows:r['features'].update(history['rows'][r['game_id']]['features'])
    adjusted_form(dataset,rows)
    config=json.loads((DATA/'optimization-report.json').read_text())['promoted_config']
    margins={g['game_id']:g['home_stats']['pts']-g['away_stats']['pts'] for g in dataset['games']}
    folds={};test_predictions=None
    for season in (2024,2025,2026):
        fit,cal,test=split_season(rows,season)
        predictions=fit_candidates(fit,cal,test,config,margins)
        folds[str(season)]={'fit':span(fit),'calibration':span(cal),'evaluation':span(test),
                            'candidates':{name:summary(test,p) for name,p in predictions.items()}}
        print('Offline challenger season',season,{name:round(float(p['log_loss']),5) for name,p in folds[str(season)]['candidates'].items()},flush=True)
        if season==2026:test_predictions=(test,predictions)
    validation={name:sum(folds[str(s)]['candidates'][name]['log_loss']*folds[str(s)]['candidates'][name]['n'] for s in (2024,2025))/sum(folds[str(s)]['candidates'][name]['n'] for s in (2024,2025)) for name in CANDIDATES}
    chosen=min(validation,key=validation.get)
    test,pred=test_predictions
    saved={r['game_id']:r['probability'] for r in json.loads((DATA/'holdout-predictions.json').read_text())['rows']}
    assert max(abs(float(q)-saved[r['game_id']]) for r,q in zip(test,pred['incumbent']))<1e-8
    thresholds=[]
    p=pred['incumbent'];pick_p=np.maximum(p,1-p);won=(p>=.5)==np.array([r['y'] for r in test])
    for threshold in (.5,.6,.7,.8,.9):
        mask=pick_p>=threshold;n=int(mask.sum());wins=int(won[mask].sum())
        thresholds.append({'minimum_pick_probability':threshold,'games':n,'wins':wins,
                           'accuracy':wins/n if n else None,'mean_predicted_probability':float(pick_p[mask].mean()) if n else None,
                           'maximum_break_even_all_in_entry_price':wins/n if n else None})
    return {'candidate_definitions':{'incumbent':'Existing selected configuration, reconstructed evaluation checkpoint.',
        'recency_730_days':'Same logistic coefficients/inputs, fit loss weighted with two-year half-life.',
        'nonlinear_context':'Calibrated HGB, 7 leaves, 150 iterations, no referee or dedicated availability columns.',
        'equal_ensemble':'Equal average of calibrated incumbent and nonlinear model.',
        'margin_ridge':'Ridge alpha=100 on final margin, mapped to win probability by disjoint sigmoid calibration.',
        'opponent_adjusted_form':'Incumbent plus causal EWMA offense/defense adjusted for earlier opponent ratings; alpha=.1.'},
        'folds':folds,'validation_weighted_log_loss':validation,'validation_selected_candidate':chosen,
        'later_test_selected':folds['2026']['candidates'][chosen],'confidence_filter_diagnostics':thresholds,
        'paired_later_test_diagnostics':{name:paired_diagnostic(test,p,pred['incumbent']) for name,p in pred.items() if name!='incumbent'},
        'later_test_predictions':[{'game_id':r['game_id'],'date':r['date'],'y':r['y'],
                                   'probabilities':{name:float(p[i]) for name,p in pred.items()}} for i,r in enumerate(test)],
        'limitations':['Exploratory challenger study on previously reviewed historical periods; not a new prospective holdout.',
                       'All candidates use the same chronological fit/calibration/test blocks; no active model is saved.',
                       'Incumbent feature configuration was already selected using seasons through 2025.',
                       'Confidence-filter accuracy does not include entry prices or establish profit.']},rows


def checkpoint_tests(rows):
    quarters={}
    for season in (2025,2026,2027):
        q,_=load_quarters(DATA,season);quarters.update(q)
    prior=[r for r in rows if r['season']==2025 and r['game_id'] in quarters]
    days=sorted({r['date'] for r in prior});cut=days[int(len(days)*.7)]
    cal=[r for r in prior if r['date']>=cut];first_cal=min(instant(r['tipoff']) for r in cal)
    fit=[r for r in prior if r['date']<cut and instant(r['label_available_at'])<first_cal]
    result={}
    for season in (2026,2027):
        test=[r for r in rows if r['season']==season and r['game_id'] in quarters]
        season_result={'fit':span(fit),'calibration':span(cal),'evaluation':span(test),'states':{}}
        for checkpoint,periods in [('pregame',0),('q1',1),('halftime',2)]:
            def x(samples):
                prior_logit=logit(np.array([r['elo_probability'] for r in samples])).ravel()
                if periods==0:return prior_logit.reshape(-1,1)
                margins=np.array([sum(quarters[r['game_id']]['home'][:periods])-sum(quarters[r['game_id']]['away'][:periods]) for r in samples])
                return np.column_stack((prior_logit,margins))
            xf,xc,xt=[x(s) for s in (fit,cal,test)]
            scaler=StandardScaler().fit(xf)
            lr=LogisticRegression(C=1,max_iter=1200).fit(scaler.transform(xf),[r['y'] for r in fit])
            cal_p=lr.predict_proba(scaler.transform(xc))[:,1];test_p=lr.predict_proba(scaler.transform(xt))[:,1]
            calibrator=LogisticRegression(C=1,max_iter=1200).fit(logit(cal_p),[r['y'] for r in cal])
            probability=calibrator.predict_proba(logit(test_p))[:,1]
            season_result['states'][checkpoint]=summary(test,probability)
        result[str(season)]=season_result
    return {'protocol':'Fit early 2024-25; calibrate later 2024-25 with purged labels; freeze and evaluate 2025-26 and current 2026-27.',
            'inputs':'Past-only Elo pregame prior; add cumulative home-minus-away score at Q1 or halftime. Separate classifier per checkpoint.',
            'results':result,'limitations':['This is a quarter-end probability prototype, not a real-time play-by-play exit system.',
                'Higher in-game accuracy uses information unavailable before tipoff and is not an improvement in pregame skill.',
                'Market prices also react to score; no in-game price advantage or profitable exit is established.',
                'No possession, live lineups, fouls, timeouts, feed latency or within-quarter timing is modeled.']}


def protected_hashes():
    return {name:hashlib.sha256((DATA/name).read_bytes()).hexdigest() for name in
            ['model.pkl','model-report.json','optimization-report.json','holdout-predictions.json']}


def main():
    before=protected_hashes();dataset=load_dataset();history=json.loads((DATA/'context/history.json').read_text())
    snapshots=sorted((DATA/'snapshots').glob('*.json')) # Freeze archive list at study start.
    result={'created_at':datetime.now(timezone.utc).isoformat(),'research_only':True,
            'dataset_fingerprint':dataset['fingerprint'],'active_artifact_hashes_before':before,
            'payoff_stress_tests':payoff_tests(),'market_archive_audit':market_audit(snapshots)}
    with threadpool_limits(limits=1):
        result['model_challengers'],rows=challenger_tests(dataset,history)
        result['checkpoint_prototype']=checkpoint_tests(rows)
    after=protected_hashes();assert before==after,'Active artifacts changed during research'
    result['active_artifacts_unchanged']=True
    path=DATA/'research'/('exit-study-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print('Saved offline report:',path,flush=True)
    print('Checkpoint results',json.dumps({s:{k:{m:v[m] for m in ['n','correct','accuracy','log_loss']} for k,v in r['states'].items()} for s,r in result['checkpoint_prototype']['results'].items()}),flush=True)


if __name__=='__main__':main()
