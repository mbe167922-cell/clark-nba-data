"""NB-005 with exact Kalshi timing: ESPN score changes (from a recorder night) vs Kalshi trade prints.

Kalshi's market list is CDN-cached for 15 s, so the recorder's Kalshi rows are too coarse for timing.
Trade prints carry Kalshi's own timestamp, so this fetches every trade of each game's home-side
market (public API, generic User-Agent, cached under data/raw/kalshi/live_trades/) and compares.

    python projects/sports/tools/latency_trades.py data/live/nba-20261021.jsonl.gz 26OCT21
"""
from __future__ import annotations

import bisect
import json
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibration as cal  # noqa: E402
import latency  # noqa: E402

CACHE = cal.ROOT / "data" / "raw" / "kalshi" / "live_trades"


def home_ticker(game: str, date_code: str) -> str:
    """'INDNOP' + '26OCT21' -> 'KXNBAGAME-26OCT21INDNOP-NOP' (Kalshi lists away team first)."""
    return f"KXNBAGAME-{date_code}{game}-{game[-3:]}"


def fetch_all_trades(ticker: str) -> list[dict]:
    path = CACHE / f"{ticker}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
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
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trades), encoding="utf-8")
    return trades


def to_series(trades: list[dict]) -> list[tuple[float, float]]:
    rows = []
    for t in trades:
        price = latency.to_float(t.get("yes_price_dollars"))
        if price is None:
            continue
        ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
        rows.append((ts, price))
    return sorted(rows)


OFFSETS = list(range(-60, 61, 5))


def price_at(series: list[tuple[float, float]], times: list[float], t: float) -> float | None:
    i = bisect.bisect_right(times, t) - 1
    return series[i][1] if i >= 0 else None


def event_curve(scores: dict[str, list[tuple[float, str]]],
                trades: dict[str, list[tuple[float, float]]]) -> tuple[dict[int, float], int]:
    """Event study: average signed Kalshi move (cents, in the scoring team's favor) at each offset from
    the moment the recorder saw ESPN's new score, measured from the price 60 s before. Games priced
    outside 10-90c 60 s before are skipped (prices there barely move). Returns (curve, n baskets)."""
    acc: dict[int, list[float]] = {o: [] for o in OFFSETS}
    for game, changes in scores.items():
        series = trades.get(game, [])
        times = [ts for ts, _ in series]
        prev = None
        for t, score in changes:
            away, home = map(int, score.split("-"))
            if prev is not None and (home - prev[1]) != (away - prev[0]):
                sign = 1 if home - prev[1] > away - prev[0] else -1
                base = price_at(series, times, t - 60)
                if base is not None and 0.10 < base < 0.90:
                    for o in OFFSETS:
                        p = price_at(series, times, t + o)
                        if p is not None:
                            acc[o].append(sign * (p - base) * 100)
            prev = (away, home)
    return {o: statistics.mean(v) for o, v in acc.items() if v}, len(acc[0])


def placebo(scores: dict[str, list[tuple[float, str]]], seed: int = 2) -> dict[str, list[tuple[float, str]]]:
    """The same score changes moved to random later times (2-7 min), where no basket is happening."""
    rng = random.Random(seed)
    return {g: [(t + rng.uniform(120, 400), s) for t, s in ch] for g, ch in scores.items()}


def curve_table(scores, trades) -> str:
    real, n = event_curve(scores, trades)
    fake, m = event_curve(placebo(scores), trades)
    rows = [f"signed Kalshi move (cents) vs 60 s before the recorder saw ESPN's score; n={n} baskets (placebo n={m})",
            "offset s | real | placebo", "--- | --- | ---"]
    rows += [f"{o:+d} | {real[o]:+.2f} | {fake.get(o, 0):+.2f}" for o in OFFSETS if o in real]
    final = real.get(60)
    if final:
        half = next(o for o in OFFSETS if real.get(o, -1e9) >= final / 2)
        rows.append(f"\nfinal move {final:.2f}c; half of it already done at {half:+d} s; "
                    f"done by ESPN's moment (0 s): {real[0] / final:.0%}")
    return "\n".join(rows)


def main(argv: list[str]) -> None:
    lines = latency.load(Path(argv[0]))
    date_code = argv[1]
    scores = latency.score_changes(lines)
    trades = {game: to_series(fetch_all_trades(home_ticker(game, date_code))) for game in scores}
    print(curve_table(scores, trades))
    print("\n(Not used: the first-move lag in latency.trade_lags. A placebo at random times gave nearly the"
          " same numbers, so it measures price noise, not reaction.)")


if __name__ == "__main__":
    main(sys.argv[1:])
