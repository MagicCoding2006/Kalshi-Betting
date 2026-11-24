"""Market-anchored challengers, purged walk-forward selection and a future lockbox.

No portfolio mutations. Old published tests are quarantined, not recycled into
model selection. Numeric fitting dependencies load only in the research worker.
"""
import hashlib
import json
import math
import random
import time
from collections import Counter,defaultdict
from pathlib import Path

from .evaluation import logit,sigmoid,predict,quality,weights,fingerprint
from .execution import replay

VERSION='market-anchor-v1'
POLICY={'min_seconds':90,'max_spread':.03,'max_reference_age':5}
NAMES=['baseline_minus_market','depth_imbalance','microprice_over_spread','residual_time','residual_volatility']
CANDIDATES=[('market',None),('blend_10',.1),('blend_25',.25),('ridge_10',10.),('ridge_50',50.)]


def atomic_json(path,payload):
    path=Path(path);temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload,allow_nan=False));temporary.replace(path)


def vector(x):
    residual=max(-3,min(3,logit(x['baseline'])-logit(x['kalshi_mid'])))
    ratio=math.log(x['vol_5m']/x['sigma_30m']) if x.get('vol_5m') and x.get('sigma_30m') else 0.
    return [residual,max(-1,min(1,x.get('kalshi_imbalance',0))),
            max(-1,min(1,x.get('kalshi_micro_gap',0)/max(.005,x.get('kalshi_spread',.005)))),
            residual*max(0,min(1,x['seconds_left']/900)),residual*max(-1,min(1,ratio))]


def forecast(artifact,x):
    market=x['kalshi_mid']
    if artifact['kind']=='market':return market
    if artifact['kind']=='market_blend':correction=artifact['alpha']*(logit(x['baseline'])-logit(market))
    else:correction=sum(a*b for a,b in zip(artifact['coefficients'],vector(x)))
    return sigmoid(logit(market)+max(-.5,min(.5,correction)))


def fit(name,parameter,rows):
    common={'name':name,'version':VERSION,'reference_mode':sorted({r['x'].get('reference_source','proxy') for r in rows})}
    if name=='market':return {**common,'kind':'market'}
    if name.startswith('blend'):return {**common,'kind':'market_blend','alpha':parameter}
    import numpy as np
    from scipy.optimize import minimize
    X=np.array([vector(r['x']) for r in rows]);offset=np.array([logit(r['x']['kalshi_mid']) for r in rows])
    y=np.array([r['y'] for r in rows]);w=np.array(weights(rows))
    def objective(beta):
        raw=X@beta;z=offset+np.clip(raw,-.5,.5)
        p=1/(1+np.exp(-np.clip(z,-35,35)))
        loss=np.sum(w*(np.logaddexp(0,z)-y*z))+.5*parameter*np.dot(beta,beta)
        gradient=X.T@(w*(p-y)*(np.abs(raw)<.5))+parameter*beta
        return loss,gradient
    result=minimize(objective,np.zeros(X.shape[1]),jac=True,method='L-BFGS-B',bounds=[(0,.5)]+[(-.5,.5)]*(X.shape[1]-1))
    if not result.success:raise ValueError('Residual fit did not converge: '+result.message)
    return {**common,'kind':'market_residual','coefficients':result.x.tolist(),'features':NAMES,'penalty':parameter,'intercept':0}


def folds(rows):
    starts={};available={}
    for r in rows:
        starts[r['ticker']]=min(starts.get(r['ticker'],r['ts']),r['ts']);available[r['ticker']]=r['available']
    names=sorted(starts,key=starts.get);n=len(names)
    if n<90:raise ValueError('Refinement needs 90 development contracts after quarantining published tests')
    boundaries=[int(n*f) for f in (.4,.55,.7,.85)]+[n];result=[]
    for a,b in zip(boundaries,boundaries[1:]):
        validation=names[a:b];cut=starts[validation[0]]
        training=[t for t in names[:a] if available[t]<cut]
        train_rows=[r for r in rows if r['ticker'] in set(training)];valid_rows=[r for r in rows if r['ticker'] in set(validation)]
        if len({r['y'] for r in train_rows})<2:continue
        result.append((train_rows,valid_rows))
    return result


