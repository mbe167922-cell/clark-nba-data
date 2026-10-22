"""Live NBA recorder: ESPN live scores + Kalshi game-winner prices, every few seconds, to JSON lines.

Read-only, free public APIs (generic User-Agent, no key, no personal info). It never places, sizes
or suggests a bet. The log answers two questions: which source moves first (latency), and what
late-game prices look like in real time (raw data for the live watcher and its grader).

    python projects/sports/tools/recorder.py --hours 5 --every 2 --out data/live/2026-10-21.jsonl.gz

Each line: {"t": unix seconds when written, "src": "espn"|"book"|"kalshi", "rows": [...]}, written only
when that source changed since the previous poll, so files stay small.
- espn: live scores. book: each LIVE game's home-side bid/ask from the uncached orderbook (with its
  own fetch time), every poll. kalshi: the full market list, once a minute; it is CDN-cached for
  15 s, so never use it for timing (learned 2026-10-22, projects/sports/out/latency.md).
"""
from __future__ import annotations

import argparse
import gzip
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

UA = {"User-Agent": "Mozilla/5.0 (research script; no contact)"}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
ET = ZoneInfo("America/New_York")


def espn_snapshot(data: dict) -> list[dict]:
    """One compact row per game: state, period, clock, scores."""
    games = []
    for event in data.get("events", []):
        comp = event["competitions"][0]
        status = comp["status"]
        teams = {t["homeAway"]: t for t in comp["competitors"]}
        games.append({
            "id": event["id"], "game": event["shortName"], "state": status["type"]["state"],
            "period": status.get("period"), "clock": status.get("displayClock"),
            "home": teams["home"]["team"]["abbreviation"], "home_score": teams["home"].get("score"),
            "away": teams["away"]["team"]["abbreviation"], "away_score": teams["away"].get("score"),
        })
    return games


def kalshi_snapshot(data: dict) -> list[dict]:
    """One compact row per market side: bid, ask, last price, volume."""
    return [{
        "ticker": m["ticker"], "bid": m.get("yes_bid_dollars"), "ask": m.get("yes_ask_dollars"),
        "last": m.get("last_price_dollars"), "volume": m.get("volume_fp") or m.get("volume"),
    } for m in data.get("markets", [])]


def fetch(session: requests.Session, url: str, params: dict) -> dict | None:
    try:
        response = session.get(url, params=params, headers=UA, timeout=8)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return None  # one failed poll must not stop the recording


def all_finished(games: list[dict]) -> bool:
    return bool(games) and all(g["state"] == "post" for g in games)


ESPN_TO_KALSHI = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}
LIST_EVERY = 60.0  # the market list is CDN-cached 15 s; it is only kept for context, once a minute


def home_ticker(game: dict, date_code: str) -> str:
    """ESPN game row -> its Kalshi home-side ticker, e.g. KXNBAGAME-26OCT21INDNOP-NOP."""
    away = ESPN_TO_KALSHI.get(game["away"], game["away"])
    home = ESPN_TO_KALSHI.get(game["home"], game["home"])
    return f"KXNBAGAME-{date_code}{away}{home}-{home}"


def book_quote(data: dict) -> tuple[str | None, str | None]:
    """Uncached orderbook -> (best yes bid, best yes ask) as strings. Both sides list bids only;
    yes ask = 1 - best no bid."""
    book = data.get("orderbook_fp") or {}
    yes = [float(p) for p, _ in book.get("yes_dollars") or []]
    no = [float(p) for p, _ in book.get("no_dollars") or []]
    bid = f"{max(yes):.2f}" if yes else None
    ask = f"{1 - max(no):.2f}" if no else None
    return bid, ask


def record(hours: float, every: float, out: Path, day: str | None) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    espn_params = {"dates": day} if day else {}
    kalshi_params = {"series_ticker": "KXNBAGAME", "status": "open", "limit": 500}
    date_code = (datetime.strptime(day, "%Y%m%d") if day else datetime.now(ET)).strftime("%y%b%d").upper()
    last: dict[str, str] = {}
    lines, stop_at, list_due = 0, time.time() + hours * 3600, 0.0

    def write(src: str, rows: list[dict], fh) -> None:
        nonlocal lines
        key = json.dumps(rows, sort_keys=True)
        if key != last.get(src):
            last[src] = key
            fh.write(json.dumps({"t": round(time.time(), 3), "src": src, "rows": rows}) + "\n")
            lines += 1

    with gzip.open(out, "at", encoding="utf-8") as fh:
        while time.time() < stop_at:
            started = time.time()
            games: list[dict] = []
            data = fetch(session, ESPN, espn_params)
            if data is not None:
                games = espn_snapshot(data)
                write("espn", games, fh)
            # live prices: the uncached orderbook of each live game's home side (one call per game)
            books = []
            for game in games:
                if game["state"] != "in":
                    continue
                ticker = home_ticker(game, date_code)
                ob = fetch(session, f"{KALSHI}/{ticker}/orderbook", {})
                if ob is not None:
                    bid, ask = book_quote(ob)
                    books.append({"ticker": ticker, "bid": bid, "ask": ask, "t": round(time.time(), 3)})
            if books:
                write("book", books, fh)
            if time.time() >= list_due:
                listing = fetch(session, KALSHI, kalshi_params)
                if listing is not None:
                    write("kalshi", kalshi_snapshot(listing), fh)
                list_due = time.time() + LIST_EVERY
            fh.flush()
            if all_finished(games):
                break
            time.sleep(max(0.0, every - (time.time() - started)))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=5.0)
    parser.add_argument("--every", type=float, default=2.0, help="seconds between polls")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--date", help="ESPN date YYYYMMDD (default: ESPN's current slate)")
    args = parser.parse_args()
    print(f"recorded {record(args.hours, args.every, args.out, args.date)} changed snapshots -> {args.out}")


if __name__ == "__main__":
    main()
