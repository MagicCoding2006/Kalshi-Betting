import copy
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zlib
from datetime import datetime,timedelta,timezone
from pathlib import Path

from NBA.exit_forecast import (build_rows,partitions,weights,infer_band,range_features,fit_ranges,fit_state,
    exit_scenarios,live_research,load_bundle,state_features)
from NBA.exit_data import load_paths
from NBA.recording import SCHEMA
from NBA.risk import settings

NOW=datetime(2026,11,25,23,tzinfo=timezone.utc)

def point(t,bid=.49):
    fills={}
    for side in ('yes','no'):
        fills[side]={}
        for q in (5,10):
            def fill(price):return {'vwap':price,'price_cost':price*q,'fee_upper_estimate':.19 if q==10 else .1}
            fills[side][str(q)]={'buy':fill(.5),'sell':fill(bid)}
    return {'time':NOW.timestamp()+t,'at':(NOW+timedelta(seconds=t)).isoformat(),'day':'2026-11-25','period':2,'clock':720-t,
        'state':'in','home_score':40,'away_score':38,'score_age':2,'fills':fills}

class ResearchWindowsTests(unittest.TestCase):
    def test_future_edits_change_labels_but_not_entry_features(self):
        points=[point(t) for t in range(0,341,10)]
        before=build_rows({'g':points},{},{})[0]['180'][0]
        altered=copy.deepcopy(points);altered[18]=point(180,.75)
        after=build_rows({'g':altered},{},{})[0]['180'][0]
        self.assertEqual(before['x'],after['x']);self.assertNotEqual(before['y'],after['y'])

    def test_gaps_and_missing_depth_censor_instead_of_inventing_prices(self):
        points=[point(0),point(40),point(60)]
        ranges,_,_,excluded=build_rows({'g':points},{},{})
        self.assertEqual(ranges['60'],[]);self.assertGreater(excluded['60s_recording_gap'],0)
        points=[point(t) for t in range(0,71,10)];points[3]['fills']['yes']['10']['sell']=None
        ranges,_,_,excluded=build_rows({'g':points},{},{})
        self.assertTrue(all(r['side']!='yes' for r in ranges['60']));self.assertEqual(excluded['60s_depth_gap'],1)

    def test_partial_comparison_includes_separate_fees_and_next_quote_execution(self):
        points=[point(t) for t in range(0,341,10)]
        rows=build_rows({'g':points},{},{})[2];row=rows[0]
        self.assertAlmostEqual(row['sell_all_now'],-.58);self.assertAlmostEqual(row['sell_half_now'],-.59)
        points[18]=point(180,.8);points[19]=point(190,.4)
        row=build_rows({'g':points},{},{})[2][0]
        self.assertAlmostEqual(row['hold_all_3m'],4-.19-.05-(5+.19+.05))

    def test_pregame_prior_not_yet_available_cannot_leak_into_features(self):
        points=[point(t) for t in range(0,341,10)]
        finals={'g':{'home_win':1,'at':(NOW+timedelta(hours=1)).isoformat()}}
        rows=build_rows({'g':points},{'g':{'probability':.99,'at':(NOW+timedelta(seconds=60)).isoformat()}},finals)[1]
        self.assertEqual(rows[0]['prior'],.5);self.assertTrue(rows[0]['prior_missing']);self.assertNotIn('y',rows[0]['x'])

    def test_split_keeps_game_dates_together_and_purges_unavailable_labels(self):
        rows=[]
        for day in range(20,25):
            for game in range(3):
                at=f'2026-11-{day}T18:00:00+00:00'
                rows.append({'game_id':f'{day}-{game}','day':at[:10],'at':at,'label_at':f'2026-11-{day}T19:00:00+00:00'})
        rows[0]['label_at']='2026-11-24T23:00:00+00:00'
        train,cal,test,meta=partitions(rows)
        self.assertNotIn(rows[0],train)
        self.assertFalse(set(meta['train_game_ids'])&set(meta['test_game_ids']))
        self.assertFalse(set(meta['calibration_game_ids'])&set(meta['test_game_ids']))

    def test_each_game_has_equal_total_weight(self):
        rows=[{'game_id':'a'}]*10+[{'game_id':'b'}];w=weights(rows)
        self.assertAlmostEqual(sum(w[:10]),w[-1])

    def test_loader_keeps_first_final_receipt_and_rejects_post_tipoff_prior(self):
        with tempfile.TemporaryDirectory() as directory:
            db=sqlite3.connect(Path(directory)/'recordings.sqlite3');db.executescript(SCHEMA)
            db.execute("INSERT INTO games VALUES('g','2026-11-25','2026-11-25T19:00:00+00:00',2027,2,'BOS','NYK',NULL,'2026-11-25T18:00:00+00:00','2026-11-25T23:00:00+00:00')")
            for i,at in enumerate(('2026-11-25T22:00:00+00:00','2026-11-25T23:00:00+00:00'),1):
                db.execute('INSERT INTO requests(id,kind,url,requested_at,received_at) VALUES(?,?,?,?,?)',(i,'score','u',at,at))
                db.execute("INSERT INTO scores(game_id,request_id,observed_at,state,status_name,home_score,away_score) VALUES('g',?,?,'post','STATUS_FINAL',110,100)",(i,at))
            for i,(prob,at) in enumerate(((.6,'2026-11-25T18:59:00+00:00'),(.99,'2026-11-25T19:30:00+00:00'))):
                raw=json.dumps({'home':'BOS','away':'NYK','prediction':{'home_win_probability':prob}}).encode();digest=str(i)
                db.execute('INSERT INTO payloads VALUES(?,?,?)',(digest,zlib.compress(raw),len(raw)))
                db.execute("INSERT INTO forecasts(game_id,model_id,as_of,recorded_at,kind,payload_hash) VALUES('g','m',?,?,'issued_pregame',?)",(at,at,digest))
            db.commit();db.close()
            _,priors,finals,_=load_paths(directory)
            self.assertEqual(finals['g']['at'],'2026-11-25T22:00:00+00:00');self.assertEqual(priors['g']['probability'],.6)


