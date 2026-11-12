#!/usr/bin/env bash
# =============================================================================
# APEX CAPITAL — ONE SHIP SCRIPT: commit → push → PR → CI → merge [→ deploy]
# =============================================================================
# Every session used to end with "Jack, paste these 4 commands". This does the
# whole path itself, and only hands Jack something when a step really failed —
# by appending the exact error to docs/NEEDS_JACK.md (the one place he looks).
#
#   tools/ship.sh -m "msg" [paths...]          commit those paths, PR, wait CI, merge
#   tools/ship.sh -m "msg" -b claude/foo ...   on a named branch (created if needed)
#   tools/ship.sh --no-merge -m "msg" ...      stop after the PR is open
#   tools/ship.sh --deploy -m "msg" ...        ... then deploy to the VM  (Jack's OK first!)
#   tools/ship.sh --deploy-only                deploy current main to the VM (Jack's OK first!)
#   tools/ship.sh --dry-run ...                print what would happen, change nothing
#
# Options: -t TITLE  --body-file FILE  --allow-data  --ci-timeout SECS (default 1800)
#
# Rules it enforces (none of these are optional):
#   * Only claude/* branches. It never commits to or pushes main directly.
#   * Never `git add -A`: the Mac tree is full of runtime data/ edits. You name
#     the paths, or stage them yourself first.
#   * Refuses any staged .env file and any Kalshi arm file (`*_armed*`).
#   * Refuses staged institutional-tracker/data/ files unless --allow-data.
#   * Deploy is opt-in AND refuses unless the Mac tree's code equals origin/main,
#     because deploy.sh rsyncs the Mac main checkout — whatever is in it ships,
#     including another session's uncommitted edits. Deploying is a production
#     change: per project rules it needs Jack's OK before you pass --deploy.
#   * /usr/bin first on PATH, so `python3` is the 3.9 that has the packages.
# =============================================================================
set -uo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:$PATH"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "ship: not in a git repo" >&2; exit 2; }
cd "$ROOT"
MAC_ROOT="${APEX_MAC_ROOT:-/Users/jackbeard/Claude investment strategy}"
OWNER="jbeard1209"
MSG="" TITLE="" BRANCH="" BODY_FILE="" MERGE=1 DEPLOY=0 DEPLOY_ONLY=0 DRY=0 ALLOW_DATA=0
CI_TIMEOUT=1800
PATHS=()
ORIG_ARGS="$(printf '%q ' "$@")"

while [ $# -gt 0 ]; do
  case "$1" in
    -m) MSG="$2"; shift 2 ;;
    -t) TITLE="$2"; shift 2 ;;
    -b) BRANCH="$2"; shift 2 ;;
    --body-file) BODY_FILE="$2"; shift 2 ;;
    --no-merge) MERGE=0; shift ;;
    --deploy) DEPLOY=1; shift ;;
    --deploy-only) DEPLOY_ONLY=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --allow-data) ALLOW_DATA=1; shift ;;
    --ci-timeout) CI_TIMEOUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    --) shift; PATHS+=("$@"); break ;;
    -*) echo "ship: unknown option $1" >&2; exit 2 ;;
    *) PATHS+=("$1"); shift ;;
  esac
done

STEP="start"
say()  { printf '\033[1m[ship]\033[0m %s\n' "$*"; }
doit() { if [ "$DRY" = 1 ]; then echo "  (dry-run) $*"; else "$@"; fi; }

# On a real failure: record it for Jack with the exact error, then exit.
fail() {
  local err="$1"
  echo "ship: FAILED at step '$STEP': $err" >&2
  if [ "$DRY" = 0 ] && [ -x "$ROOT/tools/needs_jack.sh" ]; then
    "$ROOT/tools/needs_jack.sh" "ship.sh failed at '$STEP' on branch ${BRANCH:-?}" \
      "Command: tools/ship.sh ${ORIG_ARGS}
Error: $err" >/dev/null 2>&1 || true
    echo "ship: recorded in docs/NEEDS_JACK.md" >&2
  fi
  exit 1
}

retry() {  # retry N cmd...   (network steps)
  local n="$1"; shift; local i=1 out
  while :; do
    out="$("$@" 2>&1)" && { [ -n "$out" ] && echo "$out"; return 0; }
    [ "$i" -ge "$n" ] && { echo "$out"; return 1; }
    sleep $((2 ** i)); i=$((i + 1))
  done
}

code_drift_vs_main() {  # prints tracked code files where the Mac tree differs from origin/main
  git -C "$MAC_ROOT" fetch -q origin main 2>/dev/null
  { git -C "$MAC_ROOT" diff --name-only origin/main -- . ; } 2>/dev/null \
    | grep -v '^institutional-tracker/data/' | grep -v '^institutional-tracker/logs/' \
    | grep -v '^docs/NEEDS_JACK.md$' | grep -v '\.env$' || true
}

