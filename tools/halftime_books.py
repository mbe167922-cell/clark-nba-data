"""NB-032 halftime sportsbook snapshots and their pre-registered grading (`out/PREREG-NB-032.md`). Paper only.

record: polls ESPN's free scoreboard every 20 s. While a game is at halftime it reads DraftKings, FanDuel
and BetMGM moneylines from The Odds API (key from env ODDS_API_KEY; never printed or stored) and each
halftime game's uncached Kalshi orderbook, one JSON line per call. It never places, sizes or suggests a bet.

    python tools/halftime_books.py check   (free call: does the key work, credits left)
    python tools/halftime_books.py record --hours 5.5 --date 20261026 --out data/books-20261026.jsonl.gz
    python projects/sports/tools/halftime_books.py count data/live/public/books-*.jsonl.gz
    python projects/sports/tools/halftime_books.py grade data/live/public   (only at the pre-registered stop)

Each line: {"t", "src": "snap", "remaining", "used", "error", "events": [book odds], "games": [halftime games]}.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recorder import ESPN, ET, KALSHI, UA, espn_snapshot, fetch, home_ticker  # noqa: E402
from trades_bundle import read_lines  # noqa: E402  (multi-member, cut-off-safe gzip reader)

ODDS = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
BOOKS = ("draftkings", "fanduel", "betmgm")
POLL_S = 20
FIRST_CALL_GAP_S = 90      # a game newly at halftime triggers a call if the last one is this old
REFRESH_S = 240            # then every 4 min while any game is at halftime
MAX_CALLS = 16             # per night: ~30 nights fit the free 500 credits a month
CREDIT_FLOOR = 25

# pre-registered grading constants (PREREG-NB-032)
MAX_AGE_S = 60
SENSITIVITY_AGE_S = 300
MAX_DISPERSION = 0.03
MAX_SPREAD = 0.02
MIN_SIZE = 10
EDGE_MARGIN = 0.03
ENTRY_WINDOW_S = 13 * 60
CONVERGED = 0.01
REF_MOVED = 0.02
Q3_STOP_CLOCK_S = 9 * 60
EXIT_MATCH_S = 30
TARGET_HALFTIMES = 120
MIN_ENTRIES = 6
N_CONTRACTS = 10


# ---------- record ----------

def halftime_games(data: dict) -> list[dict]:
    """ESPN scoreboard -> games whose status is STATUS_HALFTIME, with full team names for matching books."""
    games = []
    for event, row in zip(data.get("events", []), espn_snapshot(data)):
        comp = event["competitions"][0]
        kind = comp["status"]["type"]
        if kind.get("name") != "STATUS_HALFTIME" and kind.get("description") != "Halftime":
            continue
        teams = {t["homeAway"]: t["team"] for t in comp["competitors"]}
        games.append({**row, "home_name": teams["home"].get("displayName"), "away_name": teams["away"].get("displayName")})
    return games


def should_call(now: float, last_call: float, new_halftime: bool, any_halftime: bool, calls: int,
                remaining: int | None) -> bool:
    if not any_halftime or calls >= MAX_CALLS or (remaining is not None and remaining < CREDIT_FLOOR):
        return False
    gap = now - last_call
    return gap >= REFRESH_S or (new_halftime and gap >= FIRST_CALL_GAP_S)


def compact_events(events: list[dict]) -> list[dict]:
    """Odds API events -> home/away names and each book's h2h prices with their last_update."""
    out = []
    for e in events:
        books = {}
        for b in e.get("bookmakers", []):
            market = next((m for m in b.get("markets", []) if m.get("key") == "h2h"), None)
            if market:
                books[b["key"]] = {"last_update": market.get("last_update") or b.get("last_update"),
                                   "prices": {o["name"]: o["price"] for o in market.get("outcomes", [])}}
        out.append({"home": e.get("home_team"), "away": e.get("away_team"), "commence": e.get("commence_time"), "books": books})
    return out


