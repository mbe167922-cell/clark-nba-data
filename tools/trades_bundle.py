"""Pack / unpack one night's Kalshi trade prints, because Kalshi's API refuses Claude's cloud (HTTP 403, 2026-10-23).

The public recorder repo (GitHub runners, where Kalshi answers) packs each night's trades for every game it recorded
into data/trades-YYYYMMDD.json.gz; the cloud daily analyst unpacks them into latency_trades.py's cache, so
latency_trades.py and feed_race.py analyze read trades without calling Kalshi. Same file in both repos.

    python tools/trades_bundle.py pack data/nba-20261022.jsonl.gz 20261022 data/trades-20261022.json.gz
    python projects/sports/tools/trades_bundle.py unpack data/live/public/trades-20261022.json.gz
"""
from __future__ import annotations

import gzip
import json
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board  # noqa: E402  (Kalshi date code)
import calibration as cal  # noqa: E402  (Kalshi HTTP with retries)
import latency  # noqa: E402  (ESPN -> Kalshi codes)
import latency_trades as lt  # noqa: E402  (home ticker, trade cache)


def read_lines(path: Path) -> list[dict]:
    """All JSON lines of a gzip file that may hold several members or be cut off at the end."""
    raw, out = path.read_bytes(), []
    while raw:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            text = d.decompress(raw)
        except zlib.error:
            break
        for line in text.decode("utf-8", "replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        raw = d.unused_data
    return out


def game_keys(lines: list[dict]) -> list[str]:
    """'AWAYHOME' Kalshi-code keys of every game that was live in a recorder.py night."""
    keys = set()
    for line in lines:
        if line.get("src") == "espn":
            for g in line.get("rows", []):
                if g.get("state") in ("in", "post"):
                    keys.add(latency.kalshi_code(g["away"]) + latency.kalshi_code(g["home"]))
    return sorted(keys)


def window(lines: list[dict], pad: float = 600) -> tuple[float, float]:
    """(first, last) recording time, padded: only trades the event studies can use are kept."""
    ts = [line["t"] for line in lines if "t" in line]
    return min(ts) - pad, max(ts) + pad


def compact(trades: list[dict], lo: float, hi: float) -> list[list[str]]:
    """Trades inside [lo, hi] as [created_time, yes_price_dollars] pairs (all the event studies read)."""
    out = []
    for t in trades:
        ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
        if lo <= ts <= hi:
            out.append([t["created_time"], t["yes_price_dollars"]])
    return out


def pack(recording: Path, day: str, out: Path) -> dict[str, int]:
    date_code = board.date_code(datetime.strptime(day, "%Y%m%d"))
    lines = read_lines(recording)
    lo, hi = window(lines)
    bundle = {}
    for key in game_keys(lines):
        ticker = lt.home_ticker(key, date_code)
        trades, cursor = [], None
        while True:
            params = {"ticker": ticker, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = cal.get("/markets/trades", params)
            trades += data.get("trades", [])
            cursor = data.get("cursor")
            if not cursor or not data.get("trades"):
                break
            time.sleep(0.2)
        bundle[ticker] = compact(trades, lo, hi)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    return {t: len(v) for t, v in bundle.items()}


def unpack(path: Path, cache: Path = lt.CACHE) -> dict[str, int]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        bundle = json.load(fh)
    cache.mkdir(parents=True, exist_ok=True)
    for ticker, pairs in bundle.items():
        trades = [{"created_time": t, "yes_price_dollars": p} for t, p in pairs]
        (cache / f"{ticker}.json").write_text(json.dumps(trades), encoding="utf-8")
    return {t: len(v) for t, v in bundle.items()}


def main(argv: list[str]) -> None:
    if argv[:1] == ["pack"] and len(argv) == 4:
        print(pack(Path(argv[1]), argv[2], Path(argv[3])))
    elif argv[:1] == ["unpack"] and len(argv) == 2:
        print(unpack(Path(argv[1])))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