class InferenceTests(unittest.TestCase):
    def test_linear_band_clips_to_binary_prices_and_abstains_outside_support(self):
        model={'means':[0],'scales':[1],'coefficients':[0],'intercept':0,'feature_min':[0],'feature_max':[1],
            'residual_low':-.2,'residual_high':.2,'point_offset':0}
        band,error=infer_band(model,[.5],.95);self.assertEqual(band['high'],1);self.assertLessEqual(band['low'],band['center'])
        self.assertEqual(infer_band(model,[5],.5)[1],'outside_training_support')

    def test_current_exit_comparison_prices_actual_quantities_and_never_scales_large_positions(self):
        p={'quantity':10,'entry_cost':5,'ticker':'t','side':'yes'}
        quote={'observed_at':NOW.isoformat(),'status':'active','multiplier':1,'book':{'yes_bids':[{'price':.6,'quantity':100}]}}
        forecast={'routes':{'yes':{'ticker':'t','side':'yes','as_of':NOW.isoformat(),'bands':{'180':{'status':'experimental','low':.4,'center':.5,'high':.7}}}}}
        rows=exit_scenarios(p,quote,forecast,settings({}),NOW)
        self.assertEqual(rows['status'],'experimental');self.assertEqual(rows['options'][1]['remaining'],5)
        self.assertLess(rows['options'][1]['net_pnl_scenario']['low'],rows['options'][1]['net_pnl_scenario']['high'])
        large=exit_scenarios({**p,'quantity':20},quote,forecast,settings({}),NOW)
        self.assertEqual(large['status'],'current_quotes_only');self.assertNotIn('net_pnl_scenario',large['options'][1])
        stale=exit_scenarios(p,{**quote,'observed_at':(NOW-timedelta(seconds=31)).isoformat()},forecast,settings({}),NOW)
        self.assertEqual(stale['status'],'unavailable')

    def test_mismatched_route_cannot_supply_future_scenarios(self):
        p={'quantity':10,'entry_cost':5,'ticker':'t','side':'no'}
        quote={'observed_at':NOW.isoformat(),'status':'active','multiplier':1,'book':{'no_bids':[{'price':.4,'quantity':100}]}}
        forecast={'routes':{'yes':{'ticker':'t','side':'yes','as_of':NOW.isoformat(),'bands':{'180':{'status':'experimental','low':.4,'center':.5,'high':.7}}}}}
        self.assertEqual(exit_scenarios(p,quote,forecast,settings({}),NOW)['status'],'current_quotes_only')

    def test_read_only_inference_module_imports_without_machine_learning_packages(self):
        code="import builtins; old=builtins.__import__; builtins.__import__=lambda n,*a,**k: (_ for _ in ()).throw(ImportError(n)) if n.split('.')[0] in ('numpy','sklearn','scipy') else old(n,*a,**k); from NBA.exit_forecast import load_bundle,linear; print(linear({'intercept':1,'coefficients':[2],'means':[0],'scales':[1]},[3]))"
        run=subprocess.run([sys.executable,'-c',code],text=True,capture_output=True)
        self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(run.stdout.strip(),'7.0')


