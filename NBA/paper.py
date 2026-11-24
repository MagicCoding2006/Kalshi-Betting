"""Fixed, unfunded exit-policy replay on newly recorded observations. No orders."""
import hashlib
import json
import math
import pickle
import sqlite3
import zlib
from datetime import timedelta
from pathlib import Path

from .data import DATA,atomic_json,instant,utcnow
from .markets import fee_terms,sweep
from desk.model import normalize_book


def prepare_checkpoints(directory=DATA):
    """Reproduce the earlier research checkpoint; save separately from live model."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from .data import load_dataset
    from .features import training_rows
    from .quarters import load_quarters
    from .model import logit,span
    directory=Path(directory);dataset=load_dataset(directory)
    rows=training_rows(dataset['games']);quarters,_=load_quarters(directory,2025)
    prior=[r for r in rows if r['season']==2025 and r['game_id'] in quarters]
    days=sorted({r['date'] for r in prior});cut=days[int(len(days)*.7)]
    cal=[r for r in prior if r['date']>=cut];first=min(instant(r['tipoff']) for r in cal)
    fit=[r for r in prior if r['date']<cut and instant(r['label_available_at'])<first]
    bundle={'kind':'quarter_checkpoint_research','trained_through':max(r['label_available_at'] for r in cal),'models':{}}
    for period in (1,2):
        def x(samples):
            return np.column_stack((logit(np.array([r['elo_probability'] for r in samples])).ravel(),
                [sum(quarters[r['game_id']]['home'][:period])-sum(quarters[r['game_id']]['away'][:period]) for r in samples]))
        xf,xc=x(fit),x(cal);scaler=StandardScaler().fit(xf)
        lr=LogisticRegression(C=1,max_iter=1200).fit(scaler.transform(xf),[r['y'] for r in fit])
        calibrator=LogisticRegression(C=1,max_iter=1200).fit(logit(lr.predict_proba(scaler.transform(xc))[:,1]),[r['y'] for r in cal])
        bundle['models'][period]={'scaler':scaler,'classifier':lr,'calibrator':calibrator}
    path=directory/'paper'/'checkpoint-model.pkl';path.parent.mkdir(parents=True,exist_ok=True)
    content=pickle.dumps(bundle,protocol=pickle.HIGHEST_PROTOCOL);path.write_bytes(content)
    report={'kind':bundle['kind'],'sha256':hashlib.sha256(content).hexdigest(),'created_at':utcnow().isoformat(),
            'fit':span(fit),'calibration':span(cal),'trained_through':bundle['trained_through'],
            'scope':'Only observed end-Q1 / halftime breaks with clock zero. Never an all-clock live model.',
            'inputs':['Past-only pregame Elo','current cumulative score margin'],
            'limitations':['Research checkpoint, not validated against market prices.','No possession, foul or lineup adjustment.']}
    atomic_json(directory/'paper'/'checkpoint-report.json',report)
    return report


def checkpoint_probability(bundle,score,features):
    """Do not carry a quarter-end estimate into the next quarter."""
    if not bundle or score.get('state')!='in' or score.get('period') not in (1,2) or score.get('clock_seconds')!=0:return None
    if 'elo_diff' not in features or 'home_court' not in features:return None
    from .model import logit
    import numpy as np
    model=bundle['models'][score['period']]
    x=np.array([[(features['elo_diff']+65*features['home_court'])*math.log(10)/400,score['home_score']-score['away_score']]])
    raw=model['classifier'].predict_proba(model['scaler'].transform(x))[:,1]
    return float(model['calibrator'].predict_proba(logit(raw))[0,1])


def partial_sweep(levels,quantity,multiplier):
    available=int(math.floor(sum(level['quantity'] for level in levels)+1e-9))
    return sweep(levels,min(quantity,available),multiplier) if available else None


class PaperStudy:
    def __init__(self,directory=DATA,checkpoint_bundle=None):
        self.directory=Path(directory);self.registration=json.loads((self.directory/'paper'/'registration.json').read_text())
        self.source=sqlite3.connect((self.directory/'recordings.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=10);self.source.row_factory=sqlite3.Row
        self.path=self.directory/'paper'/'study.sqlite3';self.db=sqlite3.connect(self.path,timeout=10);self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(game_id TEXT PRIMARY KEY,observed_at TEXT NOT NULL,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,game_id TEXT,policy TEXT,observed_at TEXT,kind TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS outcomes(game_id TEXT,policy TEXT,closed_at TEXT,pnl REAL,flags TEXT,PRIMARY KEY(game_id,policy));''')
        saved=self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        fingerprint=hashlib.sha256(json.dumps(self.registration,sort_keys=True).encode()).hexdigest()
        self.state=json.loads(saved[0]) if saved else {'last_book_id':0,'last_event_at':None,'open':{},'pending':{},'seen':[],
            'registration_hash':fingerprint,'first_run_at':utcnow().isoformat(),'unfilled_entries':0}
        if self.state['registration_hash']!=fingerprint:
            self.close();raise ValueError('Paper registration changed; use a new study directory')
        code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        versions=self.state.setdefault('engine_versions',[])
        if not versions or versions[-1]['sha256']!=code_hash:versions.append({'sha256':code_hash,'first_seen_at':utcnow().isoformat()})
        self.bundle=checkpoint_bundle

    def close(self):self.source.close();self.db.close()

    def raw(self,digest):return json.loads(zlib.decompress(self.source.execute('SELECT body FROM payloads WHERE hash=?',(digest,)).fetchone()[0]))

    def quote(self,row):
        request=self.source.execute('SELECT payload_hash,error FROM requests WHERE id=?',(row['request_id'],)).fetchone()
        if request is None or request['error'] or row['quality']!='ok':return None
        return normalize_book(self.raw(request['payload_hash']))

    def multiplier(self,row):
        r=self.source.execute('SELECT payload_hash FROM requests WHERE id=? AND received_at<=?',(row['series_request_id'],row['observed_at'])).fetchone()
        return fee_terms(self.raw(r['payload_hash']).get('series',{})) if r else None

    def forecast(self,game_id,at):
        r=self.source.execute("SELECT * FROM forecasts WHERE game_id=? AND model_id=? AND recorded_at<=? AND as_of<=? AND kind='issued_pregame' ORDER BY as_of DESC,id DESC LIMIT 1",
            (game_id,self.registration['model_id'],at,at)).fetchone()
        if r is None or (instant(at)-instant(r['as_of'])).total_seconds()>self.registration['max_forecast_age_seconds']:return None
        return dict(r),self.raw(r['payload_hash'])

    def eligible(self,row):
        r=self.registration;at=row['observed_at'];tip=instant(row['tipoff']);lead=(tip-instant(at)).total_seconds()
        if instant(at)<instant(r['registered_at']) or not r['minimum_entry_lead_seconds']<=lead<=r['maximum_entry_lead_seconds']:return None
        if row['state']!='pre' or row['status_name'] in ('STATUS_POSTPONED','STATUS_CANCELED','STATUS_SUSPENDED') or row['score_age_seconds'] is None or row['score_age_seconds']>r['max_book_score_age_seconds']:return None
        saved=self.forecast(row['game_id'],at);book=self.quote(row);multiplier=self.multiplier(row)
        if saved is None or book is None or multiplier is None:return None
        record,payload=saved
        if payload.get('blockers'):return None
        if payload.get('home',row['home'])!=row['home'] or payload.get('away',row['away'])!=row['away']:return None
        home_p=payload['prediction']['home_win_probability'];bounds=payload.get('availability_probability_range') or {'home_min':home_p,'home_max':home_p}
        options=[]
        for side in ('yes','no'):
            outcome=row['yes_team'] if side=='yes' else (row['away'] if row['yes_team']==row['home'] else row['home'])
            p=bounds['home_min'] if outcome==row['home'] else 1-bounds['home_max']
            fill=sweep(book[side+'_asks'],r['quantity'],multiplier)
            if not fill:continue
            cost=fill['price_cost']+fill['fee_upper_estimate']+r['quantity']*r['slippage_per_contract'];edge=p-cost/r['quantity']
            if edge>=r['minimum_net_edge']:options.append({'side':side,'outcome_team':outcome,'signal_edge':edge,'signal_at':at,
                'ticker':row['ticker'],'game_id':row['game_id'],'tipoff':row['tipoff'],'home':row['home'],'away':row['away'],
                'forecast_id':record['id'],'features':payload.get('features') or {},'signal_book_id':row['id']})
        return max(options,key=lambda q:q['signal_edge']) if options else None

    def open_entry(self,pending,row):
        r=self.registration;at=row['observed_at'];delay=(instant(at)-instant(pending['signal_at'])).total_seconds()
        if delay<r['execution_latency_seconds']:return False
        if delay>r['max_execution_gap_seconds'] or (instant(row['tipoff'])-instant(at)).total_seconds()<r['minimum_entry_lead_seconds']:
            self.state['unfilled_entries']+=1;return True
        book=self.quote(row);multiplier=self.multiplier(row)
        if not book or multiplier is None or row['state']!='pre':return False
        fill=sweep(book[pending['side']+'_asks'],r['quantity'],multiplier)
        if not fill:return False
        # Recheck edge at the later execution observation; do not fill an
        # attractive old ask after the market moved through the entry limit.
        candidate=self.eligible(row)
        if not candidate or candidate['side']!=pending['side']:return False
        entry={**pending,'entry_at':at,'entry_book_id':row['id'],'entry_vwap':fill['vwap'],
               'cost':fill['price_cost']+fill['fee_upper_estimate']+r['quantity']*r['slippage_per_contract'],
               'quantity':r['quantity'],'multiplier':multiplier,
               'policies':{name:{'remaining':r['quantity'],'proceeds':0.,'pending':None,'flags':[],'partial_taken':False} for name in r['policies']}}
        self.state['open'][row['game_id']]=entry
        self.db.execute('INSERT INTO entries VALUES(?,?,?)',(row['game_id'],at,json.dumps(entry)))
        return True

    def fill_event(self,entry,name,position,at,kind,value):
        self.db.execute('INSERT INTO fills(game_id,policy,observed_at,kind,body) VALUES(?,?,?,?,?)',
            (entry['game_id'],name,at,kind,json.dumps(value)))
        if position['remaining']==0:
            self.db.execute('INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?)',(entry['game_id'],name,at,position['proceeds']-entry['cost'],json.dumps(position['flags'])))

    def update_position(self,entry,name,position,row):
        if position['remaining']==0 or row['ticker']!=entry['ticker']:return
        book=self.quote(row)
        if not book:return
        at=row['observed_at'];r=self.registration;pending=position['pending']
        if pending and (instant(at)-instant(pending['signal_at'])).total_seconds()>=r['execution_latency_seconds']:
            delay=(instant(at)-instant(pending['signal_at'])).total_seconds()
            if delay>r['max_execution_gap_seconds'] and 'execution_gap' not in position['flags']:position['flags'].append('execution_gap')
            amount=min(position['remaining'],pending['quantity'])
            fill=partial_sweep(book[entry['side']+'_bids'],amount,entry['multiplier'])
            if not fill:
                if 'insufficient_exit_depth' not in position['flags']:position['flags'].append('insufficient_exit_depth')
                return
            proceeds=fill['price_cost']-fill['fee_upper_estimate']-fill['quantity']*r['slippage_per_contract']
            position['proceeds']+=proceeds;position['remaining']-=fill['quantity'];pending['quantity']-=fill['quantity']
            self.fill_event(entry,name,position,at,pending['reason'],{'book_id':row['id'],**fill,'net_proceeds':proceeds,'signal_at':pending['signal_at']})
            if pending['quantity']==0:position['pending']=None
            return
        if pending or name=='settlement_hold':return
        fill=sweep(book[entry['side']+'_bids'],position['remaining'],entry['multiplier'])
        if not fill:return
        change=fill['vwap']-entry['entry_vwap'];held=(instant(at)-instant(entry['entry_at'])).total_seconds();reason=None;amount=position['remaining']
        if name in ('target10_stop5','target5_stop5'):
            target=.10 if name=='target10_stop5' else .05
            if change>=target-1e-9:reason='target'
            elif change<=-.05+1e-9:reason='stop'
        elif name=='time20m_before_tipoff':
            if held>=1200 or instant(at)>=instant(entry['tipoff'])-timedelta(seconds=30):reason='time'
        elif name=='partial_target10' and not position['partial_taken'] and change>=.10-1e-9:
            reason='partial_target';amount=max(1,entry['quantity']//2);position['partial_taken']=True
        elif name=='checkpoint_fair_value':
            # A pregame estimate is never reused as an in-game fair value.
            p=None
            if row['state']=='in':p=checkpoint_probability(self.bundle,dict(row),entry['features'])
            elif row['state']=='pre':
                saved=self.forecast(entry['game_id'],at)
                if saved:p=saved[1]['prediction']['home_win_probability']
            if p is not None:
                p=p if entry['outcome_team']==entry['home'] else 1-p
                net_bid=(fill['price_cost']-fill['fee_upper_estimate'])/position['remaining']-r['slippage_per_contract']
                if net_bid>=p+.01:reason='updated_fair_value'
        if reason is None and held>=r['maximum_policy_hold_seconds'] and name not in ('partial_target10',):reason='maximum_hold_time'
        if reason:position['pending']={'signal_at':at,'quantity':amount,'reason':reason}

    def step(self,now=None):
        now=now or utcnow();state=self.state;r=self.registration
        rows=self.source.execute('''SELECT b.*,g.tipoff,g.home,g.away,m.yes_team,s.state,s.status_name,s.period,s.clock_seconds,s.home_score,s.away_score
            FROM books b JOIN games g ON b.game_id=g.game_id JOIN markets m ON b.ticker=m.ticker LEFT JOIN scores s ON b.score_id=s.id
            WHERE b.id>? AND b.observed_at<=? ORDER BY b.id LIMIT 5000''',(state['last_book_id'],now.isoformat())).fetchall()
        # A cycle's HTTP responses can finish in a different order from its DB
        # writes. Process the captured batch by receipt time, never by outcome.
        for row in sorted(rows,key=lambda v:(v['observed_at'],v['id'])):
            at=row['observed_at'];game_id=row['game_id']
            if state['last_event_at'] and instant(at)<instant(state['last_event_at']):continue
            state['last_event_at']=at
            if game_id in state['open']:
                entry=state['open'][game_id]
                for name,p in entry['policies'].items():self.update_position(entry,name,p,row)
            elif game_id in state['pending']:
                pending=state['pending'][game_id]
                if row['ticker']==pending['ticker'] and self.open_entry(pending,row):del state['pending'][game_id]
            elif game_id not in state['seen']:
                candidate=self.eligible(row)
                if candidate:
                    # Compare only the other game's quotes already observed at
                    # this instant; never use a later, cheaper route.
                    alternatives=self.source.execute('''SELECT b.*,g.tipoff,g.home,g.away,m.yes_team,s.state,s.status_name,s.period,s.clock_seconds,s.home_score,s.away_score
                        FROM books b JOIN games g ON b.game_id=g.game_id JOIN markets m ON b.ticker=m.ticker LEFT JOIN scores s ON b.score_id=s.id
                        WHERE b.game_id=? AND b.observed_at<=? AND b.observed_at>=? AND b.id=(SELECT max(b2.id) FROM books b2 WHERE b2.ticker=b.ticker AND b2.observed_at<=?)''',
                        (game_id,at,(instant(at)-timedelta(seconds=20)).isoformat(),at))
                    for other in alternatives:
                        option=self.eligible(other)
                        if option and option['signal_edge']>candidate['signal_edge']:candidate=option
                    candidate['signal_at']=at;state['pending'][game_id]=candidate;state['seen'].append(game_id)
        if rows:state['last_book_id']=max(row['id'] for row in rows)
        for game_id,entry in list(state['open'].items()):
            settlement=self.source.execute("SELECT * FROM market_states WHERE ticker=? AND status IN ('settled','finalized') AND observed_at<=? ORDER BY observed_at,id LIMIT 1",(entry['ticker'],now.isoformat())).fetchone()
            if settlement:
                value=settlement['settlement_value']
                if value is None and settlement['result'] in ('yes','no'):value=float(settlement['result']=='yes')
                if value is not None:
                    if entry['side']=='no':value=1-value
                    for name,p in entry['policies'].items():
                        if p['remaining']:
                            quantity=p['remaining'];p['proceeds']+=quantity*value;p['remaining']=0
                            self.fill_event(entry,name,p,settlement['observed_at'],'settlement',{'quantity':quantity,'value':value})
            if all(p['remaining']==0 for p in entry['policies'].values()):del state['open'][game_id]
        # Cancel entry attempts even when their market stopped publishing books.
        for game_id,pending in list(state['pending'].items()):
            if (now-instant(pending['signal_at'])).total_seconds()>r['max_execution_gap_seconds']:
                del state['pending'][game_id];state['unfilled_entries']+=1
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(json.dumps(state),));self.db.commit()
        report=self.report(now);atomic_json(self.directory/'paper'/'status.json',report);return report

    def report(self,now):
        policies=[]
        matched={r[0] for r in self.db.execute('SELECT game_id FROM outcomes GROUP BY game_id HAVING count(*)=?',(len(self.registration['policies']),))}
        matched_cost=sum(json.loads(row['body'])['cost'] for row in self.db.execute('SELECT game_id,body FROM entries') if row['game_id'] in matched)
        for name in self.registration['policies']:
            rows=self.db.execute('SELECT pnl,flags FROM outcomes WHERE policy=? ORDER BY closed_at,game_id',(name,)).fetchall();values=[v['pnl'] for v in rows]
            wins=[v for v in values if v>0];losses=[v for v in values if v<0];equity=peak=drawdown=0.
            for value in values:equity+=value;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
            matched_values=[v['pnl'] for v in self.db.execute('SELECT game_id,pnl FROM outcomes WHERE policy=?',(name,)) if v['game_id'] in matched]
            policies.append({'policy':name,'closed_trades':len(values),'net_pnl':sum(values),'matched_net_pnl':sum(matched_values),
                'matched_win_rate':sum(v>0 for v in matched_values)/len(matched_values) if matched_values else None,
                'matched_roi':sum(matched_values)/matched_cost if matched_cost else None,'win_rate':len(wins)/len(values) if values else None,
                'average_win':sum(wins)/len(wins) if wins else None,'average_loss':sum(losses)/len(losses) if losses else None,
                'profit_factor':sum(wins)/-sum(losses) if losses else None,'realized_max_drawdown':drawdown,
                'flagged_trades':sum(bool(json.loads(v['flags'])) for v in rows),'open_positions':sum(e['policies'][name]['remaining']>0 for e in self.state['open'].values())})
        entries=self.db.execute('SELECT count(*) FROM entries').fetchone()[0]
        return {'schema':1,'study_id':self.registration['study_id'],'updated_at':now.isoformat(),'registered_at':self.registration['registered_at'],
            'mode':'unfunded event-time paper replay; no orders','status':'collecting' if not matched else 'early_sample','matched_closed_entries':len(matched),
            'entries':entries,'pending_entries':len(self.state['pending']),'unfilled_entry_attempts':self.state['unfilled_entries'],
            'engine_versions':self.state['engine_versions'],'registration_hash':self.state['registration_hash'],
            'processed_book_id':self.state['last_book_id'],'policies':policies,
            'open_entries':[{'game_id':e['game_id'],'ticker':e['ticker'],'side':e['side'],'outcome_team':e['outcome_team'],'entry_at':e['entry_at'],'quantity':e['quantity'],'cost':e['cost']} for e in self.state['open'].values()],
            'limitations':['No profitability conclusion from an early sample.','Captured polling observations do not recreate intervening moves or actual fills.',
                'Execution uses the next eligible observed quote after the configured delay; gaps and insufficient depth are flagged.',
                'Closed-trade P&L excludes remaining positions; policy comparisons must use the same fully completed entry cohort.',
                'Quarter fair value exists only at observed end-Q1/halftime breaks.','Stops can lose more than their nominal threshold.']}
