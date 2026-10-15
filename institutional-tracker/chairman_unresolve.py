#!/usr/bin/env python3
"""Reverse a proxy-chairman (or any) resolution on the chairman decision queue.

Usage: python3 chairman_unresolve.py <item-id> [--reason "..."]

This is a thin CLI around `proxy_chairman_agent.unresolve_item`. The actual
write to the queue lives there — proxy_chairman_agent is the one scheduled
module that writes the decision queue, and wiring_audit treats a second
human-only writer as an unscheduled orphan (correctly).
"""

from __future__ import annotations
import argparse
import sys

from proxy_chairman_agent import unresolve_item


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("item_id")
    ap.add_argument("--reason", default="(none given)")
    args = ap.parse_args(argv)
    ok = unresolve_item(args.item_id, args.reason)
    if not ok:
        print(f"item not found or queue missing: {args.item_id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