deploy() {
  STEP="deploy-precheck"
  local drift; drift="$(code_drift_vs_main)"
  if [ -n "$drift" ]; then
    fail "refusing to deploy: deploy.sh ships the Mac checkout ($MAC_ROOT), and its code differs from origin/main in: $(echo "$drift" | head -10 | tr '\n' ' ')"
  fi
  STEP="deploy"
  say "deploying origin/main from $MAC_ROOT via gcp_setup/deploy.sh"
  if [ "$DRY" = 1 ]; then echo "  (dry-run) cd institutional-tracker && bash gcp_setup/deploy.sh"; return 0; fi
  local out; out="$(cd "$MAC_ROOT/institutional-tracker" && bash gcp_setup/deploy.sh 2>&1)"
  local rc=$?
  echo "$out" | tail -15
  [ $rc -eq 0 ] || fail "deploy.sh exited $rc: $(echo "$out" | tail -5 | tr '\n' ' ')"
  STEP="deploy-verify"
  local v; v="$(/usr/bin/python3 "$ROOT/tools/sync_drift_check.py" --root "$MAC_ROOT" --no-alert 2>&1)"
  echo "$v" | head -12
  echo "$v" | grep -q 'vm_differs\|vm_missing' && fail "deploy ran but the VM still differs from main: $(echo "$v" | grep -A3 'vm_' | head -6 | tr '\n' ' ')"
  say "deployed; VM matches main"
}

if [ "$DEPLOY_ONLY" = 1 ]; then deploy; exit 0; fi
[ -n "$MSG" ] || { echo "ship: -m \"message\" is required" >&2; exit 2; }

