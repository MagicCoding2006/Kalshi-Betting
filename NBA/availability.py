"""Causal availability checks shared by scans, API views and entry sizing."""
import hashlib
import json

from .data import instant


def fingerprint(observation, fixture):
    if not observation:
        return None
    game = observation.get('game_injuries', {}).get(fixture['game_id'], {})
    teams = game.get('teams', {})
    line = observation.get('lineups', {}).get(fixture['game_id'], {}).get('teams', {})
    value = {'teams': {code: sorted((p['id'], p['status']) for p in teams.get(code, [])) for code in (fixture['home'],fixture['away'])},
             'coverage': game.get('coverage'), 'lineups': line}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def report_metadata(observation, fixture):
    game = (observation or {}).get('game_injuries', {}).get(fixture['game_id'], {})
    return {key:game.get(key) for key in ['coverage','url','published_at','checked_at','unmatched','pending_secondary']}


def entry_safety(game, now, observation=None):
    if not all(game.get(key) for key in ('game_id','home','away','tipoff')):
        return {'blocked':True,'reasons':['game_identity_unverified'],'messages':['Game identity or availability verification is incomplete.']}
    reasons=[];messages=[]
    detail=game.get('context') or {}
    metadata=detail.get('availability') or {}
    fingerprint_used=game.get('availability_fingerprint')
    horizon=(instant(game['tipoff'])-now).total_seconds()
    if horizon<=0:
        return {'blocked':True,'reasons':['game_started'],'messages':['Pregame entries are closed.']}
    if horizon>24*3600:
        return {'blocked':True,'reasons':['availability_outside_24h'],'messages':['Availability is not verified for an entry this far ahead.']}
    def block(reason,message):reasons.append(reason);messages.append(message)
    # A newer observation can invalidate an old price immediately, even while
    # a slow market scan is still running. It never upgrades the old forecast.
    if observation and fingerprint_used!=fingerprint(observation,game):
        block('availability_changed_since_prediction','Availability changed: waiting for a forecast using the new report.')
    current_meta=report_metadata(observation,game) if observation else metadata
    checked=current_meta.get('checked_at');published=current_meta.get('published_at')
    age=(now-instant(checked)).total_seconds() if checked else None
    source_age=(now-instant(published)).total_seconds() if published else None
    limit=120 if horizon<=90*60 else 300
    if age is None or not -5<=age<=limit or source_age is None or not -5<=source_age<=3600:
        block('injury_report_stale_or_missing','Official game-specific injury verification is missing or stale; new entries are blocked.')
    coverage=current_meta.get('coverage') or {}
    if any(coverage.get(game[side])!='reported' for side in ('home','away')):
        block('injury_report_not_submitted','One or both teams have not submitted a usable official report for this game.')
    uncertain=[];heavy=[];conflicts=[];unreliable=[]
    for side,rotation in detail.get('rotations',{}).items():
        key_players=[p for p in rotation.get('players',[]) if p.get('baseline_minutes',0)>=24 and p.get('availability') in ('questionable','doubtful','uncertain','daytoday')]
        uncertain.extend({'team':game[side], 'name':p['name'], 'status':p['availability'], 'baseline_minutes':p['baseline_minutes']} for p in key_players)
        if rotation.get('out_minutes',0)>=60:
            heavy.append(game[side])
        conflicts.extend(p['name'] for p in rotation.get('players',[]) if p.get('source_conflict') and p.get('baseline_minutes',0)>0)
        if rotation.get('missing') or rotation.get('unknown_share',0)>.25:unreliable.append(game[side])
    if uncertain:
        block('key_player_uncertain','Important-player availability unresolved: '+', '.join(p['name'] for p in uncertain)+'.')
    if heavy:
        block('large_rotation_disruption','Large rotation disruption (60+ projected baseline minutes absent): '+', '.join(heavy)+'. The injury effect is outside a well-validated regime.')
    if conflicts:
        block('injury_sources_disagree','Newer availability news awaits official confirmation: '+', '.join(conflicts)+'.')
    if unreliable:
        block('rotation_projection_unreliable','Projected rotation or player-impact coverage is incomplete: '+', '.join(unreliable)+'.')
    if horizon<=30*60 and any(not detail.get('rotations',{}).get(side,{}).get('confirmed') for side in ('home','away')):
        block('lineups_unconfirmed_near_tipoff','Starting lineups are unconfirmed within 30 minutes of tipoff; new entries are blocked.')
    return {'blocked':bool(reasons),'reasons':reasons,'messages':messages,'key_players_uncertain':uncertain,
            'official_checked_at':checked,'official_published_at':published,'check_age_seconds':age,
            'max_check_age_seconds':limit,'official_url':current_meta.get('url'),
            'policy':'Conservative entry filters, not fitted confidence intervals or validated optimal thresholds.'}
