#!/usr/bin/env python3
"""Unraid Disk I/O Monitor -> Discord (v2, Python).

Started by the User Scripts wrapper, which holds every setting and passes them in as
environment variables. This file, its state (state.json) and the test history
(test_history.jsonl) live together in STATE_DIR. A state.sh left by the bash version
is migrated automatically on first start and kept as state.sh.migrated.

Every SAMPLE_SECONDS it measures each array disk's %util with no gaps between samples,
and mid-sample reads qBittorrent (via Qui's client proxy, sync/maindata) and which
containers have files open on the array (lsof).

Reports
  Interval alert (every COMPILE_MINUTES, only if a disk hit THRESHOLD_PERCENT):
    per-disk average and time over threshold, plus qBittorrent activity.
  Daily (00:00) and rollup (every WEEKLY_INTERVAL_DAYS): disks with stream owners,
    qBittorrent, upload (array vs pool, fresh, new torrents), upload by tier, array
    streams, and a second message with per-tracker stats. Rollups add which drivers
    move with disk load and a comparison against previous tests.

Tests
  Change TEST_NAME and restart to close a test: it is saved to test_history.jsonl and
  a side-by-side comparison is posted. Run the wrapper with --compare to post the
  current test so far.
"""
import ctypes
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

if sys.version_info < (3, 8):
    sys.exit("diskio_monitor needs Python 3.8 or newer")

VERSION = "2.2"
# Bump with any release that changes how test results are measured or calculated, so the
# update reminder can warn that updating mid-test mixes old and new numbers.
MEASUREMENT_VERSION = 1


# ================================================================== settings
def _env(name, default):
    v = os.environ.get(name)
    return default if v is None else v


def _int(name, default):
    try:
        return int(float(os.environ[name]))
    except (KeyError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _list(name, default):
    v = os.environ.get(name)
    return list(default) if v is None else [x.strip() for x in v.split("\n") if x.strip()]


WEBHOOK = _env("DISCORD_WEBHOOK_URL", "")
THRESHOLD = _int("THRESHOLD_PERCENT", 85)
SAMPLE_SECONDS = max(2, _int("SAMPLE_SECONDS", 30))
COMPILE_MINUTES = max(1, _int("COMPILE_MINUTES", 240))
HEALTH_PING_HOURS = _float("HEALTH_PING_HOURS", 12)
WEEKLY_DAYS = max(1, _int("WEEKLY_INTERVAL_DAYS", 7))
EXCLUDE_DEVICES = _env("EXCLUDE_DEVICES", "loop|sr|md|zram|dm-")
MONITOR_NAME_REGEX = _env("MONITOR_NAME_REGEX", r"^disk[0-9]+$")
MOVER_NAMES = _list("MOVER_PROCESS_NAMES", ["mover", "age_mover"])
TEST_NAME = _env("TEST_NAME", "Test 1")
COMPARE_COUNT = max(1, _int("TEST_COMPARE_COUNT", 3))
STATE_DIR = _env("STATE_DIR", "/mnt/user/appdata/scripts/diskio_monitor")
SAVE_INTERVAL = _int("STATE_SAVE_INTERVAL_SEC", 300)
QBIT_ENABLED = _bool("QBIT_ENABLED", True)
QUI_CONTAINER = _env("QUI_CONTAINER_NAME", "qui")
QUI_API_KEY = _env("QUI_API_KEY", "")
PATH_MAP = _env("QBIT_PATH_MAP", "/data=/mnt/user/data")
POOL_NAMES = _list("POOL_NAMES", ["cache"])
TIER_TAG_REGEX = _env("TIER_TAG_REGEX", r"^tier ?(?<n>[0-9]+)$")
RATIO_SPLIT = _float("RATIO_SPLIT", 2.0)
RATIO_TXT = f"{RATIO_SPLIT:g}" if "." in f"{RATIO_SPLIT:g}" else f"{RATIO_SPLIT:.1f}"
NEW_TORRENT_DAYS = _int("NEW_TORRENT_DAYS", 7)
TRACKER_ALIASES = _list("TRACKER_ALIASES", [])
STREAMS_ENABLED = _bool("STREAMS_ENABLED", True)
STREAM_SHARES = _list("STREAM_SHARES", ["data"])
STREAM_TOP_N = _int("STREAM_TOP_N", 8)
LSOF_TIMEOUT = _int("STREAM_LSOF_TIMEOUT", 20)
LOG_FILE = _env("LOG_FILE", "/var/log/diskio_monitor.log")
LOG_MAX_BYTES = _int("LOG_MAX_BYTES", 10 * 1024 * 1024)
UPDATE_CHECK = _bool("UPDATE_CHECK", True)

INSTANCES = []
for _e in _list("QBIT_INSTANCES", []):
    _p = _e.split("|")
    if len(_p) >= 2 and _p[0] and _p[1]:
        INSTANCES.append({"label": _p[0], "url": _p[1].rstrip("/"),
                          "map": _p[2] if len(_p) > 2 and _p[2] else PATH_MAP})

# Internals - normally never changed (overridable only for testing).
DISKSTATS_FILE = _env("DIM_DISKSTATS", "/proc/diskstats")
SYS_BLOCK_DIR = _env("DIM_SYS_BLOCK", "/sys/block")
DISKS_INI = _env("DIM_DISKS_INI", "/var/local/emhttp/disks.ini")
PROC_DIR = _env("DIM_PROC", "/proc")
MNT_ROOT = _env("DIM_MNT", "/mnt")
WORK_DIR = _env("DIM_WORK", "/tmp/diskio_monitor")
UPDATE_URL = _env("DIM_UPDATE_URL",
                  "https://raw.githubusercontent.com/TotAnon/scripts/main/diskio_monitor/diskio_monitor.py")
README_URL = "https://github.com/TotAnon/scripts/tree/main/diskio_monitor#updating"
CHANGELOG_URL = "https://github.com/TotAnon/scripts/blob/main/diskio_monitor/CHANGELOG.md"
ISSUES_URL = "https://github.com/TotAnon/scripts/issues"
DOWNTIME_REPORT_MIN_SEC = 300

STATE_JSON = os.path.join(STATE_DIR, "state.json")
LEGACY_STATE = os.path.join(STATE_DIR, "state.sh")
HISTORY_FILE = os.path.join(STATE_DIR, "test_history.jsonl")
LOCK_FILE = os.path.join(STATE_DIR, "monitor.pid")
HOST = os.uname().nodename

COLOR_ALERT, COLOR_OK, COLOR_DAILY, COLOR_ROLLUP = 15158332, 3066993, 3447003, 10181046


# ================================================================== logging / helpers
_log_day = None


def log(msg):
    global _log_day
    try:
        today = time.strftime("%Y-%m-%d")
        if os.path.exists(LOG_FILE):
            if os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES or (_log_day and _log_day != today):
                os.replace(LOG_FILE, LOG_FILE + ".1")
        _log_day = today
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def mono_ms():
    return int(time.monotonic() * 1000)


def fmt_duration(sec):
    sec = int(sec)
    return f"{sec // 3600}h {(sec % 3600) // 60}m" if sec >= 3600 else f"{sec // 60}m"


def fmt_minutes(ms):
    m = ms / 60000
    return fmt_duration(ms // 1000) if m >= 60 else f"{m:.1f} min"


def fmt_gb(b):
    g = b / 1073741824
    return f"{g / 1024:.2f} TB" if g >= 1024 else f"{g:.1f} GB"


def mbps(bytes_per_sec):
    return f"{bytes_per_sec / 1048576:.2f}"


def pct(a, b):
    return f"{100 * a / b:.0f}" if b > 0 else "0"


def avg(s, c, digits=1):
    return f"{s / c:.{digits}f}" if c else f"{0:.{digits}f}"


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def local_midnight(ts):
    d = datetime.fromtimestamp(ts)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def next_boundary(interval_sec, now=None):
    """Next timestamp strictly after now that is a clean multiple of interval_sec from local midnight."""
    now = int(now if now is not None else time.time())
    return now + interval_sec - ((now - local_midnight(now)) % interval_sec)


def interval_sec(window):
    return {"interval": COMPILE_MINUTES * 60, "daily": 86400, "weekly": WEEKLY_DAYS * 86400}[window]


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


# ================================================================== windows & state
DISK_ARRAYS = ["SUM_UTIL", "SAMPLE_COUNT", "OVER_SAMPLES", "OVER_MS", "OVER_EVENTS", "PREV_OVER"]
STREAM_ARRAYS = ["CTR_SUM", "CTR_PEAK", "CTR_OVER_SUM", "DISKCTR_SUM", "DISK_STREAM_SAMPLES"]
TRK_ARRAYS = ["TRK_ACTIVE", "TRK_SPEED", "TRK_BYTES", "TRK_TIER", "TRK_LIMITED", "TRK_ATCAP"]
TIER_ARRAYS = ["TIER_ACTIVE", "TIER_SPEED", "TIER_BYTES", "TIER_BYTES_LT", "TIER_BYTES_GE",
               "TIER_LIMITED", "TIER_ATCAP", "TIER_CROSSED"]
QBIT_ARRAYS = ["QBIT_SUM_SPEED", "QBIT_SUM_ACTIVE", "QBIT_SAMPLE_COUNT",
               "QBIT_OVER_SUM_SPEED", "QBIT_OVER_SUM_ACTIVE", "QBIT_OVER_SAMPLE_COUNT"]
ARRAY_KEYS = DISK_ARRAYS + STREAM_ARRAYS + TRK_ARRAYS + TIER_ARRAYS + QBIT_ARRAYS
SCALAR_KEYS = (["AGG_SUM", "AGG_SAMPLES", "DOWNTIME_SEC", "STREAM_SAMPLES", "STREAM_OVER_SAMPLES",
                "SAMPLED_MS", "MOVER_MS", "QB_BYTES", "TORRENT_BYTES", "FRESH_BYTES", "ARRAY_BYTES",
                "POOL_BYTES", "UNKNOWN_BYTES", "QB_PEERS_SUM", "QB_SWARM_SUM", "NEW_COUNT", "NEW_SIZE",
                "CORR_N", "CORR_SU", "CORR_SUU"]
               + [f"CORR_{a}_{b}" for b in ("SPD", "ACT", "PEER") for a in ("S", "SS", "SX")]
               + ["QBIT_SUM_SPEED_TOTAL", "QBIT_SUM_ACTIVE_TOTAL", "QBIT_SAMPLE_COUNT_TOTAL",
                  "QBIT_OVER_SUM_SPEED_TOTAL", "QBIT_OVER_SUM_ACTIVE_TOTAL", "QBIT_OVER_SAMPLE_COUNT_TOTAL"])
WINDOW_NAMES = ["interval", "daily", "weekly", "test"]
BASH_PREFIX = {"interval": "", "daily": "DAILY_", "weekly": "WEEKLY_", "test": "TEST_"}
BASH_START = {"interval": "WINDOW_START", "daily": "DAILY_START", "weekly": "WEEKLY_START", "test": "TEST_START"}
BASH_NEXT = {"interval": "NEXT_INTERVAL_BOUNDARY", "daily": "NEXT_DAILY_BOUNDARY", "weekly": "NEXT_WEEKLY_BOUNDARY"}


def new_window(start):
    w = {k: {} for k in ARRAY_KEYS}
    w.update({k: 0 for k in SCALAR_KEYS})
    w["START"] = int(start)
    return w


def fix_window(w, now):
    for k in ARRAY_KEYS:
        if not isinstance(w.get(k), dict):
            w[k] = {}
    for k in SCALAR_KEYS:
        w[k] = to_int(w.get(k, 0))
    w["START"] = to_int(w.get("START"), now) or now
    return w


def add(d, key, val):
    d[key] = d.get(key, 0) + val


ST = {}          # persisted state
NOW = int(time.time())
LAST_SAVE = 0


def wins():
    return ST["windows"].values()


def fresh_state(now):
    return {"version": 2,
            "cfg": {"compile_minutes": COMPILE_MINUTES, "weekly_days": WEEKLY_DAYS},
            "test_name": TEST_NAME,
            "windows": {w: new_window(now) for w in WINDOW_NAMES},
            "next": {w: next_boundary(interval_sec(w), now) for w in BASH_NEXT},
            "last_notify": now, "last_sample_ts": now, "alltime_prev": {}}


# ---- migration from the bash version's `declare -p` state file
_DECL = re.compile(r"^declare\s+-(\S+)\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_ITEM = re.compile(r'\[("(?:[^"\\]|\\.)*"|[^\]]*)\]=("(?:[^"\\]|\\.)*"|[^\s)]*)')
_PLAIN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _unquote(s):
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return re.sub(r"\\(.)", r"\1", s)


def _shell_word(v):
    try:
        parts = shlex.split(v)
        return parts[0] if parts else ""
    except ValueError:
        return v.strip("\"'")


def parse_bash_state(path):
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = _DECL.match(line)
            if m:
                flags, name, val = m.groups()
                if "A" in flags or "a" in flags:
                    out[name] = {_unquote(k): _unquote(v) for k, v in _ITEM.findall(val)}
                else:
                    out[name] = _shell_word(val)
                continue
            m = _PLAIN.match(line)
            if m:
                out[m.group(1)] = _shell_word(m.group(2))
    return out


def migrate_bash_state(path, now):
    data = parse_bash_state(path)
    st = fresh_state(now)
    for w in WINDOW_NAMES:
        p = BASH_PREFIX[w]
        win = new_window(to_int(data.get(BASH_START[w]), now) or now)
        for k in ARRAY_KEYS:
            src = data.get(p + k)
            if isinstance(src, dict):
                win[k] = {kk: (vv if k == "TRK_TIER" else to_int(vv)) for kk, vv in src.items()}
        for k in SCALAR_KEYS:
            if p + k in data:
                win[k] = to_int(data[p + k])
        if not isinstance(data.get(p + "OVER_MS"), dict):   # oldest format counted samples only
            win["OVER_MS"] = {d: n * SAMPLE_SECONDS * 1000 for d, n in win["OVER_SAMPLES"].items()}
        st["windows"][w] = win
    test_name = data.get("STATE_TEST_NAME", "")
    if not test_name:   # pre-test versions: seed the test from the current rollup period
        st["windows"]["test"] = json.loads(json.dumps(st["windows"]["weekly"]))
    st["test_name"] = test_name or TEST_NAME
    for w, var in BASH_NEXT.items():
        st["next"][w] = to_int(data.get(var)) or next_boundary(interval_sec(w), now)
    st["last_notify"] = to_int(data.get("LAST_NOTIFY")) or now
    st["last_sample_ts"] = to_int(data.get("LAST_SAMPLE_TS")) or int(os.path.getmtime(path))
    prev = data.get("QB_ALLTIME_PREV")
    st["alltime_prev"] = {k: to_int(v) for k, v in prev.items()} if isinstance(prev, dict) else {}
    st["cfg"] = {"compile_minutes": to_int(data.get("STATE_CFG_COMPILE_MINUTES")) or None,
                 "weekly_days": to_int(data.get("STATE_CFG_WEEKLY_DAYS")) or None}
    return st, not test_name


def save_state():
    global LAST_SAVE
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        ST["cfg"] = {"compile_minutes": COMPILE_MINUTES, "weekly_days": WEEKLY_DAYS}
        tmp = STATE_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ST, f, separators=(",", ":"))
        os.replace(tmp, STATE_JSON)
        save_qbit_state()
    except OSError as e:
        log(f"State save failed: {e}")
    LAST_SAVE = int(time.time())