# ── branch ────────────────────────────────────────────────────────────────
STEP="branch"
CUR="$(git branch --show-current)"
if [ -z "$BRANCH" ]; then
  case "$CUR" in claude/*) BRANCH="$CUR" ;; *) BRANCH="claude/ship-$(date +%Y%m%d-%H%M%S)" ;; esac
fi
case "$BRANCH" in claude/*) ;; *) fail "branch must be claude/* (got '$BRANCH')";; esac
if [ "$CUR" != "$BRANCH" ]; then
  if git show-ref -q --verify "refs/heads/$BRANCH"; then
    if git merge-base --is-ancestor "$BRANCH" HEAD; then
      # The branch is behind what we're on (e.g. a detached worktree that moved
      # forward to main): moving it to HEAD drops nothing and keeps the edits.
      doit git checkout -q -B "$BRANCH" || fail "git checkout -B $BRANCH failed"
    else
      doit git checkout -q "$BRANCH" \
        || fail "git checkout $BRANCH failed; it has commits HEAD lacks and the working tree has edits — commit or stash, or ship on a new -b branch"
    fi
  else doit git checkout -q -b "$BRANCH" || fail "git checkout -b $BRANCH failed"; fi
fi
say "branch $BRANCH"

# ── stage + guard ─────────────────────────────────────────────────────────
STEP="stage"
if [ ${#PATHS[@]} -gt 0 ]; then doit git add -- "${PATHS[@]}" || fail "git add failed"; fi
STAGED="$(git diff --cached --name-only)"
if [ "$DRY" = 1 ] && [ ${#PATHS[@]} -gt 0 ]; then
  STAGED="$(printf '%s\n%s' "$STAGED" "$(git status --porcelain -- "${PATHS[@]}" | cut -c4-)" | sort -u | sed '/^$/d')"
fi
BAD_ENV="$(echo "$STAGED" | grep -E '(^|/)\.env(\.|$)' || true)"
BAD_ARM="$(echo "$STAGED" | grep -i '_armed' || true)"
BAD_DATA="$(echo "$STAGED" | grep '^institutional-tracker/data/' || true)"
[ -z "$BAD_ENV" ] || { git reset -q -- $BAD_ENV; fail "refusing to commit .env file(s): $BAD_ENV"; }
[ -z "$BAD_ARM" ] || { git reset -q -- $BAD_ARM; fail "refusing to commit Kalshi arm file(s): $BAD_ARM"; }
if [ -n "$BAD_DATA" ] && [ "$ALLOW_DATA" = 0 ]; then
  fail "refusing staged data/ files without --allow-data: $(echo "$BAD_DATA" | head -5 | tr '\n' ' ')"
fi

# ── commit ────────────────────────────────────────────────────────────────
STEP="commit"
if [ -n "$STAGED" ]; then
  say "committing $(echo "$STAGED" | wc -l | tr -d ' ') file(s)"
  doit git commit -q -m "$MSG" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" \
    || fail "git commit failed (pre-commit hook?): $(git commit --dry-run 2>&1 | tail -3 | tr '\n' ' ')"
elif [ -z "$(git log --oneline origin/main..HEAD 2>/dev/null)" ]; then
  fail "nothing staged and $BRANCH has no commits beyond origin/main — nothing to ship"
else
  say "nothing new staged; shipping existing commits on $BRANCH"
fi

# ── push (pre-push gate runs here, ~8 min) ───────────────────────────────
STEP="push"
# A worktree is not the Mac checkout: it has no .env and its committed liveness
# reports read as weeks old, so the pre-push gate blocks every push from it.
# Give the gate what the Mac tree has — placeholder creds (they cannot trade or
# email) and the Mac's current reports — then put the reports back. Never
# --no-verify.
RESTORE=()
if [ "$(cd "$ROOT" && pwd -P)" != "$(cd "$MAC_ROOT" 2>/dev/null && pwd -P)" ]; then
  if [ ! -f "$ROOT/institutional-tracker/.env" ]; then
    for v in ALPACA_API_KEY ALPACA_SECRET_KEY GMAIL_APP_PASSWORD; do
      [ -n "${!v:-}" ] || export "$v=placeholder-worktree-push"
    done
    [ -n "${GMAIL_SENDER:-}" ] || export GMAIL_SENDER="placeholder@example.invalid"
  fi
  for r in coherence_report.json internal_audit_report.json; do
    src="$MAC_ROOT/institutional-tracker/data/$r"; dst="$ROOT/institutional-tracker/data/$r"
    if [ -f "$src" ] && [ "$DRY" = 0 ]; then cp "$src" "$dst" && RESTORE+=("institutional-tracker/data/$r"); fi
  done
fi
restore_reports() { [ ${#RESTORE[@]} -gt 0 ] && git checkout -q -- "${RESTORE[@]}" 2>/dev/null; true; }
say "pushing $BRANCH (the pre-push gate runs the test suite first)"
if [ "$DRY" = 1 ]; then echo "  (dry-run) git push -u origin HEAD:$BRANCH"
else
  OUT="$(retry 4 git -c core.sshCommand="ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=30" \
         push -u origin "HEAD:$BRANCH")" || { restore_reports; fail "git push failed: $(echo "$OUT" | grep -v '^=*$' | tail -8 | tr '\n' ' ')"; }
  restore_reports
fi

# ── PR ────────────────────────────────────────────────────────────────────
STEP="pr"
PR="$(gh pr list --head "$BRANCH" --state open --json number -q '.[0].number' 2>/dev/null)"
if [ -z "$PR" ]; then
  BODY="$(mktemp)"
  { [ -n "$BODY_FILE" ] && cat "$BODY_FILE" || echo "$MSG"
    printf '\n\nShipped by `tools/ship.sh`.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n'
  } > "$BODY"
  if [ "$DRY" = 1 ]; then echo "  (dry-run) gh pr create --base main --head $BRANCH --title \"${TITLE:-$MSG}\""; PR="(dry)"
  else
    OUT="$(retry 3 gh pr create --base main --head "$BRANCH" --title "${TITLE:-$MSG}" --body-file "$BODY")" \
      || fail "gh pr create failed: $(echo "$OUT" | tail -5 | tr '\n' ' ')"
    PR="$(gh pr list --head "$BRANCH" --state open --json number -q '.[0].number')"
    gh pr edit "$PR" --add-assignee "$OWNER" >/dev/null 2>&1 || true
  fi
  rm -f "$BODY"
fi
URL="$(gh pr view "$PR" --json url -q .url 2>/dev/null || echo "PR $PR")"
say "PR: $URL"
[ "$MERGE" = 1 ] || { say "--no-merge: stopping with the PR open"; exit 0; }

# ── wait for CI ───────────────────────────────────────────────────────────
STEP="ci"
if [ "$DRY" = 1 ]; then echo "  (dry-run) gh pr checks $PR --watch"
else
  say "waiting for CI (timeout ${CI_TIMEOUT}s)"
  DEADLINE=$(( $(date +%s) + CI_TIMEOUT ))
  # checks take a few seconds to register after the push
  while [ -z "$(gh pr checks "$PR" 2>/dev/null)" ]; do
    [ "$(date +%s)" -gt "$DEADLINE" ] && fail "no CI checks appeared on $URL within ${CI_TIMEOUT}s"
    sleep 10
  done
  while :; do
    gh pr checks "$PR" --watch --interval 20 >/dev/null 2>&1; RC=$?
    CHECKS="$(gh pr checks "$PR" 2>&1)"
    if echo "$CHECKS" | grep -qE $'\t(pending|queued|in_progress)\t'; then
      [ "$(date +%s)" -gt "$DEADLINE" ] && fail "CI still running on $URL after ${CI_TIMEOUT}s"
      sleep 15; continue
    fi
    [ $RC -eq 0 ] && break
    fail "CI failed on $URL: $(echo "$CHECKS" | grep -E $'\t(fail|cancel)' | head -5 | tr '\t\n' '  ')"
  done
  say "CI green"
fi

# ── merge ─────────────────────────────────────────────────────────────────
STEP="merge"
if [ "$DRY" = 1 ]; then echo "  (dry-run) gh pr merge $PR --merge"
else
  OUT="$(retry 3 gh pr merge "$PR" --merge)" || fail "gh pr merge failed on $URL: $(echo "$OUT" | tail -5 | tr '\n' ' ')"
  say "merged $URL"
fi

# ── bring the Mac checkout along (fast-forward only, never over a dirty tree) ─
STEP="heal"
if [ "$DRY" = 0 ]; then
  /usr/bin/python3 "$ROOT/tools/sync_drift_check.py" --root "$MAC_ROOT" --heal --no-alert 2>&1 | head -12 || true
fi

[ "$DEPLOY" = 1 ] && deploy
say "done"
