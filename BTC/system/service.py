"""Hybrid BTC outcome pipeline: live collectors plus import of recorded quotes."""
import argparse
import concurrent.futures
import importlib.util
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from pathlib import Path

from .store import Store
from .features import CandleFeatures,build
from .feeds import Venue,IndexFeed,run_venue
from .settlement import rules
from .evaluation import predict
from .execution import cash_cost

ROOT=Path(__file__).resolve().parents[2]


class System:
    def __init__(self,engine,directory=None):
        production=ROOT/'BTC'/'.data'/'btc.sqlite3.research.sqlite3'
        default=ROOT/'BTC'/'.data'/'system'
        if engine and Path(engine.research_path).resolve()!=production.resolve():default=Path(engine.research_path+'.outcomes')
        self.engine=engine;self.directory=Path(directory or default)
        self.store=Store(self.directory);self.stop=engine.stop if engine else threading.Event();self.lock=threading.RLock()
        self.venues={name:Venue(name,self.store) for name in ('coinbase','kraken')};self.index=IndexFeed(self.store)
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix='btc-outcome-jobs')
        self.job={'status':'idle'};self.live=None;self.models={};self.model_mtime=0;self.threads=[];self.last_quote=0;self.last_bucket=None
        self._candles=None;self._candles_at=0;self.child=None
        self.refined=None;self.refined_mtime=0
        self.swing_model=None;self.swing_mtime=0

    def candle_features(self):
        if time.time()-self._candles_at>30 or self._candles is None:
            path=ROOT/'BTC'/'.data'/'btc.sqlite3.research.sqlite3'
            if self.engine:path=Path(self.engine.research_path)
            if not path.exists():return CandleFeatures([])
            connection=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
            connection.row_factory=sqlite3.Row
            try:self._candles=CandleFeatures([dict(r) for r in connection.execute('SELECT ts,close FROM candles ORDER BY ts')])
            finally:connection.close()
            self._candles_at=time.time()
        return self._candles

    def sync_contracts(self,request,earliest=None):
        from desk.server import KALSHI
        params={'series_ticker':'KXBTC15M','status':'settled','limit':100,'min_close_ts':int(earliest or time.time()-7*86400)}
        count=0
        for _ in range(30):
            if self.stop.is_set():return count
            page=request(KALSHI+'/markets?'+urllib.parse.urlencode(params))
            for market in page.get('markets',[]):
                if market.get('result') in ('yes','no'):self.store.contract(market);count+=1
            cursor=page.get('cursor')
            if not cursor:break
            params['cursor']=cursor
        return count

    def import_recordings(self,request):
        source=Path(self.engine.research_path) if self.engine else ROOT/'BTC'/'.data'/'btc.sqlite3.research.sqlite3'
        connection=sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True);connection.row_factory=sqlite3.Row
        try:
            first=connection.execute('SELECT MIN(ts) FROM snapshots').fetchone()[0]
            if first is None:raise ValueError('No recorded BTC snapshots yet')
            self.sync_contracts(request,first-900);markets={m['ticker']:m for m in self.store.contracts()}
            candle=self.candle_features();bucket=None;count=0
            for item in connection.execute('SELECT * FROM snapshots ORDER BY ts'):
                if self.stop.is_set():break
                market=markets.get(item['ticker'])
                if market is None or not rules(market)['supported']:continue
                book=json.loads(zlib.decompress(item['raw']))
                self.store.quote(item['ticker'],item['ts'],item['fee_multiplier'],book)
                key=(item['ticker'],int(item['ts']//5))
                if key==bucket:continue
                bucket=key
                if item['spot_time'] is None or not 0<=item['ts']-item['spot_time']<=15:continue
                sigma,vol5=candle.at(item['ts'])
                x=build(item['spot'],market.get('floor_strike'),market['close_ts']-item['ts'],sigma,vol5,book)
                if x:
                    x['source_received']=item['ts'];x['spot_received']=item['spot_time'];x['book_received']=item['ts']
                    if self.store.observation(item['ticker'],item['ts'],x,'recorded_quote_import'):count+=1
            self.store.flush();return count
        finally:connection.close()

    def sample(self):
        engine=self.engine;now=time.time()
        with engine.lock:
            market=dict(engine.market);book=dict(engine.book);book_time=engine.book_time
            reference=dict(engine.reference);multiplier=engine.fee_multiplier if engine.fee_supported else None
        if not market:return
        streamed=self.index.snapshot(market['ticker'],now)
        if streamed:book,book_time=streamed
        if not 0<=now-book_time<=2:return
        if market['ticker']!=getattr(self,'_ticker',None):self.store.contract(market);self._ticker=market['ticker']
        if book_time>self.last_quote:
            self.store.quote(market['ticker'],book_time,multiplier,book);self.last_quote=book_time
        if not rules(market)['supported']:return
        bucket=(market['ticker'],int(now//5))
        if bucket==self.last_bucket:self.store.flush();return
        self.last_bucket=bucket
        raw=self.index.latest
        source='raw_brti' if raw and 0<=now-raw['exchange_ts']<=5 and raw['received']<=now else 'proxy'
        spot=raw['price'] if source=='raw_brti' else reference.get('price')
        spot_time=raw['received'] if source=='raw_brti' else reference.get('time',0)
        if not spot or not 0<=now-spot_time<=15:return
        health={};venues={}
        for name,venue in self.venues.items():
            with venue.lock:health[name],venues[name]=venue.features(time.time())
        now=time.time()
        from desk.model import timestamp
        sigma,vol5=self.candle_features().at(now)
        x=build(spot,market.get('floor_strike'),timestamp(market['close_time'])-now,sigma,vol5,book,venues,source)
        if x is None:return
        x.update(source_received=now,spot_received=spot_time,book_received=book_time,kalshi_source='authenticated_ws' if streamed else 'public_rest',
                 cb_received=health['coinbase']['last_receipt'] if venues['coinbase'] else None,
                 kr_received=health['kraken']['last_receipt'] if venues['kraken'] else None)
        recorded=self.store.observation(market['ticker'],now,x,'live_join')
        path=self.directory/'models.json'
        if path.exists() and path.stat().st_mtime!=self.model_mtime:
            payload=json.loads(path.read_text());self.models=payload['artifacts'];self.model_mtime=path.stat().st_mtime
        predictions={}
        refined_path=self.directory/'refined-model.json'
        if refined_path.exists() and refined_path.stat().st_mtime!=self.refined_mtime:
            import hashlib
            frozen=json.loads(refined_path.read_text());material={k:v for k,v in frozen.items() if k!='artifact_hash'}
            if hashlib.sha256(json.dumps(material,sort_keys=True).encode()).hexdigest()!=frozen['artifact_hash']:raise ValueError('Refined model hash mismatch')
            self.refined=frozen;self.refined_mtime=refined_path.stat().st_mtime
        models=dict(self.models)
        if self.refined:models['D']=self.refined['champion']
        unit_cost=lambda a:cash_cost(a,math.ceil((.07*multiplier*a*(1-a)-1e-12)*1e6)/1e6) if multiplier is not None else None
        for name,artifact in models.items():
            if source not in artifact.get('reference_mode',['proxy']):continue
            if name=='B' and not x['flow_ready']:continue
            if 'cb_tfi_60' in artifact.get('features',[]) and not x['flow_ready']:continue
            p=predict(artifact,x)
            predictions[name]={'p_yes':p,'yes_edge':p-unit_cost(x['yes_ask']) if multiplier is not None else None,
                               'no_edge':1-p-unit_cost(x['no_ask']) if multiplier is not None else None,
                               'holdout_id':artifact.get('holdout_id',self.refined['protocol_id'] if self.refined else None)}
        challenger=None
        if self.refined and source in self.refined['candidate']['reference_mode']:
            p=predict(self.refined['candidate'],x);emitted=time.time()
            if recorded and emitted>=self.refined.get('published_at',emitted):
                self.store.shadow(self.refined['protocol_id'],self.refined['artifact_hash'],market['ticker'],now,emitted,p)
            challenger={'p_yes':p,'name':self.refined['candidate']['name'],'development_gate':self.refined['development_gate'],
                        'yes_edge':p-unit_cost(x['yes_ask']) if multiplier is not None else None,
                        'no_edge':1-p-unit_cost(x['no_ask']) if multiplier is not None else None,
                        'message':'Shadow forecast only; prospective validation required before any trading integration.'}
        with self.lock:self.live={'ts':now,'ticker':market['ticker'],'features':x,'venues':health,'models':predictions,
                                  'rules':rules(market),'source':source,'fee_known':multiplier is not None,'challenger':challenger}

    def status(self):
        now=time.time();venues={}
        for name,v in self.venues.items():venues[name]=v.features(now)[0]
        with self.lock:live=self.live;job=dict(self.job)
        def saved(name):
            path=self.directory/name
            return json.loads(path.read_text()) if path.exists() else None
        return {'live':live,'venues':venues,'brti':{'status':self.index.status,'raw_available':bool(self.index.latest and now-self.index.latest['received']<=5)},
                'stats':self.store.stats(),'report':self.store.latest(),'job':job,'settlement_verification':self.store.verification()[:20],
                'refinement':saved('refinement.json'),'prospective':saved('prospective-status.json'),
                'swing_report':saved('adaptive-swing-report.json'),'fixed_swing_report':saved('swing-report.json'),
                'model_target':'Official BTC 15-minute settlement outcome; not seconds-ahead quote movement',
                'limitations':['Raw BRTI verification requires an entitled read-only Kalshi API key. Proxy inputs are explicitly tagged.',
                               'Derivatives/Binance are deferred until the matched spot-flow experiment passes its validation gate.',
                               'No model here can place live or paper portfolio orders. Repeated holdout inspection is tracked, not treated as untouched research.']}

    def start_job(self,kind,request):
        if kind not in ('import','train','export','refine','swing','adaptive'):raise ValueError('Choose import, train, refine, swing, adaptive or export')
        with self.lock:
            if self.job['status']=='running':raise ValueError('An outcome research job is already running')
            self.job={'kind':kind,'status':'running','started':time.time(),'message':'Starting'}
        self.pool.submit(self._job,kind,request)

    def _job(self,kind,request):
        try:
            if kind=='import':message=f'Imported {self.import_recordings(request)} five-second observations with official contract metadata'
            else:
                python=ROOT/'.venv-btc-research'/'bin'/'python'
                if not python.exists():raise ValueError('Run BTC/system/setup.sh to prepare the isolated research environment')
                env={k:v for k,v in os.environ.items() if k not in ('KALSHI_API_KEY_ID','KALSHI_PRIVATE_KEY_PATH')}
                spec=importlib.util.find_spec('torch')
                if sys.platform=='darwin' and spec:
                    omp=Path(next(iter(spec.submodule_search_locations)))/'lib'
                    if (omp/'libomp.dylib').exists():env['DYLD_LIBRARY_PATH']=str(omp)
                command=[str(python),'-m','BTC.system.train','--directory',str(self.directory),'--kind',kind]
                if kind in ('swing','adaptive'):command+=['--source',str(self.engine.research_path)]
                self.child=subprocess.Popen(command,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:stdout,stderr=self.child.communicate(timeout=600 if kind=='adaptive' else 240)
                except subprocess.TimeoutExpired:
                    self.child.terminate()
                    try:self.child.communicate(timeout=5)
                    except subprocess.TimeoutExpired:self.child.kill();self.child.communicate()
                    raise ValueError('Research worker exceeded its time limit and was stopped')
                if self.child.returncode:raise ValueError(stderr[-2000:] or 'Research worker failed')
                message=stdout.strip().splitlines()[-1]
                if kind=='refine':self.check_prospective()
            with self.lock:self.job.update(status='complete',message=message)
        except Exception as exc:
            with self.lock:self.job.update(status='error',message=str(exc))
        finally:self.child=None

    def swing_status(self,target=None):
        from .adaptive_swings import live
        from desk.model import timestamp
        if target is not None and (not math.isfinite(target) or target<=0):raise ValueError('BTC target must be a finite positive price')
        now=time.time();e=self.engine
        with e.lock:
            market=dict(e.market);book=dict(e.book);book_time=e.book_time;reference=dict(e.reference)
            multiplier=e.fee_multiplier if e.fee_supported else None
        if not market or not 0<=now-book_time<=2:return {'status':'waiting','reason':'Awaiting a fresh BTC market and two-sided book'}
        raw=self.index.latest
        if raw and 0<=now-raw['exchange_ts']<=5 and raw['received']<=now:reference={'price':raw['price'],'time':raw['received'],'source':'raw BRTI'}
        if not reference.get('price') or not 0<=now-reference.get('time',0)<=5:return {'status':'waiting','reason':'Awaiting a reference received within five seconds'}
        bids,asks=book.get('yes_bids',[]),book.get('yes_asks',[])
        if not bids or not asks or bids[0]['price']>asks[0]['price']:return {'status':'waiting','reason':'No valid two-sided Kalshi book'}
        b,a=bids[0],asks[0];bd=sum(x['quantity'] for x in bids[:5]);ad=sum(x['quantity'] for x in asks[:5])
        sigma,_=self.candle_features().at(now);seconds=max(0,timestamp(market['close_time'])-now)
        from .settlement import averaging_probability
        baseline=averaging_probability(reference['price'],market['floor_strike'],seconds,sigma) if sigma else None
        samples=e.research.store.rows(3);momentum=0.
        previous=[r for r in samples if r['ticker']==market['ticker'] and 0<book_time-r['ts']<=2 and r['mid'] is not None]
        if previous:momentum=(a['price']+b['price'])/2-previous[-1]['mid']
        row={'ts':now,'ticker':market['ticker'],'spot':reference['price'],'spot_time':reference['time'],'strike':market['floor_strike'],
             'seconds_left':seconds,'baseline':baseline,'mid':(a['price']+b['price'])/2,'bid':b['price'],'ask':a['price'],
             'bid_qty':b['quantity'],'ask_qty':a['quantity'],'spread':a['price']-b['price'],'imbalance':(bd-ad)/(bd+ad) if bd+ad else 0.,
             'momentum':momentum,'fee_multiplier':multiplier}
        path=self.directory/'adaptive-swing-model.json'
        with self.lock:
            if path.exists() and path.stat().st_mtime!=self.swing_mtime:self.swing_model=json.loads(path.read_text());self.swing_mtime=path.stat().st_mtime
            model=self.swing_model
        try:calculated=live(row,sigma,model,target)
        except ImportError:return {'status':'waiting','reason':'Adaptive simulation needs NumPy. Run BTC/system/setup.sh and start with .venv-btc-research/bin/python server.py'}
        return {'status':'live','source':reference.get('source','Coinbase proxy'),'book_received':book_time,**calculated}

    def public_trades(self,request):
        seen=set()
        while not self.stop.is_set():
            try:
                ticker=self.engine.market.get('ticker')
                if ticker:
                    page=request('https://external-api.kalshi.com/trade-api/v2/markets/trades?'+urllib.parse.urlencode({'ticker':ticker,'limit':100}))
                    from .feeds import stamp
                    for trade in page.get('trades',[]):
                        identity=trade.get('trade_id')
                        if identity and identity not in seen:
                            self.store.event('kalshi_rest','trade',stamp(trade['created_time']) if trade.get('created_time') else None,time.time(),trade);seen.add(identity)
                    if len(seen)>10000:seen=set()
            except Exception as exc:self.store.gap('kalshi_rest',str(exc))
            self.stop.wait(5)

    def run(self,request):
        thread=threading.Thread(target=self.public_trades,args=(request,),daemon=True,name='btc-kalshi-public-trades');thread.start();self.threads.append(thread)
        for venue in self.venues.values():
            thread=threading.Thread(target=run_venue,args=(venue,self.stop),daemon=True,name='btc-'+venue.name);thread.start();self.threads.append(thread)
        thread=threading.Thread(target=self.index.run,args=(self.engine,self.stop),daemon=True,name='btc-kalshi-index');thread.start();self.threads.append(thread)
        last_sync=0
        while not self.stop.is_set():
            try:
                self.sample()
                self.store.flush()
                if time.time()-last_sync>300:
                    # Metadata/outcome requests run outside the sampling loop.
                    if self.job['status']!='running':self.pool.submit(self.maintenance,request)
                    last_sync=time.time()
            except Exception as exc:
                with self.lock:self.job.update(message='Sampling: '+str(exc))
            self.stop.wait(.5)
        self.store.flush()

    def check_prospective(self):
        from .refinement import prospective,atomic_json
        atomic_json(self.directory/'prospective-status.json',prospective(self.store))

    def maintenance(self,request):
        self.sync_contracts(request)
        if not self.stop.is_set():self.check_prospective()

    def close(self):
        self.stop.set()
        if self.child is not None:self.child.terminate()
        for thread in self.threads:thread.join(timeout=4)
        self.pool.shutdown(wait=True,cancel_futures=True);self.store.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--import-recordings',action='store_true');args=parser.parse_args()
    from desk.server import get_json
    system=System(None)
    try:print('Imported',system.import_recordings(get_json))
    finally:system.close()

if __name__=='__main__':main()
