"""Separate research database: bounded raw books, features, reference history, reports."""
import json
import math
import sqlite3
import threading
import time
import zlib
from pathlib import Path


class Store:
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False);self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL NOT NULL,ticker TEXT NOT NULL,
            mid REAL,bid REAL,ask REAL,spread REAL,bid_qty REAL,ask_qty REAL,imbalance REAL,microprice REAL,
            momentum REAL,seconds_left REAL,spot REAL,spot_time REAL,strike REAL,fee_multiplier REAL,raw BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS snap_time ON snapshots(ts);
          CREATE INDEX IF NOT EXISTS snap_ticker ON snapshots(ticker,ts);
          CREATE TABLE IF NOT EXISTS candles(ts INTEGER PRIMARY KEY,close REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,model TEXT NOT NULL,payload TEXT NOT NULL);
        ''')
        self.db.commit();self.previous=None;self.writes=0
        with self.lock:self.count=self.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]

    def record(self,book,ticker,observed,seconds_left,reference,strike,multiplier):
        bids,asks=book.get('yes_bids',[]),book.get('yes_asks',[])
        bid=bids[0]['price'] if bids else None;ask=asks[0]['price'] if asks else None
        valid=bid is not None and ask is not None and bid<=ask
        mid=(bid+ask)/2 if valid else None
        bq=bids[0]['quantity'] if bids else 0.;aq=asks[0]['quantity'] if asks else 0.
        bd=sum(l['quantity'] for l in bids[:5]);ad=sum(l['quantity'] for l in asks[:5]);depth=bd+ad
        imbalance=(bd-ad)/depth if depth else 0.
        micro=(ask*bq+bid*aq)/(bq+aq) if valid and bq+aq else mid
        previous=self.previous
        momentum=mid-previous['mid'] if mid is not None and previous and previous['ticker']==ticker and previous['mid'] is not None and 0<observed-previous['ts']<=2 else 0.
        self.previous={'ticker':ticker,'mid':mid,'ts':observed}
        raw={k:v[:10] for k,v in book.items()}
        row=(observed,ticker,mid,bid,ask,ask-bid if valid else None,bq,aq,imbalance,micro,momentum,
             seconds_left,reference.get('price'),reference.get('time'),strike,multiplier,zlib.compress(json.dumps(raw,separators=(',',':')).encode()))
        with self.lock:
            self.db.execute('INSERT INTO snapshots(ts,ticker,mid,bid,ask,spread,bid_qty,ask_qty,imbalance,microprice,momentum,seconds_left,spot,spot_time,strike,fee_multiplier,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
            self.count+=1;self.writes+=1
            if self.writes%1000==0:
                self.db.execute('DELETE FROM snapshots WHERE ts<?',(observed-3*86400,))
                self.count=self.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
            self.db.commit()

    def rows(self,limit=30000):
        with self.lock:
            rows=self.db.execute('SELECT id,ts,ticker,mid,bid,ask,spread,bid_qty,ask_qty,imbalance,microprice,momentum,seconds_left,spot,spot_time,strike,fee_multiplier FROM snapshots ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_candles(self,candles):
        values=[]
        for c in candles:
            if all(math.isfinite(c[k]) and c[k]>0 for k in ('close','high','low')) and c['low']<=c['close']<=c['high']:
                values.append((int(c['ts']),c['close'],c['high'],c['low']))
        with self.lock:
            self.db.executemany('INSERT OR REPLACE INTO candles VALUES(?,?,?,?)',values)
            self.db.execute('DELETE FROM candles WHERE ts<?',(time.time()-7*86400,));self.db.commit()

    def candles(self):
        with self.lock:return [dict(r) for r in self.db.execute('SELECT * FROM candles ORDER BY ts')]

    def save_report(self,report):
        with self.lock:
            self.db.execute('INSERT INTO reports(created,model,payload) VALUES(?,?,?)',(time.time(),report['model'],json.dumps(report,allow_nan=False)))
            self.db.execute('DELETE FROM reports WHERE id<=(SELECT COALESCE(MAX(id),0)-20 FROM reports)');self.db.commit()

    def settings(self):
        with self.lock:
            row=self.db.execute("SELECT value FROM settings WHERE key='research'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_settings(self,settings):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('research',?)",(json.dumps(settings),));self.db.commit()

    def reports(self):
        with self.lock:rows=self.db.execute('SELECT model,payload FROM reports ORDER BY id DESC LIMIT 20').fetchall()
        latest={}
        for row in rows:
            if row['model'] not in latest:latest[row['model']]=json.loads(row['payload'])
        return list(latest.values())

    def stats(self):
        rows=self.rows(121)
        gaps=[b['ts']-a['ts'] for a,b in zip(rows,rows[1:]) if a['ticker']==b['ticker']]
        with self.lock:
            first=self.db.execute('SELECT ts FROM snapshots ORDER BY ts LIMIT 1').fetchone()
            h=self.db.execute('SELECT COUNT(*),MIN(ts),MAX(ts) FROM candles').fetchone()
        disk=sum(p.stat().st_size for p in self.path.parent.glob(self.path.name+'*') if p.is_file())
        return {'snapshots':self.count,'first':first[0] if first else None,'last':rows[-1]['ts'] if rows else None,
                'actual_interval_ms':sorted(gaps)[len(gaps)//2]*1000 if gaps else None,'target_interval_ms':500,
                'history_candles':h[0],'history_start':h[1],'history_end':h[2],
                'history_hours':(h[2]-h[1])/3600 if h[1] is not None and h[2] is not None else 0,
                'storage_mb':disk/1048576,'retention_days':3}

    def close(self):
        with self.lock:self.db.close()
