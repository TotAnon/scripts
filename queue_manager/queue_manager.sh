#!/bin/bash
# User Scripts entry: queue_manager (custom-network aware)
set -e

BASE="/mnt/user/appdata/scripts/natorr"

# Only relevant if settings.update.delete_self_on_update is enabled in
# queue_manager.yml: the script deletes its own queue_manager.py once a
# newer version is detected, and relies on THIS launcher to fetch it back
# before the next run. With delete_self_on_update left at its default
# (false), queue_manager.py is never missing and this is a no-op.
# queue_manager.yml itself is never touched by this - only the .py.
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
