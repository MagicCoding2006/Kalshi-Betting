"""Versioned, auditable SportsDataverse ingestion; no network on import."""
import csv
import hashlib
import io
import json
import math
import os
import tempfile
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / '.data'
SCHEMA = 1
RELEASES = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download'
ALIASES = {'GS': 'GSW', 'NY': 'NYK', 'NO': 'NOP', 'SA': 'SAS', 'WSH': 'WAS', 'PHO': 'PHX', 'UTAH': 'UTA', 'BRK': 'BKN'}
TEAMS = frozenset('ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS'.split())
BOX_FIELDS = {'pts': 'team_score', 'fgm': 'field_goals_made', 'fga': 'field_goals_attempted',
              'fg3m': 'three_point_field_goals_made', 'fg3a': 'three_point_field_goals_attempted',
              'ftm': 'free_throws_made', 'fta': 'free_throws_attempted', 'oreb': 'offensive_rebounds',
              'dreb': 'defensive_rebounds', 'tov': 'total_turnovers'}


def utcnow():
    return datetime.now(timezone.utc)


def instant(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return dt.astimezone(timezone.utc)


def team(value):
    code = ALIASES.get(value.upper(), value.upper())
    if code not in TEAMS:
        raise ValueError('Unrecognized NBA team: ' + value)
    return code


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temporary files keep simultaneous readers on the last complete version.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = stream.name
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
    os.replace(temporary, path)


def download(url, path):
    request = urllib.request.Request(url, headers={'User-Agent': 'NBA-Market-Edge/1.0', 'Accept': '*/*'})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read(40_000_001)
    if len(content) > 40_000_000:
        raise ValueError('Source exceeded 40 MB limit')
    # Validate before replacing a usable cached file.
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    if not rows:
        raise ValueError('Source returned no CSV rows: ' + url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {'url': url, 'retrieved_at': utcnow().isoformat(), 'sha256': hashlib.sha256(content).hexdigest(),
            'bytes': len(content), 'rows': len(rows)}


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def schedules(rows, excluded=None):
    result = {}
    for row in rows:
        if str(row.get('season_type')) not in ('2', '2.0'):
            continue  # Version 1 is regular season only.
        game_id = str(row.get('game_id') or row['id'])
        try:
            home, away = team(row['home_abbreviation']), team(row['away_abbreviation'])
        except ValueError as exc:
            # ESPN includes All-Star teams and unresolved Cup TBD placeholders
            # in some regular-season schedules. Never guess their identities.
            if excluded is not None:
                excluded.append({'game_id': game_id, 'reason': str(exc)})
            continue
        item = {'game_id': game_id, 'date': row['game_date'], 'tipoff': instant(row['date']).isoformat(),
                'season': int(float(row['season'])), 'home': home,
                'away': away, 'neutral': row.get('neutral_site', '').lower() == 'true',
                'state': row['status_type_state'], 'venue': row.get('venue_full_name')}
        if item['home'] == item['away']:
            raise ValueError('Schedule has the same team twice: ' + game_id)
        if game_id in result and item != result[game_id]:
            raise ValueError('Conflicting schedule rows: ' + game_id)
        result[game_id] = item
    return result


def box_stats(row):
    missing = [column for column in BOX_FIELDS.values() if row.get(column) in (None, '')]
    if missing:
        raise ValueError('Missing box counts: ' + ', '.join(missing))
    stats = {key: float(row[column]) for key, column in BOX_FIELDS.items()}
    if any(not math.isfinite(v) or v < 0 or v != int(v) for v in stats.values()):
        raise ValueError('Invalid box score counts')
    if any(stats[a] > stats[b] for a, b in [('fgm', 'fga'), ('fg3m', 'fg3a'), ('ftm', 'fta'), ('fg3m', 'fgm'), ('fg3a', 'fga')]):
        raise ValueError('Made shots exceed attempts')
    if stats['pts'] != 2 * stats['fgm'] + stats['fg3m'] + stats['ftm'] or not stats['fga']:
        raise ValueError('Box score does not reconcile to points')
    return stats


def games_from_rows(rows, schedule):
    grouped, seen = defaultdict(list), {}
    excluded = []
    for row in rows:
        if str(row.get('season_type')) not in ('2', '2.0'):
            continue
        try:
            key = (str(row['game_id']), team(row['team_abbreviation']))
        except ValueError as exc:
            excluded.append({'game_id': str(row['game_id']), 'reason': str(exc)})
            continue
        if key in seen:
            if row != seen[key]:
                raise ValueError('Conflicting duplicate box score: ' + str(key))
            continue
        seen[key] = row
        grouped[key[0]].append(row)
    games = []
    for game_id, pair in grouped.items():
        try:
            fixture = schedule[game_id]
            if len(pair) != 2 or fixture['state'] != 'post':
                raise ValueError('Two final team rows and a completed schedule entry required')
            home = next(row for row in pair if row['team_home_away'] == 'home')
            away = next(row for row in pair if row['team_home_away'] == 'away')
            if team(home['team_abbreviation']) != fixture['home'] or team(away['team_abbreviation']) != fixture['away']:
                raise ValueError('Box score teams disagree with schedule')
            if (home['opponent_team_id'] != away['team_id'] or away['opponent_team_id'] != home['team_id']
                    or home['game_date'] != away['game_date'] or home['game_date'] != fixture['date']):
                raise ValueError('Opponent/date fields do not match')
            hs, aws = box_stats(home), box_stats(away)
            if hs['pts'] == aws['pts'] or hs['pts'] != float(away['opponent_team_score']) or aws['pts'] != float(home['opponent_team_score']):
                raise ValueError('Final scores disagree or are tied')
            # Historical box scores have no publication timestamp. Use a deliberately
            # late availability assumption: noon UTC the next local calendar day.
            available = datetime.fromisoformat(fixture['date']).replace(hour=12, tzinfo=timezone.utc) + timedelta(days=1)
            games.append({**fixture, 'home_stats': hs, 'away_stats': aws, 'available_at': available.isoformat()})
        except (KeyError, StopIteration, ValueError) as exc:
            excluded.append({'game_id': game_id, 'reason': str(exc) or 'Incomplete home/away pair'})
    return sorted(games, key=lambda g: (g['available_at'], g['game_id'])), excluded


def refresh(start, end, directory=DATA):
    """Years are season END years: 2027 means the 2026–27 NBA season."""
    if not 2003 <= start <= end <= utcnow().year + 1:
        raise ValueError('Invalid season end-year range')
    directory = Path(directory)
    raw = directory / 'raw'
    manifest, all_schedule, all_rows, excluded_schedule = [], {}, [], []
    for year in range(start, end + 1):
        for tag, name in [('espn_nba_team_boxscores', f'team_box_{year}.csv'),
                          ('espn_nba_schedules', f'nba_schedule_{year}.csv')]:
            path = raw / name
            meta_path = raw / (name + '.source.json')
            # Completed past seasons are immutable locally until cache metadata is removed;
            # a current season is always refreshed. Verify cached content hashes.
            metadata = json.loads(meta_path.read_text()) if meta_path.exists() else None
            current_end = utcnow().year + int(utcnow().month >= 9)
            reusable = (year < current_end and path.exists() and metadata
                        and metadata['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest())
            if reusable:
                manifest.append(metadata)
            else:
                print('Fetching ' + name, flush=True)
                metadata = download(f'{RELEASES}/{tag}/{name}', path)
                atomic_json(meta_path, metadata)
                manifest.append(metadata)
        yearly = schedules(read_csv(raw / f'nba_schedule_{year}.csv'), excluded_schedule)
        if all_schedule.keys() & yearly.keys():
            raise ValueError('Game IDs overlap seasons')
        all_schedule.update(yearly)
        all_rows.extend(read_csv(raw / f'team_box_{year}.csv'))
    games, excluded = games_from_rows(all_rows, all_schedule)
    if not games:
        raise ValueError('No valid completed regular-season games')
    dataset = {'schema': SCHEMA, 'generated_at': utcnow().isoformat(), 'sources': manifest,
               'games': games, 'schedule': sorted(all_schedule.values(), key=lambda g: g['tipoff']),
               'excluded': excluded, 'excluded_schedule': excluded_schedule,
               'availability_policy': '12:00 UTC day after local game date; assumed, not observed'}
    dataset['fingerprint'] = hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest()
    atomic_json(directory / 'dataset.json', dataset)
    return {'games': len(games), 'excluded': len(excluded), 'latest_game': games[-1]['date'],
            'fingerprint': dataset['fingerprint']}


def load_dataset(directory=DATA):
    path = Path(directory) / 'dataset.json'
    if not path.exists():
        raise ValueError('No NBA dataset. Run python3 -m NBA refresh first.')
    dataset = json.loads(path.read_text())
    if dataset.get('schema') != SCHEMA:
        raise ValueError('Unsupported NBA dataset version; refresh it')
    return dataset
