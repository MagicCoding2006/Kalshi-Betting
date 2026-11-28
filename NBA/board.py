"""Full schedule, latest-history previews and preserved pregame forecasts."""
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import atomic_json, instant
from .features import FeatureState, state_at


def pregame_archive(directory):
    directory = Path(directory)
    path = directory/'pregame-predictions.json'
    if path.exists():
        return json.loads(path.read_text())
    records = {}
    # Bootstrap predictions for games that already started from the snapshots
    # collected before this feature was added. Store only actually issued values.
    for source in sorted((directory/'snapshots').glob('*.json')):
        data = json.loads(source.read_text()).get('scan', {})
        remember(records, data.get('games', []), data.get('generated_at'), data.get('model_id'))
    return records


def remember(records, games, as_of, model_id):
    if not as_of:
        return
    for game in games:
        prediction = game.get('prediction')
        if prediction is None or instant(as_of) >= instant(game['tipoff']):
            continue
        previous = records.get(game['game_id'])
        if previous is None or instant(previous['as_of']) < instant(as_of):
            records[game['game_id']] = {'prediction': prediction, 'as_of': as_of, 'model_id': model_id,
                                       'features': game.get('features'), 'profiles': game.get('profiles'),
                                       'context': game.get('context'),
                                       'availability_fingerprint':game.get('availability_fingerprint'),'entry_safety':game.get('entry_safety'),
                                       'availability_probability_range': game.get('availability_probability_range')}


def schedule_context(features, profiles, fixture, schedule):
    """Future rest is known from the schedule; future game performance is unknown."""
    values, result = dict(features), deepcopy(profiles)
    day = datetime.fromisoformat(fixture['date']).date()
    for side in ('home', 'away'):
        code = fixture[side]
        days = [(day-datetime.fromisoformat(g['date']).date()).days for g in schedule
                if code in (g['home'], g['away']) and g.get('state') not in ('postponed', 'canceled')
                and instant(g['tipoff']) < instant(fixture['tipoff']) and 0 < (day-datetime.fromisoformat(g['date']).date()).days <= 14]
        result[side].update(rest=min(7, min(days)-1) if days else 7,
                            b2b=int(1 in days), three_in_four=int(sum(d <= 3 for d in days) >= 2),
                            last7=sum(d <= 6 for d in days))
    h, a = result['home'], result['away']
    values.update(rest_diff=h['rest']-a['rest'], home_b2b=h['b2b'], away_b2b=a['b2b'],
                  home_three_in_four=h['three_in_four'], away_three_in_four=a['three_in_four'],
                  games_last7_diff=h['last7']-a['last7'])
    return values, result


