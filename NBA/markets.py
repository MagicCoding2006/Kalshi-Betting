"""Public Kalshi winner markets, exact schedule joins and depth-aware research EV."""
import json
import math
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from datetime import datetime, timedelta
from pathlib import Path

from desk.model import normalize_book
from .data import DATA, ALIASES, atomic_json, instant, team, utcnow
from .board import build_board
from .model import predict_many

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
SERIES = 'KXNBAGAME'
def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'NBA-Market-Edge/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def discover(fetch=get_json):
    result, cursor, seen = [], '', set()
    for _ in range(20):
        query = urllib.parse.urlencode({'series_ticker': SERIES, 'status': 'open', 'limit': 200, 'cursor': cursor})
        response = fetch(KALSHI + '/markets?' + query)
        result.extend(response.get('markets', []))
        cursor = response.get('cursor')
        if not cursor:
            return result
        if cursor in seen:
            raise ValueError('Kalshi pagination repeated a cursor')
        seen.add(cursor)
    raise ValueError('Kalshi pagination exceeded 20 pages; snapshot would be incomplete')


def variants(code):
    return {code} | {alias for alias, canonical in ALIASES.items() if canonical == code}


def match_market(market, fixtures):
    """Match local event date AND both teams AND the YES suffix. Never use expiry as tipoff."""
    event = market.get('event_ticker', '')
    token = re.fullmatch(r'KXNBAGAME-(\d{2}[A-Z]{3}\d{2})([A-Z]+)', event)
    if not token or market.get('market_type') != 'binary' or market.get('status') not in ('active', 'open'):
        return None
    try:
        local_day = datetime.strptime(token[1], '%y%b%d').date().isoformat()
        yes_team = team(market['ticker'].rsplit('-', 1)[1])
    except (KeyError, ValueError):
        return None
    if market['ticker'] != event + '-' + market['ticker'].rsplit('-', 1)[1]:
        return None
    matches = []
    for fixture in fixtures:
        pairs = {a + h for a in variants(fixture['away']) for h in variants(fixture['home'])}
        if fixture['date'] == local_day and token[2] in pairs and yes_team in (fixture['home'], fixture['away']):
            matches.append(fixture)
    if len(matches) != 1:
        return None
    return matches[0], yes_team


def fee_terms(series):
    if series.get('fee_type') not in ('quadratic', 'quadratic_with_maker_fees'):
        return None
    try:
        multiplier = float(series['fee_multiplier'])
    except (KeyError, ValueError, TypeError):
        return None
    return multiplier if math.isfinite(multiplier) and 0 <= multiplier <= 100 else None


def sweep(levels, quantity, multiplier):
    remaining, cost, fees = quantity, 0., 0.
    for level in levels:
        count = min(remaining, level['quantity'])
        if count <= 0:
            continue
        price = level['price']
        cost += count * price
        # Conservatively round each price-level fill UP to a whole cent. This is
        # an explicit cost buffer, not a claim about account-specific fee rounding.
        fees += math.ceil((.07 * multiplier * count * price * (1-price) - 1e-10) * 100) / 100
        remaining -= count
        if remaining <= 1e-8:
            return {'quantity': quantity, 'price_cost': cost, 'vwap': cost / quantity, 'fee_upper_estimate': fees}
    return None


