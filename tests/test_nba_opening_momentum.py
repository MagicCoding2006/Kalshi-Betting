import copy
import importlib.util
import tempfile
import unittest
from datetime import datetime,timedelta,timezone

from NBA.opening_momentum import simulate,feature_row,build_opening_rows,evaluate_opening,FEATURE_NAMES,opening_status

NOW=datetime(2026,11,25,23,tzinfo=timezone.utc)

def point(t,bid=.49):
    def fill(price,q):return {'vwap':price,'price_cost':price*q,'fee_upper_estimate':.19 if q==10 else .1}
    return {'at':(NOW+timedelta(seconds=t)).isoformat(),'time':NOW.timestamp()+t,'day':'2026-11-25',
        'state':'in','period':1,'clock':720-t,'home_score':2,'away_score':0,'score_age':1,'multiplier':1,
        'liquidity':{'yes':.2,'no':-.2},'fills':{s:{str(q):{'buy':fill(bid+.01,q),'sell':fill(bid,q)} for q in (5,10)} for s in ('yes','no')}}

class OpeningLabelsTests(unittest.TestCase):
    def test_profitable_executable_exit_is_the_label_not_a_winner(self):
        points=[point(0),point(10,.56),point(20,.56)]
        row,error=simulate(points,0,'yes',.55,.45,60,False)
        self.assertIsNone(error);self.assertEqual(row['y'],1);self.assertGreater(row['net_pnl'],0)
        self.assertEqual(row['label_at'],points[2]['at'])
        self.assertNotIn('home_win',row)

    def test_target_touch_with_a_worse_next_fill_is_not_success(self):
        row,error=simulate([point(0),point(10,.55),point(20,.54)],0,'yes',.55,.45,60,False)
        self.assertEqual(row['target_touched'],1);self.assertEqual(row['y'],0);self.assertEqual(row['outcome'],'target_slipped')

    def test_infeasible_tiny_scalp_is_excluded_after_costs(self):
        row,error=simulate([point(0),point(10,.56),point(20,.56)],0,'yes',.53,.45,60,False)
        self.assertIsNone(row);self.assertEqual(error,'target_cannot_cover_costs')

    def test_gap_censors_and_late_price_spike_cannot_be_a_target_hit(self):
        row,error=simulate([point(0),point(40,.6)],0,'yes',.55,.45,60,False)
        self.assertIsNone(row);self.assertEqual(error,'recording_gap')
        points=[point(t) for t in range(0,60,10)]+[point(61,.7),point(70,.7)]
        row,error=simulate(points,0,'yes',.55,.45,60,False)
        self.assertEqual(row['y'],0);self.assertEqual(row['outcome'],'time')

    def test_moving_policy_replays_the_shared_target_not_a_fixed_proxy(self):
        points=[point(t) for t in range(0,111,10)]
        points[-1]=point(110,.59);points+=[point(120,.60),point(130,.60)]
        moving,error=simulate(points,0,'yes',.60,.45,180,True)
        fixed,error=simulate(points,0,'yes',.60,.45,180,False)
        self.assertLess(moving['target_at_trigger'],.60);self.assertEqual(moving['y'],1)
        self.assertEqual(fixed['target_at_trigger'],.60);self.assertEqual(fixed['y'],0)

    def test_future_quote_and_context_cannot_change_present_features(self):
        points=[point(t) for t in range(0,191,10)]
        prior={'at':(NOW-timedelta(minutes=1)).isoformat(),'probability':.6,'features':{'out_minutes_diff':35}}
        before=feature_row(points,6,'yes',prior,.60,.45,180,True)
        altered=copy.deepcopy(points);altered[-1]=point(190,.9)
        self.assertEqual(before,feature_row(altered,6,'yes',prior,.60,.45,180,True));self.assertEqual(len(before),len(FEATURE_NAMES))
        future={**prior,'at':(NOW+timedelta(minutes=10)).isoformat()}
        missing=feature_row(points,6,'yes',future,.60,.45,180,True)
        self.assertEqual(missing[-2:],[1,1]);self.assertEqual(missing[FEATURE_NAMES.index('out_minutes_diff')],0)

    def test_only_opening_quarter_entries_are_used(self):
        points=[point(t) for t in range(0,501,10)]
        _,_,coverage=build_opening_rows({'g':points},{})
        self.assertTrue(coverage['g']['first_30_seconds_observed'])
        for p in points:p['period']=2
        rows,_,coverage=build_opening_rows({'g':points},{})
        self.assertEqual(rows,[]);self.assertEqual(coverage,{})

    def test_missing_report_stays_unpromoted(self):
        with tempfile.TemporaryDirectory() as directory:
            report=opening_status(directory);self.assertEqual(report['status'],'not_run');self.assertFalse(report['promoted'])


