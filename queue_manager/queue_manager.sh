#!/bin/bash
# User Scripts entry: queue_manager (custom-network aware)
set -e

BASE="/mnt/user/appdata/scripts/natorr"

python3 "$BASE/resolve_and_patch.py" "$BASE/queue_manager.yml" \
    radarr:7878 \
    sonarr:8989 \
    qui:7476

"$BASE/python-venv/bin/python3" "$BASE/queue_manager.py"