def load_state(now, write_migration=True):
    """Returns (state, restored, seeded_test). Migrates a bash state.sh when no state.json exists."""
    if os.path.exists(STATE_JSON):
        try:
            with open(STATE_JSON, encoding="utf-8") as f:
                st = json.load(f)
            return st, True, False
        except (OSError, ValueError) as e:
            log(f"State file {STATE_JSON} unreadable ({e}) - starting fresh.")
            return fresh_state(now), False, False
    if os.path.exists(LEGACY_STATE):
        try:
            st, seeded = migrate_bash_state(LEGACY_STATE, now)
        except (OSError, ValueError) as e:
            log(f"Could not migrate {LEGACY_STATE} ({e}) - starting fresh.")
            return fresh_state(now), False, False
        if write_migration:
            if time.time() - os.path.getmtime(LEGACY_STATE) < 120:
                log("Warning: state.sh changed in the last 2 minutes - make sure the old bash version is stopped.")
            with open(STATE_JSON + ".tmp", "w", encoding="utf-8") as f:
                json.dump(st, f, separators=(",", ":"))
            os.replace(STATE_JSON + ".tmp", STATE_JSON)
            os.replace(LEGACY_STATE, LEGACY_STATE + ".migrated")
            log("Migrated bash state.sh to state.json (original kept as state.sh.migrated).")
        return st, True, seeded
    return fresh_state(now), False, False


# ================================================================== disks
FRIENDLY = {}
DEVICES = []
DISKNAME_TO_DEV = {}


