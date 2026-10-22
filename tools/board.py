"""Tonight's NBA board: Kalshi game-winner prices with Robinhood fees and break-even.

Read-only, Kalshi's free public API (generic User-Agent, no key). Information only: it never
places, sizes or suggests a bet; a person makes every decision. Prices are Kalshi's order book, which
Robinhood mostly shares (it may route some games to Rothera, so its app price can differ).

    python projects/sports/tools/board.py            # today's games (US Eastern date)
    python projects/sports/tools/board.py 2026-10-22 # another date
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibration as cal  # noqa: E402  (same folder; fees, HTTP)

ET = ZoneInfo("America/New_York")
FROZEN_LO, FROZEN_HI = 0.90, 0.95  # the paper-tracked rule in FORWARD.md (it uses the price 5 h before close)


def date_code(day: datetime) -> str:
    """Kalshi event tickers embed the game date as YYMONDD, e.g. 26OCT21."""
    return day.strftime("%y%b%d").upper()


def to_price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if 0 < price < 1 else None


def breakeven(ask: float, n_contracts: int = 1) -> float:
    """Win rate needed to break even buying n contracts in one order at the ask, held to settlement.
    The commission rounds up to the cent per order, so 1-contract orders pay the most per contract."""
    return ask + cal.fee_per_contract(ask, n_contracts)


def board_rows(markets: list[dict]) -> list[dict]:
    rows = []
    for m in markets:
        ask, bid = to_price(m.get("yes_ask_dollars")), to_price(m.get("yes_bid_dollars"))
        rows.append({
            "game": m["event_ticker"].split("-")[-1],
            "team": m.get("yes_sub_title") or m["ticker"].split("-")[-1],
            "bid": bid, "ask": ask,
            "spread": round(ask - bid, 2) if ask and bid else None,
            "fee": cal.fee_per_contract(ask, 1) if ask else None,
            "breakeven": breakeven(ask) if ask else None,
            "breakeven10": breakeven(ask, 10) if ask else None,
            "frozen_band": bool(ask and FROZEN_LO <= ask < FROZEN_HI),
        })
    return sorted(rows, key=lambda r: (r["game"], -(r["ask"] or 0)))


def render(rows: list[dict], day: datetime) -> str:
    fmt = lambda x: "—" if x is None else f"{x:.2f}"  # noqa: E731
    lines = [f"NBA board {day:%a %b %d} (Kalshi order book, {datetime.now(ET):%H:%M} ET)",
             "game | team | bid | ask | spread | fee (1 contract) | need to win: 1 contract | 10 in one order",
             "--- | --- | --- | --- | --- | --- | --- | ---"]
    for r in rows:
        need = "—" if r["breakeven"] is None else f"{r['breakeven']:.1%} | {r['breakeven10']:.1%}"
        flag = "  <- in the paper-tracked 90-95c band (paper only)" if r["frozen_band"] else ""
        if r["breakeven"] is None:
            need = "— | —"
        lines.append(f"{r['game']} | {r['team']} | {fmt(r['bid'])} | {fmt(r['ask'])} | {fmt(r['spread'])} | "
                     f"{fmt(r['fee'])} | {need}{flag}")
    if not rows:
        lines.append("(no open game markets for this date)")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    day = datetime.strptime(argv[0], "%Y-%m-%d") if argv else datetime.now(ET)
    code = date_code(day)
    data = cal.get("/markets", {"series_ticker": cal.SERIES["NBA"], "status": "open", "limit": 500})
    todays = [m for m in data["markets"] if f"-{code}" in m["event_ticker"]]
    print(render(board_rows(todays), day))


if __name__ == "__main__":
    main(sys.argv[1:])
