#!/bin/bash
#description=Disk I/O monitor -> Discord. Settings live here; the program is diskio_monitor.py in STATE_DIR.
#############################################
# Unraid Disk I/O Monitor -> Discord  (settings + launcher)
#
# Setup
#   1. Install Python 3 (Python 3 plugin / NerdTools). Needs 3.8 or newer.
#   2. Copy diskio_monitor.py into STATE_DIR (set below).
#   3. Paste this file into User Scripts and schedule it "At Startup of Array".
#      Stop the old bash version first - its state.sh is migrated automatically.
#
# Post the current test so far vs previous tests (from a terminal):
#   bash /boot/config/plugins/user.scripts/scripts/<this script's name>/script --compare
#############################################
set -a   # every setting below is passed to the Python program

### ---- SETTINGS ---- ###
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
THRESHOLD_PERCENT=85        # %util a disk must hit to count as "over" for that sample
SAMPLE_SECONDS=30           # target length of each %util sample
COMPILE_MINUTES=240         # interval alert window, clock-aligned to midnight (e.g. 60, 120, 240)
HEALTH_PING_HOURS=12        # "still alive" ping if this long passes with no message at all; 0 = disabled
WEEKLY_INTERVAL_DAYS=7      # rollup period in days, clock-aligned to midnight
EXCLUDE_DEVICES="loop|sr|md|zram|dm-"   # device-name prefixes to ignore
# Only monitor devices whose Unraid name matches (default: array data disks only - skips
# flash and pools). Add parity with '^(disk[0-9]+|parity[0-9]*)$'; '' monitors everything.
MONITOR_NAME_REGEX='^disk[0-9]+$'
MOVER_PROCESS_NAMES=("mover" "age_mover")  # stock mover + Mover Tuning plugin's wrapper

# Test label. To finish a test and start the next one: change this, then restart the script.
# The finished test is saved and a comparison with previous tests is posted to Discord.
TEST_NAME="Test 1"
TEST_COMPARE_COUNT=3        # tests shown side by side in comparisons

# Folder holding diskio_monitor.py, its state and the test history. Keep it on the cache
# pool (appdata), not /var/log or /tmp, which are wiped on reboot.
STATE_DIR="/mnt/user/appdata/scripts/diskio_monitor"
STATE_SAVE_INTERVAL_SEC=300

QBIT_ENABLED=true
# Qui is reached via `docker exec` into Qui's own container.
QUI_CONTAINER_NAME="qui"
# One entry per qBittorrent instance: "label|proxy_url" or "label|proxy_url|path_map".
# Proxy URL: Qui -> Settings -> Client Proxy -> create a key per instance, with localhost as host:
#   http://localhost:7476/proxy/<key>
QBIT_INSTANCES=(
    "qbit-tv|http://localhost:7476/proxy/YOUR_CLIENT_KEY_1"
    "qbit-movies|http://localhost:7476/proxy/YOUR_CLIENT_KEY_2"
)
# How paths inside your qBit containers map to the host (container=host). Used to tell whether
# a torrent's files are still on a pool or have moved to the array. Applies to every instance
# without its own map. Several mappings: "/data=/mnt/user/data;/media=/mnt/user/media"
QBIT_PATH_MAP="/data=/mnt/user/data"
POOL_NAMES=("cache")       # pool(s) new downloads land on before mover runs (e.g. "cache" or "downloads")
# Tier tags (e.g. set by Qui automations). Case-insensitive; must capture the number as (?<n>...).
TIER_TAG_REGEX='^tier ?(?<n>[0-9]+)$'
RATIO_SPLIT=2.0            # tier upload is split into below / at-or-above this ratio
NEW_TORRENT_DAYS=7         # torrents younger than this count as "fresh"
# Optional tracker display names: "hostname-substring|Name". Default: derived from the hostname.
TRACKER_ALIASES=()

# Array stream attribution (daily and rollup reports): which containers have files open on
# the array, and on which disk. Counts streams (open folders), not bytes.
STREAMS_ENABLED=true
STREAM_SHARES=("data")     # user shares to attribute streams for
STREAM_TOP_N=8             # max containers listed in the streams field
STREAM_LSOF_TIMEOUT=20     # seconds; that sample's stream snapshot is skipped if lsof takes longer

LOG_FILE="/var/log/diskio_monitor.log"
LOG_MAX_BYTES=$((10 * 1024 * 1024))   # 10MB - rotate on size cap or new day, keep 1 backup
### ------------------ ###

set +a
# Bash can't export arrays, so lists are passed as newline-separated text.
pass_list() { local -n _arr="$1"; local v; v=$(printf '%s\n' "${_arr[@]}"); unset -n _arr; unset "$1"; export "$1=$v"; }
for list in MOVER_PROCESS_NAMES QBIT_INSTANCES POOL_NAMES TRACKER_ALIASES STREAM_SHARES; do pass_list "$list"; done

PROGRAM="$STATE_DIR/diskio_monitor.py"
command -v python3 >/dev/null 2>&1 || { echo "python3 not found - install the Python 3 plugin first."; exit 1; }
[[ -f "$PROGRAM" ]] || { echo "Missing $PROGRAM - copy diskio_monitor.py there first."; exit 1; }

if [[ "${1:-}" == "--compare" ]]; then
    exec python3 "$PROGRAM" --compare
fi

# Run in the background so stop signals from User Scripts are forwarded and the program
# can save its state before exiting.
python3 "$PROGRAM" &
PY_PID=$!
trap 'kill -TERM "$PY_PID" 2>/dev/null' TERM INT HUP
while kill -0 "$PY_PID" 2>/dev/null; do wait "$PY_PID"; done