def load_friendly_names():
    current = None
    try:
        with open(DISKS_INI, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                m = re.match(r'^\["(.+)"\]$', line)
                if m:
                    current = m.group(1)
                    continue
                m = re.match(r'^device="(.+)"$', line)
                if m and current:
                    FRIENDLY[m.group(1)] = current
    except OSError:
        pass
    for dev, name in FRIENDLY.items():
        DISKNAME_TO_DEV[name] = dev


def fname(dev):
    return FRIENDLY.get(dev, dev)


def discover_devices():
    name_filter = MONITOR_NAME_REGEX
    if name_filter and not FRIENDLY:
        log(f"Unraid disk names unavailable ({DISKS_INI} missing?) - monitoring all disks unfiltered.")
        name_filter = ""
    found = []
    try:
        entries = sorted(os.listdir(SYS_BLOCK_DIR))
    except OSError:
        entries = []
    for name in entries:
        if EXCLUDE_DEVICES and re.match(f"^({EXCLUDE_DEVICES})", name):
            continue
        if os.path.exists(os.path.join(SYS_BLOCK_DIR, name, "partition")):
            continue
        if name_filter and not re.search(name_filter, fname(name)):
            continue
        found.append(name)
    return sorted(found, key=lambda d: natural_key(fname(d)))


def snapshot():
    """Field 13 of /proc/diskstats = ms spent doing I/O (basis of iostat %util)."""
    snap = {}
    try:
        with open(DISKSTATS_FILE) as f:
            for line in f:
                p = line.split()
                if len(p) > 12 and p[2] in DEV_SET:
                    snap[p[2]] = int(p[12])
    except (OSError, ValueError):
        pass
    return snap


def mover_running():
    try:
        pids = [p for p in os.listdir(PROC_DIR) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(os.path.join(PROC_DIR, pid, "comm")) as f:
                if f.read().strip() in MOVER_NAMES:
                    return True
        except OSError:
            continue
    return False


def acc_disk(w, dev, util, elapsed_ms):
    add(w["SUM_UTIL"], dev, util)
    add(w["SAMPLE_COUNT"], dev, 1)
    if util >= THRESHOLD:
        add(w["OVER_SAMPLES"], dev, 1)
        add(w["OVER_MS"], dev, elapsed_ms)
        if not w["PREV_OVER"].get(dev):
            add(w["OVER_EVENTS"], dev, 1)
        w["PREV_OVER"][dev] = 1
    else:
        w["PREV_OVER"][dev] = 0


# ================================================================== qBittorrent via Qui
KEEP_FIELDS = ("tracker", "tags", "uploaded", "upspeed", "up_limit", "ratio", "size", "added_on",
               "content_path", "num_leechs", "num_incomplete")
QB = {}          # label -> {"rid", "torrents", "alltime", "baseline"}
QB_LAST = {}     # label -> results of the latest sample
LOC_CACHE = {}
TRKNAME_CACHE = {}
_TIER_RE = re.compile(re.sub(r"\(\?<(?=[A-Za-z_])", "(?P<", TIER_TAG_REGEX), re.IGNORECASE)
_TIER_CACHE = {}


def qbit_fetch(url, timeout=25):
    try:
        r = subprocess.run(["docker", "exec", QUI_CONTAINER, "wget", "-qO-", "-T", "20", url],
                           capture_output=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def tracker_host(url):
    """Hostname only - passkeys in the announce URL are discarded immediately."""
    h = re.sub(r"^[A-Za-z0-9+.-]+://", "", url or "")
    return re.sub(r"[/:?].*$", "", h).lower() or "-"


def tier_of(tags):
    if tags in _TIER_CACHE:
        return _TIER_CACHE[tags]
    tier = "none"
    for tag in (tags or "").split(","):
        m = _TIER_RE.search(tag.strip())
        if m:
            tier = m.groupdict().get("n") or (m.group(1) if m.groups() else tag.strip())
            break
    _TIER_CACHE[tags] = tier
    return tier


def _trim(t):
    out = {k: t[k] for k in KEEP_FIELDS if k in t}
    if "tracker" in out:
        out["tracker"] = tracker_host(out["tracker"])
    return out


def inst_file(label):
    return os.path.join(WORK_DIR, "qbit_" + re.sub(r"[^A-Za-z0-9_-]", "_", label) + ".json")


def load_qbit_state():
    for inst in INSTANCES:
        st = {"rid": 0, "torrents": {}, "alltime": -1, "baseline": False}
        try:
            with open(inst_file(inst["label"]), encoding="utf-8") as f:
                saved = json.load(f)
            st["torrents"] = saved.get("torrents", {})
            st["baseline"] = True
        except (OSError, ValueError):
            pass
        QB[inst["label"]] = st


def save_qbit_state():
    if not QBIT_ENABLED:
        return
    os.makedirs(WORK_DIR, exist_ok=True)
    for label, st in QB.items():
        if st["baseline"]:
            tmp = inst_file(label) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"torrents": st["torrents"]}, f, separators=(",", ":"))
            os.replace(tmp, inst_file(label))


def process_maindata(st, data, now):
    """Merges a sync/maindata response; returns this sample's summary and per-torrent records."""
    full = bool(data.get("full_update"))
    first = not st["baseline"]
    old = st["torrents"]
    delta = data.get("torrents") or {}
    prevs = {}
    if full:
        new = {h: _trim(t) for h, t in delta.items()}
        prevs = {h: old.get(h) for h in new}
    else:
        new = old
        for h, t in delta.items():
            p = old.get(h)
            prevs[h] = dict(p) if p is not None else None
            if p is None:
                new[h] = _trim(t)
            else:
                p.update(_trim(t))
    for h in data.get("torrents_removed") or []:
        new.pop(h, None)
    st["torrents"] = new
    st["rid"] = data.get("rid", 0)
    ss = data.get("server_state") or {}
    if "alltime_ul" in ss:
        st["alltime"] = to_int(ss["alltime_ul"], -1)
    elif full:
        st["alltime"] = -1

    speed = active = peers = swarm = 0
    fresh_after = now - NEW_TORRENT_DAYS * 86400
    records = []
    for h, t in new.items():
        sp = to_int(t.get("upspeed"))
        speed += sp
        if sp > 0:
            active += 1
        peers += to_int(t.get("num_leechs"))
        swarm += max(0, to_int(t.get("num_incomplete")))
        bytes_up, crossed, is_new = 0, False, False
        if h in prevs:
            p = prevs[h]
            if p is None:
                is_new = not first
            else:
                bytes_up = max(0, to_int(t.get("uploaded")) - to_int(p.get("uploaded")))
                crossed = (p.get("ratio") or 0) < RATIO_SPLIT <= (t.get("ratio") or 0)
        if not (bytes_up > 0 or sp > 0 or crossed or is_new):
            continue
        limit = to_int(t.get("up_limit"), -1)
        records.append({
            "hash": h, "host": t.get("tracker") or "-", "tier": tier_of(t.get("tags")),
            "bytes": bytes_up, "speed": sp, "limited": limit > 0,
            "atcap": limit > 0 and sp > 0 and sp >= 0.9 * limit,
            "rge": (t.get("ratio") or 0) >= RATIO_SPLIT,
            "fresh": to_int(t.get("added_on")) >= fresh_after,
            "crossed": crossed, "new": is_new, "size": to_int(t.get("size")),
            "path": t.get("content_path") or ""})
    st["baseline"] = True
    return {"ok": True, "speed": speed, "active": active, "peers": peers, "swarm": swarm,
            "alltime": st["alltime"], "records": records}


def qbit_collect():
    now = int(time.time())
    for inst in INSTANCES:
        label = inst["label"]
        st = QB.setdefault(label, {"rid": 0, "torrents": {}, "alltime": -1, "baseline": False})
        QB_LAST[label] = {"ok": False}
        raw = qbit_fetch(f"{inst['url']}/api/v2/sync/maindata?rid={st['rid']}")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            st["rid"] = 0
            continue
        if isinstance(data, dict):
            QB_LAST[label] = process_maindata(st, data, now)


QUI_NAMES = {}   # tracker domain -> display name, from Qui's tracker customizations


def qui_base():
    for inst in INSTANCES:
        if "/proxy/" in inst["url"]:
            return inst["url"].split("/proxy/")[0]
    return "http://localhost:7476"


def refresh_qui_names():
    """Loads tracker display names (and merged domains) from Qui's tracker customizations.
    Needs QUI_API_KEY; keeps the previous names if Qui can't be read."""
    if not QUI_API_KEY:
        return
    url = qui_base() + "/api/tracker-customizations"
    try:
        r = subprocess.run(["docker", "exec", QUI_CONTAINER, "wget", "-qO-", "-T", "20",
                            "--header", f"X-API-Key: {QUI_API_KEY}", url],
                           capture_output=True, timeout=25)
        data = json.loads(r.stdout) if r.returncode == 0 and r.stdout else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        data = None
    if isinstance(data, dict):
        data = data.get("data") or data.get("customizations") or data.get("items")
    if not isinstance(data, list):
        log("Could not read Qui tracker customizations - check QUI_API_KEY. Keeping current tracker names.")
        return
    names = {}
    for c in data:
        if not isinstance(c, dict):
            continue
        disp = c.get("displayName") or c.get("display_name") or c.get("name")
        for d in c.get("domains") or []:
            if disp and isinstance(d, str) and d.strip():
                names[d.strip().lower()] = str(disp)
    if names != QUI_NAMES:
        QUI_NAMES.clear()
        QUI_NAMES.update(names)
        TRKNAME_CACHE.clear()
        log(f"Loaded {len(set(names.values()))} tracker names from Qui ({len(names)} domains).")


def tracker_name(host):
    """Display name: TRACKER_ALIASES, then Qui's tracker customizations, then derived from the hostname."""
    if host in TRKNAME_CACHE:
        return TRKNAME_CACHE[host]
    name = ""
    for a in TRACKER_ALIASES:
        sub, _, disp = a.partition("|")
        if sub and sub in host:
            name = disp
            break
    if not name and QUI_NAMES:
        parts = host.split(".")
        for i in range(len(parts) - 1):   # tracker.example.org also matches a Qui domain of example.org
            suffix = ".".join(parts[i:])
            if suffix in QUI_NAMES:
                name = QUI_NAMES[suffix]
                break
    if not name:
        parts = host.split(".")
        if host == "-":
            name = "(no tracker)"
        elif re.fullmatch(r"[0-9.]+", host):
            name = host
        elif len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac") and len(parts[-1]) == 2:
            name = parts[-3]
        elif len(parts) >= 2:
            name = parts[-2]
        else:
            name = host
    TRKNAME_CACHE[host] = name
    return name


def location_of(inst, h, cpath):
    """'pool', 'array' or 'unknown'. Only pool paths are checked, so array disks are never woken.
    Cached per torrent; cleared when mover finishes and at midnight."""
    key = (inst["label"], h)
    if key in LOC_CACHE:
        return LOC_CACHE[key]
    hp = ""
    for m in inst["map"].split(";"):
        c, _, host = m.partition("=")
        c = c.rstrip("/")
        if c and (cpath == c or cpath.startswith(c + "/")):
            hp = host.rstrip("/") + cpath[len(c):]
            break
    loc = "unknown"
    user = MNT_ROOT + "/user/"
    if hp.startswith(user):
        rel = hp[len(user):]
        loc = "pool" if any(os.path.exists(os.path.join(MNT_ROOT, p, rel)) for p in POOL_NAMES) else "array"
    elif re.match(re.escape(MNT_ROOT) + r"/disk[0-9]+/", hp):
        loc = "array"
    elif hp and any(hp.startswith(f"{MNT_ROOT}/{p}/") for p in POOL_NAMES):
        loc = "pool"
    LOC_CACHE[key] = loc
    return loc


def acc_qbit(over_any, disk_valid, array_util):
    oks = [i for i in INSTANCES if QB_LAST.get(i["label"], {}).get("ok")]
    if not oks:
        return
    tot_speed = tot_active = peers = swarm = alltime_delta = 0
    s = {k: {} for k in TRK_ARRAYS + TIER_ARRAYS}
    tb = fb = ab = pb = ub = new_count = new_size = 0
    for inst in oks:
        label = inst["label"]
        r = QB_LAST[label]
        for w in wins():
            add(w["QBIT_SUM_SPEED"], label, r["speed"])
            add(w["QBIT_SUM_ACTIVE"], label, r["active"])
            add(w["QBIT_SAMPLE_COUNT"], label, 1)
            if over_any:
                add(w["QBIT_OVER_SUM_SPEED"], label, r["speed"])
                add(w["QBIT_OVER_SUM_ACTIVE"], label, r["active"])
                add(w["QBIT_OVER_SAMPLE_COUNT"], label, 1)
        tot_speed += r["speed"]
        tot_active += r["active"]
        peers += r["peers"]
        swarm += r["swarm"]
        if r["alltime"] >= 0:
            prev = ST["alltime_prev"].get(label)
            if prev is not None and r["alltime"] > prev:
                alltime_delta += r["alltime"] - prev
            ST["alltime_prev"][label] = r["alltime"]
        for rec in r["records"]:
            if rec["new"]:
                new_count += 1
                new_size += rec["size"]
            b, sp = rec["bytes"], rec["speed"]
            if not (b > 0 or sp > 0 or rec["crossed"]):
                continue
            trk, tier = tracker_name(rec["host"]), rec["tier"]
            tb += b
            if rec["fresh"]:
                fb += b
            if b > 0:
                loc = location_of(inst, rec["hash"], rec["path"])
                if loc == "pool":
                    pb += b
                elif loc == "array":
                    ab += b
                else:
                    ub += b
            add(s["TRK_ACTIVE"], trk, 1 if sp > 0 else 0)
            add(s["TRK_SPEED"], trk, sp)
            add(s["TRK_BYTES"], trk, b)
            s["TRK_TIER"][trk] = tier
            if sp > 0 and rec["limited"]:
                add(s["TRK_LIMITED"], trk, 1)
                add(s["TIER_LIMITED"], tier, 1)
            if rec["atcap"]:
                add(s["TRK_ATCAP"], trk, 1)
                add(s["TIER_ATCAP"], tier, 1)
            add(s["TIER_ACTIVE"], tier, 1 if sp > 0 else 0)
            add(s["TIER_SPEED"], tier, sp)
            add(s["TIER_BYTES"], tier, b)
            add(s["TIER_BYTES_GE" if rec["rge"] else "TIER_BYTES_LT"], tier, b)
            if rec["crossed"]:
                add(s["TIER_CROSSED"], tier, 1)
    kb = tot_speed // 1024
    for w in wins():
        w["QBIT_SUM_SPEED_TOTAL"] += tot_speed
        w["QBIT_SUM_ACTIVE_TOTAL"] += tot_active
        w["QBIT_SAMPLE_COUNT_TOTAL"] += 1
        if over_any:
            w["QBIT_OVER_SUM_SPEED_TOTAL"] += tot_speed
            w["QBIT_OVER_SUM_ACTIVE_TOTAL"] += tot_active
            w["QBIT_OVER_SAMPLE_COUNT_TOTAL"] += 1
        w["QB_BYTES"] += alltime_delta
        w["TORRENT_BYTES"] += tb
        w["FRESH_BYTES"] += fb
        w["ARRAY_BYTES"] += ab
        w["POOL_BYTES"] += pb
        w["UNKNOWN_BYTES"] += ub
        w["QB_PEERS_SUM"] += peers
        w["QB_SWARM_SUM"] += swarm
        w["NEW_COUNT"] += new_count
        w["NEW_SIZE"] += new_size
        for k, vals in s.items():
            for key, v in vals.items():
                if k == "TRK_TIER":
                    w[k][key] = v
                else:
                    add(w[k], key, v)
        if disk_valid:
            u = array_util
            w["CORR_N"] += 1
            w["CORR_SU"] += u
            w["CORR_SUU"] += u * u
            for suffix, x in (("SPD", kb), ("ACT", tot_active), ("PEER", peers)):
                w[f"CORR_S_{suffix}"] += x
                w[f"CORR_SS_{suffix}"] += x * x
                w[f"CORR_SX_{suffix}"] += x * u


# ================================================================== array streams (lsof)
CID_NAME = {}
SNAP_CTR = {}
SNAP_DISKCTR = {}
STREAM_TRIES = 0
STREAM_FAILS = 0


def refresh_container_names():
    try:
        r = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                           capture_output=True, text=True, timeout=20)
        for line in r.stdout.splitlines():
            cid, _, name = line.partition(" ")
            if cid:
                CID_NAME[cid] = name
    except (subprocess.TimeoutExpired, OSError):
        pass


