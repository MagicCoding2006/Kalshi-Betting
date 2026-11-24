"""Causal state transitions, horizon labels and chronological walk-forward replay."""
import bisect
import math
from collections import Counter, defaultdict

DEADZONE = .001  # 0.1 cent per $1 contract; fixed experiment parameter.
HORIZONS = (2, 5, 10)


def direction(delta):
    return 0 if delta < -DEADZONE - 1e-10 else 2 if delta > DEADZONE + 1e-10 else 1


def state_key(row):
    imbalance = 0 if row['imbalance'] < -.2 else 2 if row['imbalance'] > .2 else 1
    spread = 0 if row['spread'] <= .0100001 else 1 if row['spread'] <= .0300001 else 2
    expiry = 0 if row['seconds_left'] <= 120 else 1 if row['seconds_left'] <= 600 else 2
    return (imbalance, direction(row.get('momentum', 0)), spread, expiry)


def pairs(rows, horizon=5, interior=True):
    """First observed quote at/after the horizon, at most 1 second late.

    No interpolation, contract crossing, or labels through missing data gaps.
    The interior filter applies to entry information only, never future outcomes.
    """
    groups = defaultdict(list)
    for row in rows:
        if row.get('mid') is not None:
            groups[row['ticker']].append(row)
    result = []
    for group in groups.values():
        group.sort(key=lambda r: r['ts'])
        times = [r['ts'] for r in group]
        for i, row in enumerate(group):
            if interior and not .05 <= row['mid'] <= .95:
                continue
            j = bisect.bisect_left(times, row['ts'] + horizon, lo=i+1)
            if j < len(group) and times[j] - row['ts'] <= horizon + 1:
                future = group[j]
                # Reject horizon pairs containing a sampling outage >2 seconds.
                if any(b - a > 2 for a, b in zip(times[i:j], times[i+1:j+1])):
                    continue
                delta = future['mid'] - row['mid']
                result.append({'row': row, 'future': future, 'delta': delta, 'label': direction(delta), 'key': state_key(row)})
    return sorted(result, key=lambda p: p['row']['ts'])


class Markov:
    def __init__(self):
        self.counts = defaultdict(lambda: [0, 0, 0])
        self.sums = defaultdict(float)
        self.global_counts = [0, 0, 0]
        self.global_sum = 0.
        self.matrix = [[0, 0, 0] for _ in range(3)]

    def add(self, sample):
        key, label = sample['key'], sample['label']
        self.counts[key][label] += 1
        self.sums[key] += sample['delta']
        self.global_counts[label] += 1
        self.global_sum += sample['delta']
        self.matrix[key[1]][label] += 1

    def predict(self, row):
        key = state_key(row)
        local = self.counts[key]
        support = sum(local)
        total = sum(self.global_counts)
        if total < 100:
            return None
        use_local = support >= 20
        counts = local if use_local else self.global_counts
        n = sum(counts)
        # Laplace smoothing for direction probabilities, empirical mean for price.
        probabilities = [(c + 1) / (n + 3) for c in counts]
        delta = (self.sums[key] if use_local else self.global_sum) / n
        return {'price': max(0, min(1, row['mid'] + delta)), 'delta': delta,
                'probabilities': probabilities, 'support': support, 'total_examples': total,
                'state': list(key), 'basis': 'state transitions' if use_local else 'global fallback (sparse state)'}


def cutoff_time(samples, fraction=.7):
    tickers = list(dict.fromkeys(s['row']['ticker'] for s in samples))
    if len(tickers) >= 3:
        first_test = tickers[max(1, min(len(tickers)-1, int(len(tickers)*fraction)))]
        return min(s['row']['ts'] for s in samples if s['row']['ticker'] == first_test), 'Chronological whole-contract holdout'
    return samples[int(len(samples)*fraction)]['row']['ts'], 'Within-contract chronological holdout; whole-contract split needs 3+ windows'


def estimate_fee(price, multiplier):
    return math.ceil((.07 * multiplier * price * (1-price) - 1e-10)*100)/100


