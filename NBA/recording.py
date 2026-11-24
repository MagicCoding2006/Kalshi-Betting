"""Append-only public NBA score/market recorder. Standard library only; no orders."""
import gzip
import hashlib
import json
import math
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.client import HTTPException
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import DATA, atomic_json, instant, team, utcnow
from .markets import KALSHI, SERIES, match_market
from desk.model import normalize_book

SCOREBOARD='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard'
SCHEMA="""
CREATE TABLE IF NOT EXISTS payloads(hash TEXT PRIMARY KEY,body BLOB NOT NULL,raw_bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,cycle_id INTEGER,kind TEXT NOT NULL,url TEXT NOT NULL,
 requested_at TEXT NOT NULL,received_at TEXT NOT NULL,http_date TEXT,http_status INTEGER,error TEXT,payload_hash TEXT REFERENCES payloads(hash));
CREATE INDEX IF NOT EXISTS request_kind_time ON requests(kind,received_at);
CREATE TABLE IF NOT EXISTS games(game_id TEXT PRIMARY KEY,date TEXT NOT NULL,tipoff TEXT NOT NULL,season INTEGER,season_type INTEGER,
 home TEXT NOT NULL,away TEXT NOT NULL,venue TEXT,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY,game_id TEXT NOT NULL REFERENCES games(game_id),request_id INTEGER NOT NULL REFERENCES requests(id),
 observed_at TEXT NOT NULL,state TEXT NOT NULL,status_name TEXT,period INTEGER,clock_seconds REAL,display_clock TEXT,
 home_score INTEGER,away_score INTEGER,possession TEXT,home_timeouts INTEGER,away_timeouts INTEGER,home_fouls INTEGER,away_fouls INTEGER,
 home_bonus INTEGER,away_bonus INTEGER,quarters_json TEXT,source_updated_at TEXT,UNIQUE(request_id,game_id));
CREATE INDEX IF NOT EXISTS score_game_time ON scores(game_id,observed_at);
CREATE INDEX IF NOT EXISTS score_game_id ON scores(game_id,id);
CREATE TABLE IF NOT EXISTS markets(ticker TEXT PRIMARY KEY,game_id TEXT REFERENCES games(game_id),yes_team TEXT,
 status TEXT,result TEXT,settlement_value REAL,last_request_id INTEGER,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_states(id INTEGER PRIMARY KEY,ticker TEXT NOT NULL REFERENCES markets(ticker),request_id INTEGER NOT NULL,
 observed_at TEXT NOT NULL,status TEXT,result TEXT,settlement_value REAL,UNIQUE(ticker,request_id));
CREATE TABLE IF NOT EXISTS books(id INTEGER PRIMARY KEY,ticker TEXT NOT NULL REFERENCES markets(ticker),game_id TEXT NOT NULL,
 request_id INTEGER NOT NULL REFERENCES requests(id),score_id INTEGER REFERENCES scores(id),series_request_id INTEGER,
 observed_at TEXT NOT NULL,score_age_seconds REAL,quality TEXT NOT NULL,yes_bid REAL,yes_ask REAL,no_bid REAL,no_ask REAL,
 yes_levels INTEGER,no_levels INTEGER,UNIQUE(ticker,request_id));
CREATE INDEX IF NOT EXISTS book_game_time ON books(game_id,observed_at);
CREATE INDEX IF NOT EXISTS book_ticker_time ON books(ticker,observed_at);
CREATE INDEX IF NOT EXISTS book_ticker_id ON books(ticker,id);
CREATE TABLE IF NOT EXISTS forecasts(id INTEGER PRIMARY KEY,game_id TEXT NOT NULL,model_id TEXT NOT NULL,as_of TEXT NOT NULL,
 recorded_at TEXT NOT NULL,kind TEXT NOT NULL,payload_hash TEXT NOT NULL REFERENCES payloads(hash),UNIQUE(game_id,model_id,as_of,kind));
CREATE INDEX IF NOT EXISTS forecast_game_time ON forecasts(game_id,recorded_at);
PRAGMA user_version=1;
"""


