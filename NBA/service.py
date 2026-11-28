"""Read-only API view; imports only the standard library and never trains/fetches."""
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import DATA, instant, utcnow
from .backtest import season_backtest


def public_snapshot(directory=DATA, now=None):
    now = now or utcnow()
    directory = Path(directory)
    path = directory / 'fair-prices.json'
    if not path.exists():
        result={'schema':1,'generated_at':now.isoformat(),'status':'not_ready','games':[],'model_id':None,
            'quote_max_age_seconds':90,'estimate_max_age_seconds':180,'quantity':10,'slippage_buffer':.005,'min_edge':.03,
            'reason':'A pregame model scan is pending; available recorded games and quotes can still be shown.'}
    else:result = deepcopy(json.loads(path.read_text()))
    from .live_board import load_live_games,reconcile_public_games,load_daily_source
    from .quotes import load_quotes,overlay_quotes,coverage
    result['quote_feed_errors']=[]
    try:live_games=load_live_games(directory,now)
    except sqlite3.Error as exc:
        live_games=[];result['quote_feed_errors'].append(str(exc))
    reconcile_public_games(result,live_games,now)
    if not path.exists() and result['games']:result['status']='quotes_only'
    age = (now - instant(result['generated_at'])).total_seconds()
    result.update(served_at=now.isoformat(), snapshot_age_seconds=max(0, age))
    worker_path=directory/'watcher-status.json'
    worker=json.loads(worker_path.read_text()) if worker_path.exists() else {}
    heartbeat=worker.get('heartbeat_at')
    worker['current']=bool(heartbeat and 0<=(now-instant(heartbeat)).total_seconds()<=90)
    result['update_worker']=worker
    live_path=directory/'context'/'live.json'
    observation=json.loads(live_path.read_text()) if live_path.exists() else None
    if observation and instant(observation['observed_at'])>now:
        observation=None
    result['availability_updates']={'observed_at':(observation or {}).get('observed_at'),'sources':(observation or {}).get('sources',[]),
        'errors':(observation or {}).get('errors',[]),'changes':(observation or {}).get('changes',[])}
    # If a newer training run exists, do not silently serve the old model's scan.
    report_path = directory / 'model-report.json'
    current_model = json.loads(report_path.read_text()).get('model_id') if report_path.exists() else None
    last_error_path = directory / 'scan-error.json'
    result['last_scan_error'] = json.loads(last_error_path.read_text()) if last_error_path.exists() else None
    for game in result['games']:
        reasons = []
        if instant(game['tipoff'])>now:
            from .availability import entry_safety
            rebuilding='player_context_rebuilding' in game.get('entry_safety',{}).get('reasons',[])
            game['entry_safety']=entry_safety(game,now,observation)
            if rebuilding:
                game['entry_safety']['blocked']=True
                game['entry_safety']['reasons'].append('player_context_rebuilding')
                game['entry_safety']['messages'].append('Player context is rebuilding; waiting for a new forecast.')
            if worker.get('heartbeat_at') and not worker['current']:
                game['entry_safety']['blocked']=True
                game['entry_safety']['reasons'].append('update_worker_stale')
                game['entry_safety']['messages'].append('Automatic update worker is stale; new entries are blocked.')
        if instant(game['tipoff']) <= now:
            reasons.append('game_started')
            # Preserve the recorded pregame forecast for inspection after tipoff;
            # it must never become an in-play probability or an executable edge.
            if (not game.get('pregame_prediction') and game.get('prediction')
                    and instant(result['generated_at']) < instant(game['tipoff'])):
                game['pregame_prediction'] = game['prediction']
                game['prediction_kind'] = 'saved_pregame'
                game['prediction_as_of'] = result['generated_at']
        if age < -5 or age > result['estimate_max_age_seconds']:
            reasons.append('estimate_snapshot_expired')
        if current_model != result['model_id']:
            reasons.append('model_changed_or_unavailable')
        if reasons:
            game['prediction'] = None
            game['status'] = 'WAIT'
            game['blockers'] = sorted(set(game['blockers'] + reasons))
        if age < -5 or age > result['estimate_max_age_seconds'] or current_model != result['model_id']:
            game['preview_prediction'] = None
        for market in game['markets']:
            quote_age = ((now - instant(market['quote_received_at'])).total_seconds()
                         if market['quote_received_at'] else None)
            stale = quote_age is None or quote_age < -5 or quote_age > result['quote_max_age_seconds']
            market['quote_age_seconds'] = quote_age
            for candidate in market['candidates']:
                if reasons or stale:
                    candidate.update(status='WAIT', net_edge=None, expected_profit_dollars=None)
                    candidate['blockers'] = sorted(set(candidate['blockers'] + reasons + (['quote_expired'] if stale else [])))
                if reasons:
                    candidate.update(probability=None, fair_price_cents=None)
                safety=game.get('entry_safety',{})
                if safety.get('blocked'):
                    candidate.update(status='WAIT',net_edge=None,expected_profit_dollars=None)
                    candidate['blockers']=sorted(set(candidate['blockers']+safety['reasons']))
    try:quotes,contracts=load_quotes(directory,now)
    except sqlite3.Error as exc:
        quotes,contracts={},{};result['quote_feed_errors'].append(str(exc))
    overlay_quotes(result,now,quotes,contracts)
    from .red_flags import red_flags,load_trail
    try:trail=load_trail(directory)
    except (OSError,ValueError):trail={}
    for game in result['games']:
        if game.get('home') and game.get('away') and game.get('tipoff') and instant(game['tipoff'])>now:
            game['red_flags']=red_flags(game,now,trail.get(game.get('game_id')))
    result['board_today']=now.astimezone(ZoneInfo('America/Indiana/Indianapolis')).date().isoformat()
    try:source=load_daily_source(directory,now)
    except sqlite3.Error as exc:
        source=None;result['quote_feed_errors'].append(str(exc))
    result['coverage']=coverage(result,live_games,now,source)
    result['quote_timestamp_kind']='Local HTTP receipt time; independent of forecast time and upstream exchange freshness.'
    _attach_live_model(result,quotes,directory,now)
    return result


