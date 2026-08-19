#!/usr/bin/env python3
"""
watch_universe.py — the one answer to "which tickers do we look at?"
====================================================================
Four scanning agents each carried their own private copy of this question, and
every copy answered it wrong in the same three ways. Measured against the live
VM on 2026-08-18:

  positions   `AlpacaTrader().get_positions()` returns a DataFrame whose symbol
              column is `ticker`. All four asked for `symbol`. A pandas
              `.get("symbol", [])` does not raise on a missing column — it
              returns the default — so 21 live positions produced zero tickers
              and the `except Exception: pass` wrapped around it never fired.
  watchlist   `fe_watchlist.json` is a dict {last_updated, description,
              watchlist:[...]}. All four ran it through
              `x if isinstance(x, list) else []`, which classified the whole
              dict as "not a list" and discarded 14 names.
  ml picks    `ml_shortterm_board_report.json` carries `approved` /
              `newly_queued`. The readers looked for `top_picks` / `picks`.

The consequence was invisible because three wrong guesses produce exactly the
output of a calm market. `edgar_transcript_agent` has run at 23:20 UTC every
night for months and logged "No tickers to analyze / RC=0" every time; nothing
has written `alt_data_signals.json` since 2026-07-03.

So this module is not a refactor for tidiness. It exists so that the next agent
that needs a ticker universe cannot invent a fourth wrong answer, and so that
"we found nothing" is reported as a broken input rather than as a quiet day.

Contract
--------
    tickers, counts = get_watch_tickers()          # counts is per-source
    if is_degraded(counts):                        # every source empty
        ...  # NOT a calm market. Nothing was examined.

`counts` always carries a key for every source that was consulted, including
the ones that returned zero — an absent key would be indistinguishable from a
source that was never tried, which is the whole failure this module is here to
end.
"""

import json
import logging
import os
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("watch_universe")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

#: The column `alpaca_trader.get_positions()` actually puts the symbol in.
#: `symbol` is listed second only so a future schema change is absorbed rather
#: than silently zeroing every caller — it is NOT a column that exists today.
POSITION_TICKER_COLUMNS = ("ticker", "symbol")

#: Where a ticker list can live inside each JSON source. Order matters: the
#: first key holding a list wins.
_SOURCE_KEYS = {
    "fe_watchlist":  ("watchlist", "tickers"),
    "fe_picks":      ("picks", "queue"),
    "ml_picks":      ("approved", "newly_queued", "top_picks", "picks"),
}

DEFAULT_SOURCES = ("positions", "fe_watchlist", "fe_picks", "ml_picks")

_SOURCE_FILES = {
    "fe_watchlist": "fe_watchlist.json",
    "fe_picks":     "fe_picks_queue.json",
    "ml_picks":     "ml_shortterm_board_report.json",
}


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("  %s unreadable (%s)", os.path.basename(path), exc)
        return default


def _rows(blob, keys: Sequence[str]) -> list:
    """The list of records inside `blob`, whichever of `keys` holds it.

    A bare list is itself the answer. A dict is searched in `keys` order. Any
    other shape yields nothing — but the caller records a zero for it, so a
    shape we failed to anticipate shows up as a named empty source rather than
    as silence.
    """
    if isinstance(blob, list):
        return blob
    if isinstance(blob, dict):
        for key in keys:
            value = blob.get(key)
            if isinstance(value, list):
                return value
    return []


def _ticker_of(item) -> Optional[str]:
    """A ticker out of a bare string or a {ticker|symbol: ...} record."""
    if isinstance(item, str):
        return item.strip().upper() or None
    if isinstance(item, dict):
        for key in ("ticker", "symbol"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
    return None


def position_tickers() -> List[str]:
    """Tickers of the open broker positions, or [] if the broker is unreachable.

    Resolves the symbol column by looking at what the DataFrame actually has
    instead of naming one and taking a default. The old form —
    `pos_df.get("symbol", [])` — cannot fail loudly: pandas treats a missing
    column as a miss, not an error.
    """
    from alpaca_trader import AlpacaTrader          # local: heavy import

    frame = AlpacaTrader().get_positions()
    if frame is None or frame.empty:
        return []
    column = next((c for c in POSITION_TICKER_COLUMNS if c in frame.columns), None)
    if column is None:
        # Not "no positions" — the schema moved. Say so; a silent [] here is
        # precisely the bug this module was written to end.
        raise KeyError(
            "get_positions() has no symbol column; saw %s, expected one of %s"
            % (list(frame.columns), list(POSITION_TICKER_COLUMNS))
        )
    return [t for t in (_ticker_of(v) for v in frame[column]) if t]


def get_watch_tickers(limit: int = 50,
                      sources: Sequence[str] = DEFAULT_SOURCES
                      ) -> Tuple[List[str], Dict[str, int]]:
    """(tickers, {source: tickers that source yielded}).

    The per-source count is how many tickers that source produced, NOT how many
    it added that no other source had. Counting only the new ones would report
    `fe_watchlist=0` for a healthy 14-name watchlist that happens to overlap the
    open positions — a source reading as broken because it agreed with another
    is the same confusion between silence and health this module exists to end.

    `limit` caps the returned list to keep downstream API fan-out bounded; the
    counts describe what was found *before* the cap, so a caller can still tell
    a truncated universe from an empty one.
    """
    seen: set = set()
    counts: Dict[str, int] = {}

    def take(name: str, items) -> None:
        found = {t for t in (_ticker_of(i) for i in items) if t}
        seen.update(found)
        counts[name] = len(found)

    for name in sources:
        if name == "positions":
            try:
                take("positions", position_tickers())
            except Exception as exc:
                logger.warning("  positions unreadable (%s) — contributes nothing", exc)
                counts["positions"] = 0
            continue
        filename = _SOURCE_FILES.get(name)
        if filename is None:
            logger.warning("  unknown source %r — skipped", name)
            counts[name] = 0
            continue
        take(name, _rows(_load_json(os.path.join(DATA_DIR, filename), []),
                         _SOURCE_KEYS.get(name, ())))

    return sorted(seen)[:limit], counts


def is_degraded(counts: Dict[str, int]) -> bool:
    """True when every consulted source yielded nothing.

    Separate from `not tickers` on purpose: a caller that passed a narrow
    `sources=` list and got nothing has a different problem from one that
    consulted everything and got nothing, and only the second is a broken
    input. Both come back here as True today because both mean "nothing was
    examined" — but the counts stay in the caller's hands so the distinction
    can be reported.
    """
    return bool(counts) and all(v == 0 for v in counts.values())


def describe(counts: Dict[str, int]) -> str:
    """`fe_picks=1, ml_picks=0, positions=21` — for a one-line log."""
    return ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    tickers, counts = get_watch_tickers()
    print("sources : %s" % describe(counts))
    print("degraded: %s" % is_degraded(counts))
    print("tickers : %d — %s" % (len(tickers), ", ".join(tickers)))
