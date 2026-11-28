"""Pregame red flags for choosing which games to trade.

These are review prompts, not fitted probabilities. The thresholds come from a
small local review of 38 recorded games (Nov 21-27 2026) plus one structural
fact: the promoted model has no learned availability weight (FACTOR_STUDY.md).
When a player is out it only redistributes his minutes, so it tends to overrate
a shorthanded team while the market prices the absence directly. In that review
a single key absence on the model's side did not predict losses (7 of 11 won);
a star out with 50+ total minutes or a 15+ point gap did (OKC/SGA, BOS/Tatum).
Re-check the tiers as more games are recorded.
"""
import json
import sqlite3
import zlib
from datetime import timedelta
from pathlib import Path

from .data import atomic_json, instant

OUT = {'out', 'suspension', 'suspended'}
UNCERTAIN = {'questionable', 'doubtful', 'uncertain', 'daytoday'}
KEY_MINUTES = 24          # starter-level baseline minutes
STAR_MINUTES = 30
KEY_IMPACT_RANK = 3       # top-3 impact among regular rotation players
REGULAR_MINUTES = 15
HEAVY_OUT_MINUTES = 30    # several rotation players out, even if none is a starter
SEVERE_OUT_MINUTES = 50   # star out plus this many total minutes: skip
LATE_DROP = .03           # model's own number fell this much from its recent high
BIG_GAP = .15
TRAIL_HOURS = 12
QUOTE_MAX_AGE = 90
TRAIL_FILE = 'forecast-trail.json'
LEVELS = ('skip', 'caution', 'note')


def _player(p, rank):
    r = rank.get(p['id'])
    minutes = float(p.get('baseline_minutes') or 0)
    star = minutes >= STAR_MINUTES or r == 1
    key = star or minutes >= KEY_MINUTES or (r is not None and r <= KEY_IMPACT_RANK)
    role = ('#%d impact on team' % r) if r is not None and r <= KEY_IMPACT_RANK else 'starter minutes' if minutes >= KEY_MINUTES else 'rotation'
    return {'name': p.get('name') or p['id'], 'status': p.get('availability'), 'minutes': round(minutes, 1),
            'impact_rank': r, 'role': role, 'key': key, 'star': star}


def absences(rotation):
    """Out and uncertain players with how important they normally are."""
    players = [p for p in (rotation or {}).get('players', []) if (p.get('baseline_minutes') or 0) > 0]
    regular = sorted((p for p in players if p['baseline_minutes'] >= REGULAR_MINUTES and isinstance(p.get('impact'), (int, float))),
                     key=lambda p: p['impact'], reverse=True)
    rank = {p['id']: i+1 for i, p in enumerate(regular)}
    out = sorted((_player(p, rank) for p in players if p.get('availability') in OUT), key=lambda p: -p['minutes'])
    uncertain = sorted((_player(p, rank) for p in players if p.get('availability') in UNCERTAIN), key=lambda p: -p['minutes'])
    return {'out': out, 'uncertain': uncertain, 'out_minutes': round(sum(p['minutes'] for p in out), 1),
            'key_out': [p for p in out if p['key']], 'key_uncertain': [p for p in uncertain if p['key']]}


def best_asks(game, now):
    """Cheapest fresh buy ask per team across YES and NO routes."""
    asks = {}
    for market in game.get('markets') or []:
        stamp = market.get('quote_received_at')
        if not stamp or market.get('error') or not -5 <= (now-instant(stamp)).total_seconds() <= QUOTE_MAX_AGE:
            continue
        yes = market.get('yes_team')
        if yes not in (game['home'], game['away']):
            continue
        other = game['away'] if yes == game['home'] else game['home']
        quotes = market.get('quotes') or {}
        for side, team in (('yes', yes), ('no', other)):
            ask = quotes.get(side+'_ask')
            if ask is None:
                ask = next((c.get('best_ask') for c in market.get('candidates') or [] if c.get('side') == side), None)
            if isinstance(ask, (int, float)) and 0 < ask < 1 and (team not in asks or ask < asks[team]):
                asks[team] = ask
    return asks