def _attach_live_model(snapshot,quotes,directory,now):
    """Add explicit in-game candidate/WAIT states; never changes pregame values."""
    report_path=Path(directory)/'live-model-report.json'
    if not report_path.exists():
        report_path=Path(__file__).resolve().parent/'models'/'live-model-report.json'
    report=json.loads(report_path.read_text()) if report_path.exists() else {}
    test=report.get('test') or {}
    score_baseline=report.get('score_time_baseline') or {}
    snapshot['live_model_report']={'status':report.get('status','not_trained'),
        'improves_both_metrics':report.get('improves_both_metrics',False),
        'test_games':report.get('test_games',0),'test_season':report.get('test_period',{}).get('through_season'),
        'brier':test.get('brier'),'score_time_brier':score_baseline.get('brier'),
        'log_loss':test.get('log_loss'),'score_time_log_loss':score_baseline.get('log_loss'),
        'ece':test.get('ece'),'signal_eligibility':report.get('signal_eligibility','WAIT')}
    from .live_model import load_live_model,predict_live
    from .markets import compare,top_quotes
    from .quotes import contract_matches_game
    artifact=load_live_model(directory)
    eligible_model=(artifact is not None and report.get('status')=='evaluated'
                    and report.get('improves_both_metrics') is True)
    for game in snapshot.get('games',[]):
        if game.get('game_phase')!='started':
            continue
        score=game.get('live_score') or {}
        score_age=(now-instant(score['score_observed_at'])).total_seconds() if score.get('score_observed_at') else None
        prior=(game.get('pregame_prediction') or {}).get('home_win_probability')
        state={'status':'WAIT','action':'WAIT','home_probability':None,'routes':[],
               'score_age_seconds':score_age,'threshold':.05,'reason':None,
               'model_id':(artifact or {}).get('model_id'),'basis':'Score/clock probability candidate; no historical Kalshi-profit validation.'}
        game['live_model']=state
        if not eligible_model:
            state['reason']='No evaluated live model that beats both score-only and pregame baselines.'
            continue
        if score_age is None or not 0<=score_age<=30:
            state['reason']='Score or game clock is stale; waiting for a fresh observation.'
            continue
        if score.get('period') not in range(1,5) or not isinstance(score.get('clock_seconds'),(int,float)):
            state['reason']='Live model supports regulation periods with a valid game clock only.'
            continue
        if prior is None:
            state['reason']='No frozen pregame probability is available for the live model prior.'
            continue
        remaining=(4-score['period'])*720+score['clock_seconds']
        margin=(score.get('home_score') or 0)-(score.get('away_score') or 0)
        predicted=predict_live(artifact,margin,remaining,prior)
        if predicted.get('probability') is None:
            state['reason']=predicted.get('reason','Live model abstained on this state.')
            state['status']=predicted.get('status','WAIT')
            continue
        p_home=predicted['probability'];state.update(home_probability=p_home,model_as_of=predicted['as_of'])
        matched=[q for q in quotes.values() if q.get('game_id')==game['game_id']
                 and q.get('yes_team')==game.get('home')
                 and contract_matches_game(q['ticker'],q.get('yes_team'),game)]
        if not matched:
            state['reason']='No matched Kalshi game contract is available.'
            continue
        quote=max(matched,key=lambda q:q.get('observed_at') or '')
        quote_age=(now-instant(quote['observed_at'])).total_seconds() if quote.get('observed_at') else None
        state['quote_age_seconds']=quote_age;state['ticker']=quote['ticker']
        if quote_age is None or not 0<=quote_age<=30 or not quote.get('book') or quote.get('multiplier') is None:
            state['reason']='Kalshi book, fee schedule, or quote freshness is unavailable.'
            continue
        if quote.get('status') not in ('active','open'):
            state['reason']='Kalshi market is not open.'
            continue
        books=top_quotes(quote['book'])
        if books.get('crossed'):
            state['reason']='Order book is crossed; waiting for a clean quote.'
            continue
        mid=(books.get('yes_bid')+books.get('yes_ask'))/2 if books.get('yes_bid') is not None and books.get('yes_ask') is not None else None
        if mid is not None and abs(p_home-mid)>.15:
            state['reason']='Model and market differ by over 15 points; review possible feed or mapping error.'
            continue
        candidates=compare(p_home,quote['payload'],{'fee_type':'quadratic','fee_multiplier':quote['multiplier']},
                           quantity=int(snapshot.get('quantity',10)),slippage=float(snapshot.get('slippage_buffer',.005)),min_edge=0)
        for candidate in candidates:
            side=candidate['side'];bid=books.get(side+'_bid');ask=books.get(side+'_ask')
            spread=ask-bid if ask is not None and bid is not None else None
            reason='Net edge is below the 5-point candidate threshold.'
            action='WAIT'
            if spread is None:reason='Both sides of the selected contract book are required.'
            elif spread>.05+1e-9:reason='Spread exceeds the 5-cent live candidate limit.'
            elif candidate.get('net_edge') is None:reason='Insufficient depth or cost data for the requested quantity.'
            elif candidate['net_edge']>=state['threshold']:
                action='ENTRY_CANDIDATE'
                reason='Positive model edge after observed depth, fees and slippage buffer; discretionary review required.'
            state['routes'].append({'side':side,'outcome_team':game['home'] if side=='yes' else game['away'],
                'fair_probability':candidate.get('probability'),'best_ask':ask,'best_bid':bid,'spread':spread,
                'net_edge':candidate.get('net_edge'),'expected_profit_dollars':candidate.get('expected_profit_dollars'),
                'quantity':candidate.get('quantity'),'action':action,'reason':reason})
        actionable=[r for r in state['routes'] if r['action']=='ENTRY_CANDIDATE']
        if actionable:
            best=max(actionable,key=lambda r:r['net_edge'])
            state.update(status='candidate',action='ENTRY CANDIDATE',outcome_team=best['outcome_team'],
                         side=best['side'],net_edge=best['net_edge'],reason=best['reason'])
        else:
            state['reason']='No route clears live feed, spread, depth, fee and 5-point net-edge checks.'


