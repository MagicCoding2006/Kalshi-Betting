# Historical Kalshi/NBA data collection

This is an optional, offline research dataset pipeline. It supplements the NBA service in this repository; it does not change the production model or place orders.

## What it collects

- Recent NBA regular-season play-by-play Parquet files from the community [nba_data archive](https://github.com/ramirobentes/nba_data). The archive is not affiliated with the NBA. Check source terms before redistributing downloaded data.
- Kalshi `KXNBAGAME` market metadata from the public live and historical endpoints.
- Public trade prints for one selected team contract per game, over a four-hour window ending shortly after market close.
- One-minute Kalshi bid/ask candlesticks for the same game window and selected contract. Candles include minute-level price ranges and volume.

The current local run retrieved metadata for 3,624 contracts across 1,812 events, 23,355,611 public trade prints, and 401,982 one-minute candles across 1,775 game windows. Raw files and processed CSVs are intentionally gitignored; the collection scripts and instructions are tracked instead.

These market records do **not** include historical order-book depth or queue position. Candlestick OHLC prices do not guarantee that a new order could have filled at those prices. The archive is useful for aligning model snapshots with market price history and for exploratory backtests, but it cannot establish realistic execution or profitability.

## Collect

From the repository root, create an isolated environment and install the optional data dependencies:

```powershell
python -m venv .venv-data
.venv-data\Scripts\python.exe -m pip install -r scripts\requirements-data.txt
.venv-data\Scripts\python.exe scripts\collect_public_data.py --first-season 2023 --last-season 2027 --kalshi-trades --kalshi-candlesticks
```

The NBA season argument is the ending year: `2027` means 2026–27. The Kalshi requests are read-only and do not require account credentials. The collector checkpoints each game so an interrupted run can be restarted with the same command.

To make the separate point-in-time play-by-play feature table:

```powershell
.venv-data\Scripts\python.exe scripts\build_training_snapshots.py --first-season 2023 --last-season 2027
```

This writes `data/processed/nba_training_snapshots.csv`. It is an exploratory feature table for the simple historical win-probability study; it is not the input schema used by the main `NBA` service. Its activity feature uses rebounds, steals, and blocks as a limited proxy. Public play-by-play does not provide all the timestamped deflection, loose-ball recovery, or contested-shot fields needed for a richer hustle measure.

## Local files

All raw downloads and generated snapshots live under `data/`, which is excluded from Git to avoid committing hundreds of megabytes of source data. `data/raw/collection_manifest.json` records retrieval time, source links, file sizes, and known limitations. Rerun the collector to recreate the local archive.