def fetch_odds(session: requests.Session, key: str) -> tuple[list[dict] | None, int | None, int | None, str | None]:
    """(events, credits remaining, credits used, error). Errors never include the URL, which holds the key."""
    params = {"apiKey": key, "regions": "us", "markets": "h2h", "oddsFormat": "american", "bookmakers": ",".join(BOOKS)}
    try:
        response = session.get(ODDS, params=params, headers=UA, timeout=10)
    except requests.RequestException as exc:
        return None, None, None, type(exc).__name__
    def header(name: str) -> int | None:
        value = response.headers.get(name)
        return int(float(value)) if value not in (None, "") else None
    remaining, used = header("x-requests-remaining"), header("x-requests-used")
    if response.status_code != 200:
        return None, remaining, used, f"HTTP {response.status_code}"
    try:
        return response.json(), remaining, used, None
    except ValueError:
        return None, remaining, used, "bad JSON"


def check_key(session: requests.Session, key: str) -> str:
    """The free /sports listing (costs no credit): proves the key works and shows the credits left."""
    try:
        response = session.get(ODDS.rsplit("/", 2)[0], params={"apiKey": key}, headers=UA, timeout=10)
    except requests.RequestException as exc:
        return f"key check failed: {type(exc).__name__}"
    nba = response.status_code == 200 and any(s.get("key") == "basketball_nba" for s in response.json())
    return (f"key check: HTTP {response.status_code}, NBA listed {nba}, credits left "
            f"{response.headers.get('x-requests-remaining')}, used {response.headers.get('x-requests-used')}")


def book_top(data: dict) -> dict:
    """Uncached orderbook -> home bid/ask and the contracts available at each side's best ask.
    Both sides list bids only: yes ask = 1 - best no bid (its size), away ask = 1 - best yes bid (its size)."""
    book = data.get("orderbook_fp") or {}
    yes = [(float(p), float(s)) for p, s in book.get("yes_dollars") or []]
    no = [(float(p), float(s)) for p, s in book.get("no_dollars") or []]
    best_yes, best_no = max(yes, default=None), max(no, default=None)
    return {"bid": round(best_yes[0], 4) if best_yes else None, "ask": round(1 - best_no[0], 4) if best_no else None,
            "home_ask_size": best_no[1] if best_no else 0.0, "away_ask_size": best_yes[1] if best_yes else 0.0}


def night_so_far(out: Path) -> tuple[int, float, int | None, dict[str, float]]:
    """Earlier runs' snapshots tonight: (calls, last call time, credits left, halftime start per game).
    The cap is per night, not per run (2026-10-26: two runs made 23 calls)."""
    prior = [r for r in read_lines(out) if r.get("src") == "snap"] if out.exists() else []
    prior.sort(key=lambda r: r["t"])
    since = {g["game"]: g["ht_since"] for r in prior for g in r.get("games", [])}
    return len(prior), (prior[-1]["t"] if prior else 0.0), (prior[-1]["remaining"] if prior else None), since


def record(hours: float, out: Path, day: str | None, key: str) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    espn_params = {"dates": day} if day else {}
    date_code = (datetime.strptime(day, "%Y%m%d") if day else datetime.now(ET)).strftime("%y%b%d").upper()
    since: dict[str, float] = {}
    calls, last_call, remaining, prior_since = night_so_far(out)
    seen_live = False
    stop_at = time.time() + hours * 3600
    with gzip.open(out, "at", encoding="utf-8") as fh:
        while time.time() < stop_at:
            started = time.time()
            data = fetch(session, ESPN, espn_params)
            if data is not None:
                rows = espn_snapshot(data)
                seen_live = seen_live or any(g["state"] == "in" for g in rows)
                half = halftime_games(data)
                new = [g for g in half if g["id"] not in since]
                for g in new:
                    since[g["id"]] = prior_since.get(g["game"], started)
                new = [g for g in new if g["game"] not in prior_since]
                if should_call(started, last_call, bool(new), bool(half), calls, remaining):
                    events, remaining, used, error = fetch_odds(session, key)
                    calls, last_call = calls + 1, time.time()
                    games = []
                    for g in half:
                        ob = fetch(session, f"{KALSHI}/{home_ticker(g, date_code)}/orderbook", {})
                        games.append({"game": g["game"], "away": g["away"], "home": g["home"], "away_name": g["away_name"],
                                      "home_name": g["home_name"], "ht_since": round(since[g["id"]], 3),
                                      "t": round(time.time(), 3), **(book_top(ob) if ob else {})})
                    fh.write(json.dumps({"t": round(last_call, 3), "src": "snap", "remaining": remaining, "used": used,
                                         "error": error, "events": compact_events(events or []), "games": games}) + "\n")
                    fh.flush()
                    print(f"call {calls}: {len(half)} at halftime, credits left {remaining}, error {error}")
                if seen_live and rows and all(g["state"] == "post" for g in rows):
                    break
            time.sleep(max(0.0, POLL_S - (time.time() - started)))
    return calls


