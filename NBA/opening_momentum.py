"""Opening-six-minute EXIT-event experiments. Never promotes probabilities to Kelly."""
import json
import math
from collections import Counter
from pathlib import Path

from .data import DATA,atomic_json,instant,utcnow
from .dynamic_exits import advance
from .exit_forecast import partitions,weights,export_linear,probability,probability_metrics,average,quantile

POLICY={'quantity':10,'opening_game_seconds':360,'entry_spacing_seconds':120,
    'initial_target_moves':[.03,.05,.08],'initial_stop_move':.05,'horizons_seconds':[60,180,300],
    'exit_policies':['original_levels','moving_levels'],'execution_grace_seconds':30,'max_quote_gap_seconds':30,'slippage_per_leg':.005}
BASE=['entry_ask','sell_bid','spread','target_distance','stop_distance','horizon_fraction','opening_elapsed_fraction','move_p90','moving_policy']
MOMENTUM=['bid_change_10s','bid_change_30s','bid_change_60s','score_margin','margin_change_60s','points_change_60s','score_age_seconds','book_imbalance_top3','imbalance_change_30s']
CONTEXT=['pregame_logit','out_minutes_diff','uncertain_minutes_diff','rotation_impact_diff','rest_diff','travel_1000km_diff','timezone_shift_diff','prior_missing','context_missing']
FEATURE_NAMES=BASE+MOMENTUM+CONTEXT
FAMILIES={'price_and_plan':len(BASE),'plus_score_momentum':len(BASE)+len(MOMENTUM),'plus_roster_travel':len(FEATURE_NAMES)}


def market_sample(p,side):
    fills=p['fills'][side]['10'];bid=fills['sell'];ask=fills['buy']
    if not bid or not ask:return None
    sign=1 if side=='yes' else -1
    return {'at':p['at'],'bid':bid['vwap'],'spread':ask['vwap']-bid['vwap'],'state':p['state'],
        'period':p['period'],'clock':p['clock'],'margin':sign*(p['home_score']-p['away_score']),
        'total_points':p['home_score']+p['away_score'],'score_age':p['score_age'],'imbalance':p.get('liquidity',{}).get(side,0.)}


def feature_row(points,index,side,prior,target,stop,horizon,moving):
    p=points[index];sample=market_sample(p,side);now=p['time'];history=[]
    for old in points[max(0,index-40):index+1]:
        value=market_sample(old,side)
        if value and 0<=now-old['time']<=90:history.append((old['time'],value))
    def lag(seconds):
        candidates=[(t,v) for t,v in history if seconds<=now-t<=seconds+30]
        return max(candidates,key=lambda pair:pair[0])[1] if candidates else sample
    moves=[abs(b['bid']-a['bid']) for (ta,a),(tb,b) in zip(history,history[1:]) if 0<tb-ta<=30]
    available=prior if prior and instant(prior['at'])<=instant(p['at']) else None
    features=(available or {}).get('features',{});sign=1 if side=='yes' else -1
    prob=(available or {}).get('probability',.5);prob=prob if side=='yes' else 1-prob
    prob=max(.001,min(.999,prob));past=lag(60)
    entry=p['fills'][side]['10']['buy']['vwap'];bid=sample['bid']
    x=[entry,bid,sample['spread'],target-bid,bid-stop,horizon/300,(720-p['clock'])/360,quantile(moves,.9) or 0.,int(moving)]
    x += [bid-lag(10)['bid'],bid-lag(30)['bid'],bid-past['bid'],sample['margin'],sample['margin']-past['margin'],
          sample['total_points']-past['total_points'],p['score_age'],sample['imbalance'],sample['imbalance']-lag(30)['imbalance']]
    x += [math.log(prob/(1-prob))]+[sign*features.get(name,0.) for name in ('out_minutes_diff','uncertain_minutes_diff','rotation_impact_diff','rest_diff')]
    x += [sign*features.get('travel_km_diff',0.)/1000,sign*(features.get('home_timezone_shift',0.)-features.get('away_timezone_shift',0.)),
          int(available is None),int(not features)]
    return x


