"""Kill test: were pre-game Kalshi sports favorites (70-95c) underpriced by more than Robinhood fees?

Uses only Kalshi's free public market-data API (no key, no sign-up). Generic User-Agent, no personal info.

Commands (run from the repo root):
  python projects/sports/tools/calibration.py fetch [--minutes 60] [--max-mlb 3000] [--sens 1500]
  python projects/sports/tools/calibration.py analyze
  python projects/sports/tools/calibration.py startcheck [--limit 600]   (MLB cross-check using first-pitch time)

Pre-game price rule: the last trade print at or before (market close_time - 5h). For finished game
markets close_time is when the winner was declared (~game end), and games last 2.3-3.5h, so
close - 5h is usually before the start. Sensitivity: close - 3.5h on a random subset (`--sens`), and an
"ask-side" price (last print where the taker bought YES, i.e. paid the ask).
Start-time guard (data quality, not a rule change): when Kalshi closes a market late, close - 5h lands
in the game or after the final, so a row is used only if its last print is before the scheduled start
(MLB ticker time, NBA ESPN tip). NHL/NFL tickers carry no time: there a last price within 2c of 0 or 1
counts as already decided and is dropped.
"""
from __future__ import annotations

import csv
import json
import math
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; research-script)"}
ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw" / "kalshi"
SERIES = {"MLB": "KXMLBGAME", "NFL": "KXNFLGAME", "NBA": "KXNBAGAME", "NHL": "KXNHLGAME"}
PRIMARY_HOURS = 5.0      # cutoff = close_time - 5h
SENS_HOURS = 3.5         # sensitivity cutoff
SLEEP = 0.12             # seconds between calls (limit is far higher)
WINDOW_HOURS = 3.0       # "recent" window used for the liquidity filter
MIN_PRINTS = 10          # need at least this many prints in the window
MAX_STALE_HOURS = 2.0    # last print must be within this many hours of the cutoff
EXCHANGE_FEE = 0.01      # per contract, official fee PDF effective 2026-06-01
DECIDED = 0.02           # with no start time, a last price this close to 0 or 1 is taken as post-final
BUCKETS = [(0.0, 0.30, "<0.30"), (0.30, 0.50, "0.30-0.50"), (0.50, 0.60, "0.50-0.60"),
           (0.60, 0.70, "0.60-0.70"), (0.70, 0.80, "0.70-0.80"), (0.80, 0.90, "0.80-0.90"),
           (0.90, 0.95, "0.90-0.95"), (0.95, 1.01, ">=0.95")]


# ---------- pure helpers (tested in tests/test_sports_calibration.py) ----------

def ceil_cent(x: float) -> float:
    """Round a dollar amount up to the next cent (guarding float noise)."""
    return math.ceil(round(x * 100, 6)) / 100


def fee_per_contract(price: float, n_contracts: int = 1, gold: bool = False) -> float:
    """Robinhood fee per contract: rate*p*(1-p) rounded up to the cent per ORDER, plus $0.01 exchange fee."""
    rate = 0.05 if gold else 0.10
    order_fee = ceil_cent(rate * price * (1 - price) * n_contracts)
    return order_fee / n_contracts + EXCHANGE_FEE


def bucket_of(price: float) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= price < hi:
            return name
    raise ValueError(f"price out of range: {price}")


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def profit_after_fees(price: float, won: bool, n_contracts: int = 10, gold: bool = False) -> float:
    """Profit per contract buying YES at `price` and holding to settlement."""
    return (1.0 if won else 0.0) - price - fee_per_contract(price, n_contracts, gold)


def cluster_bootstrap_roi(rows: list[tuple[float, bool, str]], n_contracts: int = 10, n_boot: int = 10_000,
                          seed: int = 13) -> tuple[float, float]:
    """95% CI of ROI after fees, resampling whole clusters (e.g. games) so entries from one game move together.
    rows = [(price, won, cluster key)]."""
    clusters: dict[str, list[tuple[float, float]]] = {}
    for price, won, key in rows:
        cost = price + fee_per_contract(price, n_contracts)
        clusters.setdefault(key, []).append((profit_after_fees(price, won, n_contracts), cost))
    sums = [(sum(p for p, _ in v), sum(c for _, c in v)) for v in clusters.values()]
    if not sums:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    k = len(sums)
    rois = []
    for _ in range(n_boot):
        profit = cost = 0.0
        for _ in range(k):
            p, c = sums[rng.randrange(k)]
            profit += p
            cost += c
        rois.append(profit / cost)
    rois.sort()
    return (rois[int(0.025 * n_boot)], rois[int(0.975 * n_boot) - 1])


