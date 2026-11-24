import math
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desk.model import DEFAULTS, digital_probability, evaluate, fee, kelly, normalize_book, propose, realized_volatility, return_statistics, validate_config


class ProbabilityTests(unittest.TestCase):
    def test_digital_at_the_money_uses_d2_not_call_delta(self):
        # S=K, sigma=.2, T=1 => N(-.1), whereas N(d1) would be .5398.
        self.assertAlmostEqual(digital_probability(100, 100, 365*86400, .2), .4601721627)

    def test_known_distribution_and_short_horizon(self):
        self.assertAlmostEqual(digital_probability(100, 100*math.exp(-.02), 365*86400, .2), .5)
        self.assertGreater(digital_probability(101, 100, 60, .6), .99)
        self.assertLess(digital_probability(99, 100, 60, .6), .01)
        self.assertEqual(digital_probability(100,100,0,.5),1)
        self.assertEqual(digital_probability(99,100,0,.5),0)
        self.assertEqual(digital_probability(101,100,900,0),1)

    def test_historical_drift_is_measured_from_completed_returns(self):
        candles=[{'time':i*60,'close':100*math.exp(i*.00001)} for i in range(62)]
        stats=return_statistics(candles,3601)
        self.assertEqual(stats['count'],59)
        self.assertAlmostEqual(stats['drift'],.00001*(365*86400/60),places=8)
        candles[-1]['close']=1e30
        self.assertEqual(return_statistics(candles,3601),stats)
        self.assertIsNone(return_statistics(candles[:20],3601)['drift'])

    def test_invalid_inputs(self):
        for args in [(0,100,900,.5),(100,-1,900,.5),(100,100,900,-.2),(float('nan'),100,900,.5)]:
            with self.assertRaises(ValueError): digital_probability(*args)

    def test_realized_volatility_excludes_incomplete_and_missing_minutes(self):
        candles=[{'time':i*60,'close':100*math.exp((i%2)*.001)} for i in range(62)]
        v,count=realized_volatility(candles,3601)
        self.assertEqual(count,59)
        self.assertGreater(v,0)
        candles[61]['close']=1e20
        self.assertEqual(realized_volatility(candles,3601),(v,count))
        self.assertEqual(realized_volatility(candles[:20],3601),(None,19))
        gaps=[{'time':i*120,'close':100+i} for i in range(50)]
        self.assertEqual(realized_volatility(gaps,10000),(None,0))


class BookAndSizingTests(unittest.TestCase):
    def setUp(self):
        self.book=normalize_book({'orderbook_fp':{'yes_dollars':[['0.40','100.50'],['0.45','23']], 'no_dollars':[['0.49','1000'],['0.50','1000.90']]}})
        self.config=dict(DEFAULTS)

    def test_reciprocal_asks_and_fractional_depth(self):
        self.assertEqual(self.book['yes_bids'][0]['price'],.45)
        self.assertEqual(self.book['yes_asks'][0],{'price':.5,'quantity':1000.9})
        self.assertEqual(self.book['no_asks'][0]['price'],.55)
        legacy=normalize_book({'orderbook':{'yes':[[42,3]],'no':[[55,9]]}})
        self.assertEqual(legacy['yes_asks'][0]['price'],.45)
        self.assertFalse(normalize_book({'orderbook_fp':{'yes_dollars':[], 'no_dollars':[]}})['yes_asks'])

    def test_fee_and_kelly(self):
        self.assertEqual(fee(100,.5),1.75)
        self.assertEqual(fee(1,.5),.02)
        self.assertEqual(fee(100,.5,2),3.50)
        self.assertAlmostEqual(kelly(.7,.5),.4)
        self.assertEqual(kelly(.4,.5),0)
        self.assertEqual(kelly(1,1),0)

    def test_sizing_includes_fees_caps_cash_and_depth(self):
        c=propose('yes',.8,self.book,self.config,10000,10000,0,1)
        self.assertGreater(c['contracts'],0)
        self.assertLessEqual(c['cost']+c['contracts']*self.config['slippage'],200)
        self.assertEqual(c['contracts'],int(c['contracts']))
        self.book['yes_asks'][0]['quantity']=2.9
        c=propose('yes',.8,self.book,self.config,10000,10000,0,1)
        self.assertEqual(c['contracts'],2)
        c=propose('yes',.8,self.book,self.config,10000,.3,0,1)
        self.assertEqual(c['contracts'],0)
        c=propose('yes',.8,self.book,self.config,10000,10000,500,1)
        self.assertEqual(c['contracts'],0)
        c=propose('yes',.52,self.book,self.config,10000,10000,0,1)
        self.assertEqual(c['contracts'],0)

    def test_config_rejects_nonfinite_unknown_and_invalid_caps(self):
        for update in [{'kelly_fraction':2},{'min_edge':float('nan')},{'bankroll':True},{'auto':True},{'max_position_pct':.1}]:
            with self.assertRaises(ValueError):validate_config(update,DEFAULTS)
        self.assertEqual(validate_config({'kelly_fraction':.5},DEFAULTS)['kelly_fraction'],.5)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.now=1767225600
        self.market={'ticker':'BTC-TEST','status':'active','open_time':'2025-12-31T23:59:00Z','close_time':'2026-01-01T00:15:00Z','floor_strike':100.,'strike_type':'greater_or_equal'}
        self.book=normalize_book({'orderbook_fp':{'yes_dollars':[['.49','10000']],'no_dollars':[['.49','10000']]}})

    def result(self,**kw):
        args=dict(market=self.market,book=self.book,spot=100.1,source_time=self.now,book_time=self.now,volatility=.6,config=DEFAULTS,now=self.now,equity=10000,cash=10000,exposure=0)
        args.update(kw)
        return evaluate(**args)

    def test_live_action_and_stale_guards(self):
        self.assertEqual(self.result()['action'],'BUY YES')
        for kw in [{'source_time':self.now-16},{'book_time':self.now-11},{'volatility':None},{'fee_supported':False}]:
            result=self.result(**kw)
            self.assertEqual(result['action'],'WAIT')
            self.assertTrue(result['blockers'])

    def test_expiry_missing_strike_unsupported_payoff(self):
        for change in [{'close_time':'2026-01-01T00:01:00Z'},{'status':'closed'},{'floor_strike':None},{'strike_type':'less'}]:
            result=self.result(market={**self.market,**change})
            self.assertEqual(result['action'],'WAIT')

    def test_empty_and_crossed_book(self):
        self.assertEqual(self.result(book={})['action'],'WAIT')
        bad=normalize_book({'orderbook_fp':{'yes_dollars':[['.9','10']],'no_dollars':[['.9','10']]}})
        self.assertIn('Crossed order book snapshot',self.result(book=bad)['blockers'])


if __name__=='__main__': unittest.main()
