"""Bounded factor search with seasonal validation, disjoint calibration and explicit holdouts."""
import hashlib
import json
import os
import pickle
import tempfile
from collections import Counter
from pathlib import Path

from .data import DATA, SCHEMA, atomic_json, instant, utcnow
from .features import FEATURE_NAMES, training_rows
from .context import CONTEXT_NAMES, GROUPS as CONTEXT_GROUPS
from .model import dependencies, scores, block_comparison, logit, span

ALL_NAMES = FEATURE_NAMES+CONTEXT_NAMES
GROUPS = {
    'strength': ['elo_diff','home_court'],
    'efficiency': [f for f in FEATURE_NAMES if f.startswith(('ortg_','drtg_'))],
    'shooting': [f for f in FEATURE_NAMES if f.startswith(('efg_','opp_efg_','three_rate_'))],
    'turnovers': [f for f in FEATURE_NAMES if f.startswith(('tov_rate_','opp_tov_rate_'))],
    'rebounding': [f for f in FEATURE_NAMES if f.startswith(('orb_rate_','opp_orb_rate_'))],
    'free_throws': [f for f in FEATURE_NAMES if f.startswith(('ftr_','opp_ftr_'))],
    'pace_history': [f for f in FEATURE_NAMES if f.startswith('possessions_') or f.endswith('_history_games')],
    'schedule': ['rest_diff','home_b2b','away_b2b','home_three_in_four','away_three_in_four','games_last7_diff'],
    **CONTEXT_GROUPS,
}


def matrix(rows,names=ALL_NAMES):
    np,_=dependencies()
    return np.array([[r['features'][f] for f in names] for r in rows],dtype=float)


def feature_weights(config):
    return [config['groups'].get(group,0) for f in ALL_NAMES for group,columns in GROUPS.items() if f in columns]


def split_season(rows,season):
    fit=[r for r in rows if r['season']<season-1]
    cal=[r for r in rows if r['season']==season-1]
    test=[r for r in rows if r['season']==season]
    if min(len(fit),len(cal),len(test))<200 or len(fit)<2000:
        raise ValueError('Insufficient seasonal fit/calibration/evaluation blocks for '+str(season))
    first_cal=min(instant(r['tipoff']) for r in cal);first_test=min(instant(r['tipoff']) for r in test)
    fit=[r for r in fit if instant(r['label_available_at'])<first_cal]
    cal=[r for r in cal if instant(r['label_available_at'])<first_test]
    return fit,cal,test


def fit_weighted(fit,cal,config):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    x=matrix(fit);scaler=StandardScaler().fit(x)
    weights=np.array(feature_weights(config),dtype=float)
    # Weights AFTER standardization change regularization emphasis. Applying
    # weights before StandardScaler would cancel the requested weighting.
    model=LogisticRegression(C=config['C'],max_iter=1200).fit(scaler.transform(x)*weights,[r['y'] for r in fit])
    raw=model.predict_proba(scaler.transform(matrix(cal))*weights)[:,1]
    calibration=LogisticRegression(C=1,max_iter=1200).fit(logit(raw),[r['y'] for r in cal])
    return {'scaler':scaler,'classifier':model,'feature_weights':weights,'calibrator':calibration}


def predict_weighted(model,rows):
    raw=model['classifier'].predict_proba(model['scaler'].transform(matrix(rows))*model['feature_weights'])[:,1]
    return model['calibrator'].predict_proba(logit(raw))[:,1]


def short_score(rows,probabilities):
    from sklearn.metrics import accuracy_score,log_loss,brier_score_loss
    y=[r['y'] for r in rows]
    return {'n':len(rows),'accuracy':float(accuracy_score(y,probabilities>=.5)),
            'log_loss':float(log_loss(y,probabilities,labels=[0,1])),
            'brier':float(brier_score_loss(y,probabilities))}


def validation_score(rows,config,seasons):
    import numpy as np
    predicted,evaluated,folds=[],[],[]
    for season in seasons:
        fit,cal,test=split_season(rows,season)
        model=fit_weighted(fit,cal,config);p=predict_weighted(model,test)
        predicted.extend(p);evaluated.extend(test)
        folds.append({'season':season,'fit':span(fit),'calibration':span(cal),'evaluation':span(test),**short_score(test,p)})
    return {**short_score(evaluated,np.array(predicted)),'folds':folds}


