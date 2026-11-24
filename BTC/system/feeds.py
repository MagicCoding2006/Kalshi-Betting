"""Read-only public spot streams and optional authenticated Kalshi index/quotes."""
import json
import math
import os
import threading
import time
import zlib
from collections import deque
from decimal import Decimal


def stamp(value):
    if isinstance(value,(float,int)):return value/1000 if value>1e12 else float(value)
    from desk.model import timestamp
    return timestamp(value)


def crc(book):
    # Kraken v2 checksum: ten asks ascending, then ten bids descending; preserve decimals.
    data=''
    for side,reverse in [('asks',False),('bids',True)]:
        for price in sorted(book[side],reverse=reverse)[:10]:
            for number in [price,book[side][price]]:
                data+=format(number,'f').replace('.','').lstrip('0')
    return zlib.crc32(data.encode())


class Venue:
    def __init__(self,name,journal):
        self.name=name;self.journal=journal;self.lock=threading.RLock();self.bids={};self.asks={}
        self.trades=deque(maxlen=100000);self.flows=deque(maxlen=50000);self.prices=deque(maxlen=20000)
        self.last_message=0.;self.last_book=0.;self.since=0.;self.ready=False;self.error='Not connected';self.reconnects=0;self.last_trade=None

    def reset(self,reason):
        with self.lock:
            self.bids={};self.asks={};self.trades.clear();self.flows.clear();self.prices.clear()
            self.ready=False;self.since=time.time();self.error=reason;self.last_trade=None;self.reconnects+=1
        self.journal.gap(self.name,reason)

    def best(self):
        if not self.bids or not self.asks:return None
        b=max(self.bids);a=min(self.asks)
        if b>=a:raise ValueError('Crossed underlying book')
        return float(b),float(a),float(self.bids[b]),float(self.asks[a])

    def update(self,bids,asks,received,snapshot=False,checksum=None):
        with self.lock:
            old=self.best() if self.ready else None
            if snapshot:
                self.bids={};self.asks={};self.since=received
                self.flows.clear();self.prices.clear();self.trades.clear();self.last_trade=None
            for levels,target in [(bids,self.bids),(asks,self.asks)]:
                for p,q in levels:
                    p,q=Decimal(str(p)),Decimal(str(q))
                    if not p.is_finite() or not q.is_finite() or p<=0 or q<0:
                        self.ready=False;raise ValueError('Invalid book level')
                    if q==0:target.pop(p,None)
                    else:target[p]=q
            if self.name=='kraken':
                self.bids=dict(sorted(self.bids.items(),reverse=True)[:100]);self.asks=dict(sorted(self.asks.items())[:100])
                if checksum is not None and crc({'bids':self.bids,'asks':self.asks})!=int(checksum):
                    self.ready=False;raise ValueError('Kraken checksum mismatch; resnapshot required')
            try:current=self.best()
            except ValueError:
                self.ready=False;raise
            if current:
                b,a,bq,aq=current;mid=(a+b)/2
                if old:
                    ob,oa,obq,oaq=old
                    ofi=(bq if b>=ob else 0)-(obq if b<=ob else 0)-(aq if a<=oa else 0)+(oaq if a>=oa else 0)
                    self.flows.append((received,ofi,max(0,obq-bq)+max(0,oaq-aq),max(0,bq-obq)+max(0,aq-oaq)))
                self.prices.append((received,mid));self.ready=True;self.error=None;self.last_book=received
            self.last_message=received

    def trade(self,price,qty,side,exchange_ts,received,trade_id=None):
        if exchange_ts>received+5:raise ValueError('Future-dated trade')
        if not all(math.isfinite(x) and x>0 for x in (price,qty)):raise ValueError('Invalid trade')
        if trade_id is not None and self.last_trade is not None:
            if trade_id<=self.last_trade:return
            if self.name=='coinbase' and trade_id!=self.last_trade+1:
                self.trades.clear();self.since=received;self.journal.gap(self.name,'Missing trade IDs; rolling flow reset')
        self.last_trade=trade_id
        if received-exchange_ts<=60:self.trades.append((received,exchange_ts,qty if side=='buy' else -qty,price))

    def consume(self,message,received=None):
        received=received or time.time()
        with self.lock:
            self.last_message=received
            if self.name=='coinbase':
                kind=message.get('type')
                if kind=='error':raise ValueError(message.get('message','Coinbase subscription error'))
                if kind=='snapshot':self.update(message['bids'],message['asks'],received,True)
                elif kind=='l2update':
                    if not self.ready:raise ValueError('L2 delta before snapshot')
                    bids=[(p,q) for side,p,q in message['changes'] if side=='buy'];asks=[(p,q) for side,p,q in message['changes'] if side=='sell']
                    self.update(bids,asks,received)
                elif kind=='match':
                    # Coinbase reports the resting maker side, so invert to get aggressor.
                    self.trade(float(message['price']),float(message['size']),'buy' if message['side']=='sell' else 'sell',stamp(message['time']),received,int(message['trade_id']))
            elif self.name=='kraken':
                channel=message.get('channel');kind=message.get('type')
                for item in message.get('data',[]):
                    if channel=='book':
                        if kind!='snapshot' and not self.ready:raise ValueError('Kraken delta before snapshot')
                        self.update([(r['price'],r['qty']) for r in item.get('bids',[])],[(r['price'],r['qty']) for r in item.get('asks',[])],received,kind=='snapshot',item.get('checksum'))
                    elif channel=='trade' and kind=='update':
                        self.trade(float(item['price']),float(item['qty']),item['side'],stamp(item['timestamp']),received,int(item['trade_id']))

    def features(self,now):
        with self.lock:
            health={'venue':self.name,'ready':self.ready and now-self.last_message<=5 and now-self.last_book<=5,'last_receipt':self.last_message,
                    'book_age':now-self.last_book if self.last_book else None,'error':self.error,'reconnects':self.reconnects}
            if self.ready and not health['ready']:health['error']='Book or stream stale; features unavailable'
            if not health['ready']:return health,{}
            best=self.best()
            if best is None:return health,{}
            bid,ask,bq,aq=best;mid=(bid+ask)/2
            result={'mid':mid,'microprice_gap_bps':((ask*bq+bid*aq)/(bq+aq)-mid)/mid*10000,'spread_bps':(ask-bid)/mid*10000}
            for radius in (5,10,25):
                buys=sum(float(q) for p,q in self.bids.items() if float(p)>=mid*(1-radius/10000))
                sells=sum(float(q) for p,q in self.asks.items() if float(p)<=mid*(1+radius/10000))
                result['depth_'+str(radius)]=(buys-sells)/(buys+sells) if buys+sells else None
            for window in (1,5,10,15,30,60):
                if now-self.since<window:
                    result['tfi_'+str(window)]=None;result['ofi_'+str(window)]=None;result['absorption_'+str(window)]=None
                    continue
                trades=[t for t in self.trades if now-window<=t[0]<=now and t[1]<=now]
                total=sum(abs(t[2]) for t in trades)
                result['tfi_'+str(window)]=(sum(t[2] for t in trades)/total if total else 0.) if self.last_trade is not None else None
                flows=[f for f in self.flows if now-window<=f[0]<=now]
                result['ofi_'+str(window)]=sum(f[1] for f in flows)/max(bq+aq,1e-9)
                result['depletion_'+str(window)]=sum(f[2] for f in flows)
                result['replenishment_'+str(window)]=sum(f[3] for f in flows)
                prices=[p for p in self.prices if now-window<=p[0]<=now]
                result['absorption_'+str(window)]=total/(abs(mid-prices[0][1])+.01) if prices else None
            return health,result