def _names(players, limit=4):
    text = ', '.join('%s (%s, %g min%s)' % (p['name'], p['status'], p['minutes'], ', '+p['role'] if p['role'] != 'rotation' else '') for p in players[:limit])
    return text + (' and %d more' % (len(players)-limit) if len(players) > limit else '')


def red_flags(game, now, trail=None):
    prediction = game.get('prediction') or game.get('preview_prediction')
    home_p = (prediction or {}).get('home_win_probability')
    probs = {game['home']: home_p, game['away']: 1-home_p} if isinstance(home_p, (int, float)) else {}
    asks = best_asks(game, now)
    gaps = {t: probs[t]-asks[t] for t in probs if t in asks}
    model_team = max(gaps, key=gaps.get) if gaps and max(gaps.values()) > 0 else None
    rotations = (game.get('context') or {}).get('rotations') or {}
    teams = {game[side]: absences(rotations.get(side)) for side in ('home', 'away')}
    flags = []

    def add(level, code, team, title, detail):
        flags.append({'level': level, 'code': code, 'team': team, 'title': title, 'detail': detail})

    for team, a in teams.items():
        opponent = game['away'] if team == game['home'] else game['home']
        heavy = a['out_minutes'] >= HEAVY_OUT_MINUTES
        if a['key_out'] or heavy:
            who = _names(a['key_out'] or a['out'])
            minutes = '%g projected minutes out in total' % a['out_minutes']
            if team == model_team:
                star = any(p['star'] for p in a['key_out'])
                severe = star and (a['out_minutes'] >= SEVERE_OUT_MINUTES or gaps[team] >= BIG_GAP)
                add('skip' if severe else 'caution', 'model_side_missing_key_players', team,
                    ('Major absence: model likes %s, but %s is missing a star' if severe else
                     'Model likes %s, but %s is missing ' + ('key players' if a['key_out'] else 'several rotation players')) % (team, team),
                    '%s; %s. The model has no learned injury weight and only hands these minutes to backups, so it overrates '
                    'shorthanded teams. Kalshi at %.0f¢ has likely priced this in.%s'
                    % (who, minutes, asks[team]*100,
                       ' Same pattern as OKC vs MIA (SGA out, 84 minutes) and BOS vs SAC (Tatum out), both lost.' if severe else
                       ' One key absence alone has not predicted losses so far; check whether the gap is explained by it.'))
            elif model_team == opponent:
                add('note', 'opponent_missing_key_players', team, '%s is missing %s' % (team, 'key players' if a['key_out'] else 'rotation players'),
                    '%s; %s. This does not inflate the model\'s view of %s, but Kalshi already knows it too.' % (who, minutes, opponent))
            else:
                add('caution', 'missing_key_players', team, '%s is missing %s' % (team, 'key players' if a['key_out'] else 'rotation players'),
                    '%s; %s. If you consider %s, the model probably overrates them.' % (who, minutes, team))
        if a['key_uncertain'] and model_team in (team, None):
            add('caution', 'key_player_uncertain', team, '%s has key players not yet ruled in' % team,
                '%s. If they sit, the model will overrate %s. Wait for the final status.' % (_names(a['key_uncertain']), team))

    if model_team and trail:
        since = now - timedelta(hours=TRAIL_HOURS)
        recent = [p if model_team == game['home'] else 1-p for at, p in trail if instant(at) >= since]
        if recent and max(recent)-probs[model_team] >= LATE_DROP:
            drop = max(recent)-probs[model_team]
            injured = bool(teams[model_team]['key_out'] or teams[model_team]['key_uncertain'])
            add('skip' if injured else 'caution', 'model_dropped_late', model_team,
                'The model\'s own number for %s fell %.0f pts in the last %dh' % (model_team, drop*100, TRAIL_HOURS),
                'From %.0f%% to %.0f%%. A late drop usually means injury or lineup news that the model only partly catches. '
                'In the recorded sample, 1 of 5 such trades won.' % (max(recent)*100, probs[model_team]*100))
    if model_team and gaps[model_team] >= BIG_GAP:
        add('caution', 'large_model_market_gap', model_team, 'Model and Kalshi disagree by %.0f pts on %s' % (gaps[model_team]*100, model_team),
            'Large gaps did not win more often than small ones. Find the reason (injury, rest, lineup news) before buying; '
            'if you cannot explain it, pass.')
    if model_team and asks[model_team] < .5:
        add('note', 'underdog', model_team, '%s is the underdog at %.0f¢' % (model_team, asks[model_team]*100),
            'Underdogs rarely bounce back to your entry once they fall behind. Plan to hold to the end at a smaller size, '
            'or skip if you intend to sell on a bounce.')

    reported = bool(rotations) and all(not (rotations.get(side) or {}).get('injury_missing', True) for side in ('home', 'away'))
    if not reported:
        add('note', 'injury_report_missing', None, 'No injury report yet',
            'Injury checks start once both teams\' reports are collected (about 24 hours before tipoff).')
    flags.sort(key=lambda f: LEVELS.index(f['level']))
    verdict = ('skip' if any(f['level'] == 'skip' for f in flags) else 'caution' if any(f['level'] == 'caution' for f in flags)
               else 'clear' if reported and model_team else 'unchecked')
    return {'verdict': verdict, 'model_team': model_team, 'model_gap': gaps.get(model_team) if model_team else None,
            'model_ask': asks.get(model_team) if model_team else None, 'teams': teams, 'flags': flags,
            'basis': 'Review prompts from a small recorded sample and the model\'s missing injury weight; not fitted probabilities.'}


