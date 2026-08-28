# =============================================================================
# APEX CAPITAL — TIME UTILS
# =============================================================================
# One aware-safe way to turn a stored timestamp into an age.
#
# THE ESCAPE THIS EXISTS FOR
# --------------------------
# 44 sites across the firm computed an age as
#
#     (datetime.now() - datetime.fromisoformat(ts[:19])).total_seconds() / 3600
#
# `"2026-08-27T15:30:05+00:00"[:19]` is `"2026-08-27T15:30:05"` — a NAIVE
# datetime carrying a UTC wall-clock reading, subtracted from a naive LOCAL
# now(). Measured on this Mac 2026-08-27: of the artifacts that carry an aware
# stamp, SIXTEEN had their age misreported, every one by exactly -4.00h — one
# UTC offset — and the size of that error changes with daylight saving.
#
# The direction is the dangerous one. An artifact whose true age lands in
# (12, 16] hours reports as 12 or less and clears a 12-hour freshness gate, so
# there is a four-hour window in every daily cycle where a producer that has
# already missed its run reads as healthy. `data/international_report.json` was
# 5.04h old and reported 1.04h while this was being written.
#
# On the VM — TZ=UTC — the identical line is correct. So every one of these was
# green in production and wrong on the machine we develop and audit from: the
# host-divergence shape (memory host_divergence_is_a_bug_class).
#
# 19 of the firm's artifacts carry aware stamps today, and five carry BOTH an
# aware and a naive value under the SAME key — agent_registry.created_at,
# fi_pipeline_log.run_at, fi_pipeline_report.run_at, chairman_resolved_archive.ts,
# rd_ml_bridge_signals.updated_at. A reader cannot pick one rule and be right,
# which is why the rule belongs here and not at 44 call sites.
#
# THE SECOND MODE, and why the fix is not "just drop the slice"
# ------------------------------------------------------------
# Removing `[:19]` does not make a site correct — it makes it raise.
# `datetime.now() - datetime.fromisoformat("...+00:00")` is
# `TypeError: can't subtract offset-naive and offset-aware datetimes`, and
# essentially every one of those sites sits inside a bare `except Exception`.
# The age then is not wrong, it is ABSENT — and absent reads as fine: the stale
# flag stays False and the producer is reported OK. A wrong number can be
# argued with; silence cannot. Both modes are handled here, once.
#
# WHY `default` IS A REQUIRED THOUGHT, NOT A CONVENIENCE
# -----------------------------------------------------
# The value substituted for an age we could not compute IS a decision
# (memory fallback-that-means-act). For a staleness alarm the fail-closed
# substitute is a huge number — "assume ancient, raise the flag". For a
# "have we done this recently?" cooldown it is also a huge number — "assume
# long ago, allow the action". The two are opposite in meaning and identical in
# form, so `age_hours()` refuses to guess: it returns None unless the caller
# names its own substitute at the call site, where a reviewer can see it.
# =============================================================================

from datetime import datetime, timezone
from typing import Optional, Tuple, Union

# The spelling for "we could not read this stamp; treat it as very old."
# Correct for a freshness gate (alarm) and for a cooldown (permit) alike —
# but it is the CALLER that must decide those mean the same thing here.
UNKNOWN_IS_ANCIENT = 999_999.0


