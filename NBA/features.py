"""The same past-only feature builder serves historical replay and pregame scans."""
import math
from collections import defaultdict, deque
from datetime import date

from .data import instant

# Neutral regularization priors, not claimed fitted parameters. Retain their exact
# values in model metadata and feature reports. Rates are fractions, ratings /100.
PRIORS = {'ortg': 110., 'drtg': 110., 'efg': .52, 'opp_efg': .52, 'tov_rate': .14,
          'opp_tov_rate': .14, 'orb_rate': .23, 'opp_orb_rate': .23, 'ftr': .25,
          'opp_ftr': .25, 'three_rate': .38, 'possessions': 100.}
WINDOWS = (10, 30)
FEATURE_NAMES = ['elo_diff', 'home_court', 'rest_diff', 'home_b2b', 'away_b2b',
                 'home_three_in_four', 'away_three_in_four', 'games_last7_diff']
FEATURE_NAMES += [f'{metric}_{window}_diff' for window in WINDOWS for metric in PRIORS]
FEATURE_NAMES += ['possessions_10_mean', 'home_history_games', 'away_history_games']


def metrics(own, opp):
    # Box-score possession estimate; 0.44 is an approximation. This is NOT pace
    # per 48 minutes: this source does not consistently provide game duration.
    poss = (own['fga'] + .44 * own['fta'] - own['oreb'] + own['tov']
            + opp['fga'] + .44 * opp['fta'] - opp['oreb'] + opp['tov']) / 2
    if poss <= 0:
        raise ValueError('Nonpositive estimated possessions')
    return {'ortg': 100 * own['pts'] / poss, 'drtg': 100 * opp['pts'] / poss,
            'efg': (own['fgm'] + .5 * own['fg3m']) / own['fga'],
            'opp_efg': (opp['fgm'] + .5 * opp['fg3m']) / opp['fga'],
            'tov_rate': own['tov'] / poss, 'opp_tov_rate': opp['tov'] / poss,
            'orb_rate': own['oreb'] / max(1, own['oreb'] + opp['dreb']),
            'opp_orb_rate': opp['oreb'] / max(1, opp['oreb'] + own['dreb']),
            'ftr': own['fta'] / own['fga'], 'opp_ftr': opp['fta'] / opp['fga'],
            'three_rate': own['fg3a'] / own['fga'], 'possessions': poss}


class FeatureState:
    def __init__(self):
        self.elo = defaultdict(lambda: 1500.)
        self.history = defaultdict(lambda: deque(maxlen=60))
        self.season = None

    def new_season(self, season):
        if self.season is not None and season > self.season:
            for key in self.elo:
                self.elo[key] = 1500 + .75 ** (season - self.season) * (self.elo[key] - 1500)
        self.season = season

    def update(self, game):
        self.new_season(game['season'])
        h, a = game['home'], game['away']
        expected = self.elo_probability(game)
        shift = 20 * (int(game['home_stats']['pts'] > game['away_stats']['pts']) - expected)
        self.elo[h] += shift
        self.elo[a] -= shift
        for own, opp, code in [('home_stats', 'away_stats', h), ('away_stats', 'home_stats', a)]:
            self.history[code].append({'date': game['date'], 'metrics': metrics(game[own], game[opp]),
                                       'game_id': game['game_id']})

    def rating(self, code, season):
        return 1500 + .75 ** max(0, season - (self.season or season)) * (self.elo[code] - 1500)

    def elo_probability(self, fixture):
        delta = self.rating(fixture['home'], fixture['season']) - self.rating(fixture['away'], fixture['season'])
        return 1 / (1 + 10 ** (-(delta + (0 if fixture['neutral'] else 65)) / 400))

    def profile(self, code, day):
        games = [g for g in self.history[code] if 0 < (day - date.fromisoformat(g['date'])).days <= 365]
        days = [(day - date.fromisoformat(g['date'])).days for g in games]
        profile = {'games': len(games), 'rest': min(7, max(0, min(days) - 1)) if days else 7,
                   'b2b': int(1 in days), 'three_in_four': int(sum(d <= 3 for d in days) >= 2),
                   'last7': sum(d <= 6 for d in days), 'last_game': games[-1]['date'] if games else None}
        for window in WINDOWS:
            recent = games[-window:]
            for metric, prior in PRIORS.items():
                profile[f'{metric}_{window}'] = (sum(g['metrics'][metric] for g in recent) + 5 * prior) / (len(recent) + 5)
        return profile

    def features(self, fixture):
        day = date.fromisoformat(fixture['date'])
        h, a = self.profile(fixture['home'], day), self.profile(fixture['away'], day)
        values = {'elo_diff': self.rating(fixture['home'], fixture['season']) - self.rating(fixture['away'], fixture['season']),
                  'home_court': int(not fixture['neutral']), 'rest_diff': h['rest'] - a['rest'],
                  'home_b2b': h['b2b'], 'away_b2b': a['b2b'],
                  'home_three_in_four': h['three_in_four'], 'away_three_in_four': a['three_in_four'],
                  'games_last7_diff': h['last7'] - a['last7']}
        values.update({f'{metric}_{window}_diff': h[f'{metric}_{window}'] - a[f'{metric}_{window}']
                       for window in WINDOWS for metric in PRIORS})
        values.update({'possessions_10_mean': (h['possessions_10'] + a['possessions_10']) / 2,
                       'home_history_games': min(30, h['games']), 'away_history_games': min(30, a['games'])})
        if any(not math.isfinite(v) for v in values.values()):
            raise ValueError('Nonfinite NBA feature')
        return values, {'home': h, 'away': a}


def state_at(games, as_of):
    state = FeatureState()
    for game in sorted(games, key=lambda g: (g['available_at'], g['game_id'])):
        if instant(game['available_at']) <= as_of:
            state.update(game)
    return state


def training_rows(games):
    """Availability queue prevents final scores of the predicted game leaking in."""
    completed = sorted(games, key=lambda g: (g['available_at'], g['game_id']))
    fixtures = sorted(games, key=lambda g: (g['tipoff'], g['game_id']))
    state, cursor, rows = FeatureState(), 0, []
    for game in fixtures:
        while cursor < len(completed) and instant(completed[cursor]['available_at']) <= instant(game['tipoff']):
            state.update(completed[cursor])
            cursor += 1
        values, profiles = state.features(game)
        # No invented strength for teams with insufficient actual game history.
        if min(profiles['home']['games'], profiles['away']['games']) < 10:
            continue
        rows.append({'game_id': game['game_id'], 'date': game['date'], 'season': game['season'],
                     'tipoff': game['tipoff'], 'label_available_at': game['available_at'],
                     'features': values, 'y': int(game['home_stats']['pts'] > game['away_stats']['pts']),
                     'elo_probability': state.elo_probability(game)})
    return rows