def model_report(directory=DATA):
    path = Path(directory) / 'model-report.json'
    return json.loads(path.read_text()) if path.exists() else {'status': 'not_trained'}


def optimization_report(directory=DATA):
    path=Path(directory)/'optimization-report.json'
    return json.loads(path.read_text()) if path.exists() else {'status':'not_run'}


def recording_status(directory=DATA,now=None):
    import sqlite3
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    directory=Path(directory);now=now or utcnow();path=directory/'collector-status.json';database=directory/'recordings.sqlite3'
    if not path.exists() or not database.exists():return {'status':'not_started','running':False,'games':[]}
    result=json.loads(path.read_text());age=(now-instant(result['last_cycle_at'])).total_seconds()
    result['heartbeat_age_seconds']=max(0,age)
    result['status']='recording' if result.get('running') and 0<=age<=max(45,3*result['next_interval_seconds']) else 'stopped_or_stale'
    result['served_at']=now.isoformat();result['database_bytes']=sum(p.stat().st_size for p in directory.glob('recordings.sqlite3*') if p.is_file())
    day=now.astimezone(ZoneInfo('America/New_York')).date()
    db=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
    try:
        result['games']=[dict(r) for r in db.execute('''SELECT g.*,s.observed_at score_observed_at,s.state,s.status_name,s.period,s.display_clock,s.home_score,s.away_score
            FROM games g JOIN scores s ON s.game_id=g.game_id WHERE s.id=(SELECT max(id) FROM scores WHERE game_id=g.game_id)
            AND (g.date=? OR (g.date=? AND s.state='in')) ORDER BY g.tipoff,g.game_id''',(day.isoformat(),(day-timedelta(days=1)).isoformat()))]
        for game in result['games']:
            game['quotes']=[dict(r) for r in db.execute('''SELECT m.yes_team,m.status,b.ticker,b.observed_at,b.quality,b.yes_bid,b.yes_ask,b.no_bid,b.no_ask
                FROM books b JOIN markets m ON b.ticker=m.ticker WHERE b.game_id=? AND b.id=(SELECT max(id) FROM books WHERE ticker=b.ticker) ORDER BY m.yes_team''',(game['game_id'],))]
    finally:db.close()
    return result


