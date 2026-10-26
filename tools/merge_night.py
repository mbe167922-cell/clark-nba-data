"""Merge copies of one night's data file without losing rows (public recorder repo, commit step).

    python tools/merge_night.py OUT COPY [COPY ...]     # missing copies are skipped; OUT may be one of the copies

Why (2026-10-26): a scheduled run checks out the commit it was triggered on, waits while the previous run records,
then committed its own (older) copy of the night's files over the newer one. Every night lost one run's rows.
Line files (nba-, race-, books-*.jsonl.gz, status-*.jsonl): the union of all rows, exact duplicates once, in time
order (`t`; rows without `t` keep their order). Trade packs (trades-*.json.gz): per ticker the union of prints.
Same file in both repos (projects/sports/tools/ and the public repo's tools/).
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trades_bundle import read_lines  # noqa: E402  (multi-member, cut-off-safe gzip reader)


def merge_lines(*copies: list[dict]) -> list[dict]:
    seen, out = set(), []
    for rows in copies:
        for row in rows:
            key = json.dumps(row, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(row)
    return sorted(out, key=lambda r: r.get("t", 0))   # stable: rows without t keep first-seen order


def merge_trades(*packs: dict) -> dict:
    out: dict[str, list] = {}
    for pack in packs:
        for ticker, prints in pack.items():
            have = out.setdefault(ticker, [])
            seen = {json.dumps(p) for p in have}
            have += [p for p in prints if json.dumps(p) not in seen]
    return {t: sorted(p, key=lambda x: x[0], reverse=True) for t, p in out.items()}   # newest first, as packed


def read(path: Path, like: Path | None = None) -> list[dict] | dict:
    """Read a copy in the format of `like` (the destination), whatever the copy's own name is."""
    like = like or path
    if like.name.startswith("trades-"):
        return json.loads(gzip.decompress(path.read_bytes()))
    if like.suffix == ".gz":
        return read_lines(path)
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def write(path: Path, merged: list[dict] | dict) -> None:
    if isinstance(merged, dict):
        data = gzip.compress(json.dumps(merged).encode("utf-8"))
    else:
        text = "".join(json.dumps(r) + "\n" for r in merged)
        data = gzip.compress(text.encode("utf-8")) if path.suffix == ".gz" else text.encode("utf-8")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def merge_files(out: Path, copies: list[Path]) -> int:
    """Merge the existing copies into `out`; returns the number of rows (or tickers) written."""
    data = [read(p, like=out) for p in copies if p.exists()]
    if not data:
        return 0
    merged = merge_trades(*data) if isinstance(data[0], dict) else merge_lines(*data)
    write(out, merged)
    return len(merged)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    n = merge_files(Path(argv[0]), [Path(p) for p in argv[1:]])
    print(f"{argv[0]}: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
