#!/usr/bin/env python3

# ─────────────────────────────────────────────────────────────────────────────
# Setup: Virtual Environment
# ─────────────────────────────────────────────────────────────────────────────
#
# 1. Create the virtual environment (once, in the script's directory):
#
#       python3 -m venv /mnt/user/appdata/scripts/natorr/python-venv
#
# 2. Install dependencies (once, or after updating):
#
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/pip install requests pyyaml
#
# 3. Verify:
#
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
#           -c "import requests, yaml; print('OK')"
#
# 4. Run the script:
#
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
#           /mnt/user/appdata/scripts/natorr/queue_manager.py
#       ... --config /path/to/queue_manager.yml
#
# Unraid User Scripts: create a script containing:
#
#       #!/bin/bash
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
#           /mnt/user/appdata/scripts/natorr/queue_manager.py
#
# ─────────────────────────────────────────────────────────────────────────────

"""
queue_manager.py - Watches Radarr/Sonarr queues for superseded
(quality-upgraded) downloads still in progress in qBittorrent (via qui's
per-instance proxy), pauses the loser, and either purges it below a
progress threshold or tracks it for resume once the winner finishes.

Configuration is read from queue_manager.yml in the same directory (or
the path passed with --config). Every Radarr/Sonarr/qui URL is a plain
http(s) URL you provide directly - no Docker container name resolution
is performed. If your containers aren't reachable via a mapped host
port, point these URLs at whatever address does reach them (container
IP on a custom/macvlan network, reverse proxy, etc.) - this script
doesn't care how you got there.

Dependencies: pip install requests pyyaml
"""

import argparse
import datetime
import fcntl
import json
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yaml

VERSION = "1.3.0"

# ─────────────────────────────────────────────────────────────────────────────
# Config defaults
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "state_dir": None,  # None -> defaults to this script's own directory
    "qui": {
        "url": "",
    },
    "instances": [],
    "settings": {
        "pause_tag": "paused-by-supersede",
        "purge_progress_threshold": 0.10,
        "enable_purge": True,
        "wait_for_empty_queue_to_resume": False,
        "excluded_trackers": [],
        "tracker_overrides": [],  # [{name: str, min_mb: float}, ...]
        "resume_max_attempts_before_alert": 50,
    },
    "discord": {
        "webhook_url": "",
    },
    "logging": {
        "max_bytes": 10 * 1024 * 1024,  # 10 MB
        "backup_count": 5,
    },
    "update": {
        "check_for_updates": True,
        "repo": "TotAnon/scripts",
        "branch": "main",
        "path_prefix": "queue_manager",
    },
}


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    merged = dict(DEFAULT_CONFIG)
    for key in ("qui", "settings", "discord", "logging", "update"):
        merged[key] = {**DEFAULT_CONFIG[key], **(data.get(key) or {})}
    merged["state_dir"] = data.get("state_dir", DEFAULT_CONFIG["state_dir"])
    merged["instances"] = data.get("instances", [])
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Globals populated in main() after config load
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR: Path
LOG_FILE: Path
STATE_FILE: Path
LOCK_FILE: Path

# Populated from config in main() before any of these are read elsewhere;
# actual defaults live only in DEFAULT_CONFIG above, not duplicated here.
PAUSE_TAG: str
PURGE_PROGRESS_THRESHOLD: float
ENABLE_PURGE: bool
WAIT_FOR_EMPTY_QUEUE_TO_RESUME: bool
EXCLUDED_TRACKERS: List[str] = []
TRACKER_MB_THRESHOLDS: List[Tuple[str, float]] = []
DISCORD_WEBHOOK_URL: str = ""
RESUME_MAX_ATTEMPTS_BEFORE_ALERT: int

# Discord embed colors
COLOR_PURGE = 0xE74C3C   # red
COLOR_PAUSE = 0xF39C12   # orange
COLOR_RESUME = 0x2ECC71  # green
COLOR_ERROR = 0x992D22   # dark red

_logger = logging.getLogger("arr_qui_watcher")


def setup_logging(log_file: Path, max_bytes: int, backup_count: int) -> None:
    _logger.setLevel(logging.INFO)
    if not _logger.handlers:
        handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(handler)