# ---------- grading (pre-registered; run once at the stop) ----------

def load_snaps(paths: list[Path]) -> list[dict]:
    snaps = []
    for path in paths:
        night = path.name.split("-")[-1].split(".")[0]
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    if row.get("src") == "snap":
                        snaps.append({**row, "night": night})
    return sorted(snaps, key=lambda s: s["t"])


def implied(american: float) -> float:
    return -american / (-american + 100) if american < 0 else 100 / (american + 100)


def nickname(name: str | None) -> str:
    return (name or "").split()[-1].lower() if name else ""


def match_event(game: dict, events: list[dict]) -> dict | None:
    return next((e for e in events if nickname(e["home"]) == nickname(game.get("home_name"))
                 and nickname(e["away"]) == nickname(game.get("away_name"))), None)


def iso_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def reference(event: dict | None, at: float, max_age: float) -> tuple[float | None, str]:
    """Median no-vig home probability of the three books, or (None, why it is not valid)."""
    if event is None:
        return None, "no event"
    probs = []
    for book in BOOKS:
        b = event["books"].get(book)
        if not b or event["home"] not in b["prices"] or event["away"] not in b["prices"] or not b.get("last_update"):
            return None, f"{book} missing"
        if at - iso_ts(b["last_update"]) > max_age:
            return None, f"{book} stale"
        qh, qa = implied(b["prices"][event["home"]]), implied(b["prices"][event["away"]])
        probs.append(qh / (qh + qa))
    if max(probs) - min(probs) > MAX_DISPERSION:
        return None, "books disagree"
    return statistics.median(probs), "valid"


def side_quote(game: dict, side: str) -> tuple[float, float, float] | None:
    """(bid, ask, size at ask) for 'home' or 'away' from the home-side orderbook."""
    bid, ask = game.get("bid"), game.get("ask")
    if bid is None or ask is None:
        return None
    if side == "home":
        return bid, ask, game.get("home_ask_size", 0.0)
    return round(1 - ask, 4), round(1 - bid, 4), game.get("away_ask_size", 0.0)


def entry_signal(game: dict, ref_home: float, fee) -> dict | None:
    """The pre-registered entry: reference(side) - ask >= 2 fees + spread + 3c, spread <= 2c, >= 10 at the ask."""
    if game.get("bid") is None or game.get("ask") is None or game["t"] - game["ht_since"] > ENTRY_WINDOW_S:
        return None
    spread = game["ask"] - game["bid"]
    if spread > MAX_SPREAD + 1e-9:
        return None
    for side, ref in (("home", ref_home), ("away", 1 - ref_home)):
        bid, ask, size = side_quote(game, side)
        if size >= MIN_SIZE and ref - ask >= 2 * fee(ask, N_CONTRACTS) + spread + EDGE_MARGIN - 1e-9:
            return {"side": side, "ask": ask, "ref": ref, "t": game["t"]}
    return None


def game_key(game: dict) -> str:
    from latency import kalshi_code
    return kalshi_code(game["away"]) + kalshi_code(game["home"])


def clock_s(clock: str | None) -> float | None:
    if not clock:
        return None
    try:
        if ":" in clock:
            m, s = clock.split(":")
            return int(m) * 60 + float(s)
        return float(clock)
    except ValueError:
        return None


def recorder_index(lines: list[dict]) -> tuple[dict, dict, set]:
    """Recorder file -> per game: (time of 3:00 into Q3), home book rows [(t, bid, ask)], games that reached final."""
    from latency import kalshi_code
    q3_stop, books, final = {}, {}, set()
    for x in lines:
        if x["src"] == "espn":
            for g in x["rows"]:
                key = kalshi_code(g["away"]) + kalshi_code(g["home"])
                if g["state"] == "post":
                    final.add(key)
                c = clock_s(g.get("clock"))
                if key not in q3_stop and g.get("period") == 3 and c is not None and c <= Q3_STOP_CLOCK_S:
                    q3_stop[key] = x["t"]
        elif x["src"] == "book":
            for r in x["rows"]:
                if r.get("bid") and r.get("ask"):
                    books.setdefault(r["ticker"].split("-")[1][7:], []).append((r.get("t", x["t"]), float(r["bid"]), float(r["ask"])))
    return q3_stop, books, final


