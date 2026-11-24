"""Isolated LightGBM/calibration training and Parquet/DuckDB export worker."""
import argparse
import json
import math
import time
from pathlib import Path

from .features import BASE,FLOW,KALSHI
from .evaluation import logit,sigmoid,weights,split_contracts,quality,paired_interval,portable_tree,predict,fingerprint
from .execution import replay
from .store import Store


def train(rows,store,directory):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    import lightgbm as lgb
    complete=[r for r in rows if r['x'].get('flow_ready')]
    flow_mode=len({r['ticker'] for r in complete})>=40
    cohort=complete if flow_mode else rows
    split=split_contracts(cohort)
    folds={name:[r for r in cohort if r['ticker'] in set(tickers)] for name,tickers in split.items()}
    if len({r['y'] for r in folds['train']})<2 or len({r['y'] for r in folds['calibration']})<2:
        raise ValueError('Training and calibration each need both outcomes across distinct contracts')
    holdout_id=fingerprint(cohort,split);report={'created':time.time(),'status':'complete','holdout_id':holdout_id,'split':{k:len(v) for k,v in split.items()},
        'contract_membership':split,'cohort':'Matched fresh order-flow cohort' if flow_mode else 'Recorded proxy-price / Kalshi-only cohort; no retrospective exchange-flow imputation',
        'experiments':[],'model_selection':'Fixed parameters; no test-set tuning. Calibration uses its own later whole-contract fold.',
        'label_timing':sorted({r['market'].get('label_timing','unknown') for r in cohort}),
        'limits':'Proxy-price inputs cannot reconstruct BRTI settlement. Small holdouts and one market regime do not establish a trading edge.'}
    artifacts={}
    for name,columns in [('A',BASE),('B',BASE+FLOW),('C',BASE+(FLOW if flow_mode else [])+KALSHI)]:
        if name=='B' and not flow_mode:
            report['experiments'].append({'name':'B','status':'collecting','reason':f'Fresh spot-flow contracts: {len({r["ticker"] for r in complete})}/40. Historical quote-only records cannot supply missing trade flow.'});continue
        def matrix(group):return np.array([[r['x'].get(c) if r['x'].get(c) is not None else np.nan for c in columns] for r in group],dtype=float)
        if name=='A':
            raw=np.array([logit(r['x']['baseline']) for r in folds['calibration']]);artifact={'kind':'baseline'};importance=[]
        else:
            fit=folds['train'];model=lgb.LGBMClassifier(n_estimators=120,num_leaves=7,max_depth=3,min_child_samples=50,learning_rate=.035,
                reg_lambda=2.,verbosity=-1,n_jobs=1,random_state=42,boost_from_average=name!='C')
            params={}
            if name=='C':params['init_score']=np.array([logit(r['x']['kalshi_mid']) for r in fit])
            model.fit(matrix(fit),np.array([r['y'] for r in fit]),sample_weight=np.array(weights(fit)),feature_name=columns,**params)
            raw=model.booster_.predict(matrix(folds['calibration']),raw_score=True)
            if name=='C':raw+=np.array([logit(r['x']['kalshi_mid']) for r in folds['calibration']])
            artifact={'kind':'lightgbm','trees':portable_tree(model.booster_.dump_model()),'offset':name=='C'}
            importance=sorted([{'feature':c,'gain':float(g)} for c,g in zip(columns,model.booster_.feature_importance(importance_type='gain'))],key=lambda r:r['gain'],reverse=True)
        calibration=LogisticRegression(C=1.,solver='lbfgs',random_state=42)
        calibration.fit(raw.reshape(-1,1),np.array([r['y'] for r in folds['calibration']]),sample_weight=np.array(weights(folds['calibration'])))
        artifact.update(calibration=[float(calibration.coef_[0,0]),float(calibration.intercept_[0])],features=columns,
                        reference_mode=sorted({r['x'].get('reference_source','proxy') for r in cohort}),holdout_id=holdout_id)
        test=folds['test'];pred=[predict(artifact,r['x']) for r in test];market=[r['x']['kalshi_mid'] for r in test]
        if name!='A':
            direct=model.booster_.predict(matrix(test),raw_score=True)
            if name=='C':direct+=np.array([logit(r['x']['kalshi_mid']) for r in test])
            expected=[sigmoid(artifact['calibration'][0]*v+artifact['calibration'][1]) for v in direct]
            if max(abs(a-b) for a,b in zip(pred,expected))>1e-7:raise ValueError('Portable tree inference does not match LightGBM')
        result={'name':name,'status':'complete','description':{'A':'Calibrated averaging-aware statistical baseline','B':'LightGBM baseline + spot order flow','C':'Residual correction to Kalshi prices' if flow_mode else 'Residual correction: Kalshi-only ablation (B pending)'}[name],
                'quality':quality(test,pred),'market_quality':quality(test,market),'vs_market':paired_interval(test,pred,market),
                'feature_importance':importance[:15],'execution':replay(test,artifact,store.quote_after),
                'predictions':[{'ticker':r['ticker'],'ts':r['ts'],'p':p,'market':r['x']['kalshi_mid'],'y':r['y']} for r,p in zip(test,pred)][-120:]}
        report['experiments'].append(result);artifacts[name]=artifact
    report['sequence_gate']='Blocked: require fresh-flow B to improve on A across at least 30 independent test contracts with a contract-bootstrap interval below zero.'
    if 'B' in artifacts:
        test=folds['test'];a=[predict(artifacts['A'],r['x']) for r in test];b=[predict(artifacts['B'],r['x']) for r in test]
        comparison=paired_interval(test,b,a);report['B_vs_A']=comparison
        if comparison['independent_windows']>=30 and comparison['contract_bootstrap_95'][1]<0:report['sequence_gate']='Eligible for a separate sequence-model experiment; no automatic promotion to trading.'
    path=Path(directory)/'models.json';temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'created':time.time(),'artifacts':artifacts},allow_nan=False));temporary.replace(path)
    return report


