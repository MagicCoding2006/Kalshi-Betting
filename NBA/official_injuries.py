"""Game-specific NBA injury reports. Parsing runs only in the NBA worker venv."""
import hashlib
import io
import json
import re
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import atomic_json, instant, utcnow

INDEX = 'https://www.nba.com/players/injury-report'
PDF_PATTERN = r'https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf'
TEAM_NAMES = dict(zip('ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS'.split(),
    ['Atlanta Hawks','Boston Celtics','Brooklyn Nets','Charlotte Hornets','Chicago Bulls','Cleveland Cavaliers',
     'Dallas Mavericks','Denver Nuggets','Detroit Pistons','Golden State Warriors','Houston Rockets','Indiana Pacers',
     'LA Clippers','Los Angeles Lakers','Memphis Grizzlies','Miami Heat','Milwaukee Bucks','Minnesota Timberwolves',
     'New Orleans Pelicans','New York Knicks','Oklahoma City Thunder','Orlando Magic','Philadelphia 76ers','Phoenix Suns',
     'Portland Trail Blazers','Sacramento Kings','San Antonio Spurs','Toronto Raptors','Utah Jazz','Washington Wizards']))


def player_key(name):
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]', '', re.sub(r'\b(jr|sr|ii|iii|iv)\b', '', name))


def report_time(url):
    match = re.fullmatch(PDF_PATTERN, url)
    if not match:
        raise ValueError('Unrecognized official report URL')
    day, hour, minute, ampm = match.groups()
    return datetime.strptime(day+' '+hour+':'+minute+ampm, '%Y-%m-%d %I:%M%p').replace(tzinfo=ZoneInfo('America/New_York'))


def latest_url(html, now):
    urls = [match.group(0) for match in re.finditer(PDF_PATTERN, html)]
    urls = [url for url in set(urls) if report_time(url) <= now]
    if not urls:
        raise ValueError('No published NBA injury report link is available')
    return max(urls, key=report_time)


