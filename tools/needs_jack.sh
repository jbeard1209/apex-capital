#!/usr/bin/env bash
# Append one item to the single "needs Jack" list: docs/NEEDS_JACK.md.
#
#   tools/needs_jack.sh "short title" "what failed, the exact error, and the one thing Jack must do"
#
# Rule (CLAUDE.md): try tools/ship.sh / gh / the fix yourself first. Add an item
# here only after a REAL failure, with the exact error text — never a pre-emptive
# "Jack, please paste this". The list is published hourly to the private
# apex-reports repo (NEEDS_JACK.md) by tools/sync_drift_check.py --publish.
#
# Writes to the Mac checkout's copy (APEX_MAC_ROOT), not a worktree's, because
# that is the copy the publisher reads. Same title already open → not re-added.
set -euo pipefail
TITLE="${1:?usage: needs_jack.sh TITLE DETAIL}"
DETAIL="${2:-}"
MAC_ROOT="${APEX_MAC_ROOT:-/Users/jackbeard/Claude investment strategy}"
F="$MAC_ROOT/docs/NEEDS_JACK.md"
[ -f "$F" ] || F="$(git rev-parse --show-toplevel)/docs/NEEDS_JACK.md"
if grep -qF -- "- [ ] **$TITLE**" "$F"; then echo "already listed: $TITLE"; exit 0; fi
STAMP="$(date '+%Y-%m-%d %H:%M %Z')"
ITEM="$(printf -- '- [ ] **%s** (%s, %s)\n%s' "$TITLE" "$STAMP" "$(hostname -s)" "$(printf '%s\n' "$DETAIL" | sed 's/^/      /')")"
# Insert at the end of "## Open" (before "## Done"), dropping the placeholder.
ITEM="$ITEM" /usr/bin/python3 - "$F" <<'PY'
import os, sys
p = sys.argv[1]; s = open(p).read().replace("(nothing yet)\n", "")
item = os.environ["ITEM"].rstrip("\n") + "\n"
i = s.find("\n## Done")
s = (s.rstrip("\n") + "\n\n" + item) if i < 0 else (s[:i].rstrip("\n") + "\n\n" + item + s[i:])
open(p, "w").write(s)
PY
echo "added to $F: $TITLE"
