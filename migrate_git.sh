#!/bin/bash
# =============================================================================
# APEX CAPITAL — One-time Git Migration Script
# Moves the git repo root from institutional-tracker/ to the parent folder
# so that ALL company files get pushed to GitHub going forward.
#
# Run this ONCE from Terminal:
#   bash "/Users/jackbeard/Claude investment strategy/migrate_git.sh"
# =============================================================================

set -e  # stop on any error

ROOT="/Users/jackbeard/Claude investment strategy"
cd "$ROOT"

echo ""
echo "=============================================="
echo "  Apex Capital — Git Migration"
echo "=============================================="
echo ""

# Safety check — make sure we're not already a git repo at root level
if [ -d ".git" ]; then
  echo "✓ .git already exists at root — migration already done."
  echo "  Just run:  git add -A && git commit -m 'Add all root files' && git push"
  exit 0
fi

# Check the nested git exists
if [ ! -d "institutional-tracker/.git" ]; then
  echo "ERROR: No .git found in institutional-tracker/. Nothing to migrate."
  exit 1
fi

echo "Step 1 — Moving .git from institutional-tracker/ to root..."
mv institutional-tracker/.git .
echo "  ✓ Done"

echo ""
echo "Step 2 — Staging all files..."
git add -A
echo "  ✓ Done"

echo ""
echo "Step 3 — Committing..."
git commit -m "Migrate repo to root — all company files now tracked"
echo "  ✓ Done"

echo ""
echo "Step 4 — Pushing to GitHub (force, because root changed)..."
git push origin HEAD --force
echo "  ✓ Done"

echo ""
echo "=============================================="
echo "  Migration complete!"
echo ""
echo "  Your GitHub repo now includes EVERYTHING:"
echo "  institutional-tracker/, dashboard/, config.py,"
echo "  org_structure.py, railway.json, Procfile, etc."
echo ""
echo "  Next: Connect this repo to Railway."
echo "  See dashboard/DEPLOY.md for instructions."
echo "=============================================="
echo ""