def read_url(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 NBA-Market-Edge/3.0'})
    with urllib.request.urlopen(request, timeout=8) as response:
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise ValueError('Official injury response exceeds 5 MB')
    return body


def extract_rows(content):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    if not 1 <= len(reader.pages) <= 30:
        raise ValueError('Unexpected official injury page count')
    all_rows = []
    boundaries = None
    for page_index, page in enumerate(reader.pages):
        chunks = []
        def visit(text, matrix, text_matrix, font, size):
            if text.strip():
                x = text_matrix[4]*matrix[0]+text_matrix[5]*matrix[2]+matrix[4]
                y = text_matrix[4]*matrix[1]+text_matrix[5]*matrix[3]+matrix[5]
                chunks.append((x, y, text.strip()))
        page.extract_text(visitor_text=visit)
        if page_index == 0:
            headers = {text: (x, y) for x, y, text in chunks if text in ('Matchup', 'Team', 'Player', 'Current', 'Reason')}
            if len(headers) != 5 or max(y for x,y in headers.values())-min(y for x,y in headers.values()) > 2:
                raise ValueError('NBA injury table columns changed; refusing ambiguous extraction')
            starts = [headers[name][0]-2 for name in ('Matchup', 'Team', 'Player', 'Current', 'Reason')]
            if starts != sorted(starts):
                raise ValueError('NBA injury table has unordered columns')
            boundaries = [0, 118, *starts]
            header_y = headers['Team'][1]
        # Transform text into physical page coordinates. Some NBA PDFs have a
        # flipped global matrix; layout-mode line order alone reverses their rows.
        lines = defaultdict(list)
        for x, y, text in chunks:
            if y < 70 or y > float(page.mediabox.height)-80 or (page_index == 0 and y >= header_y-2):
                continue
            lines[round(y*2)/2].append((x, text))
        for y, words in sorted(lines.items(), reverse=True):
            cells = [[] for _ in boundaries]
            for x, text in sorted(words):
                column = max(i for i, left in enumerate(boundaries) if x >= left)
                cells[column].append(text)
            all_rows.append([' '.join(words).replace('- ', '-').strip() for words in cells])
    return all_rows


def parse_rows(rows):
    teams = {player_key(value): code for code, value in TEAM_NAMES.items()}
    teams.update({player_key('Los Angeles Clippers'): 'LAC', player_key('LA Lakers'): 'LAL'})
    games = {}
    day = matchup = code = None
    previous = None
    for date_cell, time_cell, match_cell, team_cell, name, status, reason in rows:
        if re.fullmatch(r'\d{2}/\d{2}/\d{4}', date_cell):
            day = datetime.strptime(date_cell, '%m/%d/%Y').date().isoformat()
        if re.fullmatch(r'[A-Z]{3}@[A-Z]{3}', match_cell):
            matchup = match_cell
        if team_cell:
            code = teams.get(player_key(team_cell))
            if code is None:
                raise ValueError('Unrecognized official injury team: '+team_cell)
        if not day or not matchup or not code:
            if name and status:
                raise ValueError('Injury row lacks game/team context')
            continue
        if code not in matchup.split('@'):
            raise ValueError('Official team is inconsistent with matchup')
        game = games.setdefault(day+'|'+matchup, {})
        entry = game.setdefault(code, {'status': 'reported', 'players': []})
        if 'NOTYETSUBMITTED' in re.sub(r'\s+', '', status+reason).upper():
            entry['status'] = 'not_submitted'; previous = None
            continue
        if name and status:
            canonical = status.lower().replace(' ', '')
            if canonical not in ('out','questionable','doubtful','probable','available'):
                raise ValueError('Unrecognized official player status: '+status)
            if ',' not in name:
                raise ValueError('Unrecognized official player name: '+name)
            last, first = name.split(',', 1)
            # Reasons span multiple vertically centered lines. Participation
            # status is the model input; do not attach a neighboring row's
            # medical explanation using a plain-text continuation heuristic.
            previous = {'name': (first.strip()+' '+last.strip()).strip(), 'status': canonical, 'detail': ''}
            entry['players'].append(previous)
    if not games:
        raise ValueError('No game injury rows extracted')
    return games


def collect_official(directory, now=None, fetch=read_url):
    now = now or utcnow()
    directory = Path(directory)/'context'/'official'
    directory.mkdir(parents=True, exist_ok=True)
    url = latest_url(fetch(INDEX).decode(), now)
    path = directory/'latest.json'
    cached = json.loads(path.read_text()) if path.exists() else None
    if cached and cached.get('url') == url:
        result = cached
    else:
        body = fetch(url)
        result = {'url': url, 'published_at': report_time(url).isoformat(), 'observed_at': utcnow().isoformat(),
                  'sha256': hashlib.sha256(body).hexdigest(), 'games': parse_rows(extract_rows(body))}
        (directory/url.rsplit('/',1)[-1]).write_bytes(body)
    result['checked_at'] = utcnow().isoformat()
    atomic_json(path, result)
    return result


def roster_names(directory, espn):
    result = defaultdict(dict)
    path = Path(directory)/'context'/'history.json'
    if path.exists():
        current = json.loads(path.read_text()).get('current', {})
        owner = current.get('owner', {})
        for code, games in current.get('history', {}).items():
            for game in games:
                for player in game['players']:
                    if owner.get(player['id'], code) == code:
                        result[code][player_key(player['name'])] = player['id']
    for code, injuries in espn.items():
        for injury in injuries:
            result[code][player_key(injury['name'])] = injury['id']
    return result


def game_reports(dataset, official, espn, names):
    result = {}
    for fixture in dataset['schedule']:
        key = fixture['date']+'|'+fixture['away']+'@'+fixture['home']
        report = official.get('games', {}).get(key, {})
        if not report:
            continue
        teams = {}; coverage = {}; unmatched = [];pending_secondary=[]
        for code in (fixture['home'], fixture['away']):
            item = report.get(code, {})
            coverage[code] = item.get('status', 'missing')
            if item.get('status') != 'reported':
                continue
            players = []
            for row in item['players']:
                identifier = names.get(code, {}).get(player_key(row['name']))
                if identifier:
                    players.append({**row, 'id': identifier, 'source': 'nba_official', 'source_updated_at': official['published_at']})
                else:
                    unmatched.append({'team': code, **row})
            # Official game-specific reports supersede generic fantasy flags.
            # A newer, explicitly game-dated ESPN announcement can warn of a
            # change before the next official PDF. Treat it as unresolved, not
            # as confirmation and not as an update for a different game/day.
            by_id={p['id']:p for p in players}
            for secondary in espn.get(code,[]):
                stamp=secondary.get('source_updated_at');text=(secondary.get('detail') or '').lower()
                if not stamp or instant(stamp)<=instant(official['published_at']):continue
                weekday=datetime.fromisoformat(fixture['date']).strftime('%A').lower()
                days=re.findall(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',text)
                if days:
                    applies=weekday in days
                else:
                    applies=bool(re.search(r'\b(tonight|today)\b',text)) and instant(stamp).astimezone(ZoneInfo('America/New_York')).date().isoformat()==fixture['date']
                if not applies:continue
                declared='out' if re.search(r'\b(out|ruled out|will not play|won.t play)\b',text) else 'questionable' if re.search(r'\b(questionable|doubtful|game.time decision|probable)\b',text) else 'available' if re.search(r'\b(available|will play)\b',text) else None
                old=by_id.get(secondary['id'])
                official_status=old['status'] if old else 'not_listed'
                if declared is None or declared==official_status or declared=='available' and old is None:continue
                changed={**secondary,'status':'uncertain','source':'espn_pending_official','source_conflict':True}
                if old:players.remove(old)
                players.append(changed);by_id[changed['id']]=changed
                pending_secondary.append({'team':code,'name':changed['name'],'declared_status':declared,'official_status':official_status,'source_updated_at':stamp})
            teams[code] = players
        result[fixture['game_id']] = {'teams': teams, 'coverage': coverage, 'unmatched': unmatched,
            'url': official['url'], 'published_at': official['published_at'],
            'checked_at': official['checked_at'], 'observed_at': official['observed_at'],'pending_secondary':pending_secondary}
    return result
