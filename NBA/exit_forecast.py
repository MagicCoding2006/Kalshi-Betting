"""Short-horizon bid-range and partial-exit research. No fitting in API handlers."""
import bisect
import json
import math
from collections import Counter
from pathlib import Path

from .data import DATA,atomic_json,instant,utcnow

HORIZONS=(60,180,300)
RANGE_FEATURES=['sell_bid','spread','score_time_margin','remaining_fraction','bid_change_60s','move_p90']
STATE_FEATURES=['score_time_margin','remaining_fraction','pregame_logit_remaining','late_margin']
POLICY={'quantity':10,'horizons_seconds':list(HORIZONS),'entry_spacing_seconds':300,'max_gap_seconds':30,'slippage_per_leg':.005}


def quantile(values,q):
    values=sorted(values)
    return values[min(len(values)-1,max(0,math.ceil(q*len(values))-1))] if values else None


def range_features(samples):
    """All samples must already be timestamp-cut to the prediction observation."""
    s=samples[-1];now=instant(s['at']);history=[r for r in samples if 0<=(now-instant(r['at'])).total_seconds()<=180]
    remaining=(4-s['period'])*720+s['clock'];old=[r for r in history if 50<=(now-instant(r['at'])).total_seconds()<=90]
    moves=[abs(b['bid']-a['bid']) for a,b in zip(history,history[1:]) if 0<(instant(b['at'])-instant(a['at'])).total_seconds()<=30]
    return [s['bid'],s['spread'],s['margin']/math.sqrt(1+remaining/60),remaining/2880,
            s['bid']-old[-1]['bid'] if old else 0.,quantile(moves,.9) or 0.]


def state_features(margin,remaining,prior):
    fraction=remaining/2880;logit=math.log(max(.001,min(.999,prior))/(1-max(.001,min(.999,prior))))
    return [margin/math.sqrt(1+remaining/60),fraction,logit*fraction,margin*max(0,1-remaining/180)]


def future_index(points,i,seconds):
    """Require complete observation coverage; never bridge missing windows."""
    deadline=points[i]['time']+seconds
    for j in range(i+1,len(points)):
        if points[j]['time']-points[j-1]['time']>30:return None,'recording_gap'
        if points[j]['time']>=deadline:
            return (j,None) if points[j]['time']<=deadline+30 else (None,'late_quote')
    return None,'incomplete_window'