def time_stop_quote(key: str, q3_stop: dict, books: dict) -> tuple[float, float] | None:
    """Home (bid, ask) in the first recorder book row at or after 3:00 into Q3, within 30 s."""
    t0 = q3_stop.get(key)
    if t0 is None:
        return None
    row = next((r for r in sorted(books.get(key, [])) if t0 <= r[0] <= t0 + EXIT_MATCH_S), None)
    return (row[1], row[2]) if row else None


def by_game(snaps: list[dict]) -> dict[tuple[str, str], list[tuple[dict, dict]]]:
    """(night, game key) -> [(snap, that game's row)] in time order."""
    out: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for s in snaps:
        for g in s["games"]:
            out.setdefault((s["night"], game_key(g)), []).append((s, g))
    return out


def valid_halftimes(snaps: list[dict], max_age: float = MAX_AGE_S) -> int:
    """The pre-registered sample count: games with at least one valid snapshot (no prices looked at)."""
    return sum(1 for rows in by_game(snaps).values()
               if any(reference(match_event(g, s["events"]), s["t"], max_age)[0] is not None for s, g in rows))


def simulate(snaps: list[dict], recordings: dict[str, list[dict]], max_age: float, fee) -> dict:
    """H1 paper trades, H2 rows and descriptive counts for one staleness setting."""
    index = {night: recorder_index(lines) for night, lines in recordings.items()}
    trades, h2, gaps, reasons, ungradable, excluded = [], [], [], {}, 0, 0
    for (night, key), rows in by_game(snaps).items():
        q3_stop, books, final = index.get(night, ({}, {}, set()))
        if key not in final:
            excluded += 1
            continue
        valid = []
        for s, g in rows:
            ref, why = reference(match_event(g, s["events"]), s["t"], max_age)
            reasons[why] = reasons.get(why, 0) + 1
            if ref is not None and g.get("bid") is not None and g.get("ask") is not None:
                valid.append((s, g, ref))
        if not valid:
            continue
        stop = time_stop_quote(key, q3_stop, books)
        first_s, first_g, first_ref = valid[0]
        mid0 = (first_g["bid"] + first_g["ask"]) / 2
        gaps.append(abs(first_ref - mid0))
        if stop:
            h2.append((first_ref - mid0, (stop[0] + stop[1]) / 2 - mid0, night))
        for i, (s, g, ref) in enumerate(valid):
            signal = entry_signal(g, ref, fee)
            if not signal:
                continue
            exit_bid, why = None, "time stop"
            for _, g2, ref2 in valid[i + 1:]:
                bid2, ask2, _ = side_quote(g2, signal["side"])
                ref_side = ref2 if signal["side"] == "home" else 1 - ref2
                if abs(ref_side - (bid2 + ask2) / 2) <= CONVERGED + 1e-9:
                    exit_bid, why = bid2, "converged"
                    break
                if abs(ref_side - signal["ref"]) > REF_MOVED + 1e-9:
                    exit_bid, why = bid2, "reference moved"
                    break
            if exit_bid is None and stop:
                exit_bid = stop[0] if signal["side"] == "home" else round(1 - stop[1], 4)
            if exit_bid is None:
                ungradable += 1
            else:
                pnl = exit_bid - signal["ask"] - fee(signal["ask"], N_CONTRACTS) - fee(exit_bid, N_CONTRACTS)
                trades.append({"night": night, "game": key, **signal, "exit": exit_bid, "why": why, "pnl": pnl})
            break  # one entry per game
    return {"trades": trades, "h2": h2, "gaps": gaps, "reasons": reasons, "ungradable": ungradable, "excluded": excluded}


def cluster_bootstrap(rows: list, stat, cluster, n_boot: int = 10_000, seed: int = 13) -> list[float]:
    """Sorted bootstrap distribution of stat(rows), resampling whole clusters (nights)."""
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(cluster(r), []).append(r)
    keys, rng, out = list(groups), random.Random(seed), []
    for _ in range(n_boot):
        sample = [r for _ in keys for r in groups[keys[rng.randrange(len(keys))]]]
        value = stat(sample)
        if value is not None:
            out.append(value)
    return sorted(out)


def slope(rows: list[tuple[float, float, str]]) -> float | None:
    xs, ys = [r[0] for r in rows], [r[1] for r in rows]
    if len(rows) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else None


