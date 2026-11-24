"""Timestamp public availability, announced starters and official referee assignments."""
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import HTTPException
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import DATA, atomic_json, instant, team, utcnow
from .sources import get_json

INJURIES = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries'
REFEREES = 'https://official.nba.com/referee-assignments/'
LOCATIONS = {'Atlanta':'ATL','Boston':'BOS','Brooklyn':'BKN','Charlotte':'CHA','Chicago':'CHI','Cleveland':'CLE',
             'Dallas':'DAL','Denver':'DEN','Detroit':'DET','Golden State':'GSW','Houston':'HOU','Indiana':'IND',
             'LA Clippers':'LAC','LA Lakers':'LAL','Memphis':'MEM','Miami':'MIA','Milwaukee':'MIL','Minnesota':'MIN',
             'New Orleans':'NOP','New York':'NYK','Oklahoma City':'OKC','Orlando':'ORL','Philadelphia':'PHI',
             'Phoenix':'PHX','Portland':'POR','Sacramento':'SAC','San Antonio':'SAS','Toronto':'TOR','Utah':'UTA','Washington':'WAS'}


class AssignmentTable(HTMLParser):
    def __init__(self):
        super().__init__(); self.tables=0; self.active=False; self.in_cell=False; self.cells=[]; self.rows=[]; self.text=[]
    def handle_starttag(self, tag, attrs):
        if tag=='table': self.tables+=1; self.active=self.tables==1
        if self.active and tag=='tr': self.cells=[]
        if self.active and tag in ('td','th'): self.in_cell=True; self.text=[]
    def handle_data(self, value):
        if self.active and self.in_cell: self.text.append(value)
    def handle_endtag(self, tag):
        if self.active and tag in ('td','th'):
            self.cells.append(' '.join(''.join(self.text).split())); self.in_cell=False
        if self.active and tag=='tr' and self.cells: self.rows.append(self.cells)
        if tag=='table': self.active=False


def parse_assignments(html):
    date=re.search(r'class=["\']entry-meta["\'][^>]*>\s*([^<]+)',html)
    if not date: raise ValueError('Official referee page has no dated NBA table')
    day=datetime.strptime(date[1].strip(),'%B %d, %Y').date().isoformat()
    parser=AssignmentTable(); parser.feed(html); result={}
    for row in parser.rows:
        pair=row[0].split(' @ ')
        if len(pair)!=2 or pair[0] not in LOCATIONS or pair[1] not in LOCATIONS: continue
        crew=[re.sub(r'\s*\(#\d+\)', '', x).strip() for x in row[1:4] if x]
        if len(crew)==3: result[LOCATIONS[pair[0]]+'@'+LOCATIONS[pair[1]]]=crew
    return day,result


def athlete_id(athlete):
    if athlete.get('id'): return str(athlete['id'])
    for link in athlete.get('links',[]):
        match=re.search(r'/id/(\d+)',link.get('href',''))
        if match:return match[1]
    return None


def parse_injuries(payload):
    teams={}
    for group in payload.get('injuries',[]):
        code=next((key for name,key in LOCATIONS.items() if group.get('displayName','').startswith(name)),None)
        injuries=[]
        for record in group.get('injuries',[]):
            athlete=record.get('athlete',{}); identifier=athlete_id(athlete)
            try: code=team(athlete.get('team',{}).get('abbreviation',code or ''))
            except ValueError: continue
            if not identifier:continue
            reported=record.get('type',{}).get('name',record.get('status','')).replace('INJURY_STATUS_','').lower()
            fantasy=record.get('details',{}).get('fantasyStatus',{}).get('abbreviation','').upper()
            status='questionable' if fantasy in ('GTD','DTD','Q','D') else 'out' if fantasy in ('OUT','O','IR') else reported
            text=(record.get('shortComment','')+' '+record.get('longComment','')).lower()
            # A short comment can lag a structured status. Retain conflicts
            # explicitly; the game-specific official report wins when present.
            conflict=(reported=='out' and fantasy in ('GTD','DTD','Q','D')) or (status=='out' and any(term in text for term in ('questionable','game-time decision','gametime decision','doubtful','probable')))
            if conflict:status='uncertain'
            injuries.append({'id':identifier,'name':athlete.get('displayName',''),
                             'status':status,'reported_status':reported,
                             'source_updated_at':record.get('date'),
                             'detail':record.get('shortComment') or record.get('details',{}).get('type',''),
                             'source_conflict':conflict,'source':'espn'})
        if code:teams[code]=injuries
    return teams


def parse_lineups(payload, fixture, observed_at):
    competitions=payload.get('header',{}).get('competitions',[])
    if not competitions or instant(fixture['tipoff'])<=observed_at:
        return {}
    phase=competitions[0].get('status',{}).get('type',{}).get('state')
    if phase!='pre':return {}  # Actual post-tipoff starters are never backfilled.
    lineups={}
    for group in payload.get('boxscore',{}).get('players',[]):
        try: code=team(group['team']['abbreviation'])
        except (ValueError,KeyError):continue
        ids=[]
        for stat_group in group.get('statistics',[]):
            for record in stat_group.get('athletes',[]):
                if record.get('starter') is True and athlete_id(record.get('athlete',{})):
                    ids.append(athlete_id(record['athlete']))
        if len(set(ids))==5:lineups[code]=sorted(set(ids))
    return lineups


