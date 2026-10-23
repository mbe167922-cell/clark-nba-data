# NBA live-data recordings

Nightly recordings of public NBA live data for latency research: when does each free score feed show a basket, and when does Kalshi's public game-winner price move?

- `tools/recorder.py`: ESPN scoreboard + each live game's Kalshi orderbook, every 2 s.
- `tools/feed_race.py`: ESPN scoreboard, ESPN core API, NBA live data and Kalshi's orderbook, every 1 s on one clock.
- `tools/trades_bundle.py`: the night's Kalshi trade prints (time, price) for each recorded game.
- `data/nba-YYYYMMDD.jsonl.gz`, `data/race-YYYYMMDD.jsonl.gz`, `data/trades-YYYYMMDD.json.gz`: one file per night (gzipped JSON lines; a late backup run may append a second gzip member).
- `.github/workflows/record.yml`: starts six times a day (noon to midnight ET, because GitHub fires schedules hours late), records up to 5.5 h or until every game is final, and commits the files; runs queue so they chain.

Public endpoints only, generic User-Agent, no keys, no accounts. Nothing here places, sizes or suggests a trade.