def execution_trade(row, future, predicted, available_after):
    """One-contract, non-overlapping taker replay. Never touches paper cash.

    Entry choice uses forecast + current spread; realized exit uses future bids.
    """
    if row['ts'] < available_after or row.get('fee_multiplier') is None or future.get('fee_multiplier') is None:
        return None
    candidates = []
    for side in ('yes', 'no'):
        ask = row['ask'] if side == 'yes' else 1-row['bid']
        expected_bid = predicted-row['spread']/2 if side == 'yes' else 1-predicted-row['spread']/2
        expected_bid = max(0, min(1, expected_bid))
        edge = expected_bid-ask-estimate_fee(ask,row['fee_multiplier'])-estimate_fee(expected_bid,row['fee_multiplier'])
        qty = row['ask_qty'] if side == 'yes' else row['bid_qty']
        exit_qty = future['bid_qty'] if side == 'yes' else future['ask_qty']
        if edge > 0 and qty >= 1 and exit_qty >= 1:
            bid = future['bid'] if side == 'yes' else 1-future['ask']
            pnl = bid-ask-estimate_fee(ask,row['fee_multiplier'])-estimate_fee(bid,future['fee_multiplier'])
            candidates.append({'side':side,'entry':row['ts'],'exit':future['ts'],'pnl':pnl,'expected_edge':edge})
    return max(candidates,key=lambda c:c['expected_edge'],default=None)


def metrics(records):
    n = len(records)
    if not n:
        return None
    mae = sum(abs(r['predicted']-r['actual']) for r in records)/n
    baseline = sum(abs(r['current']-r['actual']) for r in records)/n
    correct = sum(direction(r['predicted']-r['current']) == direction(r['actual']-r['current']) for r in records)
    flat = sum(direction(r['actual']-r['current']) == 1 for r in records)
    return {'examples':n,'mae_cents':mae*100,'no_change_mae_cents':baseline*100,
            'direction_accuracy':correct/n,'flat_baseline_accuracy':flat/n}


def backtest(rows, horizon=5, interior=True):
    samples = pairs(rows,horizon,interior)
    if len(samples)<200:
        return {'status':'collecting','reason':'Need 200 labeled observations; collecting real book history', 'labeled_examples':len(samples)}
    cutoff,split = cutoff_time(samples)
    train = [s for s in samples if s['future']['ts'] < cutoff]
    test = [s for s in samples if s['row']['ts'] >= cutoff]
    if len(train)<100 or len(test)<30:
        return {'status':'collecting','reason':'Need 100 training and 30 purged holdout observations','labeled_examples':len(samples)}
    model = Markov()
    for sample in train:model.add(sample)
    # Outcomes only enter the expanding model after their actual observation time.
    train_ids = {id(s) for s in train}
    pending = sorted([s for s in samples if id(s) not in train_ids],key=lambda s:s['future']['ts'])
    pointer,records,trades,available_after = 0,[],[],0
    for sample in test:
        row,future = sample['row'],sample['future']
        while pointer<len(pending) and pending[pointer]['future']['ts']<=row['ts']:
            model.add(pending[pointer]);pointer+=1
        prediction=model.predict(row)
        if prediction is None:continue
        records.append({'time':row['ts'],'target_time':future['ts'],'ticker':row['ticker'],
                        'current':row['mid'],'predicted':prediction['price'],'actual':future['mid'],
                        'imbalance':row['imbalance'],'spread':row['spread'],'state':prediction['state'],'basis':prediction['basis']})
        trade=execution_trade(row,future,prediction['price'],available_after)
        if trade:trades.append(trade);available_after=trade['exit']
    return {'status':'complete','model':'markov','horizon':horizon,'interior':interior,
            'split':split,'split_time':cutoff,'train_examples':len(train),'test_examples':len(test),
            'metrics':metrics(records),'net_pnl':sum(t['pnl'] for t in trades),'trades':len(trades),
            'trade_details':trades[-100:],'replay':records[-150:],'matrix':model.matrix,
            'note':'Walk-forward labels mature before updates. Overlapping forecast errors are correlated; trades do not overlap. P&L assumes one contract at visible quotes, estimated taker fees, no queue or latency simulation.'}