def compare(probability, book_payload, series, quantity=10, slippage=.005, min_edge=.03, probability_bounds=None):
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 1000:
        raise ValueError('Quantity must be an integer from 1 to 1000')
    if ((probability is not None and (not math.isfinite(probability) or not 0 <= probability <= 1)) or not math.isfinite(slippage)
            or not 0 <= slippage <= .25 or not math.isfinite(min_edge) or not 0 <= min_edge <= 1):
        raise ValueError('Invalid probability, slippage or minimum edge')
    book = normalize_book(book_payload)
    lower,upper=probability_bounds or (probability,probability)
    if probability is not None and not 0 <= lower <= probability <= upper <= 1:raise ValueError('Invalid availability probability bounds')
    multiplier = fee_terms(series)
    candidates = []
    crossed = (book['yes_bids'] and book['yes_asks']
               and book['yes_bids'][0]['price'] > book['yes_asks'][0]['price'] + 1e-8)
    for side, p in [('yes', probability), ('no', 1-probability if probability is not None else None)]:
        edge_probability=(lower if side=='yes' else 1-upper) if probability is not None else None
        levels = book[side + '_asks']
        reasons = []
        if crossed:
            reasons.append('crossed_book')
        if multiplier is None:
            reasons.append('unsupported_fee_schedule')
        fill = sweep(levels, quantity, multiplier) if multiplier is not None else None
        if fill is None:
            reasons.append('insufficient_executable_depth' if multiplier is not None else 'cost_unavailable')
        bids=book[side+'_bids']
        item = {'side': side, 'probability': p, 'fair_price_cents': 100*p if p is not None else None,
                'best_bid':bids[0]['price'] if bids else None,
                'best_ask': levels[0]['price'] if levels else None, 'quantity': quantity,
                'net_edge': None, 'expected_profit_dollars': None, 'status': 'WAIT', 'blockers': reasons}
        if not reasons:
            all_in = fill['price_cost'] + fill['fee_upper_estimate'] + slippage * quantity
            item.update(fill,all_in_cost=all_in,slippage_buffer_per_contract=slippage)
            if p is None:
                item.update(status='quotes_only',blockers=['pregame_estimate_unavailable']);candidates.append(item);continue
            edge = edge_probability - all_in / quantity
            item.update(fill, all_in_cost=all_in, slippage_buffer_per_contract=slippage,
                        net_edge=edge, expected_profit_dollars=quantity * edge_probability - all_in,
                        edge_probability=edge_probability,baseline_net_edge=p-all_in/quantity,
                        ev_assumption='Least favorable availability scenario' if probability_bounds else 'Baseline probability',
                        status='research_edge' if edge >= min_edge else 'no_edge')
        candidates.append(item)
    return candidates


def top_quotes(book):
    result={}
    for side in ('yes','no'):
        for kind in ('bid','ask'):
            levels=book.get(side+'_'+kind+'s',[])
            result[side+'_'+kind]=levels[0]['price'] if levels else None
            result[side+'_'+kind+'_quantity']=levels[0]['quantity'] if levels else 0
    result['crossed']=bool(result['yes_bid'] is not None and result['yes_ask'] is not None and result['yes_bid']>result['yes_ask']+1e-9)
    return result


def market_view(game,yes_team,payload,series,received_at,quantity=10,slippage=.005,min_edge=.03,status='active'):
    """Quote display is independent of having a usable pregame forecast."""
    book=normalize_book(payload);quotes=top_quotes(book);prediction=game.get('prediction')
    p=prediction['home_win_probability' if yes_team==game['home'] else 'away_win_probability'] if prediction else None
    bounds=game.get('availability_probability_range');interval=None
    if p is not None and bounds:interval=(bounds['home_min'],bounds['home_max']) if yes_team==game['home'] else (1-bounds['home_max'],1-bounds['home_min'])
    candidates=compare(p,payload,series,quantity,slippage,min_edge,interval)
    mid=(quotes['yes_bid']+quotes['yes_ask'])/2 if quotes['yes_bid'] is not None and quotes['yes_ask'] is not None else None
    gap=abs(p-mid) if p is not None and mid is not None else None
    blockers=list(game.get('entry_safety',{}).get('reasons',[]))
    if gap is not None and gap>.15+1e-9:blockers.append('large_model_market_disagreement')
    if status not in ('active','open'):blockers.append('market_not_open')
    if blockers:
        for candidate in candidates:
            candidate.update(diagnostic_net_edge=candidate.get('net_edge'),status='WAIT',net_edge=None,expected_profit_dollars=None)
            candidate['blockers']=sorted(set(candidate['blockers']+blockers))
    return {'quotes':quotes,'candidates':candidates,'quote_received_at':received_at,'market_mid':mid,'model_market_gap':gap,'market_status':status,'error':None}


def fixture_blockers(fixture, dataset, bundle, profiles, now, available=None):
    blockers = []
    if fixture.get('live_model_scope_verified') is False:blockers.append('live_fixture_scope_unverified')
    if fixture['state'] != 'pre' or instant(fixture['tipoff']) <= now:
        blockers.append('game_started_or_not_scheduled')
    if (now - instant(dataset['generated_at'])).total_seconds() > 36 * 3600:
        blockers.append('dataset_older_than_36_hours')
    if (now - instant(bundle['report']['created_at'])).total_seconds() > 30 * 86400:
        blockers.append('model_older_than_30_days')
    labels_through = bundle['report'].get('deployment', {}).get('labels_available_through')
    if labels_through and instant(labels_through) > now:
        blockers.append('model_contains_future_labels')
    if min(profiles['home']['games'], profiles['away']['games']) < 10:
        blockers.append('fewer_than_10_prior_games')
    if available is None:
        available = {g['game_id'] for g in dataset['games'] if instant(g['available_at']) <= now}
    # Covers stale/missing final boxes AND games scheduled before this future
    # fixture whose results are not yet known. Do not invent tomorrow's rest/form.
    teams = {fixture['home'], fixture['away']}
    for prior in dataset['schedule']:
        days = (datetime.fromisoformat(fixture['date']) - datetime.fromisoformat(prior['date'])).days
        if (0 < days <= 14 and teams & {prior['home'], prior['away']}
                and prior['state'] not in ('postponed', 'canceled') and prior['game_id'] not in available):
            blockers.append('preceding_game_result_unavailable:' + prior['game_id'])
    return sorted(set(blockers))