def collect(dataset, directory=DATA, now=None, min_interval=25):
    directory=Path(directory); now=now or utcnow(); path=directory/'context'/'live.json'
    previous=None
    if path.exists():
        previous=json.loads(path.read_text())
        if previous.get('schema')==2 and 0 <= (now-instant(previous['observed_at'])).total_seconds()<min_interval:return previous
    result={'schema':2,'observed_at':now.isoformat(),'injuries':{},'lineups':{},'referees':{},'game_injuries':{},'sources':[],'errors':[]}
    def referees():
        with urllib.request.urlopen(urllib.request.Request(REFEREES,headers={'User-Agent':'NBA-Market-Edge/3.0'}),timeout=8) as response:
            return parse_assignments(response.read(2_000_000).decode())
    from .official_injuries import collect_official, game_reports, roster_names
    jobs={'injuries':lambda:get_json(INJURIES),'official':lambda:collect_official(directory,now)}
    # Referee failures cannot stop injury checks; keep their independent receipt.
    if previous:
        old_source=next((s for s in previous.get('sources',[]) if s['kind']=='referees'),None)
        if old_source and 0<=(now-instant(old_source['observed_at'])).total_seconds()<600:
            result.update(referee_date=previous.get('referee_date'),referees=previous.get('referees',{}));result['sources'].append(old_source)
        else:jobs['referees']=referees
    else:jobs['referees']=referees
    fixtures={f['game_id']:f for f in dataset['schedule'] if now<instant(f['tipoff'])<=now+timedelta(minutes=90)}
    for key,fixture in fixtures.items():
        jobs['lineup:'+key]=lambda key=key:get_json('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+key)
    official=None
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(call):kind for kind,call in jobs.items()}
        for future in as_completed(futures):
            kind=futures[future]
            try:
                value=future.result();received=utcnow().isoformat()
                if kind=='injuries':
                    result['injuries']=parse_injuries(value)
                    result['sources'].append({'kind':'injuries','url':INJURIES,'source_time':value.get('timestamp'),'observed_at':received})
                elif kind=='official':
                    official=value
                    result['sources'].append({'kind':'official_injuries','url':value['url'],'published_at':value['published_at'],'observed_at':received})
                elif kind=='referees':
                    result['referee_date'],result['referees']=value
                    result['sources'].append({'kind':'referees','url':REFEREES,'source_date':value[0],'observed_at':received})
                else:
                    key=kind.split(':',1)[1];lineups=parse_lineups(value,fixtures[key],instant(received))
                    result['lineups'][key]={'teams':lineups,'observed_at':received,'status':'confirmed' if len(lineups)==2 else 'not_announced',
                        'url':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+key}
            except (OSError,HTTPException,ValueError,KeyError,TypeError,ImportError) as exc:
                result['errors'].append(kind+': '+str(exc))
    if official:
        result['game_injuries']=game_reports(dataset,official,result['injuries'],roster_names(directory,result['injuries']))
    # Preserve prior failed-source data only with its original timestamps. It
    # can explain a failure but cannot masquerade as newly verified availability.
    if previous:
        if not result['game_injuries']:
            result['game_injuries']=previous.get('game_injuries',{})
        if not result['injuries']:
            result['injuries']=previous.get('injuries',{})
    changes=[c for c in (previous or {}).get('changes',[]) if (now-instant(c['observed_at'])).total_seconds()<86400]
    for game_id,game in result['game_injuries'].items():
        old=(previous or {}).get('game_injuries',{}).get(game_id,{})
        for code,rows in game['teams'].items():
            if old.get('coverage',{}).get(code)!='reported':continue
            before={p['id']:p for p in old.get('teams',{}).get(code,[])};after={p['id']:p for p in rows}
            for key in sorted(set(before)|set(after)):
                a=before.get(key,{}).get('status','not_listed');b=after.get(key,{}).get('status','not_listed')
                if a!=b:changes.append({'game_id':game_id,'team':code,'player_id':key,'name':after.get(key,before.get(key))['name'],
                    'before':a,'after':b,'observed_at':utcnow().isoformat(),'source_url':game['url']})
    result['changes']=changes[-100:]
    result['completed_at']=utcnow().isoformat()
    # The combined observation is usable only after every source was received.
    result['observed_at']=result['completed_at']
    atomic_json(path,result)
    atomic_json(directory/'context'/'observations'/(now.strftime('%Y%m%dT%H%M%S%fZ')+'.json'),result)
    return result


def snapshot_at(directory, cutoff, max_age_seconds=900):
    """A report collected after cutoff cannot describe historical availability."""
    path=Path(directory)/'context'/'live.json'
    if not path.exists():return None
    value=json.loads(path.read_text())
    age=(cutoff-instant(value['observed_at'])).total_seconds()
    return value if value.get('schema')==2 and 0<=age<=max_age_seconds else None
