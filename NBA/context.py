"""Past-only travel, rotations, player impact, availability and referee context."""
import bisect
import json
import hashlib
import math
import re
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import DATA, atomic_json, instant, utcnow
from .sources import player_rows, official_rows, foul_rows

GROUPS = {
    'travel': ['home_travel_km','away_travel_km','travel_km_diff','home_timezone_shift','away_timezone_shift',
               'home_travel_missing','away_travel_missing'],
    'rotation_impact': ['rotation_impact_diff','starter_impact_diff','bench_impact_diff','top_player_impact_diff',
                        'minutes_concentration_diff','rotation_continuity_diff','home_unknown_impact_share',
                        'away_unknown_impact_share','home_rotation_missing','away_rotation_missing'],
    'availability': ['out_minutes_diff','uncertain_minutes_diff','home_injury_report_missing','away_injury_report_missing',
                     'home_confirmed_lineup','away_confirmed_lineup','lineup_change_diff'],
    'referees': ['crew_home_margin_tendency','crew_fouls_per100','crew_fta_per100','referee_crew_missing'],
}
CONTEXT_NAMES = [f for group in GROUPS.values() for f in group]


def normalize_name(value):
    return re.sub(r'[^a-z0-9]', '', value.lower())


def haversine(a, b):
    lat1, lat2 = math.radians(a['latitude']), math.radians(b['latitude'])
    delta_lat, delta_lon = lat2-lat1, math.radians(b['longitude']-a['longitude'])
    value = math.sin(delta_lat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(delta_lon/2)**2
    return 6371*2*math.asin(min(1, math.sqrt(value)))


def impact_models(games, players):
    """Regularized box-score impact proxies fitted on PREVIOUS seasons only."""
    import numpy as np
    from scipy.sparse import csr_matrix
    from sklearn.linear_model import Ridge
    models, metadata = {}, {}
    for season in sorted({g['season'] for g in games}):
        past = [g for g in games if season-3 <= g['season'] < season and g['game_id'] in players]
        ids = sorted({p['id'] for g in past for roster in players[g['game_id']].values() for p in roster if p['minutes'] > 0})
        if len(past) < 300 or not ids:
            models[season] = {}; metadata[season] = {'games':len(past),'status':'insufficient_prior_seasons'}; continue
        columns = {identifier:i for i,identifier in enumerate(ids)}
        rr, cc, vv, targets = [], [], [], []
        home_adv = np.mean([g['home_stats']['pts']-g['away_stats']['pts'] for g in past if not g['neutral']])
        for index, game in enumerate(past):
            for side, sign in [('home',1),('away',-1)]:
                roster=players[game['game_id']][game[side]]; total=sum(p['minutes'] for p in roster)
                for player in roster:
                    if player['minutes'] > 0:
                        rr.append(index);cc.append(columns[player['id']]);vv.append(sign*5*player['minutes']/total)
            targets.append(game['home_stats']['pts']-game['away_stats']['pts']-(0 if game['neutral'] else home_adv))
        matrix=csr_matrix((vv,(rr,cc)),shape=(len(past),len(ids)))
        fit=Ridge(alpha=100,fit_intercept=False,solver='lsqr',tol=1e-5).fit(matrix,np.array(targets))
        models[season]={identifier:float(coef) for identifier,coef in zip(ids,fit.coef_)}
        metadata[season]={'games':len(past),'players':len(ids),'through':max(g['date'] for g in past),
                          'alpha':100,'method':'Regularized game-margin regression on player minute shares; box-score proxy, not possession RAPM.'}
        print('Player impact baseline for season '+str(season)+': '+str(len(past))+' prior games',flush=True)
    return models,metadata


def distribute_minutes(raw):
    """Allocate 240 team minutes, with a 48-minute cap per available player."""
    positive={key:max(0,float(value)) for key,value in raw.items() if value > 0}
    result=dict.fromkeys(raw,0.)
    remaining=240.
    while positive and remaining > 1e-8:
        total=sum(positive.values()); capped=[]
        for key,value in positive.items():
            if remaining*value/total >= 48:
                result[key]=48.;capped.append(key)
        if not capped:
            for key,value in positive.items():result[key]=remaining*value/total
            remaining=0;break
        for key in capped:positive.pop(key);remaining-=48
    return result,remaining


class ContextState:
    def __init__(self,players,officials,venues,impacts,fouls=None):
        self.players=players;self.officials=officials;self.venues=venues;self.impacts=impacts
        self.fouls=fouls or {}
        self.history=defaultdict(lambda:deque(maxlen=10));self.owner={};self.ref_history=defaultdict(lambda:deque(maxlen=200))

    def update(self,game):
        rosters=self.players.get(game['game_id'])
        if rosters:
            for code,roster in rosters.items():
                self.history[code].append({'date':game['date'],'players':roster,'game_id':game['game_id']})
                for player in roster:
                    if player['minutes']>0:self.owner[player['id']]=code
        h,a=game['home_stats'],game['away_stats']
        poss=(h['fga']+.44*h['fta']-h['oreb']+h['tov']+a['fga']+.44*a['fta']-a['oreb']+a['tov'])/2
        foul_counts=self.fouls.get(game['game_id'],{})
        foul_rate=100*(foul_counts[game['home']]+foul_counts[game['away']])/max(1,poss) if game['home'] in foul_counts and game['away'] in foul_counts else None
        for name in self.officials.get(game['game_id'],[]):
            self.ref_history[normalize_name(name)].append({'margin':h['pts']-a['pts'],'fta':100*(h['fta']+a['fta'])/max(1,poss),'fouls':foul_rate})

    def rotation(self,code,season,day,injuries=None,confirmed=None):
        games=[g for g in self.history[code] if 0 < (day-datetime.fromisoformat(g['date']).date()).days <= 90][-5:]
        base=defaultdict(float);names={};last_starters=[];role_minutes=[]
        for game in games:
            for player in game['players']:
                identifier=player['id'];names[identifier]=player['name']
                if self.owner.get(identifier,code)==code:base[identifier]+=player['minutes']/len(games)
                if player['starter'] and player['minutes']>0:role_minutes.append(player['minutes'])
        if games:last_starters=[p['id'] for p in games[-1]['players'] if p['starter'] and base.get(p['id'],0)>0]
        baseline,_=distribute_minutes(base)
        injury_map={r['id']:r for r in injuries or []}; out={key for key,r in injury_map.items() if r['status'] in ('out','suspension','suspended')}
        uncertain={key for key,r in injury_map.items() if key not in out and r['status'] not in ('active','available','probable')}
        raw={key:value for key,value in base.items() if key not in out}
        starters=[key for key in (confirmed or last_starters) if key not in out]
        if confirmed and role_minutes:
            typical=sum(role_minutes)/len(role_minutes)
            for identifier in confirmed:
                if identifier not in out:raw[identifier]=max(raw.get(identifier,0),typical)
        projected,unallocated=distribute_minutes(raw)
        for key in sorted(projected,key=projected.get,reverse=True):
            if len(starters)>=5:break
            if key not in starters:starters.append(key)
        coefficients=self.impacts.get(season,{})
        strength=lambda keys:sum(projected.get(key,0)*coefficients.get(key,0)/48 for key in keys)
        player_list=[{'id':key,'name':names.get(key,injury_map.get(key,{}).get('name',key)),
                      'projected_minutes':float(projected.get(key,0)),'baseline_minutes':float(baseline.get(key,0)),
                      'impact':coefficients.get(key),'starter':key in starters,
                      'availability':injury_map.get(key,{}).get('status','unreported'),
                      'source_conflict':injury_map.get(key,{}).get('source_conflict',False),
                      'availability_source':injury_map.get(key,{}).get('source')} for key in set(base)|set(injury_map)|set(projected)]
        player_list.sort(key=lambda p:p['projected_minutes'],reverse=True)
        previous={p['id'] for p in games[-1]['players'] if p['minutes']>0} if games else set()
        return {'impact':strength(projected),'starter_impact':strength(starters),
                'bench_impact':strength(set(projected)-set(starters)),
                'top_impact':max((coefficients.get(key,0) for key in projected if projected[key]>0),default=0),
                'concentration':sum((value/240)**2 for value in projected.values()),
                'continuity':sum(value for key,value in projected.items() if key in previous)/240,
                'unknown_share':sum(value for key,value in projected.items() if key not in coefficients)/240,
                'missing':int(not games or unallocated>1),'out_minutes':sum(baseline.get(key,0) for key in out),
                'uncertain_minutes':sum(baseline.get(key,0) for key in uncertain),
                'injury_missing':int(injuries is None),'confirmed':int(bool(confirmed) and len(confirmed)==5),
                'lineup_change':len(set(starters)-set(last_starters)) if confirmed else 0,
                'players':player_list,'unallocated_minutes':unallocated}

    def features(self,fixture,previous_venues,observation=None,crew=None):
        values=dict.fromkeys(CONTEXT_NAMES,0.)
        day=datetime.fromisoformat(fixture['date']).date();destination=self.venues.get(fixture['game_id'],{})
        rotations={}
        for side in ('home','away'):
            code=fixture[side];origin=previous_venues.get(code,{})
            missing=not all(key in origin and key in destination for key in ('latitude','longitude'))
            values[side+'_travel_missing']=int(missing)
            values[side+'_travel_km']=0 if missing else haversine(origin,destination)
            shift=0
            if not missing and origin.get('timezone') and destination.get('timezone'):
                tip=instant(fixture['tipoff'])
                shift=(tip.astimezone(ZoneInfo(destination['timezone'])).utcoffset()-tip.astimezone(ZoneInfo(origin['timezone'])).utcoffset()).total_seconds()/3600
            values[side+'_timezone_shift']=shift
            game_report=observation.get('game_injuries',{}).get(fixture['game_id'],{}) if observation else {}
            injuries=game_report.get('teams',{}).get(code,observation.get('injuries',{}).get(code)) if observation else None
            line=observation.get('lineups',{}).get(fixture['game_id'],{}) if observation else {}
            line_cutoff=min(instant(fixture['tipoff']),instant(observation['observed_at'])) if observation else instant(fixture['tipoff'])
            confirmed=line.get('teams',{}).get(code) if line and instant(line['observed_at']) <= line_cutoff else None
            rotation=self.rotation(code,fixture['season'],day,injuries,confirmed);rotations[side]=rotation
            values[side+'_unknown_impact_share']=rotation['unknown_share'];values[side+'_rotation_missing']=rotation['missing']
            values[side+'_injury_report_missing']=rotation['injury_missing'];values[side+'_confirmed_lineup']=rotation['confirmed']
        for feature,key in [('rotation_impact_diff','impact'),('starter_impact_diff','starter_impact'),('bench_impact_diff','bench_impact'),
                            ('top_player_impact_diff','top_impact'),('minutes_concentration_diff','concentration'),
                            ('rotation_continuity_diff','continuity'),('out_minutes_diff','out_minutes'),
                            ('uncertain_minutes_diff','uncertain_minutes'),('lineup_change_diff','lineup_change')]:
            values[feature]=rotations['home'][key]-rotations['away'][key]
        values['travel_km_diff']=values['home_travel_km']-values['away_travel_km']
        histories=[self.ref_history[normalize_name(name)] for name in crew or []]
        values['referee_crew_missing']=int(not crew or not any(histories))
        if histories:
            margin=[sum(g['margin'] for g in history)/(len(history)+50) for history in histories]
            fta=[(sum(g['fta'] for g in history)+50*45)/(len(history)+50) for history in histories]
            values['crew_home_margin_tendency']=sum(margin)/len(margin)
            values['crew_fta_per100']=sum(fta)/len(fta)
            foul_rates=[(sum(g['fouls'] for g in h if g.get('fouls') is not None)+50*40)/(sum(g.get('fouls') is not None for g in h)+50) for h in histories]
            values['crew_fouls_per100']=sum(foul_rates)/len(foul_rates)
        from .availability import fingerprint,report_metadata
        return values,{'rotations':rotations,'referees':crew or [],'travel_destination':destination,
                       'travel_source':'Scheduled venue-city distance proxy','availability':report_metadata(observation,fixture),
                       'availability_fingerprint':fingerprint(observation,fixture)}


def prepare(dataset,directory=DATA):
    """Build a reusable historical context table with the same cutoff as the base features."""
    directory=Path(directory)
    path=directory/'context'/'history.json'
    signature=hashlib.sha256(b''.join((directory/'context'/name).read_bytes() for name in ('venues.json','sources.json'))).hexdigest()
    if path.exists():
        cached=json.loads(path.read_text())
        if cached.get('dataset_fingerprint')==dataset['fingerprint'] and cached.get('source_signature')==signature and cached.get('version')==2:return cached
    players,skipped=player_rows(dataset,directory);officials=official_rows(dataset,directory)
    venues=json.loads((directory/'context'/'venues.json').read_text())
    impacts,impact_meta=impact_models(dataset['games'],players)
    state=ContextState(players,officials,venues,impacts,foul_rows(dataset,directory))
    completed=sorted(dataset['games'],key=lambda g:(g['available_at'],g['game_id']))
    observations=[]
    for source in sorted((directory/'context'/'observations').glob('*.json')):
        value=json.loads(source.read_text())
        if value.get('schema')!=2:value['injuries']={}
        observations.append(value)
    stamps=[instant(o['observed_at']) for o in observations]
    previous,rows,cursor={}, {},0
    now=utcnow()
    for fixture in sorted(dataset['schedule'],key=lambda g:(g['tipoff'],g['game_id'])):
        cutoff=min(now,instant(fixture['tipoff']))
        while cursor<len(completed) and instant(completed[cursor]['available_at'])<=cutoff:
            state.update(completed[cursor]);cursor+=1
        index=bisect.bisect_right(stamps,cutoff)-1
        observation=observations[index] if index>=0 and (cutoff-stamps[index]).total_seconds()<=900 else None
        crew=None;assignment_kind='unavailable'
        if observation and observation.get('referee_date')==fixture['date']:
            crew=observation.get('referees',{}).get(fixture['away']+'@'+fixture['home'])
            if crew:assignment_kind='timestamped_official_assignment'
        # Final-record crew identities are a reconstruction assumption, not an
        # archived proof of which assignment was available before a past tipoff.
        morning=datetime.fromisoformat(fixture['date']).replace(hour=9,tzinfo=ZoneInfo('America/New_York'))
        if not crew and fixture['game_id'] in officials and instant(fixture['tipoff'])>=morning:
            crew=officials[fixture['game_id']];assignment_kind='historical_final_crew_assumed_known_at_9am'
        values,detail=state.features(fixture,previous,observation,crew)
        rows[fixture['game_id']]={'features':values,'referee_assignment_kind':assignment_kind,
                                  'availability_observed_at':observation['observed_at'] if observation else None}
        for code in (fixture['home'],fixture['away']):previous[code]=venues.get(fixture['game_id'],{})
    # Compact current roster state is sufficient for live recomputation without
    # loading historical Parquet data in the server or quote worker.
    current={'history':{key:list(value) for key,value in state.history.items()},'owner':state.owner,
             'ref_history':{key:list(value) for key,value in state.ref_history.items()},'impacts':impacts[max(impacts)],
             'season':max(impacts),'venues':venues}
    payload={'version':2,'generated_at':utcnow().isoformat(),'dataset_fingerprint':dataset['fingerprint'],'source_signature':signature,
             'feature_names':CONTEXT_NAMES,'rows':rows,'player_games':len(players),'skipped_player_rows':skipped,
             'impact_models':impact_meta,'current':current,
             'assumptions':['Historical referee identities come from final records and assume normal 09:00 ET publication. Unarchived assignment changes cannot be ruled out.',
                            'Injury and confirmed-lineup history begins with timestamped observations; it is never inferred from actual DNPs or final starters.',
                            'Player impact is a regularized box-score minute-share proxy, not causal RAPM.',
                            'Travel measures city-to-city scheduled movement, not actual flights.']}
    atomic_json(path,payload)
    return payload


def live_features(dataset,history,observation,now):
    current=history['current'];state=ContextState({}, {},current['venues'],{current['season']:current['impacts']})
    state.history.update({key:deque(value,maxlen=10) for key,value in current['history'].items()})
    state.owner=current['owner'];state.ref_history.update({key:deque(value,maxlen=200) for key,value in current['ref_history'].items()})
    previous={};result={}
    for fixture in sorted(dataset['schedule'],key=lambda g:(g['tipoff'],g['game_id'])):
        if instant(fixture['tipoff'])>now:
            obs=observation if observation and 0<=(now-instant(observation['observed_at'])).total_seconds()<=900 and instant(fixture['tipoff'])<=now+timedelta(hours=24) else None
            crew=obs.get('referees',{}).get(fixture['away']+'@'+fixture['home']) if obs and obs.get('referee_date')==fixture['date'] else None
            values,detail=state.features(fixture,previous,obs,crew)
            scenarios=[]
            if obs:
                uncertain={side:{p['id'] for p in detail['rotations'][side]['players']
                                  if p['availability'] not in ('out','suspension','suspended','active','available','probable','unreported')
                                  and p['baseline_minutes']>0} for side in ('home','away')}
                for sides,label in [(('home',),'Home uncertain players unavailable'),(('away',),'Away uncertain players unavailable'),(('home','away'),'All uncertain players unavailable')]:
                    if not any(uncertain[side] for side in sides):continue
                    scenario=deepcopy(obs)
                    for side in sides:
                        records=scenario.get('game_injuries',{}).get(fixture['game_id'],{}).get('teams',{}).get(fixture[side],scenario.get('injuries',{}).get(fixture[side],[]))
                        for injury in records:
                            if injury['id'] in uncertain[side]:injury['status']='out'
                    alternate,_=state.features(fixture,previous,scenario,crew)
                    if alternate!=values:scenarios.append({'label':label,'features':alternate})
            result[fixture['game_id']]={'features':values,'detail':detail,
                                       'availability_observed_at':obs['observed_at'] if obs else None,'scenarios':scenarios}
        for code in (fixture['home'],fixture['away']):previous[code]=current['venues'].get(fixture['game_id'],{})
    return result