def resolve_owner(pid, cmd):
    """Container name, '(host) <process>', or '(exited) <command>' if it ended before lookup."""
    try:
        with open(os.path.join(PROC_DIR, pid, "cgroup")) as f:
            m = re.search(r"[0-9a-f]{64}", f.read())
    except OSError:
        return f"(exited) {cmd or 'unknown'}"
    if m:
        cid = m.group(0)
        if cid not in CID_NAME:
            refresh_container_names()
            CID_NAME.setdefault(cid, cid[:12])   # don't re-query unknown ids
        return CID_NAME[cid]
    try:
        with open(os.path.join(PROC_DIR, pid, "comm")) as f:
            return "(host) " + f.read().strip()
    except OSError:
        return f"(host) {cmd or 'pid ' + pid}"


def stream_snapshot():
    """Fills SNAP_CTR[owner] = streams and SNAP_DISKCTR['dev|owner'] = streams on that disk.
    Stream keys come from /mnt/diskN/<share>/a/b/c paths (mostly held by shfs); each non-shfs
    open file is matched to streams by the stream's deepest folder name."""
    global STREAM_TRIES, STREAM_FAILS
    SNAP_CTR.clear()
    SNAP_DISKCTR.clear()
    STREAM_TRIES += 1
    cmd = []
    if shutil.which("nice"):
        cmd += ["nice", "-n", "19"]
    if shutil.which("ionice"):   # lowest best-effort priority, NOT idle: idle starves during spikes
        cmd += ["ionice", "-c2", "-n7"]
    cmd += ["lsof", "-Fpcn", os.path.join(MNT_ROOT, "user")] + sorted(glob.glob(os.path.join(MNT_ROOT, "disk*")))
    t0 = mono_ms()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=LSOF_TIMEOUT,
                           env={**os.environ, "LANG": "en_US.UTF8"})
    except subprocess.TimeoutExpired:
        STREAM_FAILS += 1
        log(f"Stream snapshot skipped: lsof exceeded {LSOF_TIMEOUT}s.")
        return False
    except OSError:
        STREAM_FAILS += 1
        return False
    took = mono_ms() - t0
    if took > 10000:
        log(f"Stream snapshot slow: lsof took {took // 1000}s.")

    shares = set(STREAM_SHARES)
    stream_disks = {}
    proc_files = set()
    cmds = {}
    pid = cmd_name = None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            pid, cmd_name = val, None
        elif tag == "c":
            cmd_name = val
            cmds[pid] = val
        elif tag == "n":
            a = val.split("/")
            if len(a) > 4 and a[1] == "mnt" and re.fullmatch(r"disk[0-9]+", a[2]) and a[3] in shares and a[4]:
                key = "/".join(x for x in a[4:7] if x)
                stream_disks.setdefault(key, set()).add(a[2])
            if cmd_name != "shfs":
                proc_files.add((pid, val))
    leaves = {k: k.rsplit("/", 1)[-1] for k in stream_disks}
    seen_ck, seen_dk, owners = set(), set(), {}
    for p, path in proc_files:
        for key, leaf in leaves.items():
            if f"/{leaf}/" in path or path.endswith("/" + leaf):
                if p not in owners:
                    owners[p] = resolve_owner(p, cmds.get(p))
                owner = owners[p]
                if (owner, key) not in seen_ck:
                    seen_ck.add((owner, key))
                    add(SNAP_CTR, owner, 1)
                for disk in stream_disks[key]:
                    dev = DISKNAME_TO_DEV.get(disk)
                    if dev and (dev, owner, key) not in seen_dk:
                        seen_dk.add((dev, owner, key))
                        add(SNAP_DISKCTR, f"{dev}|{owner}", 1)
    return True


