"""Reconcile daily recorded fixtures without changing historical model datasets."""
import json
import sqlite3
import zlib
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import instant


def load_daily_source(directory,now):
    path=Path(directory)/'recordings.sqlite3'
    if not path.exists():return None
    day=now.astimezone(ZoneInfo('America/Indiana/Indianapolis')).date().isoformat()
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
    try:
        row=db.execute("SELECT r.*,p.body FROM requests r LEFT JOIN payloads p ON p.hash=r.payload_hash WHERE r.kind='scoreboard' AND r.url LIKE ? ORDER BY r.id DESC LIMIT 1",('%dates='+day.replace('-','')+'%',)).fetchone()
        if row is None:return None
        if row['error'] or row['body'] is None:return {'received_at':row['received_at'],'error':row['error'] or 'No daily source payload','games':None,'excluded':[]}
        from .recording import scoreboard_games
        games,excluded=scoreboard_games(json.loads(zlib.decompress(row['body'])))
        return {'received_at':row['received_at'],'games':games,'excluded':excluded,'error':None}
    finally:db.close()


def load_live_games(directory,now):
    path=Path(directory)/'recordings.sqlite3'
    if not path.exists():return []
    today=now.astimezone(ZoneInfo('America/Indiana/Indianapolis')).date()
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
    try:
        rows=[dict(r) for r in db.execute('''SELECT g.*,s.observed_at score_observed_at,s.state live_state,s.status_name,
            s.period,s.display_clock,s.home_score,s.away_score,r.payload_hash FROM games g
            JOIN scores s ON s.id=(SELECT max(id) FROM scores WHERE game_id=g.game_id)
            JOIN requests r ON r.id=s.request_id WHERE g.date BETWEEN ? AND ? AND s.observed_at<=?''',
            ((today-timedelta(days=1)).isoformat(),(today+timedelta(days=7)).isoformat(),now.isoformat()))]
        payloads={}
        for row in rows:
            digest=row.pop('payload_hash')
            if digest and digest not in payloads:
                raw=db.execute('SELECT body FROM payloads WHERE hash=?',(digest,)).fetchone()
                payloads[digest]=json.loads(zlib.decompress(raw[0])) if raw else {}
            event=next((e for e in payloads.get(digest,{}).get('events',[]) if str(e.get('id'))==row['game_id']),{})
            competition=(event.get('competitions') or [{}])[0]
            row['neutral']=competition.get('neutralSite')
        return rows
    finally:db.close()


def merge_schedule(dataset,rows,now):
    mapped={g['game_id']:dict(g) for g in dataset['schedule']}
    for row in rows:
        previous=mapped.get(row['game_id'])
        if previous and {previous['home'],previous['away']}!={row['home'],row['away']}:continue
        age=(now-instant(row['score_observed_at'])).total_seconds()
        if previous and not 0<=age<=300:continue
        state=row['live_state']
        name=(row.get('status_name') or '').upper()
        if 'POSTPON' in name:state='postponed'
        elif 'CANCEL' in name:state='canceled'
        season=row.get('season') or (previous or {}).get('season') or now.year+int(now.month>=9)
        # All canonical daily games can appear, while non-regular-season or
        # unverified new fixtures remain outside the prediction model.
        verified=previous is not None or row.get('season_type')==2
        value={**(previous or {}),**{k:row[k] for k in ('game_id','date','tipoff','home','away','venue')},
               'season':season,'state':state,'neutral':row['neutral'] if isinstance(row.get('neutral'),bool) else (previous or {}).get('neutral',False),
               'live_schedule_observed_at':row['score_observed_at'],'live_model_scope_verified':verified}
        if not previous and not isinstance(row.get('neutral'),bool):value['live_model_scope_verified']=False
        mapped[row['game_id']]=value
    return {**dataset,'schedule':sorted(mapped.values(),key=lambda g:(g['tipoff'],g['game_id']))}


def reconcile_public_games(result,rows,now):
    mapped={g['game_id']:g for g in result['games'] if g.get('game_id')}
    for row in rows:
        game=mapped.get(row['game_id']);age=(now-instant(row['score_observed_at'])).total_seconds()
        if game and {game['home'],game['away']}!={row['home'],row['away']}:continue
        if game is None:
            game={k:row[k] for k in ('game_id','date','tipoff','home','away','venue')}
            game.update(status='WAIT',blockers=['waiting_for_prediction_scan'],prediction=None,preview_prediction=None,pregame_prediction=None,
                        features={},profiles={},markets=[],missing_inputs=['pregame_model_scan'])
            result['games'].append(game);mapped[row['game_id']]=game
        if 0<=age<=300:
            changed=any(game.get(k)!=row[k] for k in ('date','tipoff','home','away'))
            if changed:
                game.update(prediction=None,preview_prediction=None,status='WAIT')
                game['blockers']=sorted(set(game.get('blockers',[])+['schedule_changed_since_prediction']))
            game.update({k:row[k] for k in ('date','tipoff','home','away','venue')})
            phase=row['live_state'];status=(row.get('status_name') or '').upper()
            if 'POSTPON' in status:phase='postponed'
            elif 'CANCEL' in status:phase='canceled'
            game['game_phase']=phase if phase in ('postponed','canceled') else 'final' if phase=='post' else 'started' if phase=='in' else 'pregame'
            game['state']=phase
            game['live_score']={k:row.get(k) for k in ('home_score','away_score','period','display_clock','score_observed_at','status_name')}
            if phase in ('in','post','postponed','canceled') and instant(game['tipoff'])>now:
                game.update(prediction=None,preview_prediction=None,status='WAIT')
                game['blockers']=sorted(set(game.get('blockers',[])+['game_started']))
    result['games'].sort(key=lambda g:(g['tipoff'],g.get('game_id','')))
