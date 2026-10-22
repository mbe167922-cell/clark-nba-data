# NBA live-data recordings

Nightly recordings of public NBA live data for latency research: when does each free score feed show a basket, and when does Kalshi's public game-winner price move?

- `tools/recorder.py`: ESPN scoreboard + each live game's Kalshi orderbook, every 2 s.
- `tools/feed_race.py`: ESPN scoreboard, ESPN core API, NBA live data and Kalshi's orderbook, every 1 s on one clock.
- `data/nba-YYYYMMDD.jsonl.gz`, `data/race-YYYYMMDD.jsonl.gz`: one file per night (gzipped JSON lines; a late backup run may append a second gzip member).
- `.github/workflows/record.yml`: runs nightly at 6:40 PM ET with an 11:30 PM ET backup, records until every game is final, commits the files.

Public endpoints only, generic User-Agent, no keys, no accounts. Nothing here places, sizes or suggests a trade.