def paper_status(directory=DATA,now=None):
    path=Path(directory)/'paper'/'status.json'
    if not path.exists():return {'status':'not_started','policies':[]}
    result=json.loads(path.read_text());age=((now or utcnow())-instant(result['updated_at'])).total_seconds()
    result['worker_current']=0<=age<=120;result['heartbeat_age_seconds']=max(0,age)
    return result


def feature_catalog():
    from .features import FEATURE_NAMES, PRIORS
    context = [('Elo rating difference', ['elo_diff'], 'Home rating minus away rating; results update past-only Elo.'),
               ('Home or neutral court', ['home_court'], '1 at the home court, 0 at a neutral venue.'),
               ('Rest difference', ['rest_diff'], 'Home days off minus away days off, capped at seven per team.'),
               ('Back-to-back flags', ['home_b2b', 'away_b2b'], 'Whether each team played on the previous calendar day.'),
               ('Three games in four days', ['home_three_in_four', 'away_three_in_four'], 'Whether each team has two prior games within three days.'),
               ('Recent schedule density', ['games_last7_diff'], 'Difference in games played in the six preceding days.'),
               ('Mean recent possessions', ['possessions_10_mean'], 'Mean of the two teams’ ten-game estimated possessions per game.'),
               ('Available history counts', ['home_history_games', 'away_history_games'], 'Actual recent games per team, capped at 30 as a model input.')]
    metrics = [('Offensive rating', 'ortg', 'Points scored per 100 estimated possessions.'),
               ('Defensive rating', 'drtg', 'Points conceded per 100 estimated possessions.'),
               ('Effective field-goal percentage', 'efg', '(FGM + 0.5 × 3PM) / FGA.'),
               ('Opponent effective field-goal percentage', 'opp_efg', 'Effective shooting percentage allowed.'),
               ('Turnover rate', 'tov_rate', 'Turnovers per estimated possession.'),
               ('Opponent turnover rate', 'opp_tov_rate', 'Opponent turnovers per estimated possession.'),
               ('Offensive rebound share', 'orb_rate', 'OREB / (OREB + opponent DREB).'),
               ('Opponent offensive rebound share', 'opp_orb_rate', 'Offensive rebound share allowed.'),
               ('Free-throw attempt rate', 'ftr', 'FTA / FGA.'),
               ('Opponent free-throw attempt rate', 'opp_ftr', 'Opponent FTA / opponent FGA.'),
               ('Three-point attempt share', 'three_rate', '3PA / FGA.'),
               ('Estimated possessions', 'possessions', 'Box-score possessions per game, not pace per 48 minutes.')]
    groups = [{'label': label, 'features': fields, 'description': description, 'window': 'Context'} for label, fields, description in context]
    groups += [{'label': label, 'features': [f'{key}_{window}_diff' for window in (10, 30)],
                'description': description+' Home-minus-away difference in each window.', 'window': '10 and 30 games'}
               for label, key, description in metrics]
    from .context import GROUPS as EXTRA_GROUPS
    descriptions={'travel':'Scheduled venue-city distance and timezone movement; not actual flight itineraries.',
                  'rotation_impact':'Projected minutes, continuity and regularized prior-season player-impact proxies.',
                  'availability':'Timestamped reported injuries and announced lineups. Unreported/uncertain values remain explicit.',
                  'referees':'Published crew and past foul/FTA/home-margin tendencies. Historical final-crew reconstruction is research-only.'}
    names=model_report().get('feature_names',FEATURE_NAMES)
    groups += [{'label':key.replace('_',' ').title(),'features':[f for f in fields if f in names],
                'description':descriptions[key],'window':'Past-only / observed pregame'} for key,fields in EXTRA_GROUPS.items() if any(f in names for f in fields)]
    return {'feature_names': names, 'windows_games': [10, 30], 'prior_pseudo_games': 5,
            'groups': groups, 'feature_count': len(names),
            'edge_inputs': ['YES and NO order-book asks', 'available depth at the requested quantity',
                            'series taker fee and multiplier', 'whole-cent fee allowance', 'slippage buffer', 'quote freshness', 'contract settlement rules'],
            'priors': PRIORS, 'implemented': ['Elo', 'efficiency', 'four factors', 'three-point attempt rate', 'rest', 'schedule density', 'home/neutral venue',
                            'scheduled travel/time zones','projected minutes','regularized player-impact proxy','timestamped injury and lineup collection','referee assignment collection'],
            'planned': ['possession-based player impact','shot profile matchups','more timestamped injury/lineup/referee validation history'],
            'target': 'Full-game home win including overtime; NBA regular season only'}
