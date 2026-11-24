"""Validate period scores from the cached schedule export; never use them as pregame inputs."""
import ast
import math
import re
from pathlib import Path

from .data import read_csv


def period_scores(value):
    if isinstance(value, str):
        # Some releases serialize NumPy arrays as adjacent Python dictionaries,
        # without commas. Parse literal dictionaries only, never execute code.
        chunks = re.findall(r'\{[^{}]*\}', value)
        remainder = re.sub(r'\{[^{}]*\}', '', value)
        if not chunks or remainder.strip(' \t\n\r[],'):
            raise ValueError('Unrecognized period-score format')
        value = [ast.literal_eval(chunk) for chunk in chunks]
    if not isinstance(value, list) or len(value) < 4:
        raise ValueError('At least four complete quarters are required')
    periods = {}
    for item in value:
        p, score = float(item['period']), float(item['value'])
        if not math.isfinite(p) or not math.isfinite(score) or p != int(p) or score != int(score) or p < 1 or score < 0 or int(p) in periods:
            raise ValueError('Invalid or duplicate period score')
        periods[int(p)] = int(score)
    if sorted(periods) != list(range(1, len(periods)+1)):
        raise ValueError('Nonconsecutive period scores')
    return [periods[p] for p in sorted(periods)]


def load_quarters(directory, season):
    path = Path(directory)/'raw'/f'nba_schedule_{season}.csv'
    result, excluded = {}, {}
    if not path.exists():
        return result, {'source': 'Cached season schedule with quarter scores is unavailable'}
    for row in read_csv(path):
        if row.get('season_type') not in ('2', '2.0') or row.get('status_type_state') != 'post':
            continue
        identifier = row.get('game_id') or row.get('id')
        try:
            home, away = period_scores(row['home_linescores']), period_scores(row['away_linescores'])
            if len(home) != len(away) or sum(home) != float(row['home_score']) or sum(away) != float(row['away_score']):
                raise ValueError('Period scores do not reconcile with the final score')
            item = {'home': home, 'away': away}
            if identifier in result and result[identifier] != item:
                raise ValueError('Conflicting period scores')
            result[identifier] = item
        except (ValueError, KeyError, TypeError, SyntaxError) as exc:
            result.pop(identifier, None)
            excluded[identifier] = str(exc)
    return result, excluded