def scan(dataset, bundle, directory=DATA, quantity=10, slippage=.005, min_edge=.03, fetch=get_json):
    now = utcnow()
    rows, today = build_board(dataset, bundle, directory, now, fixture_blockers, predict_many)
    fixtures = [g for g in rows if now-timedelta(hours=48) < instant(g['tipoff']) <= now+timedelta(hours=48)]
    series = fetch(KALSHI + '/series/' + SERIES)['series']
    markets = discover(fetch)
    unmapped, raw_books = [], {}
    by_game = {g['game_id']: g for g in rows}
    matched_markets=[]
    for market in markets:
        matched = match_market(market, fixtures)
        if not matched:
            unmapped.append(market.get('ticker'))
            continue
        matched_markets.append((market,matched))
    def read_book(pair):
        market,matched=pair
        try:
            payload=fetch(KALSHI+'/markets/'+urllib.parse.quote(market['ticker'],safe='')+'/orderbook?depth=100')
            return payload,utcnow().isoformat(),None
        except (OSError,HTTPException,ValueError,KeyError,TypeError) as exc:return None,None,str(exc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        books=list(pool.map(read_book,matched_markets))
    for (market,matched),(payload,received,error) in zip(matched_markets,books):
        fixture, yes_team = matched
        game = by_game[fixture['game_id']]
        entry = {'ticker': market['ticker'], 'yes_team': yes_team, 'title': market.get('title'),
                 'rules_primary': market.get('rules_primary'), 'rules_secondary': market.get('rules_secondary'),
                 'candidates': [], 'quote_received_at': None, 'error': None,'quote_source':'model_scan'}
        try:
            if error:
                entry['error']=error;game['markets'].append(entry);continue
            entry.update(market_view(game,yes_team,payload,series,received,quantity,slippage,min_edge,market.get('status','active')))
            raw_books[market['ticker']] = {'payload': payload, 'received_at': entry['quote_received_at']}
        except (OSError, HTTPException,ValueError, KeyError, TypeError) as exc:
            entry['error'] = str(exc)
        game['markets'].append(entry)
    result = {'schema': 1, 'generated_at': now.isoformat(), 'status': 'research_only',
              'scope': 'All scheduled regular-season games in the current NBA season',
              'board_today': today, 'model_id': bundle['report']['model_id'],
              'dataset_generated_at': dataset['generated_at'], 'dataset_fingerprint': dataset['fingerprint'],
              'games': rows, 'unmatched_market_tickers': unmapped, 'quantity': quantity,
              'min_edge': min_edge, 'slippage_buffer': slippage,
              'quote_max_age_seconds': 90, 'estimate_max_age_seconds': 180,
              'model_market_review_threshold':.15,
              'fee_policy': 'Series multiplier, quadratic taker formula, conservative whole-cent rounding per price level.',
              'quote_timestamp_kind': 'Local HTTP receipt time; exchange event timestamp unavailable.',
              'limitations': ['Estimated fair values, not known true probabilities.',
                              'Availability adjusts projected rotations when observed; dedicated injury/lineup coefficients lack historical validation.',
                              'EV assumes normal $1/$0 game settlement; cancellation/fair-price clauses are not modeled.',
                              'No market-beating result or executable fill is established by an outcome backtest.']}
    directory = Path(directory)
    stamp = now.strftime('%Y%m%dT%H%M%S%fZ')
    archived = {**result, 'games': [g for g in rows if g['date'] == today or now < instant(g['tipoff']) <= now+timedelta(hours=48)]}
    atomic_json(directory / 'snapshots' / (stamp + '.json'), {'scan': archived, 'raw_markets': markets,
                                                         'raw_books': raw_books, 'raw_series': series})
    atomic_json(directory / 'fair-prices.json', result)
    return result