def acc_streams(w, over_any, over_devs):
    w["STREAM_SAMPLES"] += 1
    if over_any:
        w["STREAM_OVER_SAMPLES"] += 1
    for owner, n in SNAP_CTR.items():
        add(w["CTR_SUM"], owner, n)
        if n > w["CTR_PEAK"].get(owner, 0):
            w["CTR_PEAK"][owner] = n
        if over_any:
            add(w["CTR_OVER_SUM"], owner, n)
    for dev in over_devs:   # per-disk attribution only counts samples where that disk was over
        add(w["DISK_STREAM_SAMPLES"], dev, 1)
        for k, n in SNAP_DISKCTR.items():
            if k.startswith(dev + "|"):
                add(w["DISKCTR_SUM"], k, n)


# ================================================================== Discord
def _clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def send_embed(title, description, color, fields=(), footer=""):
    flds = [{"name": _clip(n, 256), "value": _clip(v or "-", 1024), "inline": bool(i)} for n, v, i in fields][:25]
    embed = {"title": _clip(title, 256), "description": _clip(description, 4096), "color": color,
             "fields": flds, "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if footer:
        embed["footer"] = {"text": _clip(footer, 2048)}

    def size():
        return (len(embed["title"]) + len(embed["description"]) + len(footer or "")
                + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]))
    while size() > 6000 and embed["fields"]:
        embed["fields"].pop()
    body = json.dumps({"embeds": [embed]}).encode()
    code, detail = None, ""
    for _ in range(3):
        req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": f"diskio-monitor/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                return True
        except urllib.error.HTTPError as e:
            code = e.code
            detail = e.read()[:200].decode("utf-8", "replace")
            if code == 429:
                try:
                    wait = float(json.loads(detail).get("retry_after", 2))
                except ValueError:
                    wait = 2
                time.sleep(wait + 1)
                continue
            if code in (400, 401, 403, 404):
                break
            time.sleep(5)
        except (urllib.error.URLError, OSError) as e:
            detail = str(e)
            time.sleep(5)
    log(f"Discord send FAILED (HTTP {code or 'none'}) for '{title}': {detail}")
    return False


# ================================================================== report building
def agg_avg(w):
    return avg(w["AGG_SUM"], w["AGG_SAMPLES"])


def footer_text(window_name, w):
    text = f"Aggregate avg: {agg_avg(w)}% • Threshold: {THRESHOLD}%"
    if w["MOVER_MS"] >= 60000:
        text += f" • 🚚 {fmt_duration(w['MOVER_MS'] // 1000)} mover"
    if w["DOWNTIME_SEC"] >= DOWNTIME_REPORT_MIN_SEC:
        text += f" • ⚠️ {fmt_duration(w['DOWNTIME_SEC'])} offline"
    if window_name != "interval":
        day = (NOW - ST["windows"]["test"]["START"]) // 86400 + 1
        text += f" • 🧪 {ST['test_name']} day {day}"
    return text


def period_label(w):
    s, e = w["START"], max(w["START"], NOW - 60)
    sd = datetime.fromtimestamp(s).strftime("%a %b %d")
    ed = datetime.fromtimestamp(e).strftime("%a %b %d")
    return datetime.fromtimestamp(e).strftime("%A %b %d") if sd == ed else f"{sd} – {ed}"


def disk_top_owners(w, dev):
    n = w["DISK_STREAM_SAMPLES"].get(dev, 0)
    if not STREAMS_ENABLED or not n:
        return ""
    items = [(v, k.split("|", 1)[1]) for k, v in w["DISKCTR_SUM"].items() if k.startswith(dev + "|")]
    items.sort(key=lambda x: -x[0])
    return " • ".join(f"{o} {v / n:.1f}" for v, o in items[:3])


def disk_fields(w, mode):
    """mode 'interval': avg + time over threshold. mode 'summary': adds stream owners on hot disks.
    With more than 14 disks, quiet disks are merged into one field (Discord allows 25 fields)."""
    fields, quiet, triggered = [], [], False
    compact = len(DEVICES) > 14
    sampled = w["SAMPLED_MS"]
    for dev in DEVICES:
        c = w["SAMPLE_COUNT"].get(dev, 0)
        if not c:
            continue
        a = avg(w["SUM_UTIL"].get(dev, 0), c)
        over_ms = w["OVER_MS"].get(dev, 0)
        name = f"{fname(dev)} ({dev})"
        if w["OVER_SAMPLES"].get(dev, 0) > 0:
            triggered = True
            share = 100 * over_ms / sampled if sampled else 0
            value = f"avg {a}%\n{share:.1f}% of time ≥{THRESHOLD}% ({fmt_minutes(over_ms)})"
            if mode == "summary":
                top = disk_top_owners(w, dev)
                if top:
                    value += f"\n📂 {top}"
            fields.append((f"🔴 {name}", value, True))
        elif compact:
            quiet.append(f"{fname(dev)} {a}%")
        else:
            fields.append((name, f"avg {a}%" + ("" if mode == "interval" else f"\nnever ≥{THRESHOLD}%"), True))
    if quiet:
        fields.append(("Other disks (avg util)", " • ".join(quiet), False))
    return fields, triggered


def qbit_compact_field(w):
    """Interval alerts: one short qBittorrent field."""
    n = w["QBIT_SAMPLE_COUNT_TOTAL"]
    if not QBIT_ENABLED:
        return []
    if not n:
        return [("📤 qBittorrent", "no data (check Qui/proxy keys)", False)]
    lines = []
    for inst in INSTANCES:
        lb = inst["label"]
        c = w["QBIT_SAMPLE_COUNT"].get(lb, 0)
        lines.append(f"{lb}: {mbps(w['QBIT_SUM_SPEED'].get(lb, 0) / c)} MB/s • {avg(w['QBIT_SUM_ACTIVE'].get(lb, 0), c)} active"
                     if c else f"{lb}: no data")
    lines.append(f"**Combined: {mbps(w['QBIT_SUM_SPEED_TOTAL'] / n)} MB/s • {avg(w['QBIT_SUM_ACTIVE_TOTAL'], n)} active**")
    no = w["QBIT_OVER_SAMPLE_COUNT_TOTAL"]
    if no:
        lines.append(f"During ≥{THRESHOLD}%: {mbps(w['QBIT_OVER_SUM_SPEED_TOTAL'] / no)} MB/s • "
                     f"{avg(w['QBIT_OVER_SUM_ACTIVE_TOTAL'], no)} active")
    return [("📤 qBittorrent", "\n".join(lines), False)]


def qbit_detail_fields(w):
    if not QBIT_ENABLED:
        return []
    out = []
    for infix, label in (("", "📤 qBittorrent (overall)"), ("OVER_", f"📤 qBittorrent (during ≥{THRESHOLD}% I/O)")):
        n = w[f"QBIT_{infix}SAMPLE_COUNT_TOTAL"]
        if not n:
            out.append((label, "No threshold crossings this period" if infix else "no data (check Qui/proxy keys)", False))
            continue
        lines = []
        for inst in INSTANCES:
            lb = inst["label"]
            c = w[f"QBIT_{infix}SAMPLE_COUNT"].get(lb, 0)
            lines.append(f"{lb}: avg {mbps(w[f'QBIT_{infix}SUM_SPEED'].get(lb, 0) / c)} MB/s • "
                         f"avg {avg(w[f'QBIT_{infix}SUM_ACTIVE'].get(lb, 0), c)} active" if c else f"{lb}: no data")
        lines.append(f"\nCombined avg: {mbps(w[f'QBIT_{infix}SUM_SPEED_TOTAL'] / n)} MB/s • "
                     f"{avg(w[f'QBIT_{infix}SUM_ACTIVE_TOTAL'], n)} active")
        out.append((label, "\n".join(lines), False))
    return out