def simulate(points,index,side,target,stop,horizon,moving):
    """Observed trigger followed by a later full-depth fill; missing windows censor."""
    from .risk import fee_at
    p=points[index];q=10;slip=.005;entry=p['fills'][side]['10']['buy'];multiplier=p.get('multiplier')
    if multiplier is None:return None,'unknown_fees'
    cost=entry['price_cost']+entry['fee_upper_estimate']+q*slip
    if q*target-fee_at(target,q,multiplier)-q*slip<=cost:return None,'target_cannot_cover_costs'
    deadline=p['time']+horizon;pending=None;target_at_trigger=target;history=[market_sample(p,side)]
    position={'ticker':'research','side':side,'quantity':q,'entry_price':entry['vwap'],'entry_fees':entry['fee_upper_estimate']+q*slip,
        'opened_at':p['at'],'stop_bid':stop,'target_bid':target}
    break_even=next((c/100 for c in range(1,100) if q*c/100-fee_at(c/100,q,multiplier)-q*slip>=cost),None)
    for j in range(index+1,len(points)):
        current=points[j]
        if current['time']-points[j-1]['time']>30:return None,'recording_gap'
        sample=market_sample(current,side)
        if sample is None:return None,'depth_gap'
        fill=current['fills'][side]['10']['sell']
        if pending:
            if current['time']>deadline+30:return None,'late_execution'
            net=fill['price_cost']-fill['fee_upper_estimate']-q*slip-cost
            executable_target=bool(pending=='target' and fill['vwap']-slip>=target_at_trigger-1e-9 and net>0)
            return {'label_at':current['at'],'y':int(executable_target),'target_touched':int(pending=='target'),
                'outcome':pending if pending!='target' or executable_target else 'target_slipped',
                'net_pnl':net,'exit_bid':fill['vwap'],'target_at_trigger':target_at_trigger},None
        if current['time']>deadline+30:return None,'late_observation'
        if current['time']>deadline:
            pending='time';continue
        history.append(sample)
        if moving:
            decision=advance(position,{'slippage':slip},instant(current['at']),history,break_even)
            position['dynamic_exit_state']=decision['state'];pending=decision['state'].get('trigger')
            target_at_trigger=decision['active_target']
        elif sample['bid']<=stop+1e-9:pending='stop'
        elif sample['bid']>=target-1e-9:pending='target'
        if not pending and current['time']>=deadline:pending='time'
    return None,'incomplete_window'


def build_opening_rows(paths,priors):
    rows=[];excluded=Counter();coverage={}
    for gid,points in paths.items():
        opening=[(i,p) for i,p in enumerate(points) if p['period']==1 and 360<=p['clock']<=720]
        if not opening:continue
        coverage[gid]={'first_observed_game_second':720-opening[0][1]['clock'],'last_observed_game_second':720-opening[-1][1]['clock'],
            'first_30_seconds_observed':opening[0][1]['clock']>=690,'observations':len(opening)}
        last=-math.inf
        for index,p in opening:
            if p['time']-last<120:continue
            last=p['time']
            for side in ('yes','no'):
                sample=market_sample(p,side)
                if not sample:excluded['entry_depth']+=1;continue
                entry=p['fills'][side]['10']['buy']['vwap'];stop=max(.01,entry-.05)
                if sample['bid']<=stop:excluded['entry_already_at_stop']+=1;continue
                for move in POLICY['initial_target_moves']:
                    target=entry+move
                    if target>=1:excluded['target_above_payout']+=1;continue
                    for horizon in POLICY['horizons_seconds']:
                        for moving in (False,True):
                            result,error=simulate(points,index,side,target,stop,horizon,moving)
                            if error:excluded[error]+=1;continue
                            x=feature_row(points,index,side,priors.get(gid),target,stop,horizon,moving)
                            rows.append({'game_id':gid,'day':p['day'],'at':p['at'],'side':side,'policy':'moving_levels' if moving else 'original_levels',
                                'target_move':move,'horizon_seconds':horizon,'opening_game_second':720-p['clock'],'x':x,**result})
    return rows,dict(excluded),coverage


def summarize(rows):
    if not rows:return {'plans':0,'games':0,'successful_exit_rate':None,'mean_net_pnl':None}
    return {'plans':len(rows),'unique_entry_observations':len({(r['game_id'],r['at'],r['side']) for r in rows}),'games':len({r['game_id'] for r in rows}),'successful_exit_rate':average([r['y'] for r in rows],rows),
        'target_touch_rate':average([r['target_touched'] for r in rows],rows),'mean_net_pnl':average([r['net_pnl'] for r in rows],rows),
        'outcomes':dict(Counter(r['outcome'] for r in rows))}


def game_fit_weights(rows):
    counts=Counter(r['game_id'] for r in rows)
    return [1/counts[r['game_id']] for r in rows]