def build_rows(paths,priors,finals):
    ranges={str(h):[] for h in HORIZONS};states=[];exits=[];excluded=Counter()
    for gid,points in paths.items():
        prior_record=priors.get(gid);last=-math.inf
        for i,p in enumerate(points):
            if p['time']-last<300:continue
            available_prior=prior_record if prior_record and instant(prior_record['at'])<=instant(p['at']) else None
            prior=available_prior['probability'] if available_prior else .5
            last=p['time'];remaining=(4-p['period'])*720+p['clock']
            # Final outcomes are labels only and never predictors.
            if gid in finals:
                states.append({'game_id':gid,'day':p['day'],'at':p['at'],'label_at':finals[gid]['at'],'y':finals[gid]['home_win'],
                    'x':state_features(p['home_score']-p['away_score'],remaining,prior),
                    'baseline_x':state_features(p['home_score']-p['away_score'],remaining,.5)[:2],
                    'prior':prior,'prior_missing':available_prior is None,'market':None})
            for side in ('yes','no'):
                buy=p['fills'][side]['10']['buy'];sell=p['fills'][side]['10']['sell']
                if not buy or not sell:excluded['entry_depth']+=1;continue
                sign=1 if side=='yes' else -1
                samples=[]
                for past in points[max(0,i-40):i+1]:
                    f=past['fills'][side]['10'];b=f['sell'];a=f['buy']
                    if b and a and 0<=p['time']-past['time']<=180:
                        samples.append({'at':past['at'],'bid':b['vwap'],'spread':a['vwap']-b['vwap'],
                            'margin':sign*(past['home_score']-past['away_score']),'period':past['period'],'clock':past['clock']})
                if not samples or samples[-1]['at']!=p['at']:continue
                x=range_features(samples)
                for horizon in HORIZONS:
                    j,error=future_index(points,i,horizon)
                    if error:excluded[str(horizon)+'s_'+error]+=1;continue
                    if any(points[k]['fills'][side]['10']['sell'] is None for k in range(i,j+1)):
                        excluded[str(horizon)+'s_depth_gap']+=1;continue
                    future=points[j]['fills'][side]['10']['sell']
                    ranges[str(horizon)].append({'game_id':gid,'day':p['day'],'at':p['at'],'label_at':points[j]['at'],'side':side,'x':x,
                        'y':future['vwap']-sell['vwap'],'current_bid':sell['vwap']})
                # Matched 3-minute decisions: first later quote for immediate sale,
                # first quote AFTER horizon observation for delayed execution.
                j,error=future_index(points,i,180)
                if error or i+1>=len(points) or j+1>=len(points):excluded['exit_incomplete']+=1;continue
                end=points[j+1];early=points[i+1]
                if early['time']-p['time']>30 or end['time']-points[j]['time']>30:excluded['exit_gap']+=1;continue
                if any(points[k]['fills'][side][str(q)]['sell'] is None for k in range(i+1,j+2) for q in (5,10)):
                    excluded['exit_depth_gap']+=1;continue
                def proceeds(row,q):
                    f=row['fills'][side][str(q)]['sell']
                    return f['price_cost']-f['fee_upper_estimate']-q*.005
                cost=buy['price_cost']+buy['fee_upper_estimate']+.05
                exits.append({'game_id':gid,'day':p['day'],'at':p['at'],'label_at':end['at'],'side':side,
                    'sell_all_now':proceeds(early,10)-cost,'sell_half_now':proceeds(early,5)+proceeds(end,5)-cost,
                    'hold_all_3m':proceeds(end,10)-cost})
                if side=='yes' and states and states[-1]['game_id']==gid and states[-1]['at']==p['at']:
                    states[-1]['market']=(buy['vwap']+sell['vwap'])/2
    return ranges,states,exits,dict(excluded)


def partitions(rows):
    days=sorted({r['day'] for r in rows})
    if len(days)<3:return [],[],[],{}
    cal_day=days[max(1,int(len(days)*.6))];test_day=days[max(days.index(cal_day)+1,int(len(days)*.8))]
    cal_start=min(instant(r['at']) for r in rows if r['day']>=cal_day)
    test_start=min(instant(r['at']) for r in rows if r['day']>=test_day)
    train=[r for r in rows if r['day']<cal_day and instant(r['label_at'])<cal_start]
    cal=[r for r in rows if cal_day<=r['day']<test_day and instant(r['label_at'])<test_start]
    test=[r for r in rows if r['day']>=test_day]
    meta={'calibration_from':cal_day,'test_from':test_day,
        'train_games':len({r['game_id'] for r in train}),'calibration_games':len({r['game_id'] for r in cal}),'test_games':len({r['game_id'] for r in test}),
        'train_game_ids':sorted({r['game_id'] for r in train}),'calibration_game_ids':sorted({r['game_id'] for r in cal}),'test_game_ids':sorted({r['game_id'] for r in test})}
    return train,cal,test,meta


def weights(rows):
    counts=Counter(r['game_id'] for r in rows)
    w=[1/counts[r['game_id']] for r in rows];scale=len(rows)/sum(w)
    return [v*scale for v in w]


def average(values,rows):
    w=weights(rows);return sum(v*a for v,a in zip(values,w))/sum(w)


def export_linear(model,scaler,rows):
    return {'means':list(map(float,scaler.mean_)),'scales':list(map(float,scaler.scale_)),
        'coefficients':list(map(float,model.coef_.ravel())),'intercept':float(model.intercept_.item()),
        'feature_min':[min(r['x'][i] for r in rows) for i in range(len(rows[0]['x']))],
        'feature_max':[max(r['x'][i] for r in rows) for i in range(len(rows[0]['x']))]}


def linear(model,x):
    return model['intercept']+sum(c*(v-m)/s for c,v,m,s in zip(model['coefficients'],x,model['means'],model['scales']))