def verdict_h1(trades: list[dict]) -> dict:
    n = len(trades)
    if n < MIN_ENTRIES:
        return {"n": n, "verdict": "KILL", "why": f"{n} entries < {MIN_ENTRIES} (too rare)"}
    pnls = [t["pnl"] for t in trades]
    mean = statistics.fmean(pnls)
    boot = cluster_bootstrap(trades, lambda s: statistics.fmean(t["pnl"] for t in s) if s else None, lambda t: t["night"])
    lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1]
    p = sum(1 for b in boot if b <= 0) / len(boot)
    nights = sorted({t["night"] for t in trades})
    half = nights[len(nights) // 2] if nights else ""
    halves = [statistics.fmean([t["pnl"] for t in trades if (t["night"] < half) == first] or [0.0]) for first in (True, False)]
    if mean <= 0:
        verdict = "KILL"
    elif p < 0.05 and lo > 0 and min(halves) > 0:
        verdict = "SURVIVES"
    else:
        verdict = "FRAGILE"
    return {"n": n, "mean": mean, "lo": lo, "hi": hi, "p": p, "halves": halves, "verdict": verdict}


def verdict_h2(rows: list[tuple[float, float, str]]) -> dict:
    b = slope(rows)
    if b is None:
        return {"n": len(rows), "verdict": "n/a"}
    boot = cluster_bootstrap(rows, slope, lambda r: r[2])
    lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1]
    return {"n": len(rows), "slope": b, "lo": lo, "hi": hi, "verdict": "SURVIVES" if lo > 0 else "KILL"}


def grade(folder: Path) -> str:
    from calibration import fee_per_contract
    from latency import load
    snaps = load_snaps(sorted(folder.glob("books-*.jsonl.gz")))
    nights = {s["night"] for s in snaps}
    recordings = {n: load(folder / f"nba-{n}.jsonl.gz") for n in nights if (folder / f"nba-{n}.jsonl.gz").exists()}
    main = simulate(snaps, recordings, MAX_AGE_S, fee_per_contract)
    sens = simulate(snaps, recordings, SENSITIVITY_AGE_S, fee_per_contract)
    h1, h1s, h2 = verdict_h1(main["trades"]), verdict_h1(sens["trades"]), verdict_h2(main["h2"])
    gaps = sorted(main["gaps"])
    lines = [f"# NB-032 grading ({len(nights)} nights, {valid_halftimes(snaps)} valid halftimes; PREREG-NB-032)", "",
             f"- H1: {h1}", f"- H1s (300 s): {h1s}", f"- H2: {h2}",
             f"- |reference - Kalshi mid| at first valid snapshot: n={len(gaps)}, median "
             f"{statistics.median(gaps) * 100 if gaps else float('nan'):.1f}c, max {max(gaps, default=0) * 100:.1f}c",
             f"- snapshot validity: {main['reasons']}; ungradable trades {main['ungradable']}; games not final {main['excluded']}"]
    return "\n".join(lines) + "\n"


def count(paths: list[Path]) -> str:
    """Nightly progress only: valid halftimes and credits; no gaps, entries or P&L (pre-registered)."""
    snaps = load_snaps(paths)
    remaining = next((s["remaining"] for s in reversed(snaps) if s.get("remaining") is not None), None)
    errors = sum(1 for s in snaps if s.get("error"))
    return (f"NB-032: {valid_halftimes(snaps)}/{TARGET_HALFTIMES} valid halftimes; {len(snaps)} odds calls "
            f"({errors} failed); credits left {remaining}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--hours", type=float, default=5.5)
    rec.add_argument("--date")
    rec.add_argument("--out", type=Path, required=True)
    sub.add_parser("check")
    cnt = sub.add_parser("count")
    cnt.add_argument("paths", nargs="+", type=Path)
    grd = sub.add_parser("grade")
    grd.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    if args.cmd in ("record", "check"):
        key = os.environ.get("ODDS_API_KEY", "").strip()
        if not key:
            print("no ODDS_API_KEY: halftime book snapshots skipped")
            return 0
        if args.cmd == "check":
            print(check_key(requests.Session(), key))
        else:
            print(f"{record(args.hours, args.out, args.date, key)} odds calls -> {args.out}")
    elif args.cmd == "count":
        print(count(args.paths))
    else:
        print(grade(args.folder))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
