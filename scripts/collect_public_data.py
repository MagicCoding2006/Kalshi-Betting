"""Download public NBA play-by-play and Kalshi NBA game market research data.

Raw downloads go under data/raw (gitignored). No API keys are required for these
read-only market-data endpoints. This script does not access user accounts/orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import time
from typing import Any, Iterator

import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GITHUB_API = "https://api.github.com/repos/ramirobentes/nba_data/releases/tags/pbp"
GITHUB_RELEASE = "https://github.com/ramirobentes/nba_data/releases/download/pbp"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXNBAGAME"


def request_json(session: requests.Session, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for attempt in range(7):
        try:
            response = session.get(url, params=params, timeout=(10, 45))
        except requests.RequestException:
            if attempt == 6:
                raise
            time.sleep(min(2 ** attempt, 30))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Repeated API failures: {url}")


def download_nba_play_by_play(session: requests.Session, seasons: range) -> list[dict[str, Any]]:
    release = request_json(session, GITHUB_API)
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    dest = RAW / "nba"
    dest.mkdir(parents=True, exist_ok=True)
    manifest = []
    for season in seasons:
        name = f"pbp_regular_{season}.parquet"
        asset = assets.get(name)
        if not asset:
            raise RuntimeError(f"Season asset is missing from source release: {name}")
        path = dest / name
        if not path.exists() or path.stat().st_size != asset["size"]:
            temp = path.with_suffix(path.suffix + ".part")
            with session.get(f"{GITHUB_RELEASE}/{name}", stream=True, timeout=(10, 120)) as response:
                response.raise_for_status()
                with temp.open("wb") as out:
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            out.write(block)
            if temp.stat().st_size != asset["size"]:
                temp.unlink(missing_ok=True)
                raise IOError(f"Downloaded size mismatch for {name}")
            temp.replace(path)
        manifest.append({"name": name, "bytes": path.stat().st_size, "source": asset["browser_download_url"]})
        print(f"NBA {name}: {path.stat().st_size:,} bytes")
    return manifest


def paginated_markets(session: requests.Session, endpoint: str) -> Iterator[dict[str, Any]]:
    cursor = ""
    while True:
        params: dict[str, Any] = {"series_ticker": SERIES, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = request_json(session, f"{KALSHI}/{endpoint}", params)
        yield from page.get("markets", [])
        cursor = page.get("cursor", "")
        if not cursor:
            break


def collect_kalshi_markets(session: requests.Session) -> list[dict[str, Any]]:
    # Older settled markets and current/recent markets live behind separate API tiers.
    all_markets: dict[str, dict[str, Any]] = {}
    for endpoint in ("historical/markets", "markets"):
        count = 0
        for market in paginated_markets(session, endpoint):
            all_markets[market["ticker"]] = market
            count += 1
        print(f"Kalshi {endpoint}: {count:,} contracts before deduplication")
    markets = sorted(all_markets.values(), key=lambda m: (m.get("occurrence_datetime", ""), m["ticker"]))
    dest = RAW / "kalshi"
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / f"{SERIES.lower()}_markets.jsonl.gz"
    with gzip.open(output, "wt", encoding="utf-8") as f:
        for market in markets:
            f.write(json.dumps(market, separators=(",", ":")) + "\n")
    print(f"Kalshi unique markets: {len(markets):,}; saved {output.relative_to(ROOT)}")
    return markets


def _unix_seconds(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def collect_kalshi_in_game_trades(session: requests.Session, markets: list[dict[str, Any]], delay: float) -> int:
    cutoff = request_json(session, f"{KALSHI}/historical/cutoff")
    cutoff_ts = _unix_seconds(cutoff["trades_created_ts"])
    # One team contract per event is sufficient to study its YES probability; the
    # complementary team probability is 1 - YES. Select the market consistently.
    by_event: dict[str, dict[str, Any]] = {}
    for market in markets:
        if market.get("result") not in ("yes", "no"):
            continue
        by_event.setdefault(market["event_ticker"], market)
    chosen = list(by_event.values())
    dest = RAW / "kalshi"
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / f"{SERIES.lower()}_in_game_trades.jsonl.gz"
    checkpoint = dest / f"{SERIES.lower()}_in_game_trades.done.txt"
    completed = set(checkpoint.read_text(encoding="utf-8").splitlines()) if checkpoint.exists() else set()
    if output.exists() and not checkpoint.exists():
        # Recover progress if an earlier run stopped before checkpoints were added.
        with gzip.open(output, "rt", encoding="utf-8") as prior:
            for line in prior:
                completed.add(json.loads(line)["event_ticker"])
        checkpoint.write_text("\n".join(sorted(completed)) + ("\n" if completed else ""), encoding="utf-8")
    total = 0
    mode = "at" if output.exists() else "wt"
    with gzip.open(output, mode, encoding="utf-8") as f, checkpoint.open("a", encoding="utf-8") as done_file:
        for index, market in enumerate(chosen, start=1):
            if market["event_ticker"] in completed:
                continue
            close_time = market.get("close_time") or market.get("expected_expiration_time")
            if not close_time:
                continue
            end_ts = _unix_seconds(close_time) + 120
            min_ts = end_ts - 4 * 60 * 60
            route = "historical/trades" if end_ts < cutoff_ts else "markets/trades"
            cursor = ""
            seen: set[str] = set()
            records = []
            while True:
                params: dict[str, Any] = {
                    "ticker": market["ticker"], "min_ts": min_ts, "max_ts": end_ts, "limit": 1000,
                }
                if cursor:
                    params["cursor"] = cursor
                page = request_json(session, f"{KALSHI}/{route}", params)
                for trade in page.get("trades", []):
                    if trade["trade_id"] in seen:
                        continue
                    seen.add(trade["trade_id"])
                    record = {
                        "event_ticker": market["event_ticker"],
                        "market_ticker": market["ticker"],
                        "yes_sub_title": market.get("yes_sub_title"),
                        "result": market.get("result"),
                        **trade,
                    }
                    records.append(record)
                cursor = page.get("cursor", "")
                if not cursor:
                    break
            for record in records:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
            f.flush()
            total += len(records)
            done_file.write(market["event_ticker"] + "\n")
            done_file.flush()
            completed.add(market["event_ticker"])
            if index % 50 == 0 or index == len(chosen):
                print(f"Kalshi games with trade history checked: {index:,}/{len(chosen):,}; trade prints: {total:,}")
            if delay > 0:
                time.sleep(delay)
    with gzip.open(output, "rt", encoding="utf-8") as f:
        total = sum(1 for _ in f)
    print(f"Kalshi in-game prints in archive: {total:,}; saved {output.relative_to(ROOT)}")
    return total


def collect_kalshi_in_game_candlesticks(
    session: requests.Session, markets: list[dict[str, Any]], delay: float
) -> int:
    """Collect one-minute bid/ask candles for a four-hour window around each game."""
    cutoff = request_json(session, f"{KALSHI}/historical/cutoff")
    cutoff_ts = _unix_seconds(cutoff["market_settled_ts"])
    by_event: dict[str, dict[str, Any]] = {}
    for market in markets:
        if market.get("result") not in ("yes", "no"):
            continue
        by_event.setdefault(market["event_ticker"], market)
    chosen = list(by_event.values())
    dest = RAW / "kalshi"
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / f"{SERIES.lower()}_in_game_1m_candlesticks.jsonl.gz"
    checkpoint = dest / f"{SERIES.lower()}_in_game_1m_candlesticks.done.txt"
    completed = set(checkpoint.read_text(encoding="utf-8").splitlines()) if checkpoint.exists() else set()
    if output.exists() and not checkpoint.exists():
        with gzip.open(output, "rt", encoding="utf-8") as prior:
            for line in prior:
                completed.add(json.loads(line)["event_ticker"])
        checkpoint.write_text("\n".join(sorted(completed)) + ("\n" if completed else ""), encoding="utf-8")
    mode = "at" if output.exists() else "wt"
    with gzip.open(output, mode, encoding="utf-8") as f, checkpoint.open("a", encoding="utf-8") as done_file:
        for index, market in enumerate(chosen, start=1):
            event = market["event_ticker"]
            if event in completed:
                continue
            close_time = market.get("close_time") or market.get("expected_expiration_time")
            if not close_time:
                continue
            end_ts = _unix_seconds(close_time) + 120
            start_ts = end_ts - 4 * 60 * 60
            if end_ts < cutoff_ts:
                url = f"{KALSHI}/historical/markets/{market['ticker']}/candlesticks"
            else:
                url = f"{KALSHI}/series/{SERIES}/markets/{market['ticker']}/candlesticks"
            page = request_json(session, url, {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": 1,
            })
            candles = page.get("candlesticks", [])
            for candle in candles:
                f.write(json.dumps({
                    "event_ticker": event,
                    "market_ticker": market["ticker"],
                    "yes_sub_title": market.get("yes_sub_title"),
                    "result": market.get("result"),
                    **candle,
                }, separators=(",", ":")) + "\n")
            f.flush()
            done_file.write(event + "\n")
            done_file.flush()
            completed.add(event)
            if index % 50 == 0 or index == len(chosen):
                print(f"Kalshi game windows with 1m candles checked: {index:,}/{len(chosen):,}")
            if delay > 0:
                time.sleep(delay)
    with gzip.open(output, "rt", encoding="utf-8") as f:
        total = sum(1 for _ in f)
    print(f"Kalshi 1m in-game candles: {total:,}; saved {output.relative_to(ROOT)}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect public NBA and Kalshi historical research data")
    parser.add_argument("--first-season", type=int, default=2023, help="NBA season end year, e.g. 2023 means 2022-23")
    parser.add_argument("--last-season", type=int, default=2027)
    parser.add_argument("--kalshi-trades", action="store_true", help="Also collect in-game public trade prints; this makes many API requests")
    parser.add_argument("--kalshi-candlesticks", action="store_true", help="Also collect public one-minute in-game bid/ask candles")
    parser.add_argument("--request-delay", type=float, default=0.5, help="Delay between per-game Kalshi trade queries")
    args = parser.parse_args()
    if args.first_season > args.last_season or args.request_delay < 0:
        parser.error("invalid season range or negative request delay")

    RAW.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        session.headers.update({"User-Agent": "KalshiTrading-research-data-collector/0.1"})
        nba_files = download_nba_play_by_play(session, range(args.first_season, args.last_season + 1))
        markets = collect_kalshi_markets(session)
        trade_count = collect_kalshi_in_game_trades(session, markets, args.request_delay) if args.kalshi_trades else None
        candle_count = collect_kalshi_in_game_candlesticks(session, markets, args.request_delay) if args.kalshi_candlesticks else None
    manifest_path = RAW / "collection_manifest.json"
    previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "nba_source": "https://github.com/ramirobentes/nba_data",
        "nba_source_note": "Derived from public NBA play-by-play feeds; not NBA-affiliated. Review source terms before redistribution.",
        "nba_files": nba_files,
        "kalshi_source": "https://docs.kalshi.com/",
        "kalshi_series": SERIES,
        "kalshi_contract_count": len(markets),
        "kalshi_game_count": len({m["event_ticker"] for m in markets}),
        "kalshi_trade_print_count": trade_count if trade_count is not None else previous_manifest.get("kalshi_trade_print_count"),
        "kalshi_one_minute_candlestick_count": candle_count if candle_count is not None else previous_manifest.get("kalshi_one_minute_candlestick_count"),
        "limitations": [
            "Play-by-play does not contain timestamped deflections, loose-ball recoveries, or contested-shot counts.",
            "Public trade prints are not historical order-book snapshots and do not prove an order could fill at that price.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote manifest: {manifest_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