def comparison(rows,pred,market,confidence=.9875):
    diffs=defaultdict(list)
    for row,p,m in zip(rows,pred,market):diffs[row['ticker']].append((p-row['y'])**2-(m-row['y'])**2)
    values=[sum(v)/len(v) for v in diffs.values()];rng=random.Random(917)
    boot=sorted(sum(rng.choice(values) for _ in values)/len(values) for _ in range(2000))
    tail=(1-confidence)/2
    return {'mean_brier_difference':sum(values)/len(values),'interval':[boot[int(len(boot)*tail)],boot[min(len(boot)-1,int(len(boot)*(1-tail)))]],
            'confidence':confidence,'independent_contracts':len(values)}


def diagnostics(store):
    report=store.latest()
    if not report:return {'status':'No original study available'}
    rows=store.dataset();periods={}
    for name,tickers in report['contract_membership'].items():
        outcomes={r['ticker']:r['y'] for r in rows if r['ticker'] in tickers}
        periods[name]={'contracts':len(outcomes),'yes_rate':sum(outcomes.values())/len(outcomes) if outcomes else None}
    models={};path=store.directory/'models.json'
    artifacts=json.loads(path.read_text())['artifacts'] if path.exists() else {}
    test=[r for r in rows if r['ticker'] in report['contract_membership']['test']]
    for e in report['experiments']:
        if not e.get('execution'):continue
        fills=e['execution']['fills'];a=artifacts.get(e['name']);cal=a.get('calibration') if a else None
        corrected=replay(test,a,store.quote_after) if a else None
        models[e['name']]={'sides':dict(Counter(p['side'] for p in fills)),
            'previous_pnl':e['execution']['net_pnl'],'cent_aligned_replay':corrected,
            'calibration_at_50':sigmoid(cal[1]) if cal else None,'calibration_intercept':cal[1] if cal else None}
    return {'periods':periods,'models':models,'limits':'Descriptive audit of already exposed tests; not new validation.'}


def freeze_protocol(store,rows):
    path=store.directory/'refinement-protocol.json'
    if path.exists():
        existing=json.loads(path.read_text())
        if existing['version']!=VERSION or existing['candidates']!=[n for n,p in CANDIDATES]:raise ValueError('Frozen protocol differs from the requested candidate set')
        return existing
    now=time.time();exposed=set()
    with store.lock:
        for r in store.db.execute('SELECT payload FROM reports'):
            exposed.update(json.loads(r[0]).get('contract_membership',{}).get('test',[]))
    eligible=[r for r in rows if r['ticker'] not in exposed and r['available']<=now and r['ts']<now]
    tickers=sorted({r['ticker'] for r in eligible})
    if len(tickers)<90:raise ValueError(f'Only {len(tickers)} development contracts after excluding {len(exposed)} previously published test contracts; need 90')
    protocol={'version':VERSION,'frozen_at':now,'development_tickers':tickers,'quarantined_tests':sorted(exposed),
              'development_fingerprint':fingerprint(eligible,{'development':tickers}),'prospective_contracts_required':30,
              'candidates':[n for n,p in CANDIDATES],'entry_policy':POLICY}
    protocol['id']=hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()[:20]
    # A rerun reuses this lockbox and candidate set; it cannot silently move the test boundary.
    with path.open('x') as handle:json.dump(protocol,handle)
    return protocol


