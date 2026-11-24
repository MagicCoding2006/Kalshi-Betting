"""Availability cutoffs, realistic rotation constraints and seasonal factor selection."""
import copy
import importlib.util
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from NBA.context import ContextState, distribute_minutes, impact_models
from NBA.data import atomic_json
from NBA.experiments import ALL_NAMES, GROUPS, feature_weights, select_winners, split_season
from NBA.live_context import parse_injuries, parse_lineups, parse_assignments, snapshot_at
from NBA.markets import compare


class ContextTests(unittest.TestCase):
    def state(self):
        state=ContextState({}, {}, {}, {2027:{str(i):i-5 for i in range(10)}})
        state.history['BOS'].append({'date':'2026-11-20','game_id':'past','players':[
            {'id':str(i),'name':f'Player {i}','minutes':24,'starter':i<5} for i in range(10)]})
        return state

    def test_out_players_removed_and_uncertain_minutes_not_guessed(self):
        state=self.state();day=date(2026,11,21)
        uncertain=[{'id':'0','name':'Player 0','status':'questionable'}]
        base=state.rotation('BOS',2027,day)
        current=state.rotation('BOS',2027,day,uncertain)
        self.assertEqual(current['impact'],base['impact'])
        self.assertEqual(current['uncertain_minutes'],24)
        out=state.rotation('BOS',2027,day,[{**uncertain[0],'status':'out'}])
        players={p['id']:p for p in out['players']}
        self.assertEqual(players['0']['projected_minutes'],0)
        self.assertAlmostEqual(sum(p['projected_minutes'] for p in players.values()),240)
        self.assertTrue(all(0<=p['projected_minutes']<=48 for p in players.values()))
        self.assertEqual(out['out_minutes'],24)
        self.assertEqual(state.rotation('BOS',2027,date(2026,11,20))['missing'],1,'Own-game minutes cannot enter projection')

    def test_minutes_caps_and_unallocated_minutes_are_explicit(self):
        projected,left=distribute_minutes({'a':100,**{str(i):1 for i in range(7)}})
        self.assertAlmostEqual(sum(projected.values()),240)
        self.assertEqual(projected['a'],48)
        self.assertEqual(left,0)
        projected,left=distribute_minutes({'a':30,'b':30})
        self.assertEqual(left,144)

    def test_future_confirmed_starters_rejected_even_before_tipoff(self):
        state=self.state()
        fixture={'home':'BOS','away':'NYK','season':2027,'game_id':'1','date':'2026-11-21','tipoff':'2026-11-21T23:00:00+00:00'}
        observation={'observed_at':'2026-11-21T21:00:00+00:00','lineups':{'1':{
            'observed_at':'2026-11-21T22:00:00+00:00','teams':{'BOS':['0','1','2','3','4']}}}}
        values,_=state.features(fixture,{},observation)
        self.assertEqual(values['home_confirmed_lineup'],0)
        observation['lineups']['1']['observed_at']=observation['observed_at']
        values,_=state.features(fixture,{},observation)
        self.assertEqual(values['home_confirmed_lineup'],1)

    def test_lineups_must_be_announced_before_start_with_five_unique_players(self):
        now=datetime(2026,11,21,21,tzinfo=timezone.utc)
        f={'tipoff':(now+timedelta(hours=1)).isoformat()}
        p={'header':{'competitions':[{'status':{'type':{'state':'pre'}}}]},'boxscore':{'players':[
            {'team':{'abbreviation':'BOS'},'statistics':[{'athletes':[
                {'starter':True,'athlete':{'id':str(i)}} for i in range(5)]}]}]}}
        self.assertEqual(len(parse_lineups(p,f,now)['BOS']),5)
        self.assertEqual(parse_lineups(p,f,now+timedelta(hours=1)),{})
        p['header']['competitions'][0]['status']['type']['state']='post'
        self.assertEqual(parse_lineups(p,f,now),{})
        p['header']['competitions'][0]['status']['type']['state']='pre'
        p['boxscore']['players'][0]['statistics'][0]['athletes'][-1]['athlete']['id']='0'
        self.assertEqual(parse_lineups(p,f,now),{})

    def test_conflicting_espn_out_and_game_time_decision_is_uncertain(self):
        record={'athlete':{'id':'1','displayName':'Player','team':{'abbreviation':'BOS'}},
                'type':{'name':'INJURY_STATUS_OUT'},'details':{'fantasyStatus':{'abbreviation':'GTD'}}}
        payload={'injuries':[{'displayName':'Boston Celtics','injuries':[record]}]}
        self.assertEqual(parse_injuries(payload)['BOS'][0]['status'],'uncertain')
        self.assertTrue(parse_injuries(payload)['BOS'][0]['source_conflict'])
        record['details']={};record['shortComment']='Listed as questionable for tonight.'
        self.assertEqual(parse_injuries(payload)['BOS'][0]['status'],'uncertain')
        record['shortComment']='Will not play.'
        self.assertEqual(parse_injuries(payload)['BOS'][0]['status'],'out')
        record['type']['name']='INJURY_STATUS_DAYTODAY';record['details']={'fantasyStatus':{'abbreviation':'OUT'}}
        record['shortComment']='Questionable for tonight.'
        self.assertEqual(parse_injuries(payload)['BOS'][0]['status'],'uncertain')

    def test_snapshot_rejects_future_stale_and_old_schema(self):
        now=datetime(2026,11,21,21,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            for schema,seconds,valid in [(2,0,True),(2,1,False),(2,-901,False),(1,0,False)]:
                atomic_json(Path(d)/'context'/'live.json',{'schema':schema,'observed_at':(now+timedelta(seconds=seconds)).isoformat()})
                self.assertEqual(snapshot_at(d,now) is not None,valid)

    def test_official_assignment_parser_requires_date_and_three_refs(self):
        html='<div class="entry-meta">November 21, 2026</div><table><tr><td>Boston @ Sacramento</td><td>A (#1)</td><td>B (#2)</td><td>C (#3)</td></tr></table>'
        self.assertEqual(parse_assignments(html),('2026-11-21',{'BOS@SAC':['A','B','C']}))
        with self.assertRaises(ValueError):parse_assignments('<table></table>')

    def test_yes_and_no_edges_use_opposite_availability_extremes(self):
        book={'orderbook_fp':{'yes_dollars':[['0.40','20']],'no_dollars':[['0.45','20']]}}
        series={'fee_type':'quadratic','fee_multiplier':1}
        base=compare(.6,book,series)
        yes,no=compare(.6,book,series,probability_bounds=(.5,.7))
        self.assertAlmostEqual(yes['edge_probability'],.5)
        self.assertAlmostEqual(no['edge_probability'],.3)
        self.assertAlmostEqual(yes['net_edge'],base[0]['net_edge']-.1)
        self.assertAlmostEqual(no['net_edge'],base[1]['net_edge']-.1)
        self.assertEqual(yes['fair_price_cents'],60)


class FactorSelectionTests(unittest.TestCase):
    def test_every_input_has_one_group_and_disabled_groups_zero_their_columns(self):
        flattened=[f for columns in GROUPS.values() for f in columns]
        self.assertEqual(len(flattened),63)
        self.assertEqual(set(flattened),set(ALL_NAMES))
        config={'groups':{g:float(g=='travel')*2 for g in GROUPS}}
        weights=feature_weights(config)
        for name,weight in zip(ALL_NAMES,weights):self.assertEqual(weight,2 if name in GROUPS['travel'] else 0)

    def test_selection_ignores_test_scores_and_research_only_winner(self):
        def trial(name,loss,accuracy,eligible=True):
            return {'config':{'name':name,'eligible':eligible},'validation':{'log_loss':loss,'accuracy':accuracy},'test':{'accuracy':1}}
        trials=[trial('fair',.58,.66),trial('accuracy',.59,.68),trial('research',.4,.9,False)]
        fair,accurate=select_winners(trials)
        self.assertEqual(fair['config']['name'],'fair')
        self.assertEqual(accurate['config']['name'],'accuracy')
        trials[1]['test']['accuracy']=0
        self.assertEqual(select_winners(trials),(fair,accurate))

    def test_seasonal_fit_and_calibration_purge_late_labels(self):
        rows=[]
        for season,n in [(2022,2001),(2023,201),(2024,201)]:
            for i in range(n):
                tip=datetime(season-1,10,1,tzinfo=timezone.utc)+timedelta(minutes=i)
                rows.append({'season':season,'tipoff':tip.isoformat(),'label_available_at':(tip+timedelta(hours=4)).isoformat()})
        rows[0]['label_available_at']=rows[-1]['tipoff']
        rows[2001]['label_available_at']=rows[-1]['tipoff']
        fit,cal,test=split_season(rows,2024)
        self.assertEqual((len(fit),len(cal),len(test)),(2000,200,201))
        self.assertLess(max(r['label_available_at'] for r in fit),min(r['tipoff'] for r in cal))
        self.assertLess(max(r['label_available_at'] for r in cal),min(r['tipoff'] for r in test))

    @unittest.skipUnless(importlib.util.find_spec('sklearn'),'Requires NBA ML dependencies')
    def test_player_impact_for_current_season_ignores_current_results(self):
        games=[];players={}
        for season in (2025,2026):
            for i in range(300):
                key=f'{season}-{i}'
                games.append({'game_id':key,'season':season,'date':f'{season}-01-01','neutral':False,
                              'home':'BOS','away':'NYK','home_stats':{'pts':100+i%10},'away_stats':{'pts':100}})
                players[key]={code:[{'id':code,'minutes':240}] for code in ('BOS','NYK')}
        with patch('builtins.print'):
            first,meta=impact_models(games,players)
            changed=copy.deepcopy(games)
            for g in changed:
                if g['season']==2026:g['home_stats']['pts']=200
            second,_=impact_models(changed,players)
        self.assertEqual(first[2026],second[2026])
        self.assertEqual(meta[2026]['through'],'2025-01-01')

    @unittest.skipUnless(importlib.util.find_spec('sklearn'),'Requires NBA ML dependencies')
    def test_feature_emphasis_is_applied_after_scaling(self):
        import numpy as np
        from NBA.experiments import fit_weighted
        rows=[{'features':{name:(i+1)*(j+1) for j,name in enumerate(ALL_NAMES)},'y':i%2} for i in range(80)]
        config={'C':.1,'groups':{g:float(g!='shooting')*2 for g in GROUPS}}
        model=fit_weighted(rows[:60],rows[60:],config)
        transformed=model['scaler'].transform([[r['features'][f] for f in ALL_NAMES] for r in rows[:60]])*model['feature_weights']
        self.assertAlmostEqual(float(np.std(transformed[:,ALL_NAMES.index('elo_diff')])),2)
        self.assertEqual(float(np.max(np.abs(transformed[:,ALL_NAMES.index('efg_10_diff')]))),0)


if __name__=='__main__':unittest.main()
