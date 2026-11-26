"""Expose the new paper-entry filters without promoting an untrained in-game model."""
from dataclasses import asdict

from nba_kalshi.signals import MarketQuote, SignalRules, evaluate_paper_signal
from .data import instant
from .quotes import contract_matches_game
from .scalp_trial import trial_status
from .risk import fee_at

MAX_SPREAD=.05


def dashboard_assessments(snapshot, quotes, config, positions, directory, now, trial=None):
    trial=trial if trial is not None else trial_status(directory);result={}
    relevant={q['game_id'] for q in quotes.values()}|{p['game_id'] for p in positions}
    for game in snapshot.get('games',[]):
        game_id=game['game_id']
        if game_id not in relevant and not game.get('markets'):continue
        blocked=[];routes=[]
        if instant(game['tipoff'])<=now:blocked.append('The pregame sizing screen closes at tipoff. Check the separate live score/clock model panel for any in-game entry candidate.')
        if game.get('entry_safety',{}).get('blocked',True):blocked.extend(game.get('entry_safety',{}).get('messages') or ['Availability verification blocks new entries.'])
        if game.get('status')!='research_estimate' or game.get('blockers'):blocked.append('Current forecast eligibility checks have not passed.')
        if not game.get('prediction'):blocked.append('A current pregame probability is unavailable.')
        if any(p['game_id']==game_id for p in positions):blocked.append('A position in this game is already tracked.')
        remaining=max(0,config['equity']*config['max_total_fraction']-config['other_exposure']-sum(p['entry_cost'] for p in positions))
        cap=None if config['max_loss_fraction'] is None else min(config['equity']*config['max_loss_fraction'],config['cash'],config['equity']*config['max_game_fraction'],remaining)
        if cap is None or cap<=0:blocked.append('Set a positive maximum per-game loss budget and available cash to size a paper entry.')
        for ticker,quote in quotes.items():
            if quote['game_id']!=game_id or not contract_matches_game(ticker,quote.get('yes_team'),game):continue
            fresh=-5<=(now-instant(quote['observed_at'])).total_seconds()<=30
            for side in ('yes','no'):
                item={'ticker':ticker,'side':side,'yes_team':quote.get('yes_team'),'status':'blocked','contracts':0,'reason':None}
                book=quote.get('book');multiplier=quote.get('multiplier')
                if not fresh or not book or multiplier is None:item['reason']='Fresh quotes and known fees are required.'
                elif quote.get('score_state') in ('in','post'):item['reason']='Recorded game has started; an in-game model is required.'
                elif quote.get('status') not in ('active','open'):item['reason']='Market is not open.'
                else:
                    bids=book[side+'_bids'];asks=book[side+'_asks']
                    if not bids or not asks:item['reason']='Buy and sell liquidity are both required.'
                    elif asks[0]['price']<bids[0]['price']:item['reason']='Crossed order book; review prices.'
                    else:
                        ask=asks[0]['price'];bid=bids[0]['price']
                        item.update(ask=ask,bid=bid,spread=ask-bid,ask_depth=int(asks[0]['quantity']),bid_depth=int(bids[0]['quantity']),spread_ok=ask-bid<=MAX_SPREAD)
                        if blocked:item['reason']=' '.join(blocked)
                        else:
                            # Standard engine uses home/away outcomes; a NO route reverses the YES team.
                            outcome='home' if ((quote['yes_team']==game['home'])==(side=='yes')) else 'away'
                            entry_fee=fee_at(ask,1,multiplier)+config['slippage'];exit_fee=fee_at(bid,1,multiplier)+config['slippage']
                            prediction=game['prediction']['home_win_probability']
                            baseline=prediction if outcome=='home' else 1-prediction
                            if abs(baseline-(ask+bid)/2)>config['max_model_market_gap']+1e-9:
                                item['reason']='Model/market disagreement exceeds your review limit.';routes.append(item);continue
                            bounds=game.get('availability_probability_range') or {'home_min':prediction,'home_max':prediction}
                            adverse=max(0,(bounds['home_min'] if outcome=='home' else 1-bounds['home_max'])-config['probability_buffer'])
                            prediction=adverse if outcome=='home' else 1-adverse
                            signal=evaluate_paper_signal(prediction,MarketQuote(outcome,ask,bid,entry_fee,exit_fee,MAX_SPREAD,item['ask_depth'],item['bid_depth']),
                                SignalRules(max(1e-9,config['minimum_edge']),cap,config['max_contracts']))
                            item.update(status=signal.action,reason=signal.reason,contracts=signal.contracts,net_edge=signal.net_edge,
                                        full_loss_cap=cap,probability_basis='Adverse availability scenario with probability buffer; not scalp success probability.')
                routes.append(item)
        if not routes:blocked.append('No matched market quotes are available yet.')
        result[game_id]={'status':'blocked' if blocked or not any(r['status']=='paper_buy' for r in routes) else 'paper_review',
            'blockers':list(dict.fromkeys(blocked)),'routes':routes,'max_spread':MAX_SPREAD,'loss_cap':cap,
            'basis':'Paper entry screening only. Full purchase loss is capped; stop execution is not assumed. Each route is an alternative, not an additional allocation.',
            'trial':{'status':trial['status'],'generated_at':trial.get('generated_at'),'policy':trial.get('policy'),
                'summary':trial.get('summary',{}),'dynamic_comparison':trial.get('dynamic_comparison',{}),'evaluation':trial.get('evaluation',{}),
                'teams':{team:trial.get('teams',{}).get(team) for team in (game.get('away','Away'),game.get('home','Home'))},'promoted':False}}
    return result