def select_winners(trials):
    """Select exclusively on validation metrics, excluding research-only trials."""
    eligible=[t for t in trials if t['config']['eligible']]
    probability=min(eligible,key=lambda t:(t['validation']['log_loss'],-t['validation']['accuracy']))
    accuracy=min(eligible,key=lambda t:(-t['validation']['accuracy'],t['validation']['log_loss']))
    return probability,accuracy


def optimize(dataset,history,directory=DATA,promote=True):
    np,sklearn=dependencies()
    from threadpoolctl import threadpool_limits
    directory=Path(directory);now=utcnow()
    games=[g for g in dataset['games'] if instant(g['available_at'])<=now]
    rows=training_rows(games)
    for row in rows:
        row['features'].update(history['rows'][row['game_id']]['features'])
    seasons=sorted({r['season'] for r in rows});current=max(seasons);test_season=current-1
    validation_seasons=[s for s in (test_season-3,test_season-2,test_season-1) if s in seasons]
    if len(validation_seasons)!=3:raise ValueError('Three prior validation seasons are required')
    expanded={g:1. for g in GROUPS};expanded['referees']=0.
    baseline={g:float(g not in CONTEXT_GROUPS) for g in GROUPS}
    configs=[]
    for label,groups in [('baseline',baseline),('expanded',expanded)]:
        for C in (.01,.1,1.):configs.append({'name':label+' C='+str(C),'C':C,'groups':dict(groups),'eligible':True})
    for group in GROUPS:
        if group=='referees':continue
        for weight in (0.,.5,2.):
            groups={**expanded,group:weight}
            configs.append({'name':group+' ×'+str(weight),'C':.1,'groups':groups,'eligible':True})
    configs.append({'name':'referees: reconstructed-history sensitivity','C':.1,'groups':{**expanded,'referees':1.},'eligible':False})
    trials=[];seen=set()
    status={'status':'running','started_at':now.isoformat(),'completed_trials':0,'planned_trials':len(configs)+3,
            'validation_seasons':validation_seasons,'test_season':test_season,'current_season':current}
    atomic_json(directory/'optimization-report.json',status)
    def evaluate(config):
        key=json.dumps({'C':config['C'],'groups':config['groups']},sort_keys=True)
        if key in seen:return None
        seen.add(key)
        score=validation_score(rows,config,validation_seasons)
        item={'config':config,'validation':score};trials.append(item)
        status.update(completed_trials=len(trials),latest_trial=config['name'],trials=trials)
        atomic_json(directory/'optimization-report.json',status)
        print('Factor trial '+str(len(trials))+': '+config['name']+' | accuracy '+str(round(score['accuracy'],4))+' | log loss '+str(round(score['log_loss'],5)),flush=True)
        return item
    with threadpool_limits(limits=1):
        for config in configs:evaluate(config)
        # Combine the strongest individual emphasis changes, rather than
        # pretending a one-factor-at-a-time search is a global optimum.
        expanded_reference=next(t for t in trials if t['config']['name']=='expanded C=0.1')
        improvements=[]
        for group in GROUPS:
            options=[t for t in trials if t['config']['name'].startswith(group+' ×')]
            if not options:continue
            best=min(options,key=lambda t:t['validation']['log_loss'])
            if best['validation']['log_loss']<expanded_reference['validation']['log_loss']:
                improvements.append((best['validation']['log_loss'],group,best['config']['groups'][group]))
        combined=dict(expanded)
        for _,group,weight in sorted(improvements)[:4]:combined[group]=weight
        for C in (.01,.1,1.):evaluate({'name':'combined emphasis C='+str(C),'C':C,'groups':combined,'eligible':True})
        probability_winner,accuracy_winner=select_winners(trials)
        baseline_trial=next(t for t in trials if t['config']['name']=='baseline C=0.1')
        fit,cal,test=split_season(rows,test_season)
        current_rows=[r for r in rows if r['season']==current]
        evaluated=test+current_rows
        eval_model=fit_weighted(fit,cal,probability_winner['config']);p=predict_weighted(eval_model,evaluated)
        baseline_model=fit_weighted(fit,cal,baseline_trial['config']);baseline_p=predict_weighted(baseline_model,evaluated)
        accuracy_model=fit_weighted(fit,cal,accuracy_winner['config']);accuracy_p=predict_weighted(accuracy_model,evaluated)
        # Coefficient and grouped permutation diagnostics are measured on the
        # later test, after selection, and cannot influence the chosen candidate.
        coefficients=eval_model['classifier'].coef_[0]*eval_model['feature_weights']
        total=float(np.abs(coefficients).sum()) or 1.
        coefficient_rows=[{'feature':name,'standardized_coefficient':float(value),'absolute_share':float(abs(value)/total)}
                          for name,value in zip(ALL_NAMES,coefficients)]
        group_rows=[];x=matrix(test);original=p[:len(test)];base_loss=short_score(test,original)['log_loss'];rng=np.random.default_rng(42)
        for group,columns in GROUPS.items():
            indices=[ALL_NAMES.index(c) for c in columns]
            drops=[]
            for repeat in range(5):
                permuted=x.copy()
                # Permute a whole feature family together within each calendar
                # month to preserve correlations inside that family and season.
                for month in sorted({r['date'][:7] for r in test}):
                    members=np.array([i for i,r in enumerate(test) if r['date'].startswith(month)])
                    shuffled=rng.permutation(members)
                    permuted[np.ix_(members,indices)]=x[np.ix_(shuffled,indices)]
                raw=eval_model['classifier'].predict_proba(eval_model['scaler'].transform(permuted)*eval_model['feature_weights'])[:,1]
                q=eval_model['calibrator'].predict_proba(logit(raw))[:,1]
                drops.append(short_score(test,q)['log_loss']-base_loss)
            ablated=next((t for t in trials if t['config']['name']==group+' ×0.0'),None)
            varying=int(sum(np.std(matrix(fit)[:,ALL_NAMES.index(c)])>1e-8 for c in columns))
            group_rows.append({'group':group,'features':columns,'selected_emphasis':probability_winner['config']['groups'][group],
                               'learnable_feature_count':varying,
                               'coefficient_share':sum(abs(float(coefficients[i])) for i in indices)/total,
                               'test_permutation_log_loss_increase':float(np.mean(drops)),
                               'test_permutation_std':float(np.std(drops)),
                               'validation_removal_accuracy_change':(ablated['validation']['accuracy']-expanded_reference['validation']['accuracy']) if ablated else None,
                               'validation_removal_log_loss_change':(ablated['validation']['log_loss']-expanded_reference['validation']['log_loss']) if ablated else None,
                               'status':'requires_timestamped_assignment_history' if group=='referees' else 'no_historical_variation' if varying==0 else 'tested'})
        test_metrics=short_score(test,p[:len(test)]);current_metrics=short_score(current_rows,p[len(test):]) if current_rows else None
        report={**status,'status':'complete','completed_at':utcnow().isoformat(),'dataset_fingerprint':dataset['fingerprint'],
                'feature_names':ALL_NAMES,'probability_winner':probability_winner,'accuracy_winner':accuracy_winner,
                'baseline_validation':baseline_trial['validation'],'test':{'season':test_season,'selected':test_metrics,
                    'baseline':short_score(test,baseline_p[:len(test)]),'accuracy_winner':short_score(test,accuracy_p[:len(test)]),
                    'paired_vs_baseline':block_comparison(test,p[:len(test)],baseline_p[:len(test)])},
                'current_season_diagnostic':{'season':current,'selected':current_metrics,
                    'baseline':short_score(current_rows,baseline_p[len(test):]) if current_rows else None},
                'groups':group_rows,'coefficients':sorted(coefficient_rows,key=lambda r:abs(r['standardized_coefficient']),reverse=True),
                'context_coverage':{'player_games':history['player_games'],'injury_report_games':sum(r['features']['home_injury_report_missing']==0 or r['features']['away_injury_report_missing']==0 for r in rows),
                                    'confirmed_lineup_games':sum(r['features']['home_confirmed_lineup'] or r['features']['away_confirmed_lineup'] for r in rows)},
                'limitations':['This is a bounded search, not proof of a global best factor combination.',
                    'The current season and earlier broad historical summaries have already been reviewed; these are out-of-time diagnostics, not a new untouched prospective test.',
                    'Selection uses only the three validation seasons; later test outcomes never select weights or feature groups.',
                    'Coefficient shares and permutation importance describe model reliance, not causal effects or stable optimal weights.',
                    'Historical injury and confirmed-lineup labels are not inferred from final participation. Their dedicated coefficients need timestamped history.',
                    'Referee final-record identities require an unverified historical publication assumption; that sensitivity trial is ineligible for promotion.',
                    'Injuries and confirmed starters can change projected rotation features whose coefficients are learned from past games. Uncertain availability is not assigned an invented probability.',
                    'Prospective holdout collection starts now; no market-price or trading-profit claim is made.'],
                'active_objective':'validation log loss; highest validation accuracy is reported separately'}
        if promote:
            days=sorted({r['date'] for r in rows});cut=days[-60]
            deploy_cal=[r for r in rows if r['date']>=cut];first_tip=min(instant(r['tipoff']) for r in deploy_cal)
            deploy_fit=[r for r in rows if r['date']<cut and instant(r['label_available_at'])<first_tip]
            deploy=fit_weighted(deploy_fit,deploy_cal,probability_winner['config'])
            identity=hashlib.sha256(json.dumps({'dataset':dataset['fingerprint'],'config':probability_winner['config'],'time':now.isoformat()},sort_keys=True).encode()).hexdigest()[:16]
            model_report={'schema':SCHEMA,'model_id':identity,'created_at':now.isoformat(),'feature_names':ALL_NAMES,
                'kind':'weighted_logistic','dataset_fingerprint':dataset['fingerprint'],
                'source':'Real team/player box scores; past-only context; timestamped availability when available',
                'split':{'fit':span(fit),'selection':span([r for r in rows if r['season'] in validation_seasons]),'calibration':span(cal),'test':span(evaluated)},
                'held_out':{'model':scores([r['y'] for r in evaluated],p),'constant_home_rate':scores([r['y'] for r in evaluated],np.full(len(evaluated),np.mean([r['y'] for r in fit]))),
                            'elo':scores([r['y'] for r in evaluated],np.array([r['elo_probability'] for r in evaluated]))},
                'deployment':{'refit':True,'fit':span(deploy_fit),'calibration':span(deploy_cal),'labels_available_through':max(r['label_available_at'] for r in rows),
                              'performance_estimate':'Earlier frozen checkpoint; see optimization report for seasonal validation and test.'},
                'versions':{'numpy':np.__version__,'sklearn':sklearn.__version__},'configuration':probability_winner['config'],
                'availability_policy':dataset['availability_policy'],'limitations':report['limitations']}
            bundle={'schema':SCHEMA,'kind':'weighted_logistic','feature_names':ALL_NAMES,'report':model_report,**deploy}
            existing=directory/'model.pkl'
            if existing.exists() and not (directory/'model-v1.pkl').exists():
                (directory/'model-v1.pkl').write_bytes(existing.read_bytes())
                (directory/'model-v1-report.json').write_text((directory/'model-report.json').read_text())
            with tempfile.NamedTemporaryFile(dir=directory,delete=False) as stream:
                pickle.dump(bundle,stream,protocol=pickle.HIGHEST_PROTOCOL);temporary=stream.name
            os.replace(temporary,existing);atomic_json(directory/'model-report.json',model_report)
            atomic_json(directory/'holdout-predictions.json',{'model_id':identity,'rows':[
                {'game_id':r['game_id'],'date':r['date'],'y':r['y'],'probability':float(q),'elo_probability':r['elo_probability']} for r,q in zip(evaluated,p)]})
            report.update(promoted_model_id=identity,promoted_config=probability_winner['config'])
        atomic_json(directory/'optimization-report.json',report)
        return report