def load_trail(directory):
    path = Path(directory)/TRAIL_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def update_trail(directory, rows, now, recordings=None):
    """Append each pregame forecast (thinned) so later scans can see the model's recent moves."""
    path = Path(directory)/TRAIL_FILE
    trail = load_trail(directory) if path.exists() else _bootstrap(recordings or Path(directory)/'recordings.sqlite3', rows, now)
    keep = {}
    for item in rows:
        p = (item.get('prediction') or item.get('preview_prediction') or {}).get('home_win_probability')
        gid = item.get('game_id')
        if not gid or instant(item['tipoff']) < now-timedelta(days=1):
            continue
        points = trail.get(gid, [])
        if isinstance(p, (int, float)) and instant(item['tipoff']) > now:
            if not points or (now-instant(points[-1][0])).total_seconds() >= 300 or abs(points[-1][1]-p) >= .005:
                points.append([now.isoformat(), p])
        if points:
            keep[gid] = [pt for pt in points if instant(pt[0]) >= now-timedelta(hours=36)][-500:]
    atomic_json(path, keep)
    return keep


def _bootstrap(db_path, rows, now):
    """One-time seed from forecasts the recorder already archived (one per 10 minutes)."""
    trail = {}
    ids = [r['game_id'] for r in rows if r.get('game_id') and instant(r['tipoff']) > now]
    if not ids or not Path(db_path).exists():
        return trail
    try:
        db = sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
        try:
            since = (now-timedelta(hours=TRAIL_HOURS)).isoformat()
            for gid in ids:
                bucket = None
                for at, body in db.execute("""SELECT f.recorded_at,p.body FROM forecasts f JOIN payloads p ON p.hash=f.payload_hash
                        WHERE f.game_id=? AND f.kind='issued_pregame' AND f.recorded_at>=? ORDER BY f.recorded_at""", (gid, since)):
                    b = instant(at).timestamp()//600
                    if b == bucket:
                        continue
                    bucket = b
                    p = (json.loads(zlib.decompress(body)).get('prediction') or {}).get('home_win_probability')
                    if isinstance(p, (int, float)):
                        trail.setdefault(gid, []).append([at, p])
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    return trail