def fit_ranges(rows):
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    train,cal,test,split=partitions(rows)
    result={'status':'insufficient_games','split':split,'samples':len(rows),'games':len({r['game_id'] for r in rows})}
    if not split or split['train_games']<3 or split['calibration_games']<2 or split['test_games']<3:return result,None
    scaler=StandardScaler().fit([r['x'] for r in train],sample_weight=weights(train))
    model=Ridge(alpha=10).fit(scaler.transform([r['x'] for r in train]),[r['y'] for r in train],sample_weight=weights(train))
    artifact=export_linear(model,scaler,train)
    residual=[r['y']-linear(artifact,r['x']) for r in cal]
    # Each calibration game contributes equal total weight via equal-game quantiles.
    def balanced_quantile(values,q):
        pairs=sorted(zip(values,weights(cal)));total=sum(w for _,w in pairs);running=0
        for value,w in pairs:
            running+=w
            if running>=q*total:return float(value)
        return float(pairs[-1][0])
    low=balanced_quantile(residual,.1);high=balanced_quantile(residual,.9)
    artifact.update(residual_low=low,residual_high=high,point_offset=balanced_quantile(residual,.5),feature_names=RANGE_FEATURES)
    clip=lambda price:max(0.,min(1.,price))
    centers=[clip(r['current_bid']+linear(artifact,r['x'])+artifact['point_offset']) for r in test]
    bounds=[(clip(r['current_bid']+linear(artifact,r['x'])+low),clip(r['current_bid']+linear(artifact,r['x'])+high)) for r in test]
    actual=[r['current_bid']+r['y'] for r in test]
    errors=[abs(pred-target) for pred,target in zip(centers,actual)]
    baseline=average([abs(r['y']) for r in test],test)
    coverage=average([int(lo-1e-9<=target<=hi+1e-9) for (lo,hi),target in zip(bounds,actual)],test)
    widths=[hi-lo for lo,hi in bounds]
    interval_scores=[hi-lo+10*max(0,lo-target)+10*max(0,target-hi) for (lo,hi),target in zip(bounds,actual)]
    result.update(status='experimental',mean_absolute_error=average(errors,test),unchanged_bid_mae=baseline,
        interval_coverage=coverage,mean_interval_width=average(widths,test),interval_score=average(interval_scores,test),nominal_coverage=.8,improves_baseline=average(errors,test)<baseline,
        calibration_warning='Empirical residual band from separate earlier games. Serial dependence means no guaranteed coverage.',feature_names=RANGE_FEATURES)
    return result,artifact


def probability(model,x):
    value=linear(model,x)
    if model.get('calibration'):value=value*model['calibration']['coefficient']+model['calibration']['intercept']
    value=max(-35,min(35,value));return 1/(1+math.exp(-value))


def probability_metrics(rows,predictions):
    return {'brier':average([(p-r['y'])**2 for p,r in zip(predictions,rows)],rows),
        'log_loss':average([-r['y']*math.log(max(1e-9,p))-(1-r['y'])*math.log(max(1e-9,1-p)) for p,r in zip(predictions,rows)],rows)}


def fit_state(rows):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    train,cal,test,split=partitions(rows)
    result={'status':'insufficient_games','split':split,'samples':len(rows),'games':len({r['game_id'] for r in rows}),
        'prior_coverage':sum(not r['prior_missing'] for r in rows)/len(rows) if rows else 0,
        'training_outcomes':dict(Counter(r['y'] for r in train)),'missing_live_inputs':['confirmed on-court lineup','timeouts/bonus','reliable possession','shot quality']}
    if not split or split['train_games']<3 or split['test_games']<3 or len({r['y'] for r in train})<2:return result,None
    def fit(samples,key):
        scaler=StandardScaler().fit([r[key] for r in samples],sample_weight=weights(samples))
        model=LogisticRegression(C=.1,max_iter=1000).fit(scaler.transform([r[key] for r in samples]),[r['y'] for r in samples],sample_weight=weights(samples))
        return export_linear(model,scaler,[{**r,'x':r[key]} for r in samples])
    model=fit(train,'x');baseline=fit(train,'baseline_x')
    if len({r['y'] for r in cal})==2 and split['calibration_games']>=3:
        for artifact,key in ((model,'x'),(baseline,'baseline_x')):
            calibrator=LogisticRegression(C=.1,max_iter=1000).fit([[linear(artifact,r[key])] for r in cal],[r['y'] for r in cal],sample_weight=weights(cal))
            artifact['calibration']={'coefficient':float(calibrator.coef_[0,0]),'intercept':float(calibrator.intercept_[0])}
    predicted=[probability(model,r['x']) for r in test];simple=[probability(baseline,r['baseline_x']) for r in test]
    result.update(status='experimental',game_state=probability_metrics(test,predicted),score_time_baseline=probability_metrics(test,simple),
        pregame_baseline=probability_metrics(test,[r['prior'] for r in test]),feature_names=STATE_FEATURES,
        note='Game outcomes evaluated only after a final score was recorded. No market price is an input to this independent winner baseline. No production promotion.')
    market=[r for r in test if r['market'] is not None]
    if market:
        result['market_midpoint_baseline']=probability_metrics(market,[r['market'] for r in market])
        result['game_state_on_market_subset']=probability_metrics(market,[probability(model,r['x']) for r in market])
        result['market_comparison_samples']=len(market)
        result['market_comparison_games']=len({r['game_id'] for r in market})
    result['improves_score_time']=result['game_state']['brier']<result['score_time_baseline']['brier']
    return result,model


