#!/bin/bash
# User Scripts entry: queue_manager (custom-network aware)
set -e

BASE="/mnt/user/appdata/scripts/natorr"

# To update queue_manager.py: delete it and let this fetch the latest
# version fresh - same pattern as TRaSH-Guides' mover-tuning script and
# mover.py. queue_manager.py's Discord update notification tells you when
# a newer version is available; this is what actually re-fetches it once
# you (or anything else) removes the file. queue_manager.yml is never
# touched by this - only the .py.
QM_PY_URL="https://raw.githubusercontent.com/TotAnon/scripts/main/queue_manager/queue_manager.py"
if [ ! -f "$BASE/queue_manager.py" ]; then
    echo "queue_manager.py is missing - fetching the latest version from GitHub..."
    curl -fsSL "$QM_PY_URL" -o "$BASE/queue_manager.py"
    echo "Fetched queue_manager.py."
fi

python3 "$BASE/resolve_and_patch.py" "$BASE/queue_manager.yml" \
    radarr:7878 \
    sonarr:8989 \
    qui:7476

"$BASE/python-venv/bin/python3" "$BASE/queue_manager.py"