def fetch_public(url):
    requested=utcnow().isoformat()
    result={'url':url,'requested_at':requested,'payload':None,'error':None,'http_status':None,'http_date':None}
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'NBA-Local-Research/1.0','Accept':'application/json','Accept-Encoding':'gzip'})
        with urllib.request.urlopen(req,timeout=8) as response:
            body=response.read(8_000_001)
            if len(body)>8_000_000:raise ValueError('Source exceeds 8 MB')
            if response.headers.get('Content-Encoding')=='gzip':body=gzip.decompress(body)
            if len(body)>16_000_000:raise ValueError('Expanded source exceeds 16 MB')
            result.update(payload=json.loads(body),http_status=response.status,http_date=response.headers.get('Date'))
            if not isinstance(result['payload'],dict):raise ValueError('Expected a JSON object from source')
    except (OSError,HTTPException,ValueError,TypeError) as exc:
        result['error']=str(exc);result['http_status']=getattr(exc,'code',None)
    result['received_at']=utcnow().isoformat()
    return result


def number(value,integer=False):
    try:
        n=float(value)
        if not math.isfinite(n) or n<0 or (integer and n!=int(n)):return None
        return int(n) if integer else n
    except (ValueError,TypeError):return None


def scoreboard_games(payload):
    """Never infer possession from the team credited with the last play."""
    if not isinstance(payload.get('events'),list):raise ValueError('Scoreboard events missing')
    rows=[];excluded=[]
    for event in payload['events']:
        try:
            competition=event['competitions'][0];s=event.get('status') or competition['status'];phase=s['type']
            competitors={p['homeAway']:p for p in competition['competitors']};h,a=competitors['home'],competitors['away']
            home,away=team(h['team']['abbreviation']),team(a['team']['abbreviation'])
            if home==away:raise ValueError('Same team on both sides')
            tip=instant(event['date']);state=phase['state'];period=number(s.get('period'),True);clock=number(s.get('clock'))
            if clock is not None and clock>(720 if (period or 0)<=4 else 300):raise ValueError('Invalid period clock')
            hs,aws=number(h.get('score'),True),number(a.get('score'),True)
            if state in ('in','post') and (hs is None or aws is None):raise ValueError('Live/final score missing')
            possession=next((code for player,code in [(h,home),(a,away)] if player.get('possession') is True),None)
            season=event.get('season',{})
            rows.append({'game_id':str(event['id']),'date':tip.astimezone(ZoneInfo('America/New_York')).date().isoformat(),
                'tipoff':tip.isoformat(),'season':season.get('year'),'season_type':season.get('type'),
                'home':home,'away':away,'venue':competition.get('venue',{}).get('fullName'),
                'state':state,'status_name':phase.get('name'),'period':period,'clock_seconds':clock,'display_clock':s.get('displayClock'),
                'home_score':hs,'away_score':aws,'possession':possession,
                'home_timeouts':number(h.get('timeoutsRemaining'),True),'away_timeouts':number(a.get('timeoutsRemaining'),True),
                'home_fouls':number(h.get('fouls'),True),'away_fouls':number(a.get('fouls'),True),
                'home_bonus':int(h['bonus']) if isinstance(h.get('bonus'),bool) else None,
                'away_bonus':int(a['bonus']) if isinstance(a.get('bonus'),bool) else None,
                'quarters_json':json.dumps({side:[{'period':p.get('period',i+1),'value':p.get('value')} for i,p in enumerate(player.get('linescores',[]))] for side,player in [('home',h),('away',a)]}),
                'source_updated_at':event.get('lastUpdated') or competition.get('lastUpdated')})
        except (ValueError,KeyError,TypeError,IndexError) as exc:excluded.append({'game_id':event.get('id'),'error':str(exc)})
    return rows,excluded