def summarize_exits(rows):
    result={'samples':len(rows),'games':len({r['game_id'] for r in rows}),'policies':{}}
    if not rows:return result
    for name in ('sell_all_now','sell_half_now','hold_all_3m'):
        values=[r[name] for r in rows];tail=sorted(values)[:max(1,math.ceil(.1*len(values)))]
        result['policies'][name]={'mean_net_pnl':average(values,rows),'worst_net_pnl':min(values),'worst_decile_mean':sum(tail)/len(tail)}
    return result


def run_exit_trial(directory=DATA):
    from .exit_data import load_paths
    directory=Path(directory);paths,priors,finals,excluded=load_paths(directory)
    ranges,states,exits,censored=build_rows(paths,priors,finals);reports={};models={}
    for horizon,rows in ranges.items():
        reports[horizon],model=fit_ranges(rows)
        if model:models[horizon]=model
    from zoneinfo import ZoneInfo
    today=utcnow().astimezone(ZoneInfo('America/Indiana/Indianapolis')).date().isoformat()
    # Winner evaluation excludes today's early finishers to avoid selecting games by completion time.
    prior_days=[r for r in states if r['day']<today]
    state_report,state_model=fit_state(prior_days)
    state_report['same_day_samples_withheld']=len(states)-len(prior_days)
    _,_,test,split=partitions(exits)
    data_as_of=max((p['at'] for points in paths.values() for p in points),default=None)
    report={'schema':1,'source_data_as_of':data_as_of,'generated_at':utcnow().isoformat(),'status':'research_only','promoted':False,'policy':POLICY,
        'range_models':reports,'game_state':state_report,'partial_exits':{'all':summarize_exits(exits),'later_games':summarize_exits(test),'split':split},
        'exclusions':{'observations':excluded,'windows':censored},
        'limitations':['Small, partial-game sample; not calibrated trading probabilities or recommended stakes.',
          'No random snapshot split; train/calibration/test use separate game dates with labels available before later partitions.',
          'Quotes are sampled; full-depth fills and 0.5-cent slippage per leg are hypothetical, not guaranteed.',
          'Range target is future executable 10-contract bid VWAP, not a limit-order fill probability.',
          'Partial exits compare sell now / sell half now then remainder after 3 minutes / hold all 3 minutes, on identical windows.',
          'Per-game weighting limits snapshot dominance; observations and opposing sides remain correlated.',
          'Worst-decile exit statistics are descriptive sample tails, not guaranteed risk bounds.',
          'HTTP receipt timestamps do not establish true exchange or scoreboard latency.']}
    bundle={'schema':1,'trained_at':report['generated_at'],'range_models':models,'game_state_model':state_model,
        'policy':POLICY,'status':'experimental','promoted':False}
    out=directory/'research';out.mkdir(parents=True,exist_ok=True)
    # One atomic bundle keeps report and inference parameters on the same training version.
    artifact={'report':report,'models':bundle}
    atomic_json(out/'exit-trial.json',artifact)
    archive=out/'exit-trials'/('trial-'+utcnow().strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    atomic_json(archive,artifact)
    (out/'exit-trial-windows.jsonl').write_text(''.join(json.dumps({'kind':h,**r})+'\n' for h,rows in ranges.items() for r in rows))
    (out/'exit-trial-state.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in prior_days))
    (out/'exit-trial-partial.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in exits))
    return report


def load_bundle(directory=DATA):
    path=Path(directory)/'research'/'exit-trial.json'
    return json.loads(path.read_text()) if path.exists() else {'report':{'status':'not_run','range_models':{},'game_state':{},'partial_exits':{}},'models':{}}


def exit_trial_status(directory=DATA):return load_bundle(directory)['report']


def infer_band(model,x,current_bid):
    if len(x)!=len(model['means']) or not all(math.isfinite(v) for v in x):return None,'invalid_inputs'
    for value,low,high in zip(x,model['feature_min'],model['feature_max']):
        allowance=max(.02,(high-low)*.25)
        if not low-allowance<=value<=high+allowance:return None,'outside_training_support'
    shift=linear(model,x)
    clip=lambda value:max(0.,min(1.,value))
    low=clip(current_bid+shift+model['residual_low']);high=clip(current_bid+shift+model['residual_high'])
    center=clip(current_bid+shift+model.get('point_offset',0))
    return {'low':low,'center':max(low,min(high,center)),'high':high},None


def saved_prior(directory,game,now):
    import sqlite3,zlib
    path=Path(directory)/'recordings.sqlite3'
    if not path.exists():return .5,True
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        cutoff=min(now,instant(game['tipoff'])).isoformat()
        row=db.execute('''SELECT p.body FROM forecasts f JOIN payloads p ON p.hash=f.payload_hash
            WHERE f.game_id=? AND f.kind='issued_pregame' AND f.recorded_at<=? AND f.as_of<=?
            ORDER BY f.recorded_at DESC,f.id DESC LIMIT 1''',(game['game_id'],cutoff,cutoff)).fetchone()
        if row:
            raw=json.loads(zlib.decompress(row[0]));p=(raw.get('prediction') or {}).get('home_win_probability')
            if raw.get('home')==game['home'] and raw.get('away')==game['away'] and isinstance(p,(int,float)) and 0<p<1:return p,False
        return .5,True
    finally:db.close()


def live_research(snapshot,quotes,directory,now,bundle=None):
    """JSON linear inference only. No scikit-learn, fitting, orders, or active TP changes."""
    from .dynamic_exits import load_samples
    from .quotes import contract_matches_game
    bundle=load_bundle(directory) if bundle is None else bundle;models=bundle.get('models',{});result={}
    if not models.get('range_models') and not models.get('game_state_model'):return result
    trained=models.get('trained_at')
    if not trained or not 0<=(now-instant(trained)).total_seconds()<=7*86400:return result
    for game in snapshot.get('games',[]):
        eligible=[q for q in quotes.values() if q['game_id']==game['game_id'] and q.get('yes_team')==game['home'] and contract_matches_game(q['ticker'],q.get('yes_team'),game)]
        if not eligible:continue
        quote=max(eligible,key=lambda q:q['observed_at']);value={'status':'unavailable','routes':{},'game_state':None,
            'reason':'Needs fresh regulation-game scores and full 10-contract depth.','trained_at':trained}
        result[game['game_id']]=value
        if (not quote.get('book') or quote.get('multiplier') is None or quote.get('status') not in ('active','open')
            or quote.get('score_state')!='in' or not 0<=(now-instant(quote['observed_at'])).total_seconds()<=30):continue
        for side in ('yes','no'):
            p={'ticker':quote['ticker'],'side':side,'quantity':10}
            samples=load_samples(directory,p,min(now,instant(quote['observed_at'])),quote['multiplier'])
            if not samples or samples[-1]['at']!=quote['observed_at']:continue
            last=samples[-1]
            if last['state']!='in' or last['period'] not in range(1,5) or last.get('clock') is None or last.get('score_age') is None or last['score_age']>30:continue
            # Require recent consecutive history, not an implicit momentum=0 across a feed gap.
            segment=[last]
            for previous in reversed(samples[:-1]):
                delta=(instant(segment[-1]['at'])-instant(previous['at'])).total_seconds()
                if delta>30:break
                if delta>0:segment.append(previous)
            segment.reverse()
            if len(segment)<6 or (instant(last['at'])-instant(segment[0]['at'])).total_seconds()<60:
                value['reason']='Collecting one minute of continuous full-depth observations.';continue
            x=range_features(segment);bands={}
            for horizon,model in models.get('range_models',{}).items():
                band,error=infer_band(model,x,last['bid'])
                bands[horizon]={'status':'abstain','reason':error} if error else {'status':'experimental',**band}
            value['routes'][side]={'ticker':quote['ticker'],'side':side,'yes_team':game['home'],
                'outcome_team':game['home'] if side=='yes' else game['away'],'as_of':last['at'],'current_bid':last['bid'],'bands':bands}
            value.update(status='experimental',reason='Experimental bid ranges; future depth and limit fills are not guaranteed. Active TP/SL are unchanged.')
            if side=='yes' and models.get('game_state_model'):
                prior,missing=saved_prior(directory,game,now)
                sx=state_features(last['margin'],(4-last['period'])*720+last['clock'],prior)
                model=models['game_state_model']
                supported=all(lo-max(.02,(hi-lo)*.25)<=v<=hi+max(.02,(hi-lo)*.25) for v,lo,hi in zip(sx,model['feature_min'],model['feature_max']))
                value['game_state']={'status':'experimental' if supported else 'abstain','home_win_probability':probability(model,sx) if supported else None,
                    'as_of':last['at'],'prior_missing':missing,'reason':'Separate winner baseline, not scalp success probability.' if supported else 'Outside training support.'}
    return result


def exit_scenarios(position,quote,forecast,config,now):
    """Compare proceeds for the actual position; future values remain labeled scenarios."""
    from .markets import sweep
    from .risk import fee_at
    q=position['quantity'];half=q//2;remaining=q-half;result={'status':'unavailable','options':[],
        'basis':'Sell now uses recorded bids. Future values use an experimental 10-contract proxy at 3 minutes; no future fill or profit is guaranteed. Losses can exceed the displayed error band.'}
    if not quote or not quote.get('book') or quote.get('multiplier') is None or quote.get('status') not in ('active','open') or not -5<=(now-instant(quote['observed_at'])).total_seconds()<=30:return result
    multiplier=quote['multiplier'];levels=quote['book'][position['side']+'_bids'];full=sweep(levels,q,multiplier)
    if not full:return result
    cost=position['entry_cost'];slip=config['slippage']
    def proceeds(fill,quantity):return fill['price_cost']-fill['fee_upper_estimate']-quantity*slip
    net=proceeds(full,q)
    result['options'].append({'action':'sell_all_now','contracts_now':q,'net_proceeds_now':net,'net_pnl_now':net-cost,'remaining':0})
    part=sweep(levels,half,multiplier) if half else None
    if part:result['options'].append({'action':'sell_half_now','contracts_now':half,'net_proceeds_now':proceeds(part,half),
        'remaining':remaining,'remaining_entry_cost':cost*remaining/q,'note':'Future P&L is withheld until a matching supported bid band is available.'})
    result['status']='current_quotes_only'
    if not forecast or q>10:
        result['reason']='Future bands support at most 10 contracts; no larger-position extrapolation.' if q>10 else 'No supported 3-minute band for this position.'
        return result
    route=next((r for r in forecast.get('routes',{}).values() if r['ticker']==position['ticker'] and r['side']==position['side']),None)
    band=(route or {}).get('bands',{}).get('180')
    if not band or band.get('status')!='experimental' or not 0<=(now-instant(route['as_of'])).total_seconds()<=30:
        result['reason']='No supported recent 3-minute band for this route.';return result
    def future_proceeds(n,price):return n*price-fee_at(price,n,multiplier)-n*slip
    for row in result['options']:
        if row['action']=='sell_half_now':
            row['net_pnl_scenario']={key:row['net_proceeds_now']+future_proceeds(remaining,band[key])-cost for key in ('low','center','high')}
            row['note']='Half sold now, remainder modeled at the 3-minute bid band.'
    result['options'].append({'action':'hold_all_3m','contracts_now':0,'remaining':q,
        'net_pnl_scenario':{key:future_proceeds(q,band[key])-cost for key in ('low','center','high')}})
    result.update(status='experimental',forecast_as_of=route['as_of'])
    return result
