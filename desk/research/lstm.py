"""Small CPU LSTM experiment with causal range features and purged time splits."""
import math
import threading
from .history import RangeContext
from .markov import pairs,metrics,execution_trade

SEQUENCE=20
FEATURES=['yes_mid','spread','imbalance','microprice_gap','log_bid_size','log_ask_size',
          'last_quote_change','fraction_window_left','spot_strike_distance','trailing_range_position']
TRAIN_LOCK=threading.Lock()


def features(rows,candles):
    context=RangeContext(candles);result=[]
    for row in rows:
        span=context.at(row['ts'],row.get('spot'))
        valid=(row.get('mid') is not None and span and span['coverage_hours']>=48 and row.get('strike')
               and row.get('spot_time') and 0<=row['ts']-row['spot_time']<=15)
        if not valid:result.append(None);continue
        result.append([row['mid'],row['spread'],row['imbalance'],row['microprice']-row['mid'],
                       math.log1p(row['bid_qty']),math.log1p(row['ask_qty']),row['momentum'],
                       row['seconds_left']/900,(row['spot']/row['strike']-1)*100,span['position']])
    return result


def examples(rows,candles,horizon=5,interior=True,range_filter=True):
    vectors=features(rows,candles)
    targets={s['row']['id']:s for s in pairs(rows,horizon,interior)}
    result=[]
    for i in range(SEQUENCE-1,len(rows)):
        sample=targets.get(rows[i]['id'])
        if sample is None or vectors[i] is None:continue
        if range_filter and not .05<=vectors[i][-1]<=.95:continue
        start=i-SEQUENCE+1
        if any(v is None for v in vectors[start:i+1]):continue
        segment=rows[start:i+1]
        if any(a['ticker']!=b['ticker'] or b['ts']-a['ts']>2 for a,b in zip(segment,segment[1:])):continue
        result.append({'x':vectors[start:i+1],'sample':sample})
    return result


def network(size):
    import torch.nn as nn
    class QuoteLSTM(nn.Module):
        def __init__(self):
            super().__init__();self.rnn=nn.LSTM(size,16,batch_first=True);self.output=nn.Linear(16,1)
        def forward(self,x):
            sequence,_=self.rnn(x);return self.output(sequence[:,-1,:]).squeeze(-1)
    return QuoteLSTM()