def upload_field(w):
    if not QBIT_ENABLED:
        return []
    if not w["QBIT_SAMPLE_COUNT_TOTAL"]:
        return [("📦 Upload", "no data this period", False)]
    tb = w["TORRENT_BYTES"]
    total = w["QB_BYTES"] or tb
    v = f"{fmt_gb(total)} uploaded"
    if tb:
        v += f"\nFrom array {pct(w['ARRAY_BYTES'], tb)}% • pool {pct(w['POOL_BYTES'], tb)}%"
        if w["UNKNOWN_BYTES"]:
            v += f" • unknown {pct(w['UNKNOWN_BYTES'], tb)}%"
        v += f"\nFrom torrents <{NEW_TORRENT_DAYS}d old: {pct(w['FRESH_BYTES'], tb)}%"
    v += f"\n🆕 {w['NEW_COUNT']} new torrents • {fmt_gb(w['NEW_SIZE'])}"
    return [("📦 Upload", v, False)]


def tier_field(w):
    n = w["QBIT_SAMPLE_COUNT_TOTAL"]
    if not QBIT_ENABLED or not n:
        return []
    tb = w["TORRENT_BYTES"]
    keys = set(w["TIER_ACTIVE"]) | set(w["TIER_BYTES"]) | set(w["TIER_CROSSED"])
    lines = []
    for t in sorted(keys, key=lambda k: (k == "none", natural_key(k))):
        name = "No tier" if t == "none" else f"Tier {t}"
        b = w["TIER_BYTES"].get(t, 0)
        lines.append(f"**{name}**: {pct(b, tb)}% of upload • {avg(w['TIER_ACTIVE'].get(t, 0), n)} active • "
                     f"{mbps(w['TIER_SPEED'].get(t, 0) / n)} MB/s")
        extra = []
        if b:
            extra.append(f"<{RATIO_TXT}: {pct(w['TIER_BYTES_LT'].get(t, 0), b)}% • "
                         f"≥{RATIO_TXT}: {pct(w['TIER_BYTES_GE'].get(t, 0), b)}%")
        lim = w["TIER_LIMITED"].get(t, 0)
        if lim:
            extra.append(f"at cap {pct(w['TIER_ATCAP'].get(t, 0), lim)}%")
        if w["TIER_CROSSED"].get(t, 0):
            extra.append(f"{w['TIER_CROSSED'][t]} reached {RATIO_TXT}")
        if extra:
            lines.append("↳ " + " • ".join(extra))
    return [("🏷️ Upload by tier", "\n".join(lines) or "No upload activity this period", False)]


def stream_field(w):
    if not STREAMS_ENABLED:
        return []
    title = f"📂 Array streams by container ({', '.join(STREAM_SHARES)})"
    n, no = w["STREAM_SAMPLES"], w["STREAM_OVER_SAMPLES"]
    if not n:
        return [(title, "no data this period", False)]
    lines = []
    for owner, total in sorted(w["CTR_SUM"].items(), key=lambda x: -x[1])[:STREAM_TOP_N]:
        line = f"{owner}: avg {total / n:.1f} • peak {w['CTR_PEAK'].get(owner, 0)}"
        if no:
            line += f" • {w['CTR_OVER_SUM'].get(owner, 0) / no:.1f} during ≥{THRESHOLD}%"
        lines.append(line)
    return [(title, "\n".join(lines) or "No streams on the array this period", False)]


def corr_r(w, suffix):
    """Pearson r between array util and a driver; None with fewer than 30 samples."""
    n, su, suu = w["CORR_N"], w["CORR_SU"], w["CORR_SUU"]
    sx, sxx, sxu = w[f"CORR_S_{suffix}"], w[f"CORR_SS_{suffix}"], w[f"CORR_SX_{suffix}"]
    if n < 30:
        return None
    d = (n * sxx - sx * sx) * (n * suu - su * su)
    if d <= 0:
        return None
    return round((n * sxu - sx * su) / d ** 0.5, 2)


def corr_field(w):
    if not QBIT_ENABLED:
        return []
    ra, rp, rs = corr_r(w, "ACT"), corr_r(w, "PEER"), corr_r(w, "SPD")
    if ra is None:
        return []
    f = lambda v: "-" if v is None else f"{v:.2f}"
    return [("🔗 What moves with array load (r)",
             f"Active uploads {f(ra)} • Peers {f(rp)} • Upload speed {f(rs)}\n"
             "Scale -1 to 1: nearer 1 = rises and falls with disk load", False)]


def post_tracker_message(w, label):
    n = w["QBIT_SAMPLE_COUNT_TOTAL"]
    if not QBIT_ENABLED or not n or not w["TRK_ACTIVE"]:
        return
    rows = sorted(w["TRK_ACTIVE"], key=lambda t: (w["TRK_TIER"].get(t, "none") == "none",
                                                   natural_key(w["TRK_TIER"].get(t, "none")),
                                                   -w["TRK_BYTES"].get(t, 0)))
    lines, last = [], None
    for t in rows:
        tier = w["TRK_TIER"].get(t, "none")
        if tier != last:
            if lines:
                lines.append("")
            lines.append("NO TIER" if tier == "none" else f"TIER {tier}")
            lines.append(f"{'tracker':<13} {'act':>5} {'GB':>7} {'MB/s':>6} {'cap':>4}")
            last = tier
        lim = w["TRK_LIMITED"].get(t, 0)
        cap = f"{100 * w['TRK_ATCAP'].get(t, 0) / lim:.0f}%" if lim else "-"
        lines.append(f"{t[:13]:<13} {w['TRK_ACTIVE'].get(t, 0) / n:5.1f} {w['TRK_BYTES'].get(t, 0) / 1073741824:7.1f} "
                     f"{w['TRK_SPEED'].get(t, 0) / n / 1048576:6.2f} {cap:>4}")
    body = "\n".join(lines)
    if len(body) > 3900:
        body = body[:3900].rsplit("\n", 1)[0] + "\n…"
    if send_embed(f"📡 Trackers ({label}) — {HOST}", f"{period_label(w)}\n```\n{body}\n```", COLOR_DAILY,
                  footer="act = avg actively uploading • MB/s = avg upload speed • cap = time at upload limit"):
        log(f"Sent tracker breakdown ({label}).")


def interval_report():
    global STREAM_TRIES, STREAM_FAILS
    w = ST["windows"]["interval"]
    elapsed_min = (NOW - w["START"] + 30) // 60
    fields, triggered = disk_fields(w, "interval")
    fields += qbit_compact_field(w)
    footer = footer_text("interval", w)
    snap = (f" Stream snapshots: {STREAM_TRIES - STREAM_FAILS}/{STREAM_TRIES} ok, "
            f"{w['STREAM_OVER_SAMPLES']} during >={THRESHOLD}%." if STREAMS_ENABLED else "")
    log(f"Interval window compiled ({elapsed_min} min). Aggregate avg: {agg_avg(w)}%. Triggered: {int(triggered)}.{snap}")
    STREAM_TRIES = STREAM_FAILS = 0
    hours = (NOW - ST["last_notify"]) / 3600
    if triggered:
        if send_embed(f"⚠️ Disk I/O Alert — {HOST}",
                      f"One or more disks hit {THRESHOLD}%+ in the last {elapsed_min} min.", COLOR_ALERT, fields, footer):
            log("Sent threshold alert.")
            ST["last_notify"] = NOW
    elif HEALTH_PING_HOURS > 0 and hours >= HEALTH_PING_HOURS:
        if send_embed(f"✅ Still Monitoring — {HOST}", f"All quiet - no messages in the last {hours:.1f}h.",
                      COLOR_OK, fields, footer):
            log("Sent health ping.")
            ST["last_notify"] = NOW


def summary_report(window_name, title, color, with_corr=False, note=""):
    w = ST["windows"][window_name]
    fields, _ = disk_fields(w, "summary")
    fields += qbit_detail_fields(w) + upload_field(w) + tier_field(w) + stream_field(w)
    if with_corr:
        fields += corr_field(w)
    log(f"{title} compiled.")
    desc = period_label(w) + (f"\n\n{note}" if note else "")
    if send_embed(title, desc, color, fields, footer_text(window_name, w)):
        log(f"Sent: {title}")
        ST["last_notify"] = NOW