class Recording:
    def __init__(self,directory=DATA):
        self.directory=Path(directory);self.path=self.directory/'recordings.sqlite3';self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=20);self.db.row_factory=sqlite3.Row
        version=self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,1):raise ValueError('Unsupported NBA recording schema')
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA foreign_keys=ON');self.db.executescript(SCHEMA)

    def close(self):self.db.close()

    def payload(self,value):
        data=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode();digest=hashlib.sha256(data).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO payloads VALUES(?,?,?)',(digest,zlib.compress(data),len(data)))
        return digest

    def raw(self,digest):return json.loads(zlib.decompress(self.db.execute('SELECT body FROM payloads WHERE hash=?',(digest,)).fetchone()[0]))

    def request(self,cycle,kind,response):
        digest=self.payload(response['payload']) if response.get('payload') is not None else None
        return self.db.execute('INSERT INTO requests(cycle_id,kind,url,requested_at,received_at,http_date,http_status,error,payload_hash) VALUES(?,?,?,?,?,?,?,?,?)',
            (cycle,kind,response['url'],response['requested_at'],response['received_at'],response.get('http_date'),response.get('http_status'),response.get('error'),digest)).lastrowid

    def game(self,row,at):
        previous=self.db.execute('SELECT home,away FROM games WHERE game_id=?',(row['game_id'],)).fetchone()
        if previous and {previous['home'],previous['away']}!={row['home'],row['away']}:raise ValueError('Conflicting teams for recorded game identity')
        self.db.execute('INSERT INTO games VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET date=excluded.date,tipoff=excluded.tipoff,season=COALESCE(excluded.season,games.season),season_type=COALESCE(excluded.season_type,games.season_type),home=excluded.home,away=excluded.away,venue=excluded.venue,last_seen_at=excluded.last_seen_at',
            tuple(row.get(k) for k in ['game_id','date','tipoff','season','season_type','home','away','venue'])+(at,at))

    def score(self,row,request,at):
        self.game(row,at)
        fields=['game_id','request_id','observed_at','state','status_name','period','clock_seconds','display_clock','home_score','away_score','possession',
                'home_timeouts','away_timeouts','home_fouls','away_fouls','home_bonus','away_bonus','quarters_json','source_updated_at']
        value={**row,'request_id':request,'observed_at':at}
        return self.db.execute('INSERT OR IGNORE INTO scores('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',tuple(value.get(k) for k in fields)).lastrowid

    def fixtures(self):return [dict(r) for r in self.db.execute('SELECT * FROM games')]

    def market(self,row,request,at,fixtures):
        existing=self.db.execute('SELECT * FROM markets WHERE ticker=?',(row['ticker'],)).fetchone()
        # Identity matching also applies to closed contracts. This status copy
        # is only for the join; the real status and raw response are preserved.
        matched=match_market({**row,'status':'active'},fixtures)
        game_id,yes_team=(matched[0]['game_id'],matched[1]) if matched else ((existing['game_id'],existing['yes_team']) if existing else (None,None))
        value=number(row.get('settlement_value_dollars'))
        self.db.execute('INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET game_id=excluded.game_id,yes_team=excluded.yes_team,status=excluded.status,result=excluded.result,settlement_value=excluded.settlement_value,last_request_id=excluded.last_request_id,last_seen_at=excluded.last_seen_at',
            (row['ticker'],game_id,yes_team,row.get('status'),row.get('result'),value,request,at,at))
        self.db.execute('INSERT OR IGNORE INTO market_states(ticker,request_id,observed_at,status,result,settlement_value) VALUES(?,?,?,?,?,?)',
            (row['ticker'],request,at,row.get('status'),row.get('result'),value))

    def book(self,ticker,response,request,series_request,max_score_age=30):
        market=self.db.execute('SELECT * FROM markets WHERE ticker=?',(ticker,)).fetchone()
        if market is None or market['game_id'] is None:return
        at=response['received_at'];score=self.db.execute('SELECT * FROM scores WHERE game_id=? AND observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT 1',(market['game_id'],at)).fetchone()
        age=(instant(at)-instant(score['observed_at'])).total_seconds() if score else None
        quality='request_failed' if response.get('error') else 'ok'
        payload=response.get('payload')
        valid_payload=isinstance(payload,dict) and any(key in payload for key in ('orderbook_fp','orderbook'))
        if quality=='ok' and not valid_payload:quality='invalid_payload'
        book=normalize_book(payload if valid_payload else {})
        if quality=='ok' and not book['yes_bids'] and not book['no_bids']:quality='empty'
        if quality=='ok' and book['yes_bids'] and book['yes_asks'] and book['yes_bids'][0]['price']>book['yes_asks'][0]['price']:quality='crossed'
        if quality=='ok' and (age is None or age>max_score_age):quality='score_stale_or_missing'
        top=lambda key:book[key][0]['price'] if book[key] else None
        self.db.execute('INSERT INTO books(ticker,game_id,request_id,score_id,series_request_id,observed_at,score_age_seconds,quality,yes_bid,yes_ask,no_bid,no_ask,yes_levels,no_levels) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ticker,market['game_id'],request,score['id'] if score else None,series_request,at,age,quality,
             top('yes_bids'),top('yes_asks'),top('no_bids'),top('no_asks'),len(book['yes_bids']),len(book['no_bids'])))

    def forecasts(self,at):
        path=self.directory/'fair-prices.json'
        if not path.exists():return
        saved=json.loads(path.read_text());as_of=saved['generated_at']
        if instant(as_of)>instant(at):return
        relevant={r[0] for r in self.db.execute('SELECT game_id FROM games WHERE tipoff BETWEEN ? AND ?',
            ((instant(at)-timedelta(days=2)).isoformat(),(instant(at)+timedelta(days=2)).isoformat()))}
        for game in saved['games']:
            if game['game_id'] not in relevant:continue
            p=game.get('prediction')
            if p is None or instant(as_of)>=instant(game['tipoff']):continue
            self.db.execute('INSERT OR IGNORE INTO forecasts(game_id,model_id,as_of,recorded_at,kind,payload_hash) VALUES(?,?,?,?,?,?)',
                (game['game_id'],saved['model_id'],as_of,at,'issued_pregame',self.payload({'prediction':p,'home':game['home'],'away':game['away'],'features':game.get('features'),
                 'context':game.get('context'),'availability_probability_range':game.get('availability_probability_range'),
                 'blockers':game.get('blockers',[]),'tipoff':game['tipoff']})))

    def counts(self):
        names=['games','scores','books','forecasts','requests','payloads']
        result={n:self.db.execute('SELECT count(*) FROM '+n).fetchone()[0] for n in names}
        result['in_game_books']=self.db.execute("SELECT count(*) FROM books b JOIN scores s ON b.score_id=s.id WHERE s.state='in' AND b.quality='ok'").fetchone()[0]
        result['usable_books']=self.db.execute("SELECT count(*) FROM books WHERE quality='ok'").fetchone()[0]
        result['failed_requests']=self.db.execute('SELECT count(*) FROM requests WHERE error IS NOT NULL').fetchone()[0]
        return result