def train(rows,candles,path,horizon,interior,range_filter,progress,stopped):
    data=examples(rows,candles,horizon,interior,range_filter)
    if len(data)<600:
        return {'status':'collecting','model':'lstm','reason':f'Usable sequences: {len(data)}/600. Need 48+ hours of prior reference history and enough interior-range observations',
                'sequences':len(data)},None
    tickers=list(dict.fromkeys(e['sample']['row']['ticker'] for e in data))
    if len(tickers)>=4:
        train_n=max(1,int(len(tickers)*.6));val_n=max(train_n+1,int(len(tickers)*.8))
        val_cut=min(e['sample']['row']['ts'] for e in data if e['sample']['row']['ticker']==tickers[train_n])
        test_cut=min(e['sample']['row']['ts'] for e in data if e['sample']['row']['ticker']==tickers[min(val_n,len(tickers)-1)])
        split='Chronological whole-contract train/validation/test'
    else:
        val_cut=data[int(len(data)*.7)]['sample']['row']['ts'];test_cut=data[int(len(data)*.85)]['sample']['row']['ts']
        split='Within-contract chronological train/validation/test; whole-contract split needs 4+ windows'
    training=[e for e in data if e['sample']['future']['ts']<val_cut]
    validation=[e for e in data if e['sample']['row']['ts']>=val_cut and e['sample']['future']['ts']<test_cut]
    test=[e for e in data if e['sample']['row']['ts']>=test_cut]
    if min(len(training),len(validation),len(test))<60:
        return {'status':'collecting','model':'lstm','reason':'Insufficient purged train/validation/test sequences','sequences':len(data)},None
    with TRAIN_LOCK:
        import numpy as np
        import torch
        torch.set_num_threads(1);torch.manual_seed(42)
        tx=np.asarray([e['x'] for e in training],dtype='float32')
        mean=tx.reshape(-1,len(FEATURES)).mean(0);std=tx.reshape(-1,len(FEATURES)).std(0);std=np.maximum(std,1e-6)
        def tensors(group):
            x=torch.from_numpy((np.asarray([e['x'] for e in group],dtype='float32')-mean)/std)
            y=torch.tensor([e['sample']['delta']*100 for e in group],dtype=torch.float32)
            return x,y
        xtrain,ytrain=tensors(training);xval,yval=tensors(validation)
        model=network(len(FEATURES));optimizer=torch.optim.Adam(model.parameters(),lr=.001)
        loss_fn=torch.nn.SmoothL1Loss();best_loss=float('inf');best=None;bad=0;epochs=0
        for epoch in range(20):
            if stopped():return {'status':'cancelled','model':'lstm'},None
            model.train()
            # Only already-separated training sequences are shuffled into minibatches.
            indices=torch.randperm(len(training))
            for batch in indices.split(64):
                optimizer.zero_grad();loss=loss_fn(model(xtrain[batch]),ytrain[batch]);loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(),1.);optimizer.step()
            model.eval()
            with torch.no_grad():val_loss=float(loss_fn(model(xval),yval))
            epochs=epoch+1;progress(epochs/20,'Training LSTM; early stopping uses validation only')
            if val_loss<best_loss-1e-5:
                best_loss=val_loss;best={k:v.detach().clone() for k,v in model.state_dict().items()};bad=0
            else:bad+=1
            if bad>=4:break
        model.load_state_dict(best);model.eval()
        xtest,_=tensors(test)
        with torch.no_grad():deltas=model(xtest).numpy()/100
        records=[];trades=[];available_after=0
        for item,delta in zip(test,deltas):
            sample=item['sample'];row,future=sample['row'],sample['future'];predicted=max(0,min(1,row['mid']+float(delta)))
            records.append({'time':row['ts'],'target_time':future['ts'],'ticker':row['ticker'],'current':row['mid'],'predicted':predicted,'actual':future['mid']})
            trade=execution_trade(row,future,predicted,available_after)
            if trade:trades.append(trade);available_after=trade['exit']
        report={'status':'complete','model':'lstm','horizon':horizon,'range_filter':range_filter,'interior':interior,
                'train_examples':len(training),'validation_examples':len(validation),'test_examples':len(test),
                'split':split,'validation_start':val_cut,'test_start':test_cut,'epochs':epochs,'features':FEATURES,
                'train_target_end':max(e['sample']['future']['ts'] for e in training),
                'validation_target_end':max(e['sample']['future']['ts'] for e in validation),
                'metrics':metrics(records),'net_pnl':sum(t['pnl'] for t in trades),'trades':len(trades),
                'trade_details':trades[-100:],'replay':records[-150:],
                'note':'Scaler fits training data only. Trailing ranges contain completed past candles only. Validation selects epochs; test data never select weights. One-contract replay includes quoted spread and estimated fees; no queue or latency simulation.'}
        checkpoint={'weights':model.state_dict(),'mean':mean.tolist(),'std':std.tolist(),'horizon':horizon,'sequence':SEQUENCE,'features':FEATURES}
        temporary=str(path)+'.tmp';torch.save(checkpoint,temporary)
        from pathlib import Path
        Path(temporary).replace(path)
        return report,{'model':model,'mean':mean,'std':std,'horizon':horizon}


def predict(bundle,rows,candles):
    if len(rows)<SEQUENCE:return None
    segment=rows[-SEQUENCE:]
    vectors=features(segment,candles)
    if any(v is None for v in vectors) or any(a['ticker']!=b['ticker'] or b['ts']-a['ts']>2 for a,b in zip(segment,segment[1:])):return None
    import numpy as np
    import torch
    x=torch.from_numpy(((np.asarray(vectors,dtype='float32')-bundle['mean'])/bundle['std'])[None,:,:])
    with torch.no_grad():delta=float(bundle['model'](x)[0])/100
    row=rows[-1]
    return {'price':max(0,min(1,row['mid']+delta)),'as_of':row['ts'],'target_time':row['ts']+bundle['horizon'],'horizon':bundle['horizon']}


def load(path):
    import numpy as np
    import torch
    torch.set_num_threads(1)
    checkpoint=torch.load(path,map_location='cpu',weights_only=True)
    if checkpoint['features']!=FEATURES or checkpoint['sequence']!=SEQUENCE or checkpoint['horizon'] not in (2,5,10):
        raise ValueError('Saved LSTM feature schema is incompatible; retrain the experiment')
    model=network(len(FEATURES));model.load_state_dict(checkpoint['weights']);model.eval()
    return {'model':model,'mean':np.asarray(checkpoint['mean'],dtype='float32'),
            'std':np.asarray(checkpoint['std'],dtype='float32'),'horizon':checkpoint['horizon']}
