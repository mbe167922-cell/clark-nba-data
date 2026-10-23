"""Feed race: which free NBA score feed shows a basket first, and how far behind Kalshi each one is.

The question (2026-10-22): why does Kalshi move before the free scores? The market makers watch
the game on paid official feeds; our ESPN scoreboard shows a basket ~15 s after the price moved (NB-005). This
polls every free feed we found, side by side on one clock, every second:
  espn_sb    ESPN scoreboard (what the recorder and watcher use; cached ~9 s)
  espn_core  ESPN core API, each team's score (cached ~1 s)
  nba_sb     the NBA's own live-data origin, scoreboard (no cache header)
  nba_pbp    the NBA's own live-data origin, play-by-play per game (with the NBA's own event time)
  book       Kalshi orderbook, home side of each live game (uncached)
Research measurement only: nothing here alerts, sizes or places anything. The NBA's terms bar using its data
"in connection with any gambling activity", so nba_* feeds are measured here and not wired into alerts.

    python projects/sports/tools/feed_race.py record --hours 5.5 --date YYYYMMDD --out data/live/race-YYYYMMDD.jsonl.gz
    python projects/sports/tools/feed_race.py analyze data/live/race-YYYYMMDD.jsonl.gz 26OCT22
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board  # noqa: E402  (Kalshi date code)
import latency  # noqa: E402  (ESPN -> Kalshi codes)
import latency_trades as lt  # noqa: E402  (Kalshi trade prints + event study)
import recorder  # noqa: E402  (orderbook quote parsing, ESPN scoreboard URL)

NBA = "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/NBA/liveData"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{e}/competitions/{e}/competitors/{c}/score"
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-script)"}
ET = ZoneInfo("America/New_York")
SCORE_SOURCES = ["espn_sb", "espn_core", "nba_sb", "nba_pbp"]
EVERY = 1.0


# ---------- pure helpers (tested in tests/test_sports_feed_race.py) ----------

def espn_games(data: dict) -> list[dict]:
    """ESPN scoreboard -> [{key, eid, state, away, home, away_id, home_id}] with Kalshi team codes."""
    out = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        side = {c["homeAway"]: c for c in comp["competitors"]}
        a, h = side["away"], side["home"]
        out.append({"key": latency.kalshi_code(a["team"]["abbreviation"]) + latency.kalshi_code(h["team"]["abbreviation"]),
                    "eid": ev["id"], "state": ev["status"]["type"]["state"],
                    "away": a.get("score"), "home": h.get("score"), "away_id": a["id"], "home_id": h["id"]})
    return out


def nba_games(data: dict) -> list[dict]:
    """NBA scoreboard -> [{key, gid, status, away, home}] (status 1 pre, 2 live, 3 final)."""
    return [{"key": g["awayTeam"]["teamTricode"] + g["homeTeam"]["teamTricode"], "gid": g["gameId"],
             "status": g["gameStatus"], "away": g["awayTeam"]["score"], "home": g["homeTeam"]["score"]}
            for g in data.get("scoreboard", {}).get("games", [])]


def pbp_last(data: dict) -> tuple[int, int, str | None] | None:
    """NBA play-by-play -> (away, home, the NBA's own time of the last action) or None."""
    actions = data.get("game", {}).get("actions", [])
    if not actions:
        return None
    last = actions[-1]
    try:
        return int(last["scoreAway"]), int(last["scoreHome"]), last.get("timeActual")
    except (KeyError, TypeError, ValueError):
        return None


def should_stop(games: list[dict], seen_live: bool) -> bool:
    """Stop once every game is final, but only after this run saw a game live: in the morning ESPN's default
    scoreboard still shows last night's finished games (2026-10-23 smoke test stopped at once)."""
    return seen_live and bool(games) and all(g["state"] == "post" for g in games)


def first_seen(lines: list[dict]) -> dict[tuple[str, str], dict[str, float]]:
    """(game, 'away-home') -> {source: first time that source showed that score}."""
    out: dict[tuple[str, str], dict[str, float]] = {}
    for line in lines:
        if line["src"] in SCORE_SOURCES:
            out.setdefault((line["game"], f"{line['away']}-{line['home']}"), {}).setdefault(line["src"], line["t"])
    return out


def lag_table(seen: dict[tuple[str, str], dict[str, float]]) -> dict[str, dict]:
    """Per source: lags behind the fastest source over scores every source saw; first-place count."""
    lags: dict[str, list[float]] = {s: [] for s in SCORE_SOURCES}
    firsts = {s: 0 for s in SCORE_SOURCES}
    n = 0
    for (_, score), by_src in seen.items():
        if score == "0-0" or len(by_src) < len(SCORE_SOURCES):
            continue
        n += 1
        best = min(by_src.values())
        firsts[min(by_src, key=by_src.get)] += 1
        for s, t in by_src.items():
            lags[s].append(t - best)
    return {s: {"n": n, "first": firsts[s],
                "median": statistics.median(v) if v else None,
                "p90": sorted(v)[int(0.9 * (len(v) - 1))] if v else None} for s, v in lags.items()}


def source_scores(lines: list[dict], src: str) -> dict[str, list[tuple[float, str]]]:
    """One source's score changes in latency_trades.event_curve's shape: game -> [(t, 'away-home')]."""
    out: dict[str, list[tuple[float, str]]] = {}
    last: dict[str, str] = {}
    for line in lines:
        if line["src"] != src:
            continue
        score = f"{line['away']}-{line['home']}"
        if last.get(line["game"]) not in (None, score):
            out.setdefault(line["game"], []).append((line["t"], score))
        last[line["game"]] = score
    return out


# ---------- recording ----------

def get_json(session: requests.Session, url: str, params: dict | None = None) -> tuple[float, dict | None, str | None]:
    """(receive time, json or None, HTTP Date header)."""
    try:
        r = session.get(url, params=params, headers=UA, timeout=5)
        t = time.time()
        return t, (r.json() if r.status_code == 200 else None), r.headers.get("Date")
    except (requests.RequestException, ValueError):
        return time.time(), None, None


def record(hours: float, out: Path, day: str | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    last: dict[tuple[str, str], tuple] = {}
    stop = time.time() + hours * 3600
    next_clock = 0.0
    seen_live = False
    with gzip.open(out, "at", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=12) as pool:
        def write(src: str, game: str, t: float, **values) -> None:
            key, val = (src, game), tuple(values.values())
            if last.get(key) != val:
                last[key] = val
                fh.write(json.dumps({"t": round(t, 3), "src": src, "game": game, **values}) + "\n")

        while time.time() < stop:
            started = time.time()
            f_espn = pool.submit(get_json, session, recorder.ESPN, {"dates": day} if day else None)
            f_nba = pool.submit(get_json, session, f"{NBA}/scoreboard/todaysScoreboard_00.json")
            t_e, espn, date_hdr = f_espn.result()
            t_n, nba, _ = f_nba.result()
            eg = espn_games(espn) if espn else []
            ng = nba_games(nba) if nba else []
            for g in eg:
                if g["state"] == "in":
                    write("espn_sb", g["key"], t_e, away=g["away"], home=g["home"])
            for g in ng:
                if g["status"] == 2:
                    write("nba_sb", g["key"], t_n, away=g["away"], home=g["home"])
            live = [g for g in eg if g["state"] == "in"]
            seen_live = seen_live or bool(live)
            jobs = []
            for g in live:
                for side in ("away", "home"):
                    jobs.append(("core", g, side, pool.submit(get_json, session, CORE.format(e=g["eid"], c=g[f"{side}_id"]))))
                ticker = lt.home_ticker(g["key"], board.date_code(datetime.fromtimestamp(started, ET) - timedelta(hours=6)))
                jobs.append(("book", g, None, pool.submit(get_json, session, f"{recorder.KALSHI}/{ticker}/orderbook")))
            for g in ng:
                if g["status"] == 2:
                    jobs.append(("pbp", g, None, pool.submit(get_json, session, f"{NBA}/playbyplay/playbyplay_{g['gid']}.json")))
            core: dict[str, dict] = {}
            for kind, g, side, fut in jobs:
                t, data, _ = fut.result()
                if data is None:
                    continue
                if kind == "core" and data.get("value") is not None:
                    core.setdefault(g["key"], {})[side] = (int(data["value"]), t)
                elif kind == "book":
                    bid, ask = recorder.book_quote(data)
                    write("book", g["key"], t, bid=bid, ask=ask)
                elif kind == "pbp" and (p := pbp_last(data)):
                    write("nba_pbp", g["key"], t, away=p[0], home=p[1], nba_time=p[2])
            for key, sides in core.items():
                if "away" in sides and "home" in sides:
                    write("espn_core", key, max(sides["away"][1], sides["home"][1]),
                          away=sides["away"][0], home=sides["home"][0])
            if started >= next_clock and date_hdr:   # local clock vs ESPN's server Date header (1 s resolution)
                fh.write(json.dumps({"t": round(t_e, 3), "src": "clock",
                                     "offset_s": round(t_e - parsedate_to_datetime(date_hdr).timestamp(), 3)}) + "\n")
                next_clock = started + 600
            fh.flush()
            if should_stop(eg, seen_live):
                print("every game is final; stopping", flush=True)
                break
            time.sleep(max(0.0, EVERY - (time.time() - started)))


# ---------- analysis ----------

def analyze(path: Path, date_code: str) -> str:
    lines = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        try:
            for x in fh:
                if x.strip():
                    lines.append(json.loads(x))
        except (EOFError, json.JSONDecodeError):   # a recording cut off mid-write (PC slept, app closed)
            pass
    lag = lag_table(first_seen(lines))
    games = sorted({ln["game"] for ln in lines if ln["src"] in SCORE_SOURCES})
    trades = {g: lt.to_series(lt.fetch_all_trades(lt.home_ticker(g, date_code))) for g in games}
    offsets = [ln["offset_s"] for ln in lines if ln["src"] == "clock"]
    rows = ["| Feed | Scores compared | First to show it | Median behind the fastest feed | 90th pct behind | "
            "Share of Kalshi's 60 s move already done when this feed showed the score |",
            "|---|---|---|---|---|---|"]
    for s in SCORE_SOURCES:
        L = lag[s]
        curve, n = lt.event_curve(source_scores(lines, s), trades)
        share = f"{curve[0] / curve[60]:.0%} (n {n})" if curve.get(60) else "n/a"
        med = "n/a" if L["median"] is None else f"{L['median']:.1f} s"
        p90 = "n/a" if L["p90"] is None else f"{L['p90']:.1f} s"
        rows.append(f"| {s} | {L['n']} | {L['first']} | {med} | {p90} | {share} |")
    note = (f"Local clock vs ESPN server Date header: median offset {statistics.median(offsets):+.2f} s "
            f"(1 s resolution) over {len(offsets)} checks." if offsets else "No clock checks recorded.")
    return "\n".join([f"# Feed race {path.name}", "", *rows, "", note])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--hours", type=float, default=5.5)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--date", help="slate date YYYYMMDD for ESPN (default: ESPN's current day)")
    a = sub.add_parser("analyze")
    a.add_argument("path", type=Path)
    a.add_argument("date_code")
    args = ap.parse_args(argv)
    if args.cmd == "record":
        record(args.hours, args.out, args.date)
    else:
        print(analyze(args.path, args.date_code))


if __name__ == "__main__":
    main()