class Collector:
    def __init__(self,directory=DATA,interval=10,idle_interval=60,fetch=fetch_public):
        self.store=Recording(directory);self.fetch=fetch;self.interval=interval;self.idle_interval=idle_interval
        self.last_day={};self.last_summary={};self.last_discovery=None;self.last_closed=None;self.series_request=None;self.started_at=utcnow().isoformat()
        path=Path(directory)/'dataset.json'
        if path.exists():
            dataset=json.loads(path.read_text());season=utcnow().year+int(utcnow().month>=9)
            for g in dataset['schedule']:
                if g['season']==season:self.store.game(g,self.started_at)
            self.store.db.commit()

    def cycle(self,now=None):
        now=now or utcnow();db=self.store.db;cycle=db.execute('INSERT INTO cycles(started_at) VALUES(?)',(now.isoformat(),)).lastrowid
        errors=[];excluded=[]
        def save(kind,response):
            identifier=self.store.request(cycle,kind,response)
            if response.get('error'):errors.append({'kind':kind,'url':response['url'],'error':response['error'],'http_status':response.get('http_status')})
            return identifier
        today=now.astimezone(ZoneInfo('America/New_York')).date();jobs=[]
        for offset in (-1,0,1):
            day=(today+timedelta(days=offset)).isoformat()
            active=db.execute("SELECT 1 FROM scores s JOIN games g ON s.game_id=g.game_id WHERE g.date=? AND s.state='in' AND s.id=(SELECT max(id) FROM scores WHERE game_id=g.game_id) LIMIT 1",(day,)).fetchone()
            upcoming=db.execute('SELECT 1 FROM games WHERE date=? AND tipoff BETWEEN ? AND ? LIMIT 1',
                (day,now.isoformat(),(now+timedelta(minutes=90)).isoformat())).fetchone()
            ttl=self.interval if offset==0 or active or upcoming else 300
            if day not in self.last_day or (now-self.last_day[day]).total_seconds()>=ttl:
                jobs.append((day,SCOREBOARD+'?dates='+day.replace('-','')+'&limit=1000'))
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses=list(pool.map(self.fetch,[url for _,url in jobs]))
        for (day,_),response in zip(jobs,responses):
            request=save('scoreboard',response)
            if response.get('error'):continue
            try:
                games,rejected=scoreboard_games(response['payload']);excluded.extend(rejected)
                for row in games:self.store.score(row,request,response['received_at'])
                self.last_day[day]=now
            except (ValueError,KeyError,TypeError) as exc:
                db.execute('UPDATE requests SET error=? WHERE id=?',(str(exc),request));errors.append({'kind':'scoreboard_parse','error':str(exc)})
        fixtures=self.store.fixtures()
        # Keep the provider's fuller game summaries/box scores/play records for
        # later feature research. Their receipt time is preserved; missing
        # fields are never synthesized into the normalized scoreboard.
        summaries=[]
        for row in db.execute("SELECT s.game_id,s.state FROM scores s JOIN games g ON s.game_id=g.game_id WHERE g.date>=? AND s.state IN ('in','post') AND s.id=(SELECT max(id) FROM scores WHERE game_id=g.game_id)",((today-timedelta(days=1)).isoformat(),)):
            ttl=60 if row['state']=='in' else 3600
            if row['game_id'] not in self.last_summary or (now-self.last_summary[row['game_id']]).total_seconds()>=ttl:summaries.append(row['game_id'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses=list(pool.map(self.fetch,['https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+g for g in summaries]))
        for game_id,response in zip(summaries,responses):
            save('game_summary',response)
            if not response.get('error'):self.last_summary[game_id]=now
        if self.last_discovery is None or (now-self.last_discovery).total_seconds()>=60:
            series=self.fetch(KALSHI+'/series/'+SERIES);request=save('series',series)
            if not series.get('error') and isinstance(series.get('payload',{}).get('series'),dict):self.series_request=request
            cursor='';seen=set();discovered=set();complete=True
            for _ in range(20):
                response=self.fetch(KALSHI+'/markets?'+urllib.parse.urlencode({'series_ticker':SERIES,'status':'open','limit':200,'cursor':cursor}))
                request=save('market_discovery',response)
                if response.get('error'):complete=False;break
                payload=response['payload']
                if not isinstance(payload.get('markets'),list):errors.append({'kind':'market_discovery','error':'Market list missing'});complete=False;break
                for market in payload['markets']:
                    try:self.store.market(market,request,response['received_at'],fixtures);discovered.add(market['ticker'])
                    except (ValueError,KeyError,TypeError) as exc:errors.append({'kind':'market_parse','error':str(exc)})
                cursor=payload.get('cursor')
                if not cursor:break
                if cursor in seen:errors.append({'kind':'market_discovery','error':'Repeated cursor'});complete=False;break
                seen.add(cursor)
            else:complete=False;errors.append({'kind':'market_discovery','error':'Pagination limit'})
            # Open-list disappearance is not a settlement result. Fetch the
            # tracked contract until its real status/result has been received.
            missing=[r['ticker'] for r in db.execute("SELECT m.* FROM markets m JOIN games g ON m.game_id=g.game_id WHERE m.status NOT IN ('settled','finalized') AND g.tipoff>=?",((now-timedelta(days=7)).isoformat(),)) if r['ticker'] not in discovered]
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses=list(pool.map(self.fetch,[KALSHI+'/markets/'+urllib.parse.quote(t,safe='') for t in missing]))
            for ticker,response in zip(missing,responses):
                request=save('market_status',response)
                if response.get('error'):continue
                try:self.store.market(response['payload']['market'],request,response['received_at'],fixtures)
                except (ValueError,KeyError,TypeError) as exc:errors.append({'kind':'market_status_parse','ticker':ticker,'error':str(exc)})
            if complete:self.last_discovery=now
        if self.last_closed is None or (now-self.last_closed).total_seconds()>=300:
            for status in ('closed','settled'):
                cursor='';seen=set()
                for _ in range(20):
                    url=KALSHI+'/markets?'+urllib.parse.urlencode({'series_ticker':SERIES,'status':status,'limit':200,'cursor':cursor,
                        'min_close_ts' if status=='closed' else 'min_settled_ts':int((now-timedelta(hours=48)).timestamp())})
                    response=self.fetch(url);request=save('recent_'+status,response)
                    if response.get('error'):break
                    if not isinstance(response.get('payload',{}).get('markets'),list):errors.append({'kind':'recent_market_parse','error':'Missing market list'});break
                    for market in response['payload']['markets']:
                        try:self.store.market(market,request,response['received_at'],fixtures)
                        except (ValueError,KeyError,TypeError) as exc:errors.append({'kind':'recent_market_parse','error':str(exc)})
                    cursor=response['payload'].get('cursor')
                    if not cursor:break
                    if cursor in seen:errors.append({'kind':'recent_market_parse','error':'Repeated cursor'});break
                    seen.add(cursor)
                else:errors.append({'kind':'recent_market_parse','error':'Pagination limit'})
            self.last_closed=now
        eligible=list(db.execute("SELECT m.ticker FROM markets m JOIN games g ON m.game_id=g.game_id WHERE m.status IN ('active','open') AND g.tipoff BETWEEN ? AND ? ORDER BY g.tipoff,m.ticker",
            ((now-timedelta(hours=48)).isoformat(),(now+timedelta(hours=48)).isoformat())))
        tickers=[r['ticker'] for r in eligible]
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses=list(pool.map(self.fetch,[KALSHI+'/markets/'+urllib.parse.quote(t,safe='')+'/orderbook?depth=100' for t in tickers]))
        for ticker,response in sorted(zip(tickers,responses),key=lambda pair:pair[1]['received_at']):
            request=save('orderbook',response)
            try:self.store.book(ticker,response,request,self.series_request)
            except (ValueError,KeyError,TypeError) as exc:
                db.execute('UPDATE requests SET error=? WHERE id=?',(str(exc),request));errors.append({'kind':'orderbook_parse','ticker':ticker,'error':str(exc)})
        finished=utcnow();self.store.forecasts(finished.isoformat())
        state_counts={r['state']:r['n'] for r in db.execute('SELECT s.state,count(*) n FROM scores s JOIN games g ON s.game_id=g.game_id WHERE g.date=? AND s.id=(SELECT max(id) FROM scores WHERE game_id=g.game_id) GROUP BY s.state',(today.isoformat(),))}
        active=db.execute("SELECT 1 FROM scores s JOIN games g ON s.game_id=g.game_id WHERE s.state='in' AND g.date>=? AND s.id=(SELECT max(id) FROM scores WHERE game_id=g.game_id) LIMIT 1",((today-timedelta(days=1)).isoformat(),)).fetchone() is not None
        soon=db.execute('SELECT 1 FROM games WHERE tipoff BETWEEN ? AND ? LIMIT 1',(now.isoformat(),(now+timedelta(minutes=90)).isoformat())).fetchone()
        delay=self.interval if active or soon or tickers else self.idle_interval
        if any(e.get('http_status')==429 for e in errors):delay=max(delay,60)
        db.execute('UPDATE cycles SET finished_at=?,status=? WHERE id=?',(finished.isoformat(),'partial' if errors else 'ok',cycle));db.commit()
        result={'schema':1,'running':True,'started_at':self.started_at,'last_cycle_at':finished.isoformat(),
            'cycle_seconds':(finished-now).total_seconds(),'next_interval_seconds':delay,'database_path':str(self.store.path.resolve()),
            'counts':self.store.counts(),'today':today.isoformat(),'today_states':state_counts,'books_this_cycle':len(tickers),
            'errors':errors,'excluded_games':excluded,'raw_format':'SHA-256 deduplicated zlib JSON in SQLite',
            'scope':'All observed NBA games; available Kalshi KXNBAGAME winner books, including live play.',
            'limitations':['Polling samples, not a complete tick feed.','HTTP receipt time does not prove the score feed or order book is current.',
                           'Possession, fouls, timeouts and bonus stay null when the feed does not supply them.',
                           'No collection occurs while this computer is asleep or the collector is stopped.']}
        atomic_json(self.store.directory/'collector-status.json',result)
        return result


def export_recordings(directory,output,since=None):
    """Stream a consistent read-only snapshot as gzip JSONL, including raw data."""
    directory=Path(directory);output=Path(output)
    database=directory/'recordings.sqlite3'
    if output.resolve()==database.resolve():raise ValueError('Export cannot replace the database')
    db=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('BEGIN')
    lower=instant(since).isoformat() if since else '0000'
    output.parent.mkdir(parents=True,exist_ok=True);count=0
    try:
        with gzip.open(output,'xt',encoding='utf-8') as stream:
            def write(value):stream.write(json.dumps(value,separators=(',',':'),allow_nan=False)+'\n')
            write({'type':'manifest','schema':1,'exported_at':utcnow().isoformat(),'since':since,
                   'note':'Games/markets are export-time metadata; historical states are observations.'})
            for table in ['games','markets']:
                for row in db.execute('SELECT * FROM '+table):write({'type':table,**dict(row)});count+=1
            for row in db.execute('SELECT r.*,p.body FROM requests r LEFT JOIN payloads p ON r.payload_hash=p.hash WHERE r.received_at>=? ORDER BY r.id',(lower,)):
                value=dict(row);body=value.pop('body');write({'type':'request',**value,'payload':json.loads(zlib.decompress(body)) if body else None});count+=1
            for table,column in [('scores','observed_at'),('books','observed_at'),('market_states','observed_at'),('forecasts','recorded_at')]:
                for row in db.execute('SELECT * FROM '+table+' WHERE '+column+'>=? ORDER BY id',(lower,)):
                    value=dict(row)
                    if table=='forecasts':value['payload']=json.loads(zlib.decompress(db.execute('SELECT body FROM payloads WHERE hash=?',(row['payload_hash'],)).fetchone()[0]))
                    write({'type':table,**value});count+=1
    finally:db.close()
    return {'output':str(output.resolve()),'records':count,'bytes':output.stat().st_size}