def run_venue(venue,stop):
    try:from websockets.sync.client import connect
    except ImportError:
        venue.reset('Missing WebSocket dependency. Run BTC/system/setup.sh and start with .venv-btc-research/bin/python server.py');return
    url='wss://ws-feed.exchange.coinbase.com' if venue.name=='coinbase' else 'wss://ws.kraken.com/v2'
    backoff=1
    while not stop.is_set():
        try:
            venue.reset('Connecting / resnapshot')
            with connect(url,open_timeout=8,close_timeout=2,max_size=8*1024*1024) as ws:
                if venue.name=='coinbase':ws.send(json.dumps({'type':'subscribe','product_ids':['BTC-USD'],'channels':['level2_batch','matches','heartbeat']}))
                else:
                    for channel in ('book','trade'):
                        params={'channel':channel,'symbol':['BTC/USD']}
                        if channel=='book':params['depth']=100
                        ws.send(json.dumps({'method':'subscribe','params':params}))
                backoff=1
                while not stop.is_set():
                    try:raw=ws.recv(timeout=1)
                    except TimeoutError:
                        if time.time()-venue.last_message>15:raise ValueError('Feed heartbeat timeout')
                        continue
                    received=time.time();message=json.loads(raw,parse_float=Decimal)
                    # Journal decoded numeric fields portably; book checksum keeps Decimal.
                    source_time=message.get('time')
                    if source_time is None and isinstance(message.get('data'),list) and message['data']:source_time=message['data'][0].get('timestamp')
                    exchange_ts=stamp(source_time) if source_time else None
                    if exchange_ts and exchange_ts>received+5:raise ValueError('Exchange timestamp is in the future')
                    venue.journal.event(venue.name,message.get('type',message.get('channel','message')),exchange_ts,received,json.loads(raw))
                    venue.consume(message,received)
        except Exception as exc:
            venue.reset(str(exc));stop.wait(backoff);backoff=min(30,backoff*2)