@unittest.skipUnless(importlib.util.find_spec('sklearn'),'ML environment required')
class ModelTrialTests(unittest.TestCase):
    def rows(self):
        result=[]
        for day in range(20,25):
            for game in range(3):
                at=f'2026-11-{day}T18:00:00+00:00';x=[.5,.02,float(game-1),.5,.01,.01]
                result.append({'game_id':f'{day}-{game}','day':at[:10],'at':at,'label_at':f'2026-11-{day}T19:00:00+00:00',
                    'x':x,'y':.01*(game-1),'current_bid':.5})
        return result

    def test_heldout_targets_cannot_change_range_parameters_or_error_bands(self):
        rows=self.rows();report,artifact=fit_ranges(rows);self.assertEqual(report['status'],'experimental')
        altered=copy.deepcopy(rows)
        for r in altered:
            if r['day']>=report['split']['test_from']:r['y']+=.3
        other,model=fit_ranges(altered)
        self.assertEqual(artifact,model);self.assertNotEqual(report['mean_absolute_error'],other['mean_absolute_error'])

    def test_game_state_fit_and_calibration_do_not_use_test_outcomes(self):
        rows=self.rows()
        for i,r in enumerate(rows):
            r.update(x=state_features(i%3-1,1000,.6),baseline_x=[i%3-1,.5],y=i%3==1,prior=.6,prior_missing=False,market=.5)
        report,model=fit_state(rows);self.assertEqual(report['status'],'experimental');self.assertEqual(len(model['coefficients']),4)
        altered=copy.deepcopy(rows)
        for r in altered:
            if r['day']>=report['split']['test_from']:r['y']=not r['y']
        _,other=fit_state(altered);self.assertEqual(model,other)

class LiveInferenceGuardTests(unittest.TestCase):
    def test_live_bands_require_current_history_and_reject_future_models(self):
        from unittest.mock import patch
        ticker='KXNBAGAME-26NOV25NYKBOS-BOS'
        game={'game_id':'g','date':'2026-11-25','home':'BOS','away':'NYK','tipoff':(NOW-timedelta(hours=1)).isoformat()}
        quote={'ticker':ticker,'game_id':'g','yes_team':'BOS','observed_at':NOW.isoformat(),'status':'active','score_state':'in',
            'multiplier':1,'book':{'yes_bids':[{'price':.5,'quantity':20}]}}
        model={'means':[0]*6,'scales':[1]*6,'coefficients':[0]*6,'intercept':0,'feature_min':[-100]*6,'feature_max':[100]*6,'residual_low':-.1,'residual_high':.1}
        bundle={'models':{'trained_at':(NOW-timedelta(seconds=1)).isoformat(),'range_models':{'180':model}}}
        samples=[{'at':(NOW-timedelta(seconds=t)).isoformat(),'state':'in','period':2,'clock':500,'margin':2,'score_age':1,'bid':.5,'spread':.02} for t in range(70,-1,-10)]
        with patch('NBA.dynamic_exits.load_samples',return_value=samples):
            result=live_research({'games':[game]},{ticker:quote},'.',NOW,bundle)
            self.assertEqual(result['g']['routes']['yes']['bands']['180']['status'],'experimental')
            bundle['models']['trained_at']=(NOW+timedelta(seconds=1)).isoformat()
            self.assertEqual(live_research({'games':[game]},{ticker:quote},'.',NOW,bundle),{})
            bundle['models']['trained_at']=(NOW-timedelta(seconds=1)).isoformat();quote['observed_at']=(NOW-timedelta(seconds=31)).isoformat()
            self.assertEqual(live_research({'games':[game]},{ticker:quote},'.',NOW,bundle)['g']['status'],'unavailable')
