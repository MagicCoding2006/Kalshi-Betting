"""Whole-contract metrics, frozen split protocol, and portable tree inference."""
import hashlib
import json
import math
import random
from collections import defaultdict


def sigmoid(x):return 1/(1+math.exp(-max(-40,min(40,x))))
def logit(p):return math.log(max(1e-6,min(1-1e-6,p))/(1-max(1e-6,min(1-1e-6,p))))


def split_contracts(rows):
    windows={}
    for r in rows:windows.setdefault(r['ticker'],{'start':r['ts'],'available':r['available']})
    names=sorted(windows,key=lambda t:windows[t]['start'])
    if len(names)<40:raise ValueError('Need at least 40 settled contracts; snapshots are not independent outcomes')
    a=int(len(names)*.6);b=int(len(names)*.8)
    calibration,test=names[a:b],names[b:]
    train=[n for n in names[:a] if windows[n]['available']<windows[calibration[0]]['start']]
    calibration=[n for n in calibration if windows[n]['available']<windows[test[0]]['start']]
    if min(len(train),len(calibration),len(test))<6:raise ValueError('Need six contracts in every purged split')
    return {'train':train,'calibration':calibration,'test':test}


def weights(rows):
    counts=defaultdict(int)
    for row in rows:counts[row['ticker']]+=1
    return [1/counts[r['ticker']] for r in rows]


def quality(rows,probabilities):
    w=weights(rows);total=sum(w);bins=[];per_contract=defaultdict(list)
    for row,p in zip(rows,probabilities):per_contract[row['ticker']].append((p-row['y'])**2)
    for i in range(10):
        ix=[j for j,p in enumerate(probabilities) if i/10<=p<(i+1)/10 or i==9 and p==1]
        n=sum(w[j] for j in ix)
        if n:bins.append({'bin':i,'predicted':sum(w[j]*probabilities[j] for j in ix)/n,'actual':sum(w[j]*rows[j]['y'] for j in ix)/n,'weight':n,'windows':len({rows[j]['ticker'] for j in ix})})
    brier=sum(weight*(p-r['y'])**2 for r,p,weight in zip(rows,probabilities,w))/total
    loss=-sum(weight*(r['y']*math.log(max(p,1e-9))+(1-r['y'])*math.log(max(1-p,1e-9))) for r,p,weight in zip(rows,probabilities,w))/total
    ece=sum(b['weight']*abs(b['predicted']-b['actual']) for b in bins)/total
    slices=[]
    for lo,hi in [(60,180),(180,420),(420,900)]:
        ix=[i for i,r in enumerate(rows) if lo<=r['x']['seconds_left']<hi]
        if ix:slices.append({'seconds':[lo,hi],'brier':sum(w[i]*(probabilities[i]-rows[i]['y'])**2 for i in ix)/sum(w[i] for i in ix),'windows':len({rows[i]['ticker'] for i in ix})})
    return {'brier':brier,'log_loss':loss,'ece':ece,'reliability':bins,'windows':len(per_contract),'observations':len(rows),'by_time_remaining':slices}


def paired_interval(rows,predicted,baseline):
    diffs=defaultdict(list)
    for r,p,b in zip(rows,predicted,baseline):diffs[r['ticker']].append((p-r['y'])**2-(b-r['y'])**2)
    values=[sum(v)/len(v) for v in diffs.values()];rng=random.Random(42)
    boot=sorted(sum(rng.choice(values) for _ in values)/len(values) for _ in range(500))
    return {'mean_brier_difference':sum(values)/len(values),'contract_bootstrap_95':[boot[12],boot[487]],'independent_windows':len(values)}


def tree_value(node,x):
    while 'leaf_value' not in node:
        value=x.get(node['split_feature_name'])
        left=node.get('default_left',True) if value is None or isinstance(value,float) and math.isnan(value) else value<=float(node['threshold'])
        node=node['left_child'] if left else node['right_child']
    return node['leaf_value']


def portable_tree(dump):
    names=dump['feature_names']
    def assign(node):
        if 'split_feature' in node:
            node['split_feature_name']=names[node['split_feature']];assign(node['left_child']);assign(node['right_child'])
    for tree in dump['tree_info']:assign(tree['tree_structure'])
    return dump


def predict(artifact,x):
    if artifact['kind'] in ('market','market_blend','market_residual'):
        from .refinement import forecast
        return forecast(artifact,x)
    if artifact['kind']=='baseline':raw=logit(x['baseline'])
    else:
        raw=sum(tree_value(t['tree_structure'],x) for t in artifact['trees']['tree_info'])
        if artifact.get('offset'):raw+=logit(x['kalshi_mid'])
    a,b=artifact['calibration'];return sigmoid(a*raw+b)


def fingerprint(rows,split):
    material=[(r['ticker'],r['ts'],r['y']) for r in rows]
    return hashlib.sha256(json.dumps([material,split],sort_keys=True).encode()).hexdigest()[:20]