def parse_ts(raw: Union[str, datetime, None]) -> Optional[datetime]:
    """Parse a stored timestamp, PRESERVING its offset. None if unreadable.

    Accepts a datetime unchanged, so callers that may already hold one do not
    need a type check. Handles the "Z" suffix that `fromisoformat` rejects
    before Python 3.11 — the VM is 3.11 and the Mac is 3.9, and a parse that
    works on one host and raises on the other is this whole bug class.

    The truncating retries are kept ONLY as a last resort for stamps with
    trailing junk that is neither an offset nor a fraction. They run after the
    honest parse has failed, so they can no longer silently discard a real
    offset — which is the difference between this function and the 44 call
    sites it replaces.
    """
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for candidate in (s, s[:26], s[:19]):
        try:
            return datetime.fromisoformat(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _align(dt: datetime, now: Optional[datetime]) -> Tuple[datetime, datetime]:
    """Return (dt, now) as a pair that can legally be subtracted.

    Both naive, or both aware, subtract fine — aware/aware works across
    different offsets and yields a true elapsed time. Only the MIXED case needs
    a decision, and the decision is: a naive stamp in this firm means local
    wall-clock, because every naive producer here writes `datetime.now()`.
    So the naive side is given the local offset rather than the aware side
    being stripped. Stripping is what produced the -4h error in the first place.
    """
    if now is None:
        now = datetime.now(dt.tzinfo) if dt.tzinfo is not None else datetime.now()
    if (dt.tzinfo is None) == (now.tzinfo is None):
        return dt, now
    local = datetime.now().astimezone().tzinfo
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local)
    else:
        now = now.replace(tzinfo=local)
    return dt, now


def age_seconds(raw, default: Optional[float] = None,
                now: Optional[datetime] = None) -> Optional[float]:
    """Elapsed seconds since `raw`. `default` when the stamp cannot be read.

    Negative when the stamp is in the future — returned as-is and never clamped
    to zero. A future stamp means a clock or a host disagrees, and a caller that
    sees 0.0 cannot tell that from "written this instant".

    `now` is for tests and for callers that already hold a reference time; it
    may be naive or aware and is aligned to the stamp either way.
    """
    dt = parse_ts(raw)
    if dt is None:
        return default
    dt, ref = _align(dt, now)
    return (ref - dt).total_seconds()


def age_hours(raw, default: Optional[float] = None,
              now: Optional[datetime] = None) -> Optional[float]:
    """Elapsed hours since `raw`. See `age_seconds` for the default contract."""
    s = age_seconds(raw, default=None, now=now)
    return default if s is None else s / 3600.0


def age_days(raw, default: Optional[float] = None,
             now: Optional[datetime] = None) -> Optional[float]:
    """Elapsed days since `raw`, as a FLOAT. See `age_seconds`.

    Float, not `timedelta.days`. `.days` truncates toward negative infinity, so
    a stamp 23 hours old is "0 days" and a stamp one hour in the FUTURE is
    "-1 days" — the second is how an hour of clock skew becomes a report that
    something happened yesterday. Callers that want the old integer behaviour
    should floor it where the truncation is visible.
    """
    s = age_seconds(raw, default=None, now=now)
    return default if s is None else s / 86400.0


def is_stale(raw, max_age_hours: float, unknown_is_stale: bool = True,
             now: Optional[datetime] = None) -> bool:
    """True if `raw` is older than `max_age_hours`.

    `unknown_is_stale` defaults to True: a stamp we cannot read is not evidence
    of freshness. The parameter exists so a caller choosing the other way has
    to say so at the call site, where a reviewer can see it.
    """
    a = age_hours(raw, default=None, now=now)
    if a is None:
        return unknown_is_stale
    return a > max_age_hours


def as_naive_local(raw) -> Optional[datetime]:
    """Parse to a NAIVE datetime on this host's wall clock. None if unreadable.

    For the modules that deliberately work in naive local time because they do
    interval arithmetic between two producer stamps and both sides must have
    the same awareness (`deadmans_switch`). `[:19]` also produces a naive
    datetime, but a WRONG one: it keeps UTC digits and calls them local. This
    CONVERTS instead of truncating, so the module's naive contract survives a
    producer that starts writing offsets.
    """
    dt = parse_ts(raw)
    if dt is None:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt


def utc_now_iso() -> str:
    """`datetime.now(timezone.utc).isoformat()` — an aware stamp, spelled once.

    Provided so a producer converting away from naive `datetime.now()` has
    somewhere to land. Note that switching a producer to this is safe ONLY once
    its readers go through `age_hours` above: an aware stamp handed to a legacy
    `datetime.now() - fromisoformat(ts)` reader raises TypeError, which that
    reader swallows into "age unknown", which it reports as healthy.
    """
    return datetime.now(timezone.utc).isoformat()