def auth_headers():
    key_id=os.environ.get('KALSHI_API_KEY_ID');path=os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not key_id or not path:return None
    from cryptography.hazmat.primitives import serialization,hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    import base64
    with open(path,'rb') as handle:key=serialization.load_pem_private_key(handle.read(),password=None)
    ts=str(int(time.time()*1000));signature=key.sign((ts+'GET/trade-api/ws/v2').encode(),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.DIGEST_LENGTH),hashes.SHA256())
    return {'KALSHI-ACCESS-KEY':key_id,'KALSHI-ACCESS-TIMESTAMP':ts,'KALSHI-ACCESS-SIGNATURE':base64.b64encode(signature).decode()}


class IndexFeed:
    def __init__(self,store):self.store=store;self.lock=threading.RLock();self.latest=None;self.status='Credentials not configured';self.book=None;self.book_time=0;self.sequences={};self.ticker=None
    def consume(self,message,received):
        with self.lock:self._consume(message,received)

    def _consume(self,message,received):
        if message.get('type')=='error':self.status='Kalshi subscription rejected: '+str(message.get('msg',{}).get('msg','unknown'));return
        sid=message.get('sid');seq=message.get('seq')
        if sid is not None and seq is not None:
            previous=self.sequences.get(sid)
            if previous is not None and seq!=previous+1:raise ValueError('Kalshi sequence gap; resubscribe required')
            self.sequences[sid]=seq
        kind=message.get('type');msg=message.get('msg',{})
        if kind=='cfbenchmarks_value' and msg.get('index_id')=='BRTI':
            raw=json.loads(msg['data']);ts=stamp(raw['time']);price=float(raw['value'])
            if not math.isfinite(price) or price<=0:raise ValueError('Invalid BRTI value')
            if ts>received+5 or received-ts>15:raise ValueError('BRTI timestamp stale or future-dated')
            with self.lock:self.latest={'price':price,'exchange_ts':ts,'received':received};self.status='Live raw BRTI'
            self.store.tick(ts,received,price)
        elif kind=='orderbook_snapshot':
            self.book={'yes':{float(p):float(q) for p,q in msg.get('yes_dollars_fp',[])},'no':{float(p):float(q) for p,q in msg.get('no_dollars_fp',[])}}
            self.ticker=msg.get('market_ticker');self.book_time=received
        elif kind=='orderbook_delta':
            if self.book is None:raise ValueError('Kalshi delta before snapshot')
            if msg.get('market_ticker')!=self.ticker:raise ValueError('Kalshi delta for different contract')
            side=msg['side'];price=float(msg['price_dollars']);quantity=self.book[side].get(price,0)+float(msg['delta_fp'])
            if quantity<=0:self.book[side].pop(price,None)
            else:self.book[side][price]=quantity
            self.book_time=received
        self.store.event('kalshi_ws',kind,msg.get('ts_ms',0)/1000 if msg.get('ts_ms') else None,received,message)

    def snapshot(self,ticker,now):
        from desk.model import normalize_book
        with self.lock:
            if not self.book or self.ticker!=ticker or not 0<=now-self.book_time<=2:return None
            book=normalize_book({'orderbook_fp':{side+'_dollars':list(levels.items()) for side,levels in self.book.items()}})
            return book,self.book_time

    def run(self,engine,stop):
        if not os.environ.get('KALSHI_API_KEY_ID') or not os.environ.get('KALSHI_PRIVATE_KEY_PATH'):return
        try:from websockets.sync.client import connect
        except ImportError:
            self.status='Missing WebSocket dependency; run BTC/system/setup.sh';return
        while not stop.is_set():
            try:
                with connect('wss://external-api-ws.kalshi.com/trade-api/ws/v2',additional_headers=auth_headers(),open_timeout=8,close_timeout=2) as ws:
                    with self.lock:self.sequences={};self.book=None;self.ticker=None
                    ws.send(json.dumps({'id':1,'cmd':'subscribe','params':{'channels':['cfbenchmarks_value'],'index_ids':['BRTI']}}))
                    subscribed=None
                    while not stop.is_set():
                        ticker=engine.market.get('ticker')
                        if ticker and ticker!=subscribed:
                            if subscribed:break  # Fresh connection/snapshot on rollover.
                            ws.send(json.dumps({'id':2,'cmd':'subscribe','params':{'channels':['orderbook_delta','trade'],'market_tickers':[ticker]}}));subscribed=ticker
                        try:raw=ws.recv(timeout=1)
                        except TimeoutError:continue
                        self.consume(json.loads(raw),time.time())
            except Exception as exc:
                with self.lock:self.status=str(exc);self.latest=None;self.book=None;self.ticker=None
                self.store.gap('kalshi_ws',str(exc));stop.wait(5)