# ================================================================== update check
def _version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def update_note():
    """A reminder line for the daily summary while a newer version is on GitHub, else ''.
    Checked once a day; any failure just skips the reminder."""
    if not UPDATE_CHECK:
        return ""
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": f"diskio-monitor/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read(65536).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        log(f"Update check failed: {e}")
        return ""
    m = re.search(r'^VERSION\s*=\s*["\']([\d.]+)["\']', text, re.MULTILINE)
    if not m:
        log("Update check: no VERSION found in the GitHub copy.")
        return ""
    latest = m.group(1)
    if _version_tuple(latest) <= _version_tuple(VERSION):
        return ""
    note = (f"🔔 **Update available:** v{VERSION} → v{latest} · [What's new]({CHANGELOG_URL})"
            f" · [How to update]({README_URL})")
    mv = re.search(r"^MEASUREMENT_VERSION\s*=\s*(\d+)", text, re.MULTILINE)
    if mv and int(mv.group(1)) > MEASUREMENT_VERSION:
        day = (NOW - ST["windows"]["test"]["START"]) // 86400 + 1
        note += (f"\n⚠️ **This update changes how results are measured.** {ST['test_name']} is on day {day}; "
                 f"updating now mixes old and new numbers in it. Consider waiting until this test ends and "
                 f"updating when you start the next one. Not sure? [Ask for guidance]({ISSUES_URL}).")
        log(f"Update available: v{VERSION} -> v{latest} (changes measurements).")
    else:
        log(f"Update available: v{VERSION} -> v{latest}.")
    return note


# ================================================================== tests
def r2(v):
    return round(v, 2)


def test_record(w, name, start, end):
    sampled = w["SAMPLED_MS"]
    if sampled <= 0:
        return None
    days = sampled / 86400000
    disks, tms, tev, ndisks = {}, 0, 0, 0
    for dev in DEVICES:
        c = w["SAMPLE_COUNT"].get(dev, 0)
        if not c:
            continue
        ndisks += 1
        oms, ev = w["OVER_MS"].get(dev, 0), w["OVER_EVENTS"].get(dev, 0)
        tms += oms
        tev += ev
        disks[fname(dev)] = {"min_day": r2(oms / 60000 / days), "cross_day": r2(ev / days),
                             "avg": r2(w["SUM_UTIL"].get(dev, 0) / c)}
    amin = tms / 60000 / days
    rec = {"name": name, "start": int(start), "end": int(end), "days": r2(days),
           "array_min_day": r2(amin), "array_cross_day": r2(tev / days),
           "array_over_pct": r2(100 * tms / (sampled * ndisks)) if ndisks else None,
           "array_avg": float(agg_avg(w))}
    n, tb = w["QBIT_SAMPLE_COUNT_TOTAL"], w["TORRENT_BYTES"]
    if n:
        act = w["QBIT_SUM_ACTIVE_TOTAL"] / n
        gbd = (w["QB_BYTES"] or tb) / 1073741824 / days
        agbd = w["ARRAY_BYTES"] / 1073741824 / days if tb else 0
        edays = (end - start) / 86400
        rec.update({"mbps": r2(w["QBIT_SUM_SPEED_TOTAL"] / n / 1048576), "active": round(act, 1),
                    "peers": round(w["QB_PEERS_SUM"] / n, 1), "swarm": round(w["QB_SWARM_SUM"] / n),
                    "gb_day": round(gbd, 1), "new_day": r2(w["NEW_COUNT"] / edays) if edays > 0 else None,
                    "array_pct": round(100 * w["ARRAY_BYTES"] / tb, 1) if tb else None,
                    "min_per_10_active": r2(amin / (act / 10)) if act > 0 else None,
                    "min_per_100gb": r2(amin / (agbd / 100)) if agbd > 0 else None})
    rec["disks"] = disks
    rec["tiers"] = {t: round(100 * b / tb, 1) for t, b in w["TIER_BYTES"].items()} if tb else {}
    rec.update({"r_active": corr_r(w, "ACT"), "r_peers": corr_r(w, "PEER"), "r_speed": corr_r(w, "SPD")})
    return rec


def read_history():
    recs = []
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return recs


def comparison_table(recs):
    t = recs[-COMPARE_COUNT:]
    lines = []

    def cell(v, f):
        return "-" if v is None else (f % v)

    def row(label, f, get):
        vals = []
        for r in t:
            try:
                vals.append(cell(get(r), f))
            except (KeyError, TypeError, ValueError):
                vals.append("-")
        lines.append(f"{label[:17]:<17}" + "".join(f" {v:>8}" for v in vals))

    def section(title):
        lines.extend(["", title])

    lines.append(" " * 17 + "".join(f" {str(r.get('name', '?'))[:8]:>8}" for r in t))
    lines.append(f"{'Started':<17}" + "".join(f" {time.strftime('%m/%d', time.localtime(r.get('start', 0))):>8}" for r in t))
    row("Days sampled", "%.1f", lambda r: r.get("days"))
    section("DISK LOAD")
    row(f"Time ≥{THRESHOLD}% %", "%.2f", lambda r: r.get("array_over_pct"))
    row("Min over/day", "%.1f", lambda r: r.get("array_min_day"))
    row("Crossings/day", "%.1f", lambda r: r.get("array_cross_day"))
    row("Array util %", "%.1f", lambda r: r.get("array_avg"))
    for dk in sorted((t[-1].get("disks") or {}), key=natural_key):
        row(f" {dk} min/d", "%.1f", lambda r, dk=dk: (r.get("disks") or {}).get(dk, {}).get("min_day"))
    section("DEMAND (not your settings)")
    row("Swarm leechers", "%.0f", lambda r: r.get("swarm"))
    row("New torrents/day", "%.1f", lambda r: r.get("new_day"))
    section("UPLOAD")
    row("GB/day", "%.1f", lambda r: r.get("gb_day"))
    row("Avg MB/s", "%.2f", lambda r: r.get("mbps"))
    row("Active uploads", "%.1f", lambda r: r.get("active"))
    row("Peers", "%.1f", lambda r: r.get("peers"))
    row("From array %", "%.0f", lambda r: r.get("array_pct"))
    for tk in sorted((t[-1].get("tiers") or {}), key=lambda k: (k == "none", natural_key(k))):
        label = ("No tier" if tk == "none" else f"Tier {tk}") + " share %"
        row(label, "%.0f", lambda r, tk=tk: (r.get("tiers") or {}).get(tk))
    section("NORMALIZED (lower = better)")
    row("Min/d per 10 act", "%.2f", lambda r: r.get("min_per_10_active"))
    row("Min/d /100 arr GB", "%.2f", lambda r: r.get("min_per_100gb"))
    section("MOVES WITH DISK LOAD (r)")
    row("Active uploads", "%.2f", lambda r: r.get("r_active"))
    row("Peers", "%.2f", lambda r: r.get("r_peers"))
    row("Upload speed", "%.2f", lambda r: r.get("r_speed"))
    return "\n".join(lines)


def post_comparison(title, extra=None):
    recs = read_history() + ([extra] if extra else [])
    if not recs:
        return
    warn = ""
    if len(recs) >= 2:
        a, b = recs[-2].get("swarm"), recs[-1].get("swarm")
        if a and b is not None:
            d = (b - a) / a * 100
            if abs(d) > 25:
                warn = (f"⚠️ Swarm demand changed {d:+.0f}% between the last two tests - "
                        "compare the NORMALIZED rows, not raw totals.\n")
    if send_embed(title, f"{warn}```\n{comparison_table(recs)}\n```", COLOR_ROLLUP,
                  footer="- = no data • r: -1 to 1, nearer 1 = moves with disk load"):
        log(f"Sent test comparison: {title}")


def finalize_test(old_name):
    """Closes the previous test: saves its record, posts the comparison, restarts test + rollup."""
    end = ST.get("last_sample_ts") or NOW
    w = ST["windows"]["test"]
    rec = test_record(w, old_name, w["START"], end)
    if rec:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        post_comparison(f"🧪 {old_name} complete — comparison")
    else:
        log(f"Test '{old_name}' had no sampled data - not saved.")
    ST["windows"]["test"] = new_window(NOW)
    ST["windows"]["weekly"] = new_window(NOW)
    ST["next"]["weekly"] = next_boundary(interval_sec("weekly"), NOW)
    ST["test_name"] = TEST_NAME
    log(f"Test '{old_name}' closed; '{TEST_NAME}' started. Rollup period restarted.")


# ================================================================== process control
class Stop(Exception):
    pass


