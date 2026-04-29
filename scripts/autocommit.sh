#!/bin/zsh
# Twice-weekly autocommit + push of runtime artifacts (data/jobs.json, data/palace/**).
# Invoked by ~/Library/LaunchAgents/com.q-orca-kb.autocommit.plist.
set -euo pipefail

REPO=/Users/allans/code/q-orca-kb
ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(ts)] q-orca-kb autocommit starting"
cd "$REPO"

# Stage only the published runtime artifacts, including additions/deletions under data/palace.
git add -A -- data/jobs.json data/palace

if git diff --cached --quiet; then
    echo "[$(ts)] working tree clean, nothing to commit"
    exit 0
fi

MSG="chore: refresh palace + jobs ($(date '+%Y-%m-%d'))"
git commit -m "$MSG"
echo "[$(ts)] committed: $MSG"

# Rebase in case origin moved (rare for a single-user repo, but cheap insurance).
git pull --rebase --autostash

git push
echo "[$(ts)] pushed to origin"