def summarize(rows: list[tuple[float, bool]], n_contracts: int = 10, gold: bool = False) -> dict:
    """rows = [(price, won)]. Returns n, mean price, win rate, Wilson CI, ROI and its 95% CI."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for _, w in rows if w)
    prices = [p for p, _ in rows]
    profits = [profit_after_fees(p, w, n_contracts, gold) for p, w in rows]
    costs = [p + fee_per_contract(p, n_contracts, gold) for p in prices]
    mean_profit = sum(profits) / n
    sd = math.sqrt(sum((x - mean_profit) ** 2 for x in profits) / (n - 1)) if n > 1 else float("nan")
    half = 1.96 * sd / math.sqrt(n) if n > 1 else float("nan")
    mean_cost = sum(costs) / n
    lo, hi = wilson(wins, n)
    return {"n": n, "mean_price": sum(prices) / n, "win_rate": wins / n, "wilson_lo": lo, "wilson_hi": hi,
            "diff": wins / n - sum(prices) / n, "mean_fee": mean_cost - sum(prices) / n,
            "roi": mean_profit / mean_cost, "roi_lo": (mean_profit - half) / mean_cost,
            "roi_hi": (mean_profit + half) / mean_cost}


def pick_prices(trades: list[dict], cutoff: datetime) -> dict | None:
    """From prints at or before `cutoff` (newest first) return last price, ask-side price, print count, last print time.

    Returns None when the market is too thin: fewer than MIN_PRINTS in the window, or last print stale.
    """
    window_start = cutoff - timedelta(hours=WINDOW_HOURS)
    stale_limit = cutoff - timedelta(hours=MAX_STALE_HOURS)
    prints = [(parse_ts(t["created_time"]), float(t["yes_price_dollars"]), t.get("taker_side")) for t in trades]
    prints = [p for p in prints if p[0] <= cutoff]
    prints.sort(key=lambda p: p[0], reverse=True)
    if not prints or prints[0][0] < stale_limit:
        return None
    in_window = [p for p in prints if p[0] >= window_start]
    if len(in_window) < MIN_PRINTS:
        return None
    ask = next((p[1] for p in in_window if p[2] == "yes"), None)
    return {"last": prints[0][1], "ask": ask, "n_window": len(in_window), "t": prints[0][0]}


def is_pregame(picked: dict, start: datetime | None) -> bool:
    """True if the picked price can be a pre-game price. With a scheduled start, the last print must come before it
    (real starts come ~10 min later, so this is conservative); without one, only an already-decided price is rejected."""
    if start is not None:
        return picked["t"] < start
    return DECIDED < picked["last"] < 1 - DECIDED


def mlb_start_from_ticker(event_ticker: str) -> datetime | None:
    """MLB event tickers look like KXMLBGAME-26AUG172040LADCOL: YY MON DD HHMM (US Eastern first pitch)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})[A-Z]", event_ticker)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    try:
        naive = datetime.strptime(f"{yy}{mon}{dd}{hh}{mm}", "%y%b%d%H%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- fetching ----------

def get(path: str, params: dict) -> dict:
    for attempt in range(6):
        r = requests.get(BASE + path, params=params, headers=HEADERS, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        time.sleep(SLEEP)
        return r.json()
    raise RuntimeError(f"gave up on {path} {params}")


def list_markets(league: str, source: str) -> list[dict]:
    """source 'live' = /markets?status=settled (after 2026-08-19), 'hist' = /historical/markets."""
    return list_series(SERIES[league], source)


def list_series(series: str, source: str) -> list[dict]:
    """Every settled market of one Kalshi series (cached); source as in list_markets."""
    cache = RAW / f"markets_{source}_{series}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    path, extra = ("/markets", {"status": "settled"}) if source == "live" else ("/historical/markets", {})
    markets, cursor = [], None
    while True:
        params = {"series_ticker": series, "limit": 1000, **extra}
        if cursor:
            params["cursor"] = cursor
        data = get(path, params)
        markets += data["markets"]
        cursor = data.get("cursor")
        if not cursor or not data["markets"]:
            break
    keep = ["ticker", "event_ticker", "open_time", "close_time", "result", "status", "settlement_value_dollars"]
    markets = [{k: m.get(k) for k in keep} for m in markets]
    cache.write_text(json.dumps(markets), encoding="utf-8")
    return markets


def all_games(max_mlb: int) -> list[dict]:
    """Every settled yes/no game market with league and trades endpoint; MLB randomly capped (seeded)."""
    out = {}
    for league in SERIES:
        for source in ("hist", "live"):
            for m in list_markets(league, source):
                if m["result"] in ("yes", "no") and m["close_time"]:
                    out[m["ticker"]] = {**m, "league": league, "source": source}
    games = list(out.values())
    rng = random.Random(42)
    mlb = [g for g in games if g["league"] == "MLB"]
    rng.shuffle(mlb)
    games = [g for g in games if g["league"] != "MLB"] + mlb[:max_mlb]
    rng.shuffle(games)  # any partial run is a random sample
    return games


def trades_cache(game: dict, tag: str) -> Path:
    return RAW / "trades" / f"{game['ticker']}__{tag}.json"


def fetch_trades(game: dict, hours: float, tag: str) -> bool:
    """Fetch newest-first prints at/before close-hours. Returns True if a request was made."""
    path = trades_cache(game, tag)
    if path.exists():
        return False
    cutoff = parse_ts(game["close_time"]) - timedelta(hours=hours)
    ep = "/historical/trades" if game["source"] == "hist" else "/markets/trades"
    data = get(ep, {"ticker": game["ticker"], "limit": 100, "max_ts": int(cutoff.timestamp())})
    keep = ["created_time", "yes_price_dollars", "count_fp", "taker_side"]
    path.write_text(json.dumps([{k: t.get(k) for k in keep} for t in data["trades"]]), encoding="utf-8")
    return True


def cmd_fetch(minutes: float, max_mlb: int, sens: int) -> None:
    (RAW / "trades").mkdir(parents=True, exist_ok=True)
    games = all_games(max_mlb)
    print(f"{len(games)} settled game markets in scope")
    deadline = time.time() + minutes * 60
    made = 0
    for i, g in enumerate(games):
        if time.time() > deadline:
            print("time box reached")
            break
        made += fetch_trades(g, PRIMARY_HOURS, "c5")
        if i < sens:
            made += fetch_trades(g, SENS_HOURS, "c35")
        if i % 500 == 0:
            print(i, "done,", made, "requests", flush=True)
    print("fetch finished;", made, "requests")


def cmd_startcheck(limit: int) -> None:
    """Cross-check on MLB markets whose ticker carries the first-pitch time: price at start-30min vs close-5h."""
    (RAW / "trades").mkdir(parents=True, exist_ok=True)
    games = [g for g in all_games(10**9) if g["league"] == "MLB" and mlb_start_from_ticker(g["event_ticker"])]
    random.Random(7).shuffle(games)
    games = [g for g in games[:limit]]
    pairs, before_start = [], 0
    for g in games:
        start = mlb_start_from_ticker(g["event_ticker"])
        cutoff = start - timedelta(minutes=30)
        path = trades_cache(g, "st")
        if not path.exists():
            ep = "/historical/trades" if g["source"] == "hist" else "/markets/trades"
            data = get(ep, {"ticker": g["ticker"], "limit": 100, "max_ts": int(cutoff.timestamp())})
            keep = ["created_time", "yes_price_dollars", "count_fp", "taker_side"]
            path.write_text(json.dumps([{k: t.get(k) for k in keep} for t in data["trades"]]), encoding="utf-8")
        st = pick_prices(json.loads(path.read_text(encoding="utf-8")), cutoff)
        c5_path = trades_cache(g, "c5")
        c5 = pick_prices(json.loads(c5_path.read_text(encoding="utf-8")), parse_ts(g["close_time"]) - timedelta(hours=PRIMARY_HOURS)) if c5_path.exists() else None
        before_start += parse_ts(g["close_time"]) - timedelta(hours=PRIMARY_HOURS) < start
        if st:
            pairs.append((st["last"], c5["last"] if c5 else None, g["result"] == "yes"))
    both = [(a, b) for a, b, _ in pairs if b is not None]
    mad = sum(abs(a - b) for a, b in both) / len(both)
    print(f"MLB with ticker start time: {len(games)} sampled; close-5h is before first pitch in {before_start}")
    print(f"usable at start-30min: {len(pairs)}; both prices: {len(both)}; mean |start-30m price - close-5h price| = {mad:.3f}")
    print_table("MLB, last print <= first pitch - 30 min (ticker start time)",
                table([{"league": "MLB", "price": a, "won": w} for a, _, w in pairs]))


# ---------- analysis ----------

def scheduled_starts(games: list[dict]) -> dict[str, datetime]:
    """Market ticker -> scheduled start (UTC): MLB from the ticker, NBA from the cached ESPN scoreboards.
    NHL and NFL tickers carry no time, so those markets are absent."""
    import nba_edges  # imported here because nba_edges imports this module
    espn, _ = nba_edges.load_espn()
    starts = {}
    for g in games:
        start = None
        if g["league"] == "MLB":
            start = mlb_start_from_ticker(g["event_ticker"])
        elif g["league"] == "NBA" and (parsed := nba_edges.parse_ticker(g["ticker"])):
            start = espn.get(parsed[:3], {}).get("tip")
        if start:
            starts[g["ticker"]] = start
    return starts


def load_rows(hours_tag: str, price_key: str, starts: dict[str, datetime] | None = None) -> list[dict]:
    """One row per cached market; price None if thin/stale, and also (after_start) if the print is not pre-game."""
    games = {g["ticker"]: g for g in all_games(10**9)}
    if starts is None:
        starts = scheduled_starts(list(games.values()))
    rows = []
    for ticker, g in games.items():
        path = trades_cache(g, hours_tag)
        if not path.exists():
            continue
        hours = PRIMARY_HOURS if hours_tag == "c5" else SENS_HOURS
        cutoff = parse_ts(g["close_time"]) - timedelta(hours=hours)
        picked = pick_prices(json.loads(path.read_text(encoding="utf-8")), cutoff)
        if picked is None or picked[price_key] is None:
            rows.append({**g, "fetched": True, "price": None})
            continue
        if not is_pregame(picked, starts.get(ticker)):
            rows.append({**g, "fetched": True, "price": None, "after_start": True})
            continue
        rows.append({**g, "fetched": True, "price": picked[price_key], "n_window": picked["n_window"],
                     "won": g["result"] == "yes"})
    return rows


def table(rows: list[dict], n_contracts: int = 10, gold: bool = False) -> list[dict]:
    out = []
    for league in [*SERIES, "ALL"]:
        sel = [r for r in rows if r["price"] is not None and (league == "ALL" or r["league"] == league)]
        for _, _, name in BUCKETS:
            part = [(r["price"], r["won"]) for r in sel if bucket_of(r["price"]) == name]
            if part:
                out.append({"league": league, "bucket": name, **summarize(part, n_contracts, gold)})
    return out


def print_table(title: str, tab: list[dict]) -> None:
    print(f"\n## {title}")
    print("league | bucket | n | mean_px | win | diff | wilson95 | fee | ROI | ROI95")
    for r in tab:
        print(f"{r['league']} | {r['bucket']} | {r['n']} | {r['mean_price']:.3f} | {r['win_rate']:.3f} | "
              f"{r['diff']:+.3f} | [{r['wilson_lo']:.3f},{r['wilson_hi']:.3f}] | {r['mean_fee']:.3f} | "
              f"{r['roi']:+.1%} | [{r['roi_lo']:+.1%},{r['roi_hi']:+.1%}]")


def cmd_analyze() -> None:
    starts = scheduled_starts(all_games(10**9))
    rows = load_rows("c5", "last", starts)
    used = [r for r in rows if r["price"] is not None]
    late = [r for r in rows if r.get("after_start")]
    print(f"fetched {len(rows)}; usable (liquid, fresh, pre-game) {len(used)}; thin/stale {len(rows) - len(used) - len(late)}; "
          f"printed at/after the scheduled start or already decided {len(late)}")
    for league in SERIES:
        part = [r for r in used + late if r["league"] == league]
        print(f"  {league}: start time known for {sum(r['ticker'] in starts for r in part)} of {len(part)}; "
              f"dropped by the start-time guard {sum(1 for r in late if r['league'] == league)}")
    for league in [*SERIES, "ALL"]:
        n = sum(1 for r in used if league == "ALL" or r["league"] == league)
        print(league, "usable", n)
    months = sorted({r["close_time"][:7] for r in used})
    print("close months", months[0], "to", months[-1])
    print_table("PRIMARY: last print <= close-5h, 10-contract order, no Gold", table(used))
    fav = [r for r in used if 0.70 <= r["price"] < 0.95]
    for label, nc, gold in [("1-contract order, no Gold", 1, False), ("10-contract, Gold", 10, True)]:
        s = summarize([(r["price"], r["won"]) for r in fav], nc, gold)
        print(f"\nFAVORITES 0.70-0.95 pooled, {label}: n={s['n']} win={s['win_rate']:.3f} px={s['mean_price']:.3f} "
              f"ROI={s['roi']:+.1%} [{s['roi_lo']:+.1%},{s['roi_hi']:+.1%}]")
    # time split: first half vs second half of favorites by close time
    fav.sort(key=lambda r: r["close_time"])
    half = len(fav) // 2
    for name, part in [("earlier half", fav[:half]), ("later half", fav[half:])]:
        s = summarize([(r["price"], r["won"]) for r in part])
        print(f"time split {name} ({part[0]['close_time'][:10]}..{part[-1]['close_time'][:10]}): n={s['n']} "
              f"win={s['win_rate']:.3f} px={s['mean_price']:.3f} ROI={s['roi']:+.1%} [{s['roi_lo']:+.1%},{s['roi_hi']:+.1%}]")
    ask = [r for r in load_rows("c5", "ask", starts) if r["price"] is not None]
    print_table("SENSITIVITY: ask-side price (last print where taker bought YES), c5", [t for t in table(ask) if t["league"] == "ALL"])
    s35 = [r for r in load_rows("c35", "last", starts) if r["price"] is not None]
    print(f"\nSENSITIVITY c35 subset usable n={len(s35)}")
    print_table("SENSITIVITY: last print <= close-3.5h (random subset)", [t for t in table(s35) if t["league"] == "ALL"])
    # same-subset comparison for c5
    tick35 = {r["ticker"] for r in s35}
    print_table("c5 prices on the same subset", [t for t in table([r for r in used if r["ticker"] in tick35]) if t["league"] == "ALL"])
    # robustness: de-noise the price with BOTH sides of the game, p = (p_this + 1 - p_other)/2
    by_event: dict[str, list[dict]] = {}
    for r in used:
        by_event.setdefault(r["event_ticker"], []).append(r)
    paired = []
    for rs in by_event.values():
        if len(rs) == 2:
            for a, b in (rs, rs[::-1]):
                paired.append({"league": a["league"], "price": (a["price"] + 1 - b["price"]) / 2, "won": a["won"]})
    print(f"\nPAIRED: games with both sides usable: {len(paired) // 2}")
    print_table("ROBUSTNESS: two-sided price (p + 1 - p_other)/2", [t for t in table(paired) if t["league"] == "ALL"])
    fav2 = [(r["price"], r["won"]) for r in paired if 0.70 <= r["price"] < 0.95]
    s2 = summarize(fav2)
    print(f"PAIRED favorites 0.70-0.95: n={s2['n']} win={s2['win_rate']:.3f} px={s2['mean_price']:.3f} "
          f"ROI={s2['roi']:+.1%} [{s2['roi_lo']:+.1%},{s2['roi_hi']:+.1%}]")
    # exact binomial check of the 0.90-0.95 bucket vs its break-even (price + fee)
    top = [r for r in used if 0.90 <= r["price"] < 0.95]
    be = sum(r["price"] + fee_per_contract(r["price"], 10) for r in top) / len(top)
    wins = sum(r["won"] for r in top)
    tail = sum(math.comb(len(top), k) * be**k * (1 - be) ** (len(top) - k) for k in range(wins, len(top) + 1))
    print(f"0.90-0.95 exact binomial: n={len(top)} wins={wins} break-even={be:.3f}; P(>= {wins} wins | break-even) = {tail:.4f}")
    for lg in SERIES:
        part = [r for r in top if r["league"] == lg]
        print(f"  {lg}: n={len(part)} wins={sum(r['won'] for r in part)}")
    top.sort(key=lambda r: r["close_time"])
    for name, part in [("earlier half", top[: len(top) // 2]), ("later half", top[len(top) // 2:])]:
        print(f"  0.90-0.95 {name}: n={len(part)} wins={sum(r['won'] for r in part)} mean_px={sum(r['price'] for r in part) / len(part):.3f}")
    out = RAW / "results_rows.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "league", "close_time", "price", "won"])
        for r in used:
            w.writerow([r["ticker"], r["league"], r["close_time"], r["price"], int(r["won"])])
    print("\nrow-level data:", out)


if __name__ == "__main__":
    args = sys.argv[1:]
    opt = lambda name, default: type(default)(args[args.index(name) + 1]) if name in args else default  # noqa: E731
    if args and args[0] == "fetch":
        cmd_fetch(opt("--minutes", 60.0), opt("--max-mlb", 3000), opt("--sens", 1500))
    elif args and args[0] == "startcheck":
        cmd_startcheck(opt("--limit", 600))
    elif args and args[0] == "analyze":
        cmd_analyze()
    else:
        print(__doc__)