def refinement(store):
    all_rows=store.dataset();protocol=freeze_protocol(store,all_rows)
    allowed=set(protocol['development_tickers'])
    rows=[r for r in all_rows if r['ticker'] in allowed and r['available']<=protocol['frozen_at'] and r['ts']<protocol['frozen_at']]
    if fingerprint(rows,{'development':protocol['development_tickers']})!=protocol['development_fingerprint']:
        raise ValueError('Frozen development records changed; refusing to silently reuse this protocol')
    blocks=folds(rows);oof=[];scores=[];predictions={};fold_reports=[]
    if not blocks:raise ValueError('No chronological training fold contains both settlement outcomes')
    for training,validation in blocks:
        oof+=validation;fold_reports.append({'train':len({r['ticker'] for r in training}),'validation':len({r['ticker'] for r in validation}),
            'validation_tickers':sorted({r['ticker'] for r in validation}),'first_validation':validation[0]['ts'],
            'last_training_label':max(r['available'] for r in training)})
    market=[r['x']['kalshi_mid'] for r in oof];market_quality=quality(oof,market)
    artifacts={}
    for name,parameter in CANDIDATES:
        predicted=[];folds_better=0
        for training,validation in blocks:
            a=fit(name,parameter,training);values=[forecast(a,r['x']) for r in validation];predicted+=values
            folds_better+=quality(validation,values)['brier']<quality(validation,[r['x']['kalshi_mid'] for r in validation])['brier']-1e-12
        predictions[name]=predicted
        # Replay the predictions produced before each validation fold. Never use
        # the final refit's predictions to report historical walk-forward P&L.
        keyed={(r['ticker'],r['ts']):p for r,p in zip(oof,predicted)}
        execution=prediction_replay(oof,keyed,store.quote_after)
        scores.append({'name':name,'quality':quality(oof,predicted),'vs_market':comparison(oof,predicted,market),
                       'improving_folds':folds_better,'total_folds':len(blocks),'execution':execution})
        artifacts[name]=fit(name,parameter,rows)
    challenger=min((s for s in scores if s['name']!='market'),key=lambda s:s['quality']['brier'])
    gates={'enough_contracts':challenger['vs_market']['independent_contracts']>=60,'at_least_three_folds':len(blocks)>=3,
           'familywise_brier_interval_below_zero':challenger['vs_market']['interval'][1]<0,
           'improves_at_least_75pct_folds':challenger['improving_folds']>=math.ceil(.75*len(blocks)),
           'log_loss_no_worse_than_market':challenger['quality']['log_loss']<=market_quality['log_loss'],
           'positive_net_walkforward_replay':challenger['execution']['net_pnl']>0}
    accepted=all(gates.values());selected=challenger['name'] if accepted else 'market'
    best_artifact=artifacts[challenger['name']];champion=artifacts[selected]
    existing_path=store.directory/'refined-model.json'
    previous=json.loads(existing_path.read_text()) if existing_path.exists() else {}
    published=previous.get('published_at',time.time())
    frozen={'protocol_id':protocol['id'],'frozen_at':protocol['frozen_at'],'published_at':published,'candidate':best_artifact,'champion':champion,
            'development_gate':accepted,'gates':gates,'entry_policy':POLICY}
    frozen['artifact_hash']=hashlib.sha256(json.dumps(frozen,sort_keys=True).encode()).hexdigest()
    if previous.get('published_at') and previous.get('artifact_hash')!=frozen['artifact_hash']:
        raise ValueError('Refit would alter the frozen challenger; refusing to overwrite prospective predictions')
    report={'kind':'refinement','status':'complete','created':time.time(),'protocol':protocol,'scores':scores,
            'folds':fold_reports,'challenger':challenger['name'],'selected':selected,'gates':gates,'development_gate':accepted,
            'recommendation':'WAIT for prospective validation' if accepted else 'NO DEMONSTRATED EDGE · retain market benchmark',
            'diagnostics':diagnostics(store),'stress':stress_replay(oof,predictions[challenger['name']],store.quote_after),
            'selection_rule':'Five fixed candidates; no intercept; logit corrections capped at ±0.5; choose on purged walk-forward Brier, not test profit. Four challenger comparisons use 98.75% intervals (Bonferroni familywise 95%).',
            'limits':'Development results are exploratory. Old published tests are quarantined. No strategy is approved until the first 30 later eligible contracts complete the frozen prospective test. This module cannot trade.'}
    atomic_json(store.directory/'refined-model.json',frozen)
    atomic_json(store.directory/'refinement.json',report)
    return report


def prediction_replay(rows,predictions,quote_after,latency=.25,policy=None):
    # The runtime replay interface expects an artifact; an injected predictor is
    # explicit here so it cannot accidentally use the later full-data refit.
    from . import execution
    return execution.replay(rows,{'kind':'received_predictions','predictions':predictions},quote_after,
                            latency=latency,policy=POLICY if policy is None else policy)