def build_board(dataset, bundle, directory, now, blockers_for, predict_batch):
    today = now.astimezone(ZoneInfo('America/Indiana/Indianapolis')).date().isoformat()
    season = now.year+int(now.month >= 9)
    fixtures = [f for f in dataset['schedule'] if f['season'] == season]
    state = state_at(dataset['games'], now)
    # Replay the historical timeline once, rather than rebuilding every team's
    # entire history separately for every completed fixture.
    history, cursor, replay = {}, 0, FeatureState()
    completed_order = sorted(dataset['games'], key=lambda g: (g['available_at'], g['game_id']))
    for fixture in sorted((f for f in fixtures if instant(f['tipoff']) <= now), key=lambda f: (f['tipoff'], f['game_id'])):
        cutoff = instant(fixture['tipoff'])
        while cursor < len(completed_order) and instant(completed_order[cursor]['available_at']) <= cutoff:
            replay.update(completed_order[cursor]); cursor += 1
        history[fixture['game_id']] = replay.features(fixture)
    archive = pregame_archive(directory)
    available = {g['game_id'] for g in dataset['games'] if instant(g['available_at']) <= now}
    rows, pending = [], []
    labels_through = bundle['report']['deployment'].get('labels_available_through')
    completed = {g['game_id']: g for g in dataset['games']}
    holdout_path = Path(directory)/'holdout-predictions.json'
    holdout = json.loads(holdout_path.read_text()) if holdout_path.exists() else {}
    old_predictions = {r['game_id']: r for r in holdout.get('rows', [])} if holdout.get('model_id') == bundle['report']['model_id'] else {}
    old_cutoffs = [bundle['report'].get('split', {}).get(k, {}).get('through') for k in ('fit', 'selection', 'calibration')]
    old_cutoff = max(old_cutoffs) if all(old_cutoffs) else None
    context_path=Path(directory)/'context'/'history.json'
    context_history=json.loads(context_path.read_text()) if context_path.exists() else None
    live_context={}
    if context_history:
        from .context import live_features
        from .live_context import snapshot_at
        live_context=live_features(dataset,context_history,snapshot_at(directory,now),now)
    def add_prediction(item,key,values):
        pending.append((item,key,values))
        item['availability_scenarios']=[]
        for alternate in live_context.get(item['game_id'],{}).get('scenarios',[]):
            scenario={'label':alternate['label']};item['availability_scenarios'].append(scenario)
            pending.append((scenario,'prediction',{**values,**alternate['features']}))
    for fixture in fixtures:
        started = instant(fixture['tipoff']) <= now
        features, profiles = state.features(fixture)
        blockers = blockers_for(fixture, dataset, bundle, profiles, now, available)
        item = {**fixture, 'status': 'WAIT', 'blockers': blockers, 'prediction': None,
                'preview_prediction': None, 'pregame_prediction': None,
                'features': features, 'profiles': profiles, 'markets': [],
                'missing_inputs': ['injury_report', 'projected_minutes', 'confirmed_lineups', 'travel'],
                'model_id': bundle['report']['model_id'],
                'game_phase':fixture['state'] if fixture['state'] in ('postponed','canceled') else 'final' if fixture['state'] == 'post' else 'started' if started or fixture['state']=='in' else 'pregame'}
        rows.append(item)
        historical_context=context_history.get('rows',{}).get(fixture['game_id'],{}) if context_history else {}
        current_context=live_context.get(fixture['game_id'],{}) if not started else historical_context
        item['features'].update(current_context.get('features',{}))
        if current_context.get('detail'):
            item['context']=current_context['detail']
            item['availability_fingerprint']=current_context['detail'].get('availability_fingerprint')
            item['context']['availability_observed_at']=current_context.get('availability_observed_at')
            item['missing_inputs']=[]
            for side in ('home','away'):
                rotation=current_context['detail']['rotations'][side]
                if rotation['injury_missing']:item['missing_inputs'].append(side+'_injury_report')
                if not rotation['confirmed']:item['missing_inputs'].append(side+'_confirmed_lineup')
                if rotation['missing']:item['missing_inputs'].append(side+'_projected_minutes')
                if rotation['unknown_share']>0:item['missing_inputs'].append(side+'_player_impact_partial')
            if not current_context['detail']['referees']:item['missing_inputs'].append('referee_assignment')
            if current_context['features'].get('home_travel_missing') or current_context['features'].get('away_travel_missing'):item['missing_inputs'].append('travel')
        final = completed.get(fixture['game_id'])
        if final:
            item['final_score'] = {'home': final['home_stats']['pts'], 'away': final['away_stats']['pts']}
        if started:
            saved = archive.get(fixture['game_id'])
            old = old_predictions.get(fixture['game_id'])
            if saved and instant(saved['as_of']) < instant(fixture['tipoff']):
                item.update(pregame_prediction=saved['prediction'], prediction_kind='saved_pregame',
                            prediction_as_of=saved['as_of'], prediction_model_id=saved['model_id'],
                            features=saved.get('features') or features, profiles=saved.get('profiles') or profiles)
                if saved.get('context'):item['context']=saved['context']
                if saved.get('availability_probability_range'):item['availability_probability_range']=saved['availability_probability_range']
            elif (old and old_cutoff and old_cutoff < fixture['date'] and old.get('date') == fixture['date']
                  and isinstance(old.get('probability'), (int, float)) and math.isfinite(old['probability'])
                  and 0 <= old['probability'] <= 1 and final
                  and old.get('y') == int(final['home_stats']['pts'] > final['away_stats']['pts'])):
                item.update(pregame_prediction={'home_win_probability': old['probability'], 'away_win_probability': 1-old['probability']},
                            prediction_kind='historical_test', prediction_as_of=fixture['tipoff'], prediction_model_id=holdout['model_id'])
                # Reconstruct the historical feature/profile cutoff, never reuse
                # today's profiles for a historical prediction's details.
                item['features'], item['profiles'] = history[fixture['game_id']]
                item['features'].update(historical_context.get('features',{}))
            elif labels_through and instant(labels_through) <= instant(fixture['tipoff']):
                values, past_profiles = history[fixture['game_id']]
                if min(past_profiles['home']['games'], past_profiles['away']['games']) >= 10:
                    item.update(features=values, profiles=past_profiles, prediction_kind='pregame_replay',
                                prediction_as_of=fixture['tipoff'], prediction_model_id=bundle['report']['model_id'])
                    values.update(historical_context.get('features',{}))
                    pending.append((item, 'pregame_prediction', values))
            continue
        outside_horizon = instant(fixture['tipoff']) > now+timedelta(hours=48)
        hard = [b for b in blockers if not b.startswith('preceding_game_result_unavailable:')]
        if hard:
            continue
        if blockers or outside_horizon:
            values, projected = schedule_context(features, profiles, fixture, dataset['schedule'])
            item.update(features=values, profiles=projected, status='provisional', prediction_kind='provisional',
                        prediction_as_of=now.isoformat())
            if outside_horizon:
                item['blockers'].append('forecast_horizon_beyond_48_hours')
            add_prediction(item, 'preview_prediction', values)
        else:
            item.update(status='research_estimate', prediction_kind='current_pregame', prediction_as_of=now.isoformat())
            add_prediction(item, 'prediction', features)
    if pending:
        for (item, key, _), prediction in zip(pending, predict_batch(bundle, [p[2] for p in pending])):
            item[key] = prediction
    for item in rows:
        estimate=item.get('prediction') or item.get('preview_prediction')
        alternatives=item.get('availability_scenarios',[])
        if estimate and alternatives:
            probabilities=[estimate['home_win_probability']]+[s['prediction']['home_win_probability'] for s in alternatives]
            item['availability_probability_range']={'home_min':min(probabilities),'home_max':max(probabilities),
                'kind':'Conditional roster scenarios, not a confidence interval or assigned play probability',
                'baseline':'Reported-out players excluded; uncertain players retain projected minutes.'}
        if instant(item['tipoff'])>now:
            from .availability import entry_safety
            item['entry_safety']=entry_safety(item,now)
            if context_history and context_history.get('dataset_fingerprint')!=dataset.get('fingerprint'):
                item['entry_safety']['blocked']=True
                item['entry_safety']['reasons'].append('player_context_rebuilding')
                item['entry_safety']['messages'].append('Player context is rebuilding after a dataset update; new entries are blocked.')
    remember(archive, rows, now.isoformat(), bundle['report']['model_id'])
    atomic_json(Path(directory)/'pregame-predictions.json', archive)
    from .red_flags import update_trail
    update_trail(directory, rows, now)
    return rows, today
