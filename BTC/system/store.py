"""Transactional research journal with immutable received-time feature snapshots."""
import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path


class Store:
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.db=sqlite3.connect(self.directory/'system.sqlite3',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,venue TEXT,kind TEXT,exchange_ts REAL,received REAL,payload BLOB);
          CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY,venue TEXT,ts REAL,reason TEXT);
          CREATE TABLE IF NOT EXISTS ticks(exchange_ts REAL PRIMARY KEY,received REAL,price REAL);
          CREATE TABLE IF NOT EXISTS contracts(ticker TEXT PRIMARY KEY,close_ts REAL,result TEXT,available REAL,payload TEXT);
          CREATE TABLE IF NOT EXISTS observations(ticker TEXT,ts REAL,origin TEXT,features TEXT,PRIMARY KEY(ticker,ts));
          CREATE INDEX IF NOT EXISTS observations_ts ON observations(ts);
          CREATE TABLE IF NOT EXISTS quotes(ticker TEXT,ts REAL,fee REAL,book BLOB,PRIMARY KEY(ticker,ts));
          CREATE INDEX IF NOT EXISTS quotes_lookup ON quotes(ticker,ts);
          CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,created REAL,payload TEXT);
          CREATE TABLE IF NOT EXISTS shadow_forecasts(protocol_id TEXT,artifact_hash TEXT,ticker TEXT,ts REAL,emitted REAL,p_yes REAL,
            PRIMARY KEY(protocol_id,ticker,ts));
        ''');self.db.commit();self.writes=0

    def event(self,venue,kind,exchange_ts,received,payload):
        with self.lock:
            self.db.execute('INSERT INTO events(venue,kind,exchange_ts,received,payload) VALUES(?,?,?,?,?)',
                            (venue,kind,exchange_ts,received,zlib.compress(json.dumps(payload,separators=(',',':')).encode())))
            self.writes+=1
            if self.writes%1000==0:
                self.db.execute('DELETE FROM events WHERE id<=(SELECT COALESCE(MAX(id),0)-200000 FROM events)')
            # Event writes are batched with observations/quotes; one commit per second.

    def gap(self,venue,reason):
        with self.lock:
            self.db.execute('INSERT INTO gaps(venue,ts,reason) VALUES(?,?,?)',(venue,time.time(),reason));self.db.commit()

    def tick(self,exchange_ts,received,price):
        with self.lock:self.db.execute('INSERT OR IGNORE INTO ticks VALUES(?,?,?)',(exchange_ts,received,price))

    def contract(self,market):
        from desk.model import timestamp
        market=dict(market);market['open_ts']=timestamp(market.get('open_time'));market['close_ts']=timestamp(market.get('close_time'))
        available=market.get('settlement_ts')
        if isinstance(available,str):
            try:available=float(available)
            except ValueError:available=timestamp(available)
        if available and available>1e12:available/=1000
        if not available:available=max(market['close_ts'],timestamp(market.get('expected_expiration_time')) or market['close_ts']+300)
        market['label_timing']='official settlement timestamp' if market.get('settlement_ts') else 'expected-expiration proxy (not historical receipt time)'
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO contracts VALUES(?,?,?,?,?)',(market['ticker'],market['close_ts'],market.get('result'),available,json.dumps(market)));self.db.commit()
        return market

    def observation(self,ticker,ts,features,origin):
        with self.lock:
            bucket=int(ts//5)*5
            cursor=self.db.execute('INSERT OR IGNORE INTO observations SELECT ?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM observations WHERE ticker=? AND ts>=? AND ts<?)',
                                   (ticker,ts,origin,json.dumps(features,allow_nan=False),ticker,bucket,bucket+5))
            self.db.commit();return cursor.rowcount>0

    def quote(self,ticker,ts,fee,book):
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO quotes VALUES(?,?,?,?)',(ticker,ts,fee,zlib.compress(json.dumps(book,separators=(',',':')).encode())))

    def flush(self):
        with self.lock:self.db.commit()

    def shadow(self,protocol_id,artifact_hash,ticker,ts,emitted,p_yes):
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO shadow_forecasts VALUES(?,?,?,?,?,?)',(protocol_id,artifact_hash,ticker,ts,emitted,p_yes));self.db.commit()

    def dataset(self):
        with self.lock:
            rows=self.db.execute("SELECT o.*,c.result,c.available,c.payload FROM observations o JOIN contracts c USING(ticker) WHERE c.result IN ('yes','no') ORDER BY o.ts").fetchall()
        return [{'ticker':r['ticker'],'ts':r['ts'],'origin':r['origin'],'x':json.loads(r['features']),
                 'y':int(r['result']=='yes'),'available':r['available'],'market':json.loads(r['payload'])} for r in rows]

    def quote_after(self,ticker,ts,tolerance=1.):
        with self.lock:r=self.db.execute('SELECT * FROM quotes WHERE ticker=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 1',(ticker,ts,ts+tolerance)).fetchone()
        return {'ts':r['ts'],'fee':r['fee'],'book':json.loads(zlib.decompress(r['book']))} if r else None

    def contracts(self):
        with self.lock:return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM contracts ORDER BY close_ts DESC')]

    def verification(self):
        from .settlement import verify
        result=[]
        for market in self.contracts()[:100]:
            if market.get('result') not in ('yes','no'):continue
            with self.lock:
                ticks=[dict(r) for r in self.db.execute('SELECT * FROM ticks WHERE exchange_ts BETWEEN ? AND ?',(market['open_ts']-60,market['close_ts']))]
            result.append({'ticker':market['ticker'],**verify(market,ticks,ticks)})
        return result

    def report(self,payload):
        with self.lock:
            if payload.get('holdout_id'):
                prior=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM reports')]
                payload['holdout_exposures']=1+sum(p.get('holdout_id')==payload['holdout_id'] for p in prior)
            self.db.execute('INSERT INTO reports(created,payload) VALUES(?,?)',(time.time(),json.dumps(payload,allow_nan=False)));self.db.commit()

    def latest(self):
        with self.lock:rows=self.db.execute('SELECT payload FROM reports ORDER BY id DESC LIMIT 50').fetchall()
        for row in rows:
            payload=json.loads(row[0])
            if 'experiments' in payload:
                payload.setdefault('holdout_exposures',1)
                return payload
        return None

    def stats(self):
        with self.lock:
            observations=self.db.execute('SELECT COUNT(*),COUNT(DISTINCT ticker),MIN(ts),MAX(ts) FROM observations').fetchone()
            contracts=self.db.execute("SELECT COUNT(*) FROM contracts WHERE result IN ('yes','no')").fetchone()[0]
            gaps=[dict(r) for r in self.db.execute('SELECT venue,ts,reason FROM gaps ORDER BY id DESC LIMIT 15')]
        return {'observations':observations[0],'windows':observations[1],'first':observations[2],'last':observations[3],'settled_windows':contracts,'gaps':gaps,
                'raw_retention':'latest 200,000 raw messages; feature and quote journals retained'}

    def close(self):
        with self.lock:self.db.commit();self.db.close()