def stress_replay(rows,predictions,quote_after):
    keyed={(r['ticker'],r['ts']):p for r,p in zip(rows,predictions)}
    return [{'latency_ms':int(delay*1000),'result':prediction_replay(rows,keyed,quote_after,latency=delay)} for delay in (.25,1.,2.)]


def prospective(store,now=None):
    """Keep the first later cohort hidden until complete, then persist once."""
    now=time.time() if now is None else now
    path=store.directory/'refined-model.json';protocol_path=store.directory/'refinement-protocol.json'
    if not path.exists() or not protocol_path.exists():return {'status':'not_started'}
    frozen=json.loads(path.read_text());protocol=json.loads(protocol_path.read_text());final=store.directory/'prospective-final.json'
    if frozen['protocol_id']!=protocol['id']:return {'status':'error','reason':'Model and prospective protocol differ'}
    material={k:v for k,v in frozen.items() if k!='artifact_hash'}
    if hashlib.sha256(json.dumps(material,sort_keys=True).encode()).hexdigest()!=frozen['artifact_hash']:
        return {'status':'error','reason':'Frozen model hash mismatch'}
    if final.exists():
        result=json.loads(final.read_text())
        if result['artifact_hash']!=frozen['artifact_hash']:return {'status':'error','reason':'Frozen artifact changed after prospective evaluation'}
        return result
    # Only forecasts actually emitted by this frozen model are admissible.
    # Reimported history and later recomputation cannot populate the lockbox.
    with store.lock:
        forecasts=store.db.execute('SELECT * FROM shadow_forecasts WHERE protocol_id=? AND artifact_hash=? ORDER BY emitted',
                                  (protocol['id'],frozen['artifact_hash'])).fetchall()
    contracts={m['ticker']:m for m in store.contracts()};registered={}
    for r in forecasts:
        m=contracts.get(r['ticker'],{})
        if m.get('open_ts',0)>=frozen['published_at'] and frozen['published_at']<=r['emitted']<m.get('close_ts',0):registered.setdefault(r['ticker'],r['emitted'])
    names=[]
    for ticker in sorted(registered,key=registered.get):names.append(ticker)
    needed=protocol['prospective_contracts_required'];names=names[:needed]
    emitted={(r['ticker'],r['ts']):r for r in forecasts}
    rows=[]
    for r in store.dataset():
        f=emitted.get((r['ticker'],r['ts']))
        if r['ticker'] in names and r['origin']=='live_join' and r['available']<=now and f and f['emitted']<r['market']['close_ts']:
            rows.append({**r,'ts':f['emitted'],'recorded_p':f['p_yes']})
    completed={r['ticker'] for r in rows}
    progress={'status':'collecting','reserved_contracts':len(names),'completed_contracts':len(completed),'required_contracts':needed,
              'protocol_id':protocol['id'],'frozen_at':protocol['frozen_at'],'artifact_hash':frozen['artifact_hash'],
              'message':'Scores and P&L remain hidden until all 30 first eligible contracts settle. Model and selection boundary stay frozen.'}
    if len(names)<needed or len(completed)<needed:return progress
    x=frozen['candidate'];p=[r['recorded_p'] for r in rows];m=[r['x']['kalshi_mid'] for r in rows]
    q=quality(rows,p);mq=quality(rows,m);ci=comparison(rows,p,m,confidence=.95)
    execution=prediction_replay(rows,{(r['ticker'],r['ts']):r['recorded_p'] for r in rows},store.quote_after,policy=frozen.get('entry_policy',POLICY))
    future_passed=bool(ci['interval'][1]<0 and q['log_loss']<=mq['log_loss'] and execution['net_pnl']>0)
    passed=bool(frozen['development_gate'] and future_passed)
    message='Prospective test passed; manual review required before any trading integration.' if passed else 'No validated trading edge; no strategy promotion.'
    if future_passed and not frozen['development_gate']:message='Prospective metrics improved, but the development gate failed; review a new protocol before any promotion.'
    result={**progress,'status':'complete','evaluated_at':now,'tickers':names,'quality':q,'market_quality':mq,'vs_market':ci,
            'execution':execution,'passed':passed,'prospective_gate':future_passed,'development_gate':frozen['development_gate'],'message':message}
    atomic_json(final,result);return result