def _on_signal(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise Stop()


def _is_monitor(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"diskio_monitor" in f.read()
    except OSError:
        return False


def take_lock():
    """Only one monitor runs at a time; a leftover instance is stopped (it saves its state first)."""
    try:
        with open(LOCK_FILE) as f:
            old = int(f.read().strip() or 0)
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid() and _is_monitor(old):
        log(f"Another monitor instance (pid {old}) is running - stopping it first.")
        try:
            os.kill(old, signal.SIGTERM)
            for _ in range(40):
                time.sleep(0.5)
                if not _is_monitor(old):
                    break
            else:
                os.kill(old, signal.SIGKILL)
        except OSError:
            pass
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    try:
        with open(LOCK_FILE) as f:
            if int(f.read().strip() or 0) == os.getpid():
                os.remove(LOCK_FILE)
    except (OSError, ValueError):
        pass


def die_with_parent():
    """If the User Scripts wrapper is killed, we get SIGTERM too (so state is still saved)."""
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


# ================================================================== main
def startup_common():
    global DEV_SET
    load_friendly_names()
    DEVICES[:] = discover_devices()
    DEV_SET = set(DEVICES)


DEV_SET = set()


def compare_main():
    global ST, NOW
    startup_common()
    NOW = int(time.time())
    ST, restored, seeded = load_state(NOW, write_migration=False)
    if not restored:
        print("No saved state yet - nothing to compare.")
        return 0
    for name in WINDOW_NAMES:
        ST["windows"].setdefault(name, new_window(NOW))
        fix_window(ST["windows"][name], NOW)
    w = ST["windows"]["test"]
    end = ST.get("last_sample_ts") or NOW
    rec = test_record(w, f"{ST.get('test_name') or TEST_NAME}*", w["START"], end)
    post_comparison(f"🧪 {ST.get('test_name') or TEST_NAME} so far vs previous tests", rec)
    print("Comparison posted (* = test in progress).")
    return 0


def main():
    global ST, NOW
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        return 1
    if "--compare" in sys.argv[1:]:
        return compare_main()

    die_with_parent()
    take_lock()
    startup_common()
    if not DEVICES:
        log("No physical disks found, exiting.")
        release_lock()
        return 1
    log(f"Started v{VERSION} (Python {sys.version.split()[0]}). Monitoring {len(DEVICES)} disks: "
        + " ".join(f"{fname(d)}({d})" for d in DEVICES))

    NOW = int(time.time())
    ST, restored, seeded = load_state(NOW)
    for name in WINDOW_NAMES:
        ST["windows"].setdefault(name, new_window(NOW))
        fix_window(ST["windows"][name], NOW)
    ST.setdefault("next", {})
    for name in BASH_NEXT:
        ST["next"][name] = to_int(ST["next"].get(name)) or next_boundary(interval_sec(name), NOW)
    ST.setdefault("alltime_prev", {})
    ST["last_notify"] = to_int(ST.get("last_notify"), NOW) or NOW

    if restored:
        log(f"Restored state (interval window started "
            f"{datetime.fromtimestamp(ST['windows']['interval']['START']).strftime('%Y-%m-%d %H:%M:%S')}).")
        cfg = ST.get("cfg") or {}
        if cfg.get("compile_minutes") and cfg["compile_minutes"] != COMPILE_MINUTES:
            ST["next"]["interval"] = next_boundary(COMPILE_MINUTES * 60, NOW)
            log(f"COMPILE_MINUTES changed {cfg['compile_minutes']} -> {COMPILE_MINUTES}; next interval report "
                f"{datetime.fromtimestamp(ST['next']['interval']).strftime('%Y-%m-%d %H:%M')}.")
        if cfg.get("weekly_days") and cfg["weekly_days"] != WEEKLY_DAYS:
            anchor = local_midnight(ST["windows"]["weekly"]["START"])
            ST["next"]["weekly"] = max(NOW, anchor + WEEKLY_DAYS * 86400)
            log(f"WEEKLY_INTERVAL_DAYS changed {cfg['weekly_days']} -> {WEEKLY_DAYS}; next rollup "
                f"{datetime.fromtimestamp(ST['next']['weekly']).strftime('%Y-%m-%d %H:%M')}.")
        gap = NOW - to_int(ST.get("last_sample_ts"), NOW)
        if gap > SAMPLE_SECONDS * 2:
            for w in wins():
                w["DOWNTIME_SEC"] += gap
            log(f"Monitor was offline ~{fmt_duration(gap)}; that time is unsampled and will be flagged in reports.")
        if seeded:
            log(f"Test tracking started as '{ST['test_name']}', seeded with the current rollup period's data.")
        if ST.get("test_name") and ST["test_name"] != TEST_NAME:
            finalize_test(ST["test_name"])
    ST["test_name"] = TEST_NAME
    ST["last_sample_ts"] = NOW
    save_state()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGHUP, _on_signal)
    try:
        run()
    except Stop:
        pass
    finally:
        save_state()
        log("Stopped - state saved.")
        release_lock()
    return 0


def run():
    global NOW, QBIT_ENABLED, STREAMS_ENABLED
    if QBIT_ENABLED:
        for old in glob.glob(os.path.join(WORK_DIR, "qbit_*")):   # bash-version leftovers (held full tracker URLs)
            if os.path.isdir(old):
                shutil.rmtree(old, ignore_errors=True)
        load_qbit_state()
        refresh_qui_names()
        for inst in INSTANCES:
            ok = bool(qbit_fetch(f"{inst['url']}/api/v2/app/version"))
            log(f"qBittorrent instance '{inst['label']}' "
                + ("reachable via Qui proxy." if ok else f"NOT reachable at {inst['url']} - check QUI_CONTAINER_NAME/proxy key."))
    if STREAMS_ENABLED:
        if shutil.which("lsof"):
            refresh_container_names()
            log(f"Stream attribution enabled for share(s): {', '.join(STREAM_SHARES)}.")
        else:
            STREAMS_ENABLED = False
            log("lsof not found - stream attribution disabled.")

    prev, prev_ms, prev_mover = snapshot(), mono_ms(), False
    half = SAMPLE_SECONDS // 2
    while True:
        mover = mover_running()
        time.sleep(half)
        if QBIT_ENABLED:
            qbit_collect()
        snap_ok = STREAMS_ENABLED and not mover and stream_snapshot()
        time.sleep(SAMPLE_SECONDS - half)

        cur, cur_ms = snapshot(), mono_ms()
        elapsed = cur_ms - prev_ms
        mover = mover or mover_running()
        if mover and not prev_mover:
            log("Mover detected running - excluding samples from disk stats until it finishes.")
        elif prev_mover and not mover:
            log("Mover finished - resuming normal stat collection.")
            LOC_CACHE.clear()   # files may have moved from pool to array
        prev_mover = mover

        over_any, over_devs, disk_valid, array_util = False, set(), False, 0
        if mover:
            for w in wins():
                w["MOVER_MS"] += elapsed
        elif elapsed > 0:
            utils = []
            for dev in DEVICES:
                if dev not in prev or dev not in cur:
                    continue
                delta = cur[dev] - prev[dev]
                if delta < 0:
                    continue   # counter reset (disk re-attached)
                util = min(100, (delta * 100 + elapsed // 2) // elapsed)
                if util >= THRESHOLD:
                    over_any = True
                    over_devs.add(dev)
                for w in wins():
                    acc_disk(w, dev, util, elapsed)
                utils.append(util)
            if utils:
                array_util = sum(utils) // len(utils)
                disk_valid = True
                for w in wins():
                    w["AGG_SUM"] += array_util
                    w["AGG_SAMPLES"] += 1
                    w["SAMPLED_MS"] += elapsed
        prev, prev_ms = cur, cur_ms

        if snap_ok and not mover:
            for w in wins():
                acc_streams(w, over_any, over_devs)
        if QBIT_ENABLED:
            acc_qbit(over_any, disk_valid, array_util)

        NOW = int(time.time())
        ST["last_sample_ts"] = NOW
        dirty = False
        if NOW >= ST["next"]["interval"]:
            interval_report()
            if QBIT_ENABLED:
                refresh_qui_names()
            ST["windows"]["interval"] = new_window(NOW)
            ST["next"]["interval"] = next_boundary(interval_sec("interval"), NOW)
            dirty = True
        if NOW >= ST["next"]["daily"]:
            summary_report("daily", f"📊 Daily Disk I/O Summary — {HOST}", COLOR_DAILY, note=update_note())
            post_tracker_message(ST["windows"]["daily"], "Daily")
            ST["windows"]["daily"] = new_window(NOW)
            LOC_CACHE.clear()   # daily safety net for location changes
            ST["next"]["daily"] = next_boundary(86400, NOW)
            dirty = True
        if NOW >= ST["next"]["weekly"]:
            summary_report("weekly", f"🗓️ {WEEKLY_DAYS}-Day Disk I/O Summary — {HOST}", COLOR_ROLLUP, with_corr=True)
            post_tracker_message(ST["windows"]["weekly"], f"{WEEKLY_DAYS}-Day")
            if read_history():
                t = ST["windows"]["test"]
                post_comparison(f"🧪 {ST['test_name']} so far vs previous tests",
                                test_record(t, f"{ST['test_name']}*", t["START"], NOW))
            ST["windows"]["weekly"] = new_window(NOW)
            ST["next"]["weekly"] = next_boundary(interval_sec("weekly"), NOW)
            dirty = True
        if dirty or NOW - LAST_SAVE >= SAVE_INTERVAL:
            save_state()


if __name__ == "__main__":
    sys.exit(main())