def export(store,directory):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import duckdb
    directory=Path(directory).resolve()
    rows=store.dataset();root=directory/'lake';root.mkdir(exist_ok=True)
    if not rows:raise ValueError('No labeled outcome features to export')
    flat=[{'ticker':r['ticker'],'received_ts':r['ts'],'outcome':r['y'],'origin':r['origin'],**{k:v for k,v in r['x'].items() if not isinstance(v,(dict,list))}} for r in rows]
    columns=set().union(*(row.keys() for row in flat))
    flat=[{key:row.get(key) for key in sorted(columns)} for row in flat]
    from datetime import datetime,timezone
    grouped={}
    for row in flat:grouped.setdefault(datetime.fromtimestamp(row['received_ts'],timezone.utc).date().isoformat(),[]).append(row)
    paths=[]
    for date,values in grouped.items():
        folder=root/'features'/('date='+date)/'venue=joined'/'symbol=BTC';folder.mkdir(parents=True,exist_ok=True)
        path=folder/'features.parquet';pq.write_table(pa.Table.from_pylist(values),path);paths.append(str(path.relative_to(directory)))
    raw=store.db.execute('SELECT id,venue,kind,exchange_ts,received,payload FROM events ORDER BY id').fetchall()
    if raw:
        groups={}
        for event in raw:
            event=dict(event);date=datetime.fromtimestamp(event['received'],timezone.utc).date().isoformat();groups.setdefault((date,event['venue']),[]).append(event)
        for (date,venue),values in groups.items():
            folder=root/'raw'/('date='+date)/('venue='+venue)/'symbol=BTC';folder.mkdir(parents=True,exist_ok=True)
            pq.write_table(pa.Table.from_pylist(values),folder/'events.parquet')
    connection=duckdb.connect(str(root/'research.duckdb'))
    pattern=str(root/'features'/'**'/'*.parquet').replace("'","''")
    connection.execute("CREATE OR REPLACE VIEW outcome_features AS SELECT * FROM read_parquet('"+pattern+"',hive_partitioning=true,union_by_name=true)")
    count=connection.execute('SELECT COUNT(*) FROM outcome_features').fetchone()[0];connection.close()
    return {'status':'complete','created':time.time(),'exported_rows':count,'files':paths,'duckdb':'lake/research.duckdb'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True);parser.add_argument('--source');parser.add_argument('--kind',choices=['train','export','refine','swing','adaptive'],default='train');args=parser.parse_args()
    store=Store(args.directory)
    try:
        if args.kind=='adaptive':
            from .adaptive_swings import study
            report=study(args.source or str(Path(args.directory).parent/'btc.sqlite3.research.sqlite3'),args.directory)
        elif args.kind=='swing':
            from .swings import study
            report=study(args.source or str(Path(args.directory).parent/'btc.sqlite3.research.sqlite3'),args.directory)
        elif args.kind=='refine':
            from .refinement import refinement
            report=refinement(store)
        else:report=train(store.dataset(),store,args.directory) if args.kind=='train' else export(store,args.directory)
        store.report(report)
        print(json.dumps({'status':report['status'],'kind':args.kind}))
    finally:store.close()

if __name__=='__main__':main()
