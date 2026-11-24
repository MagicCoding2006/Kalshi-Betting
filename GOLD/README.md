# Gold strategy

Gold runs inside the same Signal Desk as Bitcoin. Start the app from the repository root:

```sh
python3 server.py
```

Open **http://127.0.0.1:8787/?asset=gold**, or select **Gold** in the sidebar.

`feed.py` reads `KXGOLD15M`'s public Kalshi Gold reference series and forms completed minute closes for volatility and drift. Gold's published settlement source is Pyth's 1-Ounce Gold index; the contract compares candle closes and rounds the settlement to two decimals. It does not use BTC's BRTI averaging rule or a Coinbase price substitute.

Gold has its own $100 starting paper portfolio, `.data/gold.sqlite3`, controls, settings, decisions, positions, official settlement, and reset archives. A Gold reset does not touch BTC. Shared model/HTTP/frontend code lives in `desk/`.

See the [workspace README](../README.md) for usage, assumptions, and tests. No separate Gold site or server is required.