def evaluate_opening(rows):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    train,cal,test,split=partitions(rows)
    test_plans={}
    for policy in POLICY['exit_policies']:
        for move in POLICY['initial_target_moves']:
            for horizon in POLICY['horizons_seconds']:
                values=[r for r in test if r['policy']==policy and r['target_move']==move and r['horizon_seconds']==horizon]
                test_plans[f'{policy}:{move:.2f}:{horizon}']=summarize(values)
    report={'status':'insufficient_games','split':split,'families':{},'test_summary':summarize(test),'test_plan_summaries':test_plans};artifacts={}
    if (not split or split['train_games']<3 or split['calibration_games']<2 or split['test_games']<3
        or len({r['y'] for r in train})<2):return report,artifacts
    baseline_rate=average([r['y'] for r in train],train)
    report['constant_training_rate']=probability_metrics(test,[baseline_rate]*len(test))
    for name,n in FAMILIES.items():
        tr=[{**r,'x':r['x'][:n]} for r in train];ca=[{**r,'x':r['x'][:n]} for r in cal];te=[{**r,'x':r['x'][:n]} for r in test]
        scaler=StandardScaler().fit([r['x'] for r in tr],sample_weight=game_fit_weights(tr))
        fit=LogisticRegression(C=.1,max_iter=1500).fit(scaler.transform([r['x'] for r in tr]),[r['y'] for r in tr],sample_weight=game_fit_weights(tr))
        model=export_linear(fit,scaler,tr)
        calibrated=False
        if len({r['y'] for r in ca})==2:
            from .exit_forecast import linear
            calibration=LogisticRegression(C=.1,max_iter=1500).fit([[linear(model,r['x'])] for r in ca],[r['y'] for r in ca],sample_weight=game_fit_weights(ca))
            model['calibration']={'coefficient':float(calibration.coef_[0,0]),'intercept':float(calibration.intercept_[0])};calibrated=True
        predictions=[probability(model,r['x']) for r in te];metrics=probability_metrics(te,predictions)
        bins=[]
        for low in (0,.2,.4,.6,.8):
            selected=[(r,p) for r,p in zip(te,predictions) if low<=p<low+.2 or low==.8 and p==1]
            if selected:
                sample=[r for r,_ in selected];bins.append({'from':low,'to':low+.2,'plans':len(sample),'games':len({r['game_id'] for r in sample}),
                    'mean_probability':average([p for _,p in selected],sample),'observed_success':average([r['y'] for r in sample],sample)})
        report['families'][name]={**metrics,'calibration_fitted':calibrated,'reliability':bins,
            'feature_names':FEATURE_NAMES[:n],'standardized_coefficients':dict(zip(FEATURE_NAMES[:n],model['coefficients']))}
        artifacts[name]=model
    baseline=report['families']['price_and_plan']
    for name,metrics in report['families'].items():metrics['improves_price_baseline']=metrics['brier']<baseline['brier'] and metrics['log_loss']<baseline['log_loss']
    report['evidence_note']='Tiny metric differences on very few independent games do not establish an actionable predictive improvement.'
    report.update(status='experimental',note='All three families predefined; later test games do not select a serving model. Probabilities refer to successful next-quote exits under the complete stated policy, not winners or guaranteed limit fills.')
    return report,artifacts


def run_opening_trial(directory=DATA):
    from .exit_data import load_paths
    paths,priors,_,source_exclusions=load_paths(directory);rows,excluded,coverage=build_opening_rows(paths,priors)
    evaluation,models=evaluate_opening(rows)
    slices={}
    for policy in POLICY['exit_policies']:
        for move in POLICY['initial_target_moves']:
            for horizon in POLICY['horizons_seconds']:
                values=[r for r in rows if r['policy']==policy and r['target_move']==move and r['horizon_seconds']==horizon]
                slices[f'{policy}:{move:.2f}:{horizon}']=summarize(values)
    timing={label:summarize([r for r in rows if low<=r['opening_game_second']<high]) for label,low,high in [('first_30s',0,31),('31_to_120s',31,120),('120_to_360s',120,361)]}
    report={'schema':1,'entry_timing_diagnostics':timing,'generated_at':utcnow().isoformat(),'status':'research_only','promoted':False,'policy':POLICY,
        'objective':'Observed net-positive executable target exit before stop/time, NOT final-game winner.',
        'summary':summarize(rows),'coverage':{'games':len(coverage),'eligible_first_30_seconds_games':len({r['game_id'] for r in rows if r['opening_game_second']<=30}),'first_30_seconds_games':sum(r['first_30_seconds_observed'] for r in coverage.values()),'per_game':coverage},
        'evaluation':evaluation,'plan_summaries':slices,'exclusions':{'windows':excluded,'observations':source_exclusions},
        'limitations':['Small partial-opening archive. Many plan labels from one game are not independent games.',
            'Observed asks plus entry slippage simulate entries; later observed full-depth bids simulate exits. No real orders or guaranteed queue fills.',
            'Target touch is separate from a profitable executable target fill. Data gaps censor, never become automatic losses.',
            'Moving target/stop labels replay the actual smoothed rule, not a fixed-target probability substituted into a moving policy.',
            'Chronological partitions are exploratory diagnostics on an already researched archive, not new prospective proof.',
            'The target must be observed by the horizon; next-quote execution has a 30-second grace window. A touch alone is not success.',
            'Pre-tipoff directional momentum, possession, shot quality and player mental state are not established by this study.',
            'Roster/travel inputs come only from an archived forecast already received before entry; missing context is flagged.',
            'Different target plans can have different cost-feasible entry cohorts; raw plan rates are not causal policy comparisons.',
            'No model promotion, automatic sizing or replacement of active user exit rules.']}
    out=Path(directory)/'research';out.mkdir(parents=True,exist_ok=True)
    bundle={'report':report,'models':models};atomic_json(out/'opening-momentum.json',bundle)
    atomic_json(out/'opening-trials'/('trial-'+utcnow().strftime('%Y%m%dT%H%M%S%fZ')+'.json'),bundle)
    (out/'opening-momentum-windows.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return report


def opening_status(directory=DATA):
    path=Path(directory)/'research'/'opening-momentum.json'
    return json.loads(path.read_text())['report'] if path.exists() else {'status':'not_run','summary':{},'coverage':{},'evaluation':{},'promoted':False}