def log(msg: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    try:
        _logger.info(msg)
    except Exception as e:
        print(f"Failed to write to log file: {e}")


def tracker_matches(configured_name: str, indexer: str) -> bool:
    """Case-insensitive substring match - tolerates Arr appending something
    like '(Prowlarr)'/'(Jackett)' to the raw indexer name."""
    if not configured_name or not indexer:
        return False
    return configured_name.strip().lower() in indexer.strip().lower()


def clean_indexer_label(indexer: str) -> str:
    """Strips a trailing '(Prowlarr)'/'(Jackett)' suffix for display, so
    what's shown is just whatever short name the indexer itself is
    configured with - no separate abbreviation mapping to maintain."""
    if not indexer:
        return ""
    return re.sub(r'\s*\((?:Prowlarr|Jackett)\)\s*$', '', indexer, flags=re.IGNORECASE).strip()


def get_tracker_min_mb(indexer: str, thresholds: List[Tuple[str, float]]) -> Optional[float]:
    for name, min_mb in thresholds:
        if tracker_matches(name, indexer):
            return min_mb
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Discord (batched, grouped by release, labeled by action type)
# ─────────────────────────────────────────────────────────────────────────────

DISCORD_EVENTS: List[Dict[str, Any]] = []


def record_event(kind: str, **fields) -> None:
    """kind: 'error' | 'purge' | 'pause' | 'resume'.
    'error' and 'resume' take a single 'text' field (a fully formatted line).
    'purge' and 'pause' take: app_name, name, score, amount_display,
    winner_name, winner_score, winner_indexer, loser_indexer, and
    optionally 'note' - these get grouped by release (season/title) and
    rendered as Winner/Loser boxes rather than flat lines."""
    DISCORD_EVENTS.append({"kind": kind, **fields})


def compute_release_group(app_name: str, name: str) -> Tuple[str, str]:
    """Best-effort grouping key/label so multiple episodes of the same show
    and season collapse into one pair of boxes instead of one line each. A
    movie, a full season pack, or a release that doesn't match the expected
    pattern all fall through to getting their own box."""
    name = name or "unknown release"

    if app_name == "Sonarr":
        m = re.match(r'^(.+?)\.S(\d{2})(?:E\d{1,3})?\b', name, re.IGNORECASE)
        if m:
            base, season = m.group(1), m.group(2)
            return f"{base.lower()}.s{season}", f"📺 {base.replace('.', ' ')} S{season}"
        return name.lower(), f"📺 {name}"

    if app_name == "Radarr":
        m = re.match(r'^(.*?)\.(\d{4})\.', name)
        if m:
            title = m.group(1).replace('.', ' ')
            year = m.group(2)
            return f"{title.lower()}.{year}", f"🎬 {title} ({year})"
        return name.lower(), f"🎬 {name}"

    return name.lower(), name


def build_grouped_embed(kind_events: List[Dict[str, Any]], title: str, color: int) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = {}
    group_order: List[str] = []
    for e in kind_events:
        base_key, base_label = compute_release_group(e.get("app_name", ""), e.get("name", ""))
        app_label = e.get("app_label", "")
        key = f"{app_label}|{base_key}"
        label = f"{base_label} ({app_label})" if app_label else base_label
        if key not in groups:
            groups[key] = {"label": label, "items": []}
            group_order.append(key)
        groups[key]["items"].append(e)

    fields = []
    MAX_FIELDS = 24  # Discord's cap is 25; leave room for a truncation notice
    truncated_groups = 0

    for key in group_order:
        if len(fields) >= MAX_FIELDS:
            truncated_groups += 1
            continue

        label = groups[key]["label"]
        items = groups[key]["items"]

        winner_lines, seen_winners = [], set()
        loser_lines = []
        for it in items:
            w_name = it.get("winner_name") or "unknown release"
            w_score = it.get("winner_score")
            w_indexer = it.get("winner_indexer")
            w_line = f"{w_name} [CF {w_score}]" if w_score is not None else w_name
            if w_indexer:
                w_line += f" [{w_indexer}]"
            if w_line not in seen_winners:
                seen_winners.add(w_line)
                winner_lines.append(w_line)

            l_line = it.get("name", "unknown")
            if it.get("score") is not None:
                l_line += f" [CF {it['score']}]"
            if it.get("loser_indexer"):
                l_line += f" [{it['loser_indexer']}]"
            if it.get("amount_display"):
                l_line += f" — {it['amount_display']}"
            if it.get("note"):
                l_line += f" ({it['note']})"
            loser_lines.append(l_line)

        winner_value = "```\n" + "\n".join(winner_lines) + "\n```"
        loser_value = "```\n" + "\n".join(loser_lines) + "\n```"
        if len(winner_value) > 1024:
            winner_value = winner_value[:1000] + "\n… (truncated)\n```"
        if len(loser_value) > 1024:
            loser_value = loser_value[:1000] + "\n… (truncated)\n```"

        fields.append({"name": f"{label} — Winners", "value": winner_value, "inline": False})
        fields.append({"name": f"{label} — Losers", "value": loser_value, "inline": False})

    if truncated_groups:
        fields.append({"name": "…", "value": f"{truncated_groups} more release(s) not shown", "inline": False})

    return {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def send_discord_batch(events: List[Dict[str, Any]]) -> None:
    if not DISCORD_WEBHOOK_URL or not events:
        return

    order = ["error", "purge", "pause", "resume"]
    titles = {
        "error": "⚠️ Errors",
        "purge": "🗑️ Purged",
        "pause": "⏸️ Paused",
        "resume": "▶️ Resumed",
    }
    colors = {
        "error": COLOR_ERROR,
        "purge": COLOR_PURGE,
        "pause": COLOR_PAUSE,
        "resume": COLOR_RESUME,
    }

    embeds = []
    for kind in order:
        kind_events = [e for e in events if e["kind"] == kind]
        if not kind_events:
            continue

        if kind in ("purge", "pause"):
            embeds.append(build_grouped_embed(kind_events, titles[kind], colors[kind]))
        else:
            lines = [e["text"] for e in kind_events]
            description = "\n".join(f"• {line}" for line in lines)
            if len(description) > 4000:
                description = description[:3990] + "\n… (truncated)"
            embeds.append({
                "title": titles[kind],
                "description": description,
                "color": colors[kind],
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

    if not embeds:
        return

    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        try:
            res = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=10)
            if res.status_code >= 300:
                log(f"[Discord] Webhook returned status {res.status_code}: {res.text[:200]}")
        except Exception as e:
            log(f"[Discord] Failed to send batched notification: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_tracked_state() -> Dict[str, Any]:
    """Keys are namespaced as '<app_label>:<hash>' so two different *arr
    instances (potentially pointed at two different qBittorrent instances)
    never collide over the same torrent hash. Older state files predate
    this and used the bare hash as the key with no 'hash' field in the
    entry - those are migrated in-memory here and re-saved in the new
    format on the next save."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            raw = json.load(f)
    except Exception:
        return {}

    migrated = {}
    for key, meta in raw.items():
        if "hash" not in meta:
            meta = dict(meta)
            meta["hash"] = key
            service = meta.get("service", "")
            key = f"{service}:{key}" if service else key
        migrated[key] = meta
    return migrated


def save_tracked_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Failed to save state file: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# qui proxy client
# ─────────────────────────────────────────────────────────────────────────────

class QBitProxyClient:
    # qBit torrent states that mean "still paused" across qBit 4.5.x and 4.6+ naming
    PAUSED_STATES = {"pauseddl", "pausedup", "stoppeddl", "stoppedup"}

    def __init__(self, raw_proxy_url: str, qui_base_url: str):
        # Only the PATH from raw_proxy_url is used (it carries the
        # per-instance proxy token, e.g. "/proxy/<hash>") - the host/port
        # always come from qui_base_url (your configured qui URL), so the
        # proxy path can be pasted straight from qui's UI regardless of
        # what host it shows there.
        proxy_path = urlparse(raw_proxy_url).path.rstrip('/')
        qui_parsed = urlparse(qui_base_url)
        self.base_url = f"{qui_parsed.scheme}://{qui_parsed.netloc}{proxy_path}"
        self.session = requests.Session()

    def get_live_torrent(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        h_lower = torrent_hash.lower()
        h_upper = torrent_hash.upper()

        for h in [h_lower, h_upper]:
            try:
                res = self.session.get(
                    f"{self.base_url}/api/v2/torrents/info",
                    params={"hashes": h},
                    timeout=5
                )
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data[0]
            except Exception:
                pass

        try:
            res = self.session.get(
                f"{self.base_url}/api/v2/torrents/properties",
                params={"hash": h_lower},
                timeout=5
            )
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict) and "total_size" in data:
                    total = float(data.get("total_size", 1))
                    done = float(data.get("total_downloaded", 0))
                    data["progress"] = done / total if total > 0 else 0.0
                    return data
        except Exception:
            pass

        return None

    def pause_only(self, torrent_hash: str, verify_attempts: int = 3, verify_delay: float = 1.0) -> bool:
        """Pause immediately, before any progress-threshold decision is made,
        and verify the state actually changed. qBittorrent can return 200 for
        these calls even when the pause silently didn't take effect, so the
        HTTP status alone isn't trusted - callers should treat a False return
        as 'not actually paused yet' and defer any decision.

        The state check is retried a few times with a short delay first,
        since qBit's reported state can lag a moment behind an accepted
        pause request - without this, a purely-cosmetic lag would get
        misread as a failure and needlessly deferred to the next run."""
        h = torrent_hash.lower()
        try:
            self.session.post(f"{self.base_url}/api/v2/torrents/stop", data={"hashes": h}, timeout=5)
            self.session.post(f"{self.base_url}/api/v2/torrents/pause", data={"hashes": h}, timeout=5)
        except Exception as e:
            log(f"[Proxy] Error pausing torrent {h}: {e}")
            return False

        found, state = False, None
        for attempt in range(verify_attempts):
            try:
                found, state = self._get_torrent_state(h)
            except Exception as e:
                log(f"[Proxy] Error verifying pause for torrent {h}: {e}")
                return False
            if found and state in self.PAUSED_STATES:
                return True
            if attempt < verify_attempts - 1:
                time.sleep(verify_delay)

        if not found:
            log(f"[Proxy] Torrent {h} not found in qBit when verifying pause.")
        else:
            log(f"[Proxy] Pause request for {h} did not take effect after {verify_attempts} checks (state is still '{state}').")
        return False

    def tag_and_deprioritize(self, torrent_hash: str) -> None:
        """Called once the torrent is confirmed paused and the decision is
        made to keep and track it rather than purge it: tags it and pushes
        it to the bottom of qBit's download queue. Category and Arr's queue
        entry are left alone so it can be picked back up cleanly later.

        Note: bottomQueue only has an effect if qBittorrent's "Torrent
        Queueing" option (Options > BitTorrent > Torrent Queueing) is
        enabled - if it's off, this call succeeds but does nothing, since
        there's no queue position concept to move. A logged priority of 0
        after this call is a sign queueing is disabled."""
        h = torrent_hash.lower()
        try:
            r_tag = self.session.post(f"{self.base_url}/api/v2/torrents/addTags", data={"hashes": h, "tags": PAUSE_TAG}, timeout=5)
            if r_tag.status_code >= 400:
                log(f"[Proxy] addTags for {h} returned status {r_tag.status_code}.")

            r_bottom = self.session.post(f"{self.base_url}/api/v2/torrents/bottomQueue", data={"hashes": h}, timeout=5)
            if r_bottom.status_code >= 400:
                log(f"[Proxy] bottomQueue for {h} returned status {r_bottom.status_code}.")
        except Exception as e:
            log(f"[Proxy] Error tagging/deprioritizing torrent {h}: {e}")
            return

        try:
            live = self.get_live_torrent(h)
            if live is not None:
                priority = live.get("priority")
                note = " (0 means qBit's 'Torrent Queueing' option is off, so queue position has no effect)" if priority == 0 else ""
                log(f"[Proxy] {h} queue priority after bottomQueue: {priority}{note}")
        except Exception:
            pass

    def bottom_queue(self, torrent_hash: str) -> None:
        """Re-pin a torrent to the bottom of the queue. Called periodically
        both for still-paused torrents and for resumed-but-not-yet-complete
        torrents, in case new/active downloads were added since we last
        checked and queueing didn't naturally keep it lowest-priority."""
        h = torrent_hash.lower()
        try:
            self.session.post(f"{self.base_url}/api/v2/torrents/bottomQueue", data={"hashes": h}, timeout=5)
        except Exception as e:
            log(f"[Proxy] Error re-pinning torrent {h} to bottom of queue: {e}")

    def _get_torrent_state(self, h: str) -> Tuple[bool, Optional[str]]:
        """Returns (found, state). Raises on connection failure so callers
        can tell 'qBit unreachable' apart from 'torrent genuinely gone'."""
        res = self.session.get(f"{self.base_url}/api/v2/torrents/info", params={"hashes": h}, timeout=5)
        res.raise_for_status()
        data = res.json()
        if isinstance(data, list) and len(data) > 0:
            return True, str(data[0].get("state", "")).lower()
        return False, None

    def resume(self, torrent_hash: str) -> str:
        """Start/resume a torrent, then verify with a live lookup that it
        actually left a paused state. Category is no longer handled here -
        Arr's own queue-removal call (changeCategory=True) takes care of
        that against its configured Post-Import Category before this runs.

        Returns one of:
          "resumed"     - confirmed no longer paused, tags cleared
          "stuck"       - qBit reachable, but torrent still reports paused
          "unreachable" - a request failed / qBit did not respond
          "missing"     - torrent no longer exists in qBit at all
        """
        h = torrent_hash.lower()
        try:
            r1 = self.session.post(f"{self.base_url}/api/v2/torrents/start", data={"hashes": h}, timeout=5)
            r2 = self.session.post(f"{self.base_url}/api/v2/torrents/resume", data={"hashes": h}, timeout=5)
        except Exception as e:
            log(f"[Proxy] Error requesting resume for torrent {h}: {e}")
            return "unreachable"

        if not ((r1.status_code < 400) or (r2.status_code < 400)):
            log(f"[Proxy] Resume request for {h} rejected (start={r1.status_code}, resume={r2.status_code}).")
            return "unreachable"

        found, state = False, None
        verify_attempts, verify_delay = 3, 1.0
        for attempt in range(verify_attempts):
            try:
                found, state = self._get_torrent_state(h)
            except Exception as e:
                log(f"[Proxy] Error verifying resume for torrent {h}: {e}")
                return "unreachable"
            if found and state not in self.PAUSED_STATES:
                break
            if attempt < verify_attempts - 1:
                time.sleep(verify_delay)

        if not found:
            log(f"[Proxy] Torrent {h} no longer found in qBit after resume request.")
            return "missing"

        if state in self.PAUSED_STATES:
            log(f"[Proxy] Torrent {h} still reports state '{state}' after {verify_attempts} checks.")
            return "stuck"

        try:
            self.session.post(f"{self.base_url}/api/v2/torrents/removeTags", data={"hashes": h, "tags": PAUSE_TAG}, timeout=5)
        except Exception as e:
            log(f"[Proxy] Resumed {h} but failed to remove pause tag: {e}")
        return "resumed"


# ─────────────────────────────────────────────────────────────────────────────
# Arr API
# ─────────────────────────────────────────────────────────────────────────────

def get_arr_queue(base_url: str, api_key: str) -> List[Dict[str, Any]]:
    try:
        res = requests.get(
            f"{base_url.rstrip('/')}/api/v3/queue",
            headers={"X-Api-Key": api_key},
            params={"includeUnknownSeriesItems": "true", "pageSize": 100},
            timeout=10
        )
        return res.json().get("records", [])
    except Exception as e:
        log(f"[Arr API] Error fetching queue from {base_url}: {e}")
        return []


def delete_arr_queue_item(base_url: str, api_key: str, queue_id: Any, remove_from_client: bool = False,
                           blocklist: bool = False, skip_redownload: bool = False,
                           change_category: bool = False) -> bool:
    """change_category=True asks Arr to move the underlying download to
    whatever 'Post-Import Category' is configured on its Download Client
    (Settings > Download Clients). If nothing is configured there, this is
    expected to be a no-op - so it's safe to pass regardless of whether the
    user uses a post-import category at all."""
    try:
        res = requests.delete(
            f"{base_url.rstrip('/')}/api/v3/queue/{queue_id}",
            headers={"X-Api-Key": api_key},
            params={
                "removeFromClient": "true" if remove_from_client else "false",
                "blocklist": "true" if blocklist else "false",
                "skipRedownload": "true" if skip_redownload else "false",
                "changeCategory": "true" if change_category else "false",
            },
            timeout=10
        )
        return res.status_code < 400
    except Exception as e:
        log(f"[Arr API] Error removing/updating queue ID {queue_id}: {e}")
        return False


def calculate_rank(item: Dict[str, Any], progress: float = 0.0) -> Tuple[int, int, float]:
    """(customFormatScore, quality resolution, live qBit progress). Progress
    is last so it never overrides a genuine CF/quality difference - it only
    breaks a true tie. Without it, two items tied on score and quality sort
    by Python's stable-sort fallback, i.e. whatever order Arr's queue API
    happened to return them in - which has no relationship to which one is
    actually further along in qBit, and can pick a freshly-grabbed duplicate
    as the 'winner' over a release that's already mostly downloaded."""
    score = item.get("customFormatScore", 0)
    quality_weight = item.get("quality", {}).get("quality", {}).get("resolution", 0)
    return (score, quality_weight, progress)


def normalize_progress(raw_progress: float) -> float:
    """qBit normally reports progress as 0.0-1.0, but tolerate 0-100 too."""
    prog = float(raw_progress)
    return prog if prog <= 1.0 else prog / 100.0


def attempt_pause_and_read_progress(proxy_client: QBitProxyClient, torrent_hash: str):
    """Confirms the torrent is actually paused, then reads live progress and
    total bytes downloaded. Returns (progress, name, downloaded_bytes) on
    success, or None if either step couldn't be confirmed - callers should
    treat None as 'not decidable yet, retry later' rather than falling back
    to a guess."""
    if not proxy_client.pause_only(torrent_hash):
        return None

    live_data = proxy_client.get_live_torrent(torrent_hash)
    if not live_data or "progress" not in live_data:
        return None

    progress = normalize_progress(live_data["progress"])
    return progress, live_data.get("name"), live_data.get("downloaded")


def decide_purge(tracker_min_mb: Optional[float], downloaded_bytes: Optional[float], progress: float) -> Tuple[bool, bool, str]:
    """Shared by both the initial detection pass and the pending_check
    follow-up, which used to each carry their own copy of this - one
    tracker-minimum-MB check and one percentage-threshold check, previously
    duplicated verbatim (and already slightly drifted in wording) in two
    places. Returns (would_purge, should_purge, amount_label):
      - would_purge: true if progress is below whichever threshold applies
      - should_purge: would_purge AND ENABLE_PURGE - what the caller should
        actually act on
      - amount_label: the human-readable "12.3MB (tracker minimum 100MB)"
        or "8.4%" string used in both logs and Discord notifications"""
    if tracker_min_mb is not None:
        downloaded_mb = (downloaded_bytes or 0) / (1024 * 1024)
        would_purge = downloaded_mb < tracker_min_mb
        amount_label = f"{downloaded_mb:.1f}MB (tracker minimum {tracker_min_mb:.0f}MB)"
    else:
        would_purge = progress < PURGE_PROGRESS_THRESHOLD
        amount_label = f"{progress*100:.1f}%"
    should_purge = would_purge and ENABLE_PURGE
    return would_purge, should_purge, amount_label


def make_tracked_entry(h: str, name: str, target_id: Any, app_label: str, phase: str,
                        winner_name: str, winner_score: Any, loser_score: Any,
                        winner_indexer: str, loser_indexer: str, tracker_min_mb: Optional[float]) -> Dict[str, Any]:
    """The tracked_state record shape - was built as an identical inline
    dict literal at both the point a loser is first paused and the point a
    pending_check resolves into one."""
    return {
        "hash": h,
        "name": name,
        "target_id": target_id,
        "service": app_label,
        "phase": phase,
        "resume_attempts": 0,
        "consecutive_unreachable": 0,
        "winner_name": winner_name,
        "winner_score": winner_score,
        "loser_score": loser_score,
        "winner_indexer": winner_indexer,
        "loser_indexer": loser_indexer,
        "tracker_min_mb": tracker_min_mb,
    }


def process_service(app_label: str, base_url: str, api_key: str, proxy_client: QBitProxyClient,
                     media_key: str, app_type: str, tracked_state: Dict[str, Any],
                     queue: List[Dict[str, Any]], queue_globally_clear: bool) -> None:
    log(f"[{app_label}] Found {len(queue)} items in queue.")
    if not queue and not tracked_state:
        return

    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for item in queue:
        download_id = item.get("downloadId")
        if not download_id:
            continue

        target_id = item.get(media_key)
        if not target_id and media_key == "episodeId":
            target_id = item.get("seriesId")
        if not target_id:
            continue

        grouped.setdefault(target_id, []).append(item)

    target_hash_map = {
        target_id: {i.get("downloadId", "").lower() for i in items if i.get("downloadId")}
        for target_id, items in grouped.items()
    }

    hash_to_queue_item = {
        i.get("downloadId", "").lower(): i
        for items in grouped.values() for i in items
        if i.get("downloadId")
    }

    already_tracked_hashes = {meta.get("hash", k) for k, meta in tracked_state.items() if meta.get("service") == app_label}

    # ----- Detect new supersede events (skip hashes we're already tracking/managing) -----
    for target_id, items in grouped.items():
        fresh_items = [i for i in items if i.get("downloadId", "").lower() not in already_tracked_hashes]
        if len(fresh_items) <= 1:
            continue

        # Rank on (customFormatScore, quality) alone first - the common
        # case is a real upgrade, which this alone already decides, with no
        # qBit round trips needed. Only when multiple candidates are
        # genuinely tied at the top does live progress matter (see
        # calculate_rank's docstring) - so only pay for those lookups, and
        # only for the items actually tied for first place, not every
        # candidate in the group.
        fresh_items.sort(key=calculate_rank, reverse=True)
        top_key = calculate_rank(fresh_items[0])[:2]
        tied_at_top = [i for i in fresh_items if calculate_rank(i)[:2] == top_key]

        if len(tied_at_top) > 1:
            item_progress: Dict[str, float] = {}
            for i in tied_at_top:
                dl_id = (i.get("downloadId") or "").lower()
                if not dl_id:
                    continue
                live = proxy_client.get_live_torrent(dl_id)
                if live and "progress" in live:
                    item_progress[dl_id] = normalize_progress(live["progress"])

            fresh_items.sort(
                key=lambda i: calculate_rank(i, item_progress.get((i.get("downloadId") or "").lower(), 0.0)),
                reverse=True,
            )

        winner_item = fresh_items[0]
        winner_name = winner_item.get("title") or winner_item.get("downloadId", "unknown release")
        winner_score = winner_item.get("customFormatScore", 0)
        winner_indexer = clean_indexer_label(winner_item.get("indexer", ""))
        superseded_items = fresh_items[1:]

        for loser in superseded_items:
            loser_hash = loser.get("downloadId")
            if not loser_hash:
                continue

            h = loser_hash.lower()
            loser_score = loser.get("customFormatScore", 0)
            raw_indexer = loser.get("indexer", "") or ""
            loser_indexer = clean_indexer_label(raw_indexer)

            if any(tracker_matches(t, raw_indexer) for t in EXCLUDED_TRACKERS):
                log(f"[{app_label}] Skipping '{loser.get('title', loser_hash)}' - tracker '{raw_indexer}' is on the excluded list, leaving it untouched.")
                continue

            tracker_min_mb = get_tracker_min_mb(raw_indexer, TRACKER_MB_THRESHOLDS)

            result = attempt_pause_and_read_progress(proxy_client, h)

            if result is None:
                name = loser.get("title", loser_hash)
                log(f"[{app_label}] Could not confirm pause + live progress for '{name}'; leaving pending and deferring the purge/track decision.")
                tracked_state[f"{app_label}:{h}"] = make_tracked_entry(
                    h, name, target_id, app_label, "pending_check",
                    winner_name, winner_score, loser_score, winner_indexer, loser_indexer, tracker_min_mb,
                )
                continue

            progress, live_name, downloaded_bytes = result
            name = live_name or loser.get("title", loser_hash)

            would_purge, should_purge, amount_label = decide_purge(tracker_min_mb, downloaded_bytes, progress)
            disabled_note = "deletion disabled" if (would_purge and not ENABLE_PURGE) else None

            if should_purge:
                log(f"[{app_label}] [Live qBit, post-pause] {amount_label}. Removing '{name}' via Arr (with file deletion)")
                ok = delete_arr_queue_item(base_url, api_key, loser.get("id"), remove_from_client=True)
                if not ok:
                    log(f"[{app_label}] Arr queue removal for '{name}' failed; leaving it in place for a future run to retry.")
                    record_event("error", text=f"**{app_label}**: failed to remove `{name}` via Arr (queue id {loser.get('id')}) - will retry next run")
                else:
                    record_event("purge", app_name=app_type, app_label=app_label, name=name, score=loser_score,
                                 amount_display=amount_label, winner_name=winner_name, winner_score=winner_score,
                                 winner_indexer=winner_indexer, loser_indexer=loser_indexer)
            else:
                log(f"[{app_label}] [Live qBit, post-pause] {amount_label}. Tagging + deprioritizing '{name}' (already paused){' - would have purged, but ENABLE_PURGE is disabled' if disabled_note else ''}")
                proxy_client.tag_and_deprioritize(h)
                tracked_state[f"{app_label}:{h}"] = make_tracked_entry(
                    h, name, target_id, app_label, "paused",
                    winner_name, winner_score, loser_score, winner_indexer, loser_indexer, tracker_min_mb,
                )
                record_event("pause", app_name=app_type, app_label=app_label, name=name, score=loser_score,
                             amount_display=amount_label, winner_name=winner_name, winner_score=winner_score,
                             winner_indexer=winner_indexer, loser_indexer=loser_indexer, note=disabled_note)

    # ----- Resume-management pass -----
    # Recomputed rather than reusing already_tracked_hashes above (even
    # though the expression is identical) - the detection pass just above
    # may have added new entries to tracked_state, and those need to count
    # here too so a hash we just started tracking this very run isn't
    # mistaken for "still actively competing" below.
    tracked_hashes_this_app = {meta.get("hash", k) for k, meta in tracked_state.items() if meta.get("service") == app_label}
    to_drop = []

    for thash, meta in list(tracked_state.items()):
        if meta.get("service") != app_label:
            continue

        raw_hash = meta.get("hash", thash)
        phase = meta.get("phase", "paused")

        if phase == "completing":
            live_data = proxy_client.get_live_torrent(raw_hash)
            if live_data is None:
                log(f"[{app_label}] '{meta.get('name')}' disappeared from qBit while completing; dropping from tracking.")
                record_event("error", text=f"**{app_label}**: `{meta.get('name')}` disappeared from qBit while completing - dropped from tracking")
                to_drop.append(thash)
                continue

            progress = normalize_progress(live_data.get("progress", 0.0))

            if progress >= 0.999:
                log(f"[{app_label}] '{meta.get('name')}' finished downloading; no longer tracking.")
                to_drop.append(thash)
            else:
                proxy_client.bottom_queue(raw_hash)
                log(f"[{app_label}] '{meta.get('name')}' still completing ({progress*100:.1f}%); re-pinned to bottom of queue.")
            continue

        if phase == "pending_check":
            result = attempt_pause_and_read_progress(proxy_client, raw_hash)
            if result is None:
                log(f"[{app_label}] Still can't confirm pause + live progress for '{meta.get('name')}'; remains pending.")
                continue

            progress, live_name, downloaded_bytes = result
            name = live_name or meta.get("name")
            meta["name"] = name
            winner_name = meta.get("winner_name", "an unknown release")
            winner_score = meta.get("winner_score")
            loser_score = meta.get("loser_score")
            winner_indexer = meta.get("winner_indexer", "")
            loser_indexer = meta.get("loser_indexer", "")
            tracker_min_mb = meta.get("tracker_min_mb")

            would_purge, should_purge, amount_label = decide_purge(tracker_min_mb, downloaded_bytes, progress)
            note = "deletion disabled, confirmed on a follow-up check" if (would_purge and not ENABLE_PURGE) else "confirmed on a follow-up check"

            if should_purge:
                queue_item = hash_to_queue_item.get(raw_hash)
                if queue_item and queue_item.get("id") is not None:
                    ok = delete_arr_queue_item(base_url, api_key, queue_item["id"], remove_from_client=True)
                    if ok:
                        log(f"[{app_label}] '{name}' confirmed at {amount_label}; removed via Arr.")
                        record_event("purge", app_name=app_type, app_label=app_label, name=name, score=loser_score,
                                     amount_display=amount_label, winner_name=winner_name, winner_score=winner_score,
                                     winner_indexer=winner_indexer, loser_indexer=loser_indexer,
                                     note="confirmed on a follow-up check")
                        to_drop.append(thash)
                    else:
                        log(f"[{app_label}] Arr removal failed for '{name}'; will retry.")
                else:
                    log(f"[{app_label}] '{name}' is below threshold but no current Arr queue record was found; keeping it paused instead.")
                    proxy_client.tag_and_deprioritize(raw_hash)
                    meta["phase"] = "paused"
            else:
                log(f"[{app_label}] '{name}' confirmed at {amount_label}; now tracking normally.")
                proxy_client.tag_and_deprioritize(raw_hash)
                meta["phase"] = "paused"
                record_event("pause", app_name=app_type, app_label=app_label, name=name, score=loser_score,
                             amount_display=amount_label, winner_name=winner_name, winner_score=winner_score,
                             winner_indexer=winner_indexer, loser_indexer=loser_indexer,
                             note=note)

        elif phase == "paused":
            target_id = meta.get("target_id")
            competing_hashes = {
                h for h in target_hash_map.get(target_id, set())
                if h != raw_hash and h not in tracked_hashes_this_app
            }
            still_competing = len(competing_hashes) > 0

            if still_competing:
                proxy_client.bottom_queue(raw_hash)
                continue

            if WAIT_FOR_EMPTY_QUEUE_TO_RESUME and not queue_globally_clear:
                proxy_client.bottom_queue(raw_hash)
                log(f"[{app_label}] '{meta.get('name')}' could resume, but other instances still have active downloads; holding.")
                continue

            attempts = meta.get("resume_attempts", 0) + 1
            meta["resume_attempts"] = attempts
            log(f"[{app_label}] Winner resolved. Attempting promote+resume (attempt {attempts}) for '{meta.get('name')}'.")

            queue_item = hash_to_queue_item.get(raw_hash)
            arr_ok = True
            if queue_item and queue_item.get("id") is not None:
                arr_ok = delete_arr_queue_item(
                    base_url, api_key, queue_item["id"],
                    remove_from_client=False, blocklist=False,
                    skip_redownload=True, change_category=True
                )
                if not arr_ok:
                    log(f"[{app_label}] Failed to clear/update Arr queue entry for '{meta.get('name')}'.")
            else:
                log(f"[{app_label}] No current Arr queue record found for '{meta.get('name')}'; resuming in qBit only.")

            outcome = proxy_client.resume(raw_hash) if arr_ok else "unreachable"

            if outcome == "resumed":
                suffix = f" after {attempts} attempt(s)" if attempts > 1 else ""
                note = " (Arr queue cleared, Post-Import Category applied if configured)" if (queue_item and arr_ok) else ""
                winner_name = meta.get("winner_name")
                winner_note = f" - was superseded by `{winner_name}`, which has now finished" if winner_name else ""
                log(f"[{app_label}] '{meta.get('name')}' resumed{suffix}.{note}")
                record_event("resume", text=f"**{app_label}**: `{meta.get('name')}`{suffix}{winner_note}{note}")
                meta["phase"] = "completing"
                meta["resume_attempts"] = 0
                meta["consecutive_unreachable"] = 0

            elif outcome == "missing":
                log(f"[{app_label}] '{meta.get('name')}' no longer exists in qBit; dropping from tracking.")
                record_event("error", text=f"**{app_label}**: `{meta.get('name')}` was expected to resume but no longer exists in qBit (removed manually?) - dropped from tracking")
                to_drop.append(thash)

            elif outcome == "stuck":
                if meta.get("consecutive_unreachable", 0) > 0:
                    log(f"[{app_label}] qBit reachable again for '{meta.get('name')}'; resetting outage counter.")
                meta["consecutive_unreachable"] = 0
                log(f"[{app_label}] '{meta.get('name')}' still reports paused despite reachable qBit; will retry.")

            else:  # "unreachable"
                streak = meta.get("consecutive_unreachable", 0) + 1
                meta["consecutive_unreachable"] = streak
                log(f"[{app_label}] Promote+resume attempt {attempts} could not reach qBit for '{meta.get('name')}' (outage streak: {streak}).")
                if streak >= RESUME_MAX_ATTEMPTS_BEFORE_ALERT and streak % RESUME_MAX_ATTEMPTS_BEFORE_ALERT == 0:
                    record_event(
                        "error",
                        text=f"**{app_label}**: `{meta.get('name')}` has failed to resume for {streak} consecutive unreachable attempts - qBit/qui may be down"
                    )

    for thash in to_drop:
        del tracked_state[thash]


# ─────────────────────────────────────────────────────────────────────────────
# Update check (notify-only)
#
# Checked once at the start of every run (when settings.update.check_for_
# updates is true). Compares this script's VERSION against the VERSION in
# queue_manager.py on the configured GitHub branch, and if newer, sends a
# Discord notification with the changelog since the current version and
# instructions to delete queue_manager.py yourself to update - same
# posture as TRaSH-Guides' own mover-tuning script ("Delete mover.py and
# re-run script to update"). queue_manager.sh already re-fetches
# queue_manager.py from GitHub whenever it's missing, so deleting it by
# hand is all updating ever takes - nothing here deletes it for you.
# Needs only requests + pyyaml, no extra dependency.
# ─────────────────────────────────────────────────────────────────────────────

def parse_version(v: str) -> Tuple[int, int, int]:
    m = re.match(r'^\s*(\d+)\.(\d+)\.(\d+)', v or "")
    if not m:
        raise ValueError(f"Unparseable version string: {v!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def fetch_raw(repo: str, branch: str, path_prefix: str, filename: str) -> Optional[str]:
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path_prefix}/{filename}"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code != 200:
            log(f"[Update] Fetching {filename} returned status {res.status_code}.")
            return None
        return res.text
    except Exception as e:
        log(f"[Update] Failed to fetch {filename}: {e}")
        return None


def extract_version(py_text: str) -> Optional[str]:
    m = re.search(r'^VERSION\s*=\s*["\']([\d.]+)["\']', py_text, re.MULTILINE)
    return m.group(1) if m else None


def extract_changelog_since(changelog_text: str, since_version: str) -> str:
    """Every '## [X.Y.Z] ...' section strictly newer than since_version,
    newest first - so a run that skipped several versions still reports the
    full cumulative changelog, not just the latest entry."""
    try:
        since = parse_version(since_version)
    except ValueError:
        since = (0, 0, 0)

    sections = re.split(r'(?m)^##\s*\[(\d+\.\d+\.\d+)\][^\n]*\n', changelog_text)
    parts = []
    for i in range(1, len(sections), 2):
        ver, body = sections[i], sections[i + 1]
        try:
            if parse_version(ver) > since:
                parts.append(f"**[{ver}]**\n{body.strip()}")
        except ValueError:
            continue
    return "\n\n".join(parts).strip()


def send_update_available_notification(current_version: str, new_version: str, changelog: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        return

    description = changelog if changelog else "No changelog available."
    if len(description) > 3900:
        description = description[:3890] + "\n… (truncated)"

    action_note = (
        "\n\n🗑️ To update: delete `queue_manager.py`. `queue_manager.sh` re-fetches it "
        "fresh from GitHub automatically on the next run - `queue_manager.yml` is untouched."
    )

    embed = {
        "title": f"🔔 queue_manager update available: {current_version} → {new_version}",
        "description": f"{description}{action_note}",
        "color": 0x3498DB,  # blue
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if res.status_code >= 300:
            log(f"[Discord] Update notification webhook returned status {res.status_code}: {res.text[:200]}")
    except Exception as e:
        log(f"[Discord] Failed to send update notification: {e}")


def check_for_updates(update_cfg: Dict[str, Any], notify_state_path: Path) -> None:
    """Notify-only: never downloads or writes anything except the tiny
    marker file at notify_state_path, which just remembers the last version
    we already sent a Discord notification for - without it, a script that
    runs every few minutes would re-notify on every single run for however
    long you take to update. The notification tells you to delete
    queue_manager.py yourself; queue_manager.sh re-fetches it automatically
    once it's gone - same pattern TRaSH-Guides' own mover-tuning script
    uses for mover.py, just done by hand instead of by this script."""
    if not update_cfg.get("check_for_updates", True):
        return

    repo = (update_cfg.get("repo") or "").strip()
    branch = (update_cfg.get("branch") or "main").strip()
    path_prefix = (update_cfg.get("path_prefix") or "queue_manager").strip()
    if not repo:
        log("[Update] No update.repo configured; skipping update check.")
        return

    remote_py = fetch_raw(repo, branch, path_prefix, "queue_manager.py")
    if remote_py is None:
        return

    remote_version = extract_version(remote_py)
    if not remote_version:
        log("[Update] Could not find a VERSION string in the remote queue_manager.py; skipping.")
        return

    try:
        if parse_version(remote_version) <= parse_version(VERSION):
            return
    except ValueError as e:
        log(f"[Update] {e}; skipping.")
        return

    last_notified = ""
    try:
        if notify_state_path.exists():
            last_notified = notify_state_path.read_text().strip()
    except Exception:
        pass

    if last_notified == remote_version:
        log(f"[Update] Version {remote_version} is available (current: {VERSION}) - already notified, not repeating.")
        return

    log(f"[Update] New version available: {VERSION} -> {remote_version}.")
    remote_changelog = fetch_raw(repo, branch, path_prefix, "CHANGELOG.md") or ""
    changelog_excerpt = extract_changelog_since(remote_changelog, VERSION) if remote_changelog else ""
    send_update_available_notification(VERSION, remote_version, changelog_excerpt)

    try:
        notify_state_path.write_text(remote_version)
    except Exception as e:
        log(f"[Update] Could not persist notified-version marker: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global SCRIPT_DIR, LOG_FILE, STATE_FILE, LOCK_FILE
    global PAUSE_TAG, PURGE_PROGRESS_THRESHOLD, ENABLE_PURGE, WAIT_FOR_EMPTY_QUEUE_TO_RESUME
    global EXCLUDED_TRACKERS, TRACKER_MB_THRESHOLDS, DISCORD_WEBHOOK_URL, RESUME_MAX_ATTEMPTS_BEFORE_ALERT

    parser = argparse.ArgumentParser(description="Arr + qui queue watcher")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_suffix(".yml"),
        help="Path to YAML config file (default: same name as this script, .yml extension, in the same directory)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    state_dir_cfg = config.get("state_dir")
    SCRIPT_DIR = Path(state_dir_cfg) if state_dir_cfg else Path(__file__).parent
    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE = SCRIPT_DIR / "arr_qui_watcher.log"
    STATE_FILE = SCRIPT_DIR / "paused_superseded.json"
    LOCK_FILE = SCRIPT_DIR / "watcher.lock"

    # DISCORD_WEBHOOK_URL is needed by check_for_updates() (for the update
    # notification), so it's pulled out ahead of the rest of the settings.
    DISCORD_WEBHOOK_URL = (config.get("discord") or {}).get("webhook_url", "").strip()

    setup_logging(LOG_FILE, int(config["logging"].get("max_bytes", 10 * 1024 * 1024)),
                  int(config["logging"].get("backup_count", 5)))

    # Single-instance lock: prevents two overlapping cron/user-script runs
    # from touching paused_superseded.json at the same time.
    lock_fp = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another instance of the watcher is already running - skipping this run.")
        sys.exit(0)

    DISCORD_EVENTS.clear()
    log(f"=== Running Arr Queue Watcher (v{VERSION}) ===")

    # Notify-only: never blocks or modifies anything, so this just runs and
    # falls through into the normal watch below regardless of outcome.
    check_for_updates(config.get("update") or {}, SCRIPT_DIR / "last_update_notified.txt")

    # load_config() already merged DEFAULT_CONFIG into these dicts, so every
    # key below is guaranteed present - no fallback defaults needed here.
    settings = config["settings"]
    PAUSE_TAG = settings["pause_tag"]
    PURGE_PROGRESS_THRESHOLD = float(settings["purge_progress_threshold"])
    ENABLE_PURGE = bool(settings["enable_purge"])
    WAIT_FOR_EMPTY_QUEUE_TO_RESUME = bool(settings["wait_for_empty_queue_to_resume"])
    EXCLUDED_TRACKERS = [t.strip() for t in settings["excluded_trackers"] if str(t).strip()]
    TRACKER_MB_THRESHOLDS = [
        (t.get("name", "").strip(), float(t.get("min_mb", 0)))
        for t in settings["tracker_overrides"]
        if t.get("name", "").strip()
    ]
    RESUME_MAX_ATTEMPTS_BEFORE_ALERT = int(settings["resume_max_attempts_before_alert"])

    qui_url = (config.get("qui") or {}).get("url", "").strip()
    if not qui_url:
        log("[Error] No qui url configured.")
        record_event("error", text="No qui url configured - the watcher did not run")
        send_discord_batch(DISCORD_EVENTS)
        sys.exit(1)

    instances_cfg = config.get("instances", [])
    if not instances_cfg:
        log("[Error] No *arr instances configured.")
        record_event("error", text="No *arr instances configured - the watcher did not run")
        send_discord_batch(DISCORD_EVENTS)
        sys.exit(1)

    tracked_state = load_tracked_state()

    valid_instances = []
    for inst in instances_cfg:
        label = inst.get("name", "Unknown")
        inst_type = {"radarr": "Radarr", "sonarr": "Sonarr"}.get((inst.get("type") or "").strip().lower())
        url = (inst.get("url") or "").strip()
        api_key = (inst.get("api_key") or "").strip()
        qui_proxy = (inst.get("qui_proxy") or "").strip()

        if inst_type not in ("Radarr", "Sonarr"):
            log(f"[{label}] Invalid or missing 'type' (must be 'radarr' or 'sonarr'); skipping this run.")
            record_event("error", text=f"**{label}**: invalid or missing type - skipped this run")
            continue

        if not url:
            log(f"[{label}] No url configured; skipping this run.")
            record_event("error", text=f"**{label}**: no url configured - skipped this run")
            continue

        if not qui_proxy:
            log(f"[{label}] No qui_proxy configured; skipping this run.")
            record_event("error", text=f"**{label}**: no qui_proxy configured - skipped this run")
            continue

        valid_instances.append({
            "label": label,
            "type": inst_type,
            "base_url": url.rstrip("/"),
            "api_key": api_key,
            "qui_proxy": qui_proxy,
        })

    if not valid_instances:
        save_tracked_state(tracked_state)
        send_discord_batch(DISCORD_EVENTS)
        log("=== Run Completed (no valid instances) ===")
        sys.exit(1)

    # Pre-fetch each instance's current queue once, and use it to build the
    # WAIT_FOR_EMPTY_QUEUE_TO_RESUME gate across every configured instance.
    # Our own tracked/paused entries are excluded from "is it empty", since
    # otherwise a paused loser would never see the queue as clear (it's
    # sitting in Arr's own queue by design, precisely so it can be picked
    # back up if the winner disappears).
    instance_queues = {inst["label"]: get_arr_queue(inst["base_url"], inst["api_key"]) for inst in valid_instances}
    all_tracked_hashes = {meta.get("hash") for meta in tracked_state.values() if meta.get("hash")}
    queue_globally_clear = not any(
        any(item.get("downloadId", "").lower() not in all_tracked_hashes for item in q if item.get("downloadId"))
        for q in instance_queues.values()
    )

    for inst in valid_instances:
        label = inst["label"]
        media_key = "episodeId" if inst["type"] == "Sonarr" else "movieId"
        proxy_client = QBitProxyClient(inst["qui_proxy"], qui_url)
        process_service(label, inst["base_url"], inst["api_key"], proxy_client, media_key, inst["type"],
                         tracked_state, instance_queues[label], queue_globally_clear)

    save_tracked_state(tracked_state)
    send_discord_batch(DISCORD_EVENTS)
    log("=== Run Completed ===")


if __name__ == "__main__":
    main()
