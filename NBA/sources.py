"""Additional public context sources and source manifests; network only on explicit refresh."""
import hashlib
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .data import DATA, RELEASES, atomic_json, read_csv, utcnow


def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'NBA-Market-Edge/2.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def cache_file(url, path, reuse=False):
    path = Path(path)
    meta_path = path.with_name(path.name+'.source.json')
    if reuse and path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if hashlib.sha256(path.read_bytes()).hexdigest() == meta['sha256']:
            return meta
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'NBA-Market-Edge/2.0'}), timeout=40) as response:
        content = response.read(45_000_001)
    if len(content) > 45_000_000:
        raise ValueError('Additional source exceeds 45 MB')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    meta = {'url': url, 'retrieved_at': utcnow().isoformat(), 'bytes': len(content),
            'sha256': hashlib.sha256(content).hexdigest()}
    atomic_json(meta_path, meta)
    return meta


def refresh_extra(dataset, directory=DATA):
    """Parquet keeps player-history downloads small. PyArrow is ingestion-only."""
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ValueError('Install NBA requirements to read player history (PyArrow).') from exc
    directory = Path(directory)
    seasons = sorted({g['season'] for g in dataset['games']})
    current = utcnow().year+int(utcnow().month >= 9)
    requests = [(tag, f'{stem}_{year}.parquet', year) for year in seasons
                for tag, stem in [('espn_nba_player_boxscores', 'player_box'), ('espn_nba_officials', 'officials')]]
    def pull(item):
        tag, name, year = item
        meta = cache_file(f'{RELEASES}/{tag}/{name}', directory/'raw'/name, reuse=year < current)
        print('Context source: '+name, flush=True)
        return meta
    with ThreadPoolExecutor(max_workers=4) as pool:
        manifest = list(pool.map(pull, requests))
    # Cache coordinates/timezones of actual scheduled venue cities. Distances are
    # city-to-city schedule proxies, never claimed actual flight itineraries.
    venues = {}
    for year in seasons:
        for row in read_csv(directory/'raw'/f'nba_schedule_{year}.csv'):
            identifier = row.get('game_id') or row.get('id')
            venues[identifier] = {'city': row.get('venue_address_city', ''), 'state': row.get('venue_address_state', ''),
                                  'venue': row.get('venue_full_name', ''), 'venue_id': row.get('venue_id', '')}
    geo_path = directory/'context'/'cities.json'
    cities = json.loads(geo_path.read_text()) if geo_path.exists() else {}
    us_states = set('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split())
    locations = {(v['city'], v['state']) for v in venues.values() if v['city']}
    for city, state in sorted(locations):
        key = city+'|'+state
        if key in cities and not cities[key].get('error'):
            continue
        country = 'CA' if state in ('ON', 'QC', 'BC') else 'US' if state in us_states else None
        query = urllib.parse.urlencode({'name': city, 'count': 20, 'language': 'en', 'format': 'json'})
        url = 'https://geocoding-api.open-meteo.com/v1/search?'+query
        try:
            response = get_json(url)
            options = [r for r in response.get('results', []) if not country or r.get('country_code') == country]
            if state in us_states:
                # Prefer the state when provided; population is only a fallback
                # among same-country exact-name locations.
                state_names = {'CA':'California','FL':'Florida','TX':'Texas','NY':'New York','IL':'Illinois','OH':'Ohio',
                               'GA':'Georgia','PA':'Pennsylvania','NC':'North Carolina','MA':'Massachusetts','TN':'Tennessee',
                               'MI':'Michigan','IN':'Indiana','WI':'Wisconsin','MN':'Minnesota','CO':'Colorado','AZ':'Arizona',
                               'UT':'Utah','NV':'Nevada','LA':'Louisiana','OK':'Oklahoma','OR':'Oregon','DC':'District of Columbia'}
                preferred = [r for r in options if r.get('admin1') == state_names.get(state)]
                options = preferred or options
            selected = max(options, key=lambda r: r.get('population', 0)) if options else None
            if selected:
                cities[key] = {'latitude': selected['latitude'], 'longitude': selected['longitude'],
                               'timezone': selected.get('timezone'), 'country': selected.get('country_code'),
                               'matched_name': selected['name'], 'url': url, 'retrieved_at': utcnow().isoformat()}
            else:
                cities[key] = {'error': 'No matching city', 'url': url, 'retrieved_at': utcnow().isoformat()}
        except (OSError, ValueError, KeyError) as exc:
            cities[key] = {'error': str(exc), 'url': url, 'retrieved_at': utcnow().isoformat()}
        atomic_json(geo_path, cities)
    for venue in venues.values():
        venue.update(cities.get(venue['city']+'|'+venue['state'], {}))
    atomic_json(directory/'context'/'venues.json', venues)
    atomic_json(directory/'context'/'sources.json', {'generated_at': utcnow().isoformat(), 'files': manifest,
                                                   'cities': len(cities), 'venue_games': len(venues)})
    return {'seasons': seasons, 'sources': len(manifest), 'cities': len(cities)}


def player_rows(dataset, directory=DATA):
    import pyarrow.parquet as pq
    directory = Path(directory)
    valid = {g['game_id']: g for g in dataset['games']}
    result, skipped = {}, 0
    wanted = ['game_id', 'athlete_id', 'athlete_display_name', 'team_abbreviation', 'minutes', 'starter']
    for season in sorted({g['season'] for g in dataset['games']}):
        path = directory/'raw'/f'player_box_{season}.parquet'
        table = pq.read_table(path)
        if not set(wanted) <= set(table.column_names):
            raise ValueError('Player source schema missing required columns: '+str(path))
        grouped = {}
        for row in table.select(wanted).to_pylist():
            identifier = str(row['game_id'])
            if identifier not in valid:
                continue
            game = valid[identifier]
            from .data import team
            try:
                code = team(row['team_abbreviation'])
                minutes = float(row['minutes'] or 0)
                if not 0 <= minutes <= 80 or code not in (game['home'], game['away']):
                    raise ValueError('Invalid player minutes/team')
                player = {'id': str(row['athlete_id']), 'name': str(row['athlete_display_name']),
                          'minutes': minutes, 'starter': str(row['starter']).lower() in ('true', '1')}
                grouped.setdefault(identifier, {}).setdefault(code, []).append(player)
            except (ValueError, TypeError):
                skipped += 1
        for identifier, teams in grouped.items():
            game = valid[identifier]
            # Expected regulation team minutes = 240, with extra 25 for each OT.
            # Retain genuine overtimes; missing/duplicated minute totals fail shut.
            if all(code in teams and 225 <= sum(p['minutes'] for p in teams[code]) <= 400
                   and len({p['id'] for p in teams[code]}) == len(teams[code]) for code in (game['home'], game['away'])):
                result[identifier] = teams
    return result, skipped


def official_rows(dataset, directory=DATA):
    import pyarrow.parquet as pq
    result = {}
    for season in sorted({g['season'] for g in dataset['games']}):
        path = Path(directory)/'raw'/f'officials_{season}.parquet'
        for row in pq.read_table(path, columns=['game_id', 'official_full_name']).to_pylist():
            result.setdefault(str(row['game_id']), []).append(str(row['official_full_name']))
    return {key: sorted(set(names)) for key, names in result.items()}


def foul_rows(dataset,directory=DATA):
    from .data import team
    valid={g['game_id'] for g in dataset['games']};result={}
    for season in sorted({g['season'] for g in dataset['games']}):
        for row in read_csv(Path(directory)/'raw'/f'team_box_{season}.csv'):
            if row['game_id'] not in valid or row.get('fouls') in ('',None):continue
            try:
                result.setdefault(row['game_id'],{})[team(row['team_abbreviation'])]=float(row['fouls'])
            except (ValueError,TypeError):continue
    return result
