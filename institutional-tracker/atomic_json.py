#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — ATOMIC JSON READ-MODIFY-WRITE
# =============================================================================
# One obviously-correct way to update a JSON file that more than one process
# writes. It exists because the firm has now lost data to the same three
# mistakes in six different modules, and hand-rolling the fix a seventh time
# just moves where the next one will be.
#
# THE THREE MISTAKES, and what each one costs:
#
#   1. `open(path, "w")` truncates at open(), BEFORE the replacement is
#      written. Any reader arriving in that window gets an empty or
#      half-written file. Measured on issue_tracker.json: 14 of 300 concurrent
#      reads returned []. Measured on email_buffer.json: 68 of 300.
#
#   2. A FIXED temp name — `path + ".tmp"` — is shared by every concurrent
#      writer, so writer B empties writer A's temp file mid-dump and A then
#      renames the torn result over the live file. os.replace() is genuinely
#      atomic and this defeats it anyway: the bytes were already wrong before
#      the rename. `chairman_queue.submit` carried a comment asserting the
#      write was safe against a crash, which was true, next to a temp path
#      that was not safe against a peer, which was the failure that happened.
#
#   3. No lock spanning the READ and the WRITE. Two producers both read N
#      entries, both append their own, both write N+1 — and one producer's
#      entry is gone with no error anywhere. Locking the load and the save
#      separately grants no mutual exclusion at all.
#
# On the VM these are not theoretical: four cron jobs start at 21:00, three at
# 13:30, three at 22:00, and 23 modules submit to the Chairman queue.
#
# USE `update()` FOR ANYTHING WITH MORE THAN ONE WRITER. `write()` alone is
# enough only for a file one producer regenerates wholesale each run, where a
# lost write costs one stale report rather than someone else's data.
# =============================================================================

import os
import json
import fcntl
import logging
import tempfile
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class Unreadable(Exception):
    """The file exists but could not be parsed as the expected JSON shape.

    Raised only on the mutating paths. A reader may degrade to a default; a
    writer may not, because the next thing a writer does is save, and saving
    over an unreadable file destroys whatever was in it. A degraded read that
    returns the same value as a legitimate empty is a decision disguised as an
    absence (memory `fallback-that-means-act`).
    """


def _dirname(path) -> str:
    return os.path.dirname(str(path)) or "."


@contextmanager
def locked(path):
    """Hold ONE exclusive lock for an entire read-modify-write of `path`.

    The lock lives in a SEPARATE `<path>.lock` file. It cannot live in the
    file being updated: `write()` replaces that file by rename, so the lock
    would follow the inode that just got unlinked and the next writer would
    take a lock on a different file entirely.

    ⚠ NOT REENTRANT. flock is held per open file description, so a nested
    `locked()` on the same path opens a second descriptor and blocks forever
    waiting for the outer one to release.
    """
    os.makedirs(_dirname(path), exist_ok=True)
    f = open(str(path) + ".lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def read(path, default=None, strict: bool = False, expect=None):
    """Load JSON from `path`.

    `strict` separates the two things a bare `except: return default`
    conflates — "there is nothing here" and "I could not read what is here".
    They must not share an answer, because the caller's next move after the
    second one is to write.

    `expect` is an optional type (list, dict) the payload must match; a file
    that parses but holds the wrong shape is unreadable, not empty.
    """
    if not os.path.exists(str(path)):
        return default                # genuinely absent — safe to build on
    try:
        with open(str(path)) as f:
            data = json.load(f)
        if expect is not None and not isinstance(data, expect):
            raise ValueError(f"file is {type(data).__name__}, expected "
                             f"{getattr(expect, '__name__', expect)}")
        return data
    except Exception as e:            # noqa: BLE001
        if strict:
            raise Unreadable(f"{path}: {e}") from e
        logger.warning(f"  [atomic_json] could not read {path}: {e}")
        return default


def read_for_write(path, default=None, expect=None):
    """Load on a mutating path, quarantining bytes it cannot parse.

    Archive, never delete. The alternative is that the next save overwrites
    the only copy of whatever was there, which is how a corrupt file becomes
    an empty one with no evidence that anything was lost.
    """
    try:
        return read(path, default=default, strict=True, expect=expect)
    except Unreadable as e:
        from datetime import datetime
        quarantine = f"{path}.corrupt-{datetime.now():%Y%m%dT%H%M%S}"
        try:
            os.replace(str(path), quarantine)
            logger.error(f"  [atomic_json] UNREADABLE {path} quarantined to "
                         f"{quarantine} — starting fresh. {e}")
        except OSError as move_err:
            logger.error(f"  [atomic_json] UNREADABLE {path} and it could not be "
                         f"moved aside ({move_err}) — REFUSING to write. {e}")
            raise
        return default


def write(path, data) -> None:
    """Replace `path` atomically. The live file is never truncated in place.

    mkstemp gives this writer a PRIVATE name — that is the whole difference
    from `path + ".tmp"` — and os.replace swaps it in as one operation, so a
    concurrent reader sees the whole old file or the whole new one and never
    a torn middle.

    Raises rather than returning False: a caller that just built a merged
    queue in memory and could not persist it needs to know, not to continue
    believing the write happened.
    """
    os.makedirs(_dirname(path), exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=_dirname(path),
                                   prefix="." + os.path.basename(str(path)) + ".",
                                   suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


@contextmanager
def update(path, default=None, expect=None):
    """The whole safe pattern: lock, read, hand you the value, write it back.

        with atomic_json.update(QUEUE, default=[], expect=list) as entries:
            entries.append(new_row)

    The lock is held across the read AND the write, so a peer that submits
    between them waits instead of overwriting. Mutate the yielded object in
    place; it is written back on a clean exit and left untouched if the body
    raises.

    ⚠ Do not call anything inside the body that also updates the same path.
    """
    with locked(path):
        data = read_for_write(path, default=default, expect=expect)
        yield data
        write(path, data)
