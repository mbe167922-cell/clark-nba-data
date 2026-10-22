"""Latency study (NB-005): in a recorder.py night, which moves first, ESPN's score or Kalshi's price?

For every ESPN score change in a live game, find that game's nearest Kalshi move (mid price change
of at least MIN_MOVE) within +/-WINDOW seconds. lag = Kalshi time - ESPN time: negative means Kalshi
moved first (the market already knew), positive means ESPN showed the basket first (a possible
head start for an alert). Resolution is the recorder's poll interval (~2 s), and ESPN's own CDN
cache (~4 s) is part of what is measured. Research only; never places or suggests a bet.

    python projects/sports/tools/latency.py data/live/nba-20261021.jsonl.gz
"""
from __future__ import annotations

import gzip
import json
import statistics
import sys
from pathlib import Path

MIN_MOVE = 0.02  # Kalshi mid must move at least 2c to count as "the price reacted"
WINDOW = 30.0    # seconds either side of the ESPN score change

# ESPN abbreviations that differ from Kalshi's ticker codes
ESPN_TO_KALSHI = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}


def kalshi_code(espn_abbr: str) -> str:
    return ESPN_TO_KALSHI.get(espn_abbr, espn_abbr)


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def score_changes(lines: list[dict]) -> dict[str, list[tuple[float, str]]]:
    """game key 'AWAYHOME' (Kalshi codes) -> [(time, 'away-home score')] at each live score change."""
    seen: dict[str, str] = {}
    out: dict[str, list[tuple[float, str]]] = {}
    for line in lines:
        if line["src"] != "espn":
            continue
        for g in line["rows"]:
            key = kalshi_code(g["away"]) + kalshi_code(g["home"])
            score = f"{g['away_score']}-{g['home_score']}"
            if g["state"] == "in" and key in seen and seen[key] != score:
                out.setdefault(key, []).append((line["t"], score))
            seen[key] = score
    return out


def price_moves(lines: list[dict]) -> dict[str, list[float]]:
    """game key -> times when the home side's Kalshi mid moved by >= MIN_MOVE since its last move."""
    anchor: dict[str, float] = {}
    out: dict[str, list[float]] = {}
    for line in lines:
        if line["src"] != "kalshi":
            continue
        for row in line["rows"]:
            parts = row["ticker"].split("-")  # KXNBAGAME-26OCT21ATLORL-ORL
            if len(parts) != 3:
                continue
            game, side = parts[1][7:], parts[2]
            if not game.endswith(side):
                continue  # use the home side only, so each game is counted once
            bid, ask = to_float(row["bid"]), to_float(row["ask"])
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2
            if game not in anchor:
                anchor[game] = mid
            elif abs(mid - anchor[game]) >= MIN_MOVE:
                anchor[game] = mid
                out.setdefault(game, []).append(line["t"])
    return out


def lags(scores: dict[str, list[tuple[float, str]]], moves: dict[str, list[float]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for game, changes in scores.items():
        times = moves.get(game, [])
        for t, _ in changes:
            near = [m - t for m in times if abs(m - t) <= WINDOW]
            if near:
                out.setdefault(game, []).append(min(near, key=abs))
    return out


def trade_lags(scores: dict[str, list[tuple[float, str]]],
               trades: dict[str, list[tuple[float, float]]]) -> dict[str, list[float]]:
    """Same lag, but Kalshi's side comes from its own trade timestamps (exact, uncached) instead of
    the 15 s CDN-cached market list. trades: game -> [(unix time, home-side yes price)], sorted.
    For a score change seen at t: the price before = last trade at or before t - WINDOW; the reaction
    = first trade within [t - WINDOW, t + WINDOW] that is >= MIN_MOVE away from it."""
    out: dict[str, list[float]] = {}
    for game, changes in scores.items():
        series = trades.get(game, [])
        for t, _ in changes:
            before = [p for ts, p in series if ts <= t - WINDOW]
            if not before:
                continue
            base = before[-1]
            moved = [ts for ts, p in series if t - WINDOW < ts <= t + WINDOW and abs(p - base) >= MIN_MOVE]
            if moved:
                out.setdefault(game, []).append(moved[0] - t)
    return out


def summary(per_game: dict[str, list[float]]) -> str:
    rows = ["game | matched score changes | median lag s | Kalshi first | ESPN first",
            "--- | --- | --- | --- | ---"]
    every = []
    for game, values in sorted(per_game.items()):
        every += values
        rows.append(f"{game} | {len(values)} | {statistics.median(values):+.1f} | "
                    f"{sum(v < 0 for v in values)} | {sum(v > 0 for v in values)}")
    if every:
        rows.append(f"**all** | {len(every)} | {statistics.median(every):+.1f} | "
                    f"{sum(v < 0 for v in every)} | {sum(v > 0 for v in every)}")
    else:
        rows.append("(no matched score changes)")
    return "\n".join(rows)


def main(argv: list[str]) -> None:
    lines = load(Path(argv[0]))
    print(summary(lags(score_changes(lines), price_moves(lines))))
    print("\nlag = Kalshi move time - ESPN score time; negative = Kalshi moved first.")


if __name__ == "__main__":
    main(sys.argv[1:])