@unittest.skipUnless(importlib.util.find_spec('sklearn'),'ML environment required')
class OpeningEvaluationTests(unittest.TestCase):
    def test_test_labels_do_not_change_fit_or_calibration(self):
        rows=[]
        for day in range(20,26):
            for game in range(4):
                for n in range(2):
                    at=f'2026-11-{day}T18:00:00+00:00'
                    x=[.1*(game+n)]*len(FEATURE_NAMES)
                    rows.append({'game_id':f'{day}-{game}','day':at[:10],'at':at,'label_at':f'2026-11-{day}T18:05:00+00:00',
                        'x':x,'y':int(game%2==n),'target_touched':int(game%2==n),'net_pnl':.1 if game%2==n else -.1,
                        'side':'yes','policy':'moving_levels','target_move':.05,'horizon_seconds':180,'outcome':'target' if game%2==n else 'time'})
        report,models=evaluate_opening(rows);self.assertEqual(report['status'],'experimental')
        altered=copy.deepcopy(rows)
        for r in altered:
            if r['day']>=report['split']['test_from']:r['y']=1-r['y']
        _,other=evaluate_opening(altered);self.assertEqual(models,other)
        split=report['split'];self.assertFalse(set(split['train_game_ids'])&set(split['test_game_ids']))
        self.assertFalse(set(split['calibration_game_ids'])&set(split['test_game_ids']))

class OpeningCaptureTests(unittest.TestCase):
    def test_fast_capture_is_limited_to_near_tipoff_or_fresh_first_six_minutes(self):
        from NBA.recording import Recording,opening_capture_window
        with tempfile.TemporaryDirectory() as directory:
            store=Recording(directory)
            try:
                store.game({'game_id':'g','date':'2026-11-25','tipoff':(NOW+timedelta(minutes=5)).isoformat(),
                    'season':2027,'season_type':2,'home':'BOS','away':'NYK','venue':None},NOW.isoformat())
                self.assertTrue(opening_capture_window(store.db,NOW))
                for state,period,clock,age,status,expected in [('in',1,600,0,'STATUS_IN_PROGRESS',True),('in',1,300,0,'STATUS_IN_PROGRESS',False),
                    ('in',1,600,91,'STATUS_IN_PROGRESS',False),('in',2,600,0,'STATUS_IN_PROGRESS',False),
                    ('pre',0,0,0,'STATUS_POSTPONED',False),('post',4,0,0,'STATUS_FINAL',False)]:
                    with self.subTest(state=state,period=period,clock=clock,age=age,status=status):
                        at=(NOW-timedelta(seconds=age)).isoformat()
                        req=store.request(None,'scoreboard',{'payload':{},'url':'test','requested_at':at,'received_at':at,'http_status':200})
                        store.db.execute('INSERT INTO scores(game_id,request_id,observed_at,state,status_name,period,clock_seconds,home_score,away_score) VALUES(?,?,?,?,?,?,?,?,?)',
                            ('g',req,at,state,status,period,clock,0,0))
                        self.assertEqual(opening_capture_window(store.db,NOW),expected)
            finally:store.close()
