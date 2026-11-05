# queue_manager: Sonarr/Radarr superseded-download manager for qBittorrent

A User Scripts add-on for Unraid that stops Sonarr and Radarr from wasting bandwidth and private-tracker standing when two downloads end up racing for the same episode or movie.

That happens more than you'd think: Sonarr grabs a release, then a better one shows up a few minutes later (an upgrade), or autobrr pushes the exact same release from two trackers seconds apart and both get accepted. Arr leaves both downloading. queue_manager watches the queue, picks the one worth keeping, and deals with the other one:

- **Barely started?** It's removed through Arr (with its files), so it doesn't sit there half-done.
- **Already well along?** It's paused, tagged and pushed to the bottom of qBittorrent's queue, then resumed once nothing else is downloading, so you still finish it (and keep your ratio) without it competing for bandwidth.

Every decision is posted to Discord with the winner and loser side by side.

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## How the winner is picked

Candidates for the same episode/movie are ranked the same way Sonarr/Radarr rank upgrades:

1. **Quality**, by its position in that series'/movie's own quality profile. Qualities in the same group (e.g. WEBDL-1080p and WEBRip-1080p under "WEB 1080p") count as equal, just like in Arr.
2. **Custom format score**, only when quality is equal.
3. **Revision**: a PROPER/REPACK beats the original.
4. **Download progress**, only for true duplicates (same release from two trackers): the one further along is kept.

So a 2160p release at CF 200 beats a 1080p release at CF 5000 if your profile ranks 2160p higher. The log says which of the four decided each case. If a quality profile can't be read, quality is treated as equal and CF score decides. That's logged; it never guesses.

## What happens to the loser

| Situation | Action |
|---|---|
| Loser is below `purge_progress_threshold`, or below a tracker's `min_mb` in `tracker_overrides` | 🗑️ **Purged**: removed via Arr, files deleted (unless `enable_purge: false`) |
| Loser is past that point | ⏸️ **Paused**: tagged `paused-by-supersede`, moved to the bottom of qBittorrent's queue, left in Arr's queue |
| A paused loser, once nothing on any of your qBittorrent clients is actively downloading | ▶️ **Resumed**: cleared from Arr's queue, moved to its Post-Import Category (verified, and set directly if Arr didn't), and kept at the bottom of the queue until it finishes |
| Loser's tracker is in `excluded_trackers` | Left completely alone |

The pause always happens *before* progress is measured, so the number the decision is based on can't keep climbing.

## Discord reports

| Report | When |
|---|---|
| 🗑️ **Purged** / ⏸️ **Paused** | When a loser is handled. Winner and loser in separate boxes, grouped per show/season or movie, with CF score, tracker, and progress for both |
| ▶️ **Resumed** | When a paused loser is resumed, including the category it moved to |
| ⚠️ **Errors** | Failed removals, a paused torrent that vanished from qBittorrent, qBittorrent unreachable for a long stretch, a category that won't stick |
| 🔔 **Update available** | Once per new version on GitHub, with the changelog and how to update |

## Requirements

- **Unraid** with the **User Scripts** plugin
- **Python 3.8+** (e.g. the *Python 3 for UNRAID* plugin)
- **Sonarr and/or Radarr** (v3 API or newer)
- **qBittorrent** reached through **[qui](https://github.com/autobrr/qui)**'s per-instance proxy
- *Optional:* a **Discord webhook** (channel → Edit Channel → Integrations → Webhooks)
- *Recommended:* qBittorrent's **Torrent Queueing** turned on (Options → BitTorrent). Without it, "push to the bottom of the queue" has no effect; everything else still works.

## Installation

Everything lives in one folder. These steps use `/mnt/user/appdata/scripts/natorr`, which is what `queue_manager.sh` expects; if you pick another folder, change `BASE=` in `queue_manager.sh` to match.

### 1. Create a Python virtual environment

From the Unraid terminal:

```
mkdir -p /mnt/user/appdata/scripts/natorr
python3 -m venv /mnt/user/appdata/scripts/natorr/python-venv
/mnt/user/appdata/scripts/natorr/python-venv/bin/pip install requests pyyaml
```

### 2. Download the files

```
cd /mnt/user/appdata/scripts/natorr
curl -fsSL https://raw.githubusercontent.com/TotAnon/scripts/main/queue_manager/queue_manager.py -o queue_manager.py
curl -fsSL https://raw.githubusercontent.com/TotAnon/scripts/main/queue_manager/queue_manager.yml -o queue_manager.yml
curl -fsSL https://raw.githubusercontent.com/TotAnon/scripts/main/resolve_and_patch.py -o resolve_and_patch.py
```

`resolve_and_patch.py` looks up the current IP of your `radarr`, `sonarr` and `qui` containers before each run and writes it into `queue_manager.yml`, so the script still finds them on a custom Docker network even after an IP changes. If your containers are reachable at fixed addresses instead, you can drop that step from `queue_manager.sh` and put the addresses in the yml yourself.

### 3. Edit `queue_manager.yml`

At minimum, for each Sonarr/Radarr instance:

- `api_key`: Settings → General → API Key in Sonarr/Radarr
- `qui_proxy`: the proxy URL for the matching qBittorrent instance, copied from qui. Only the `/proxy/...` part is used, so it doesn't matter what host qui shows.
- `url`: the instance's address (overwritten automatically if you use `resolve_and_patch.py`)

Then set `discord.webhook_url` if you want notifications, and check `state_dir`: that's where the log, the state file and the lock file go. Keep it on appdata, not `/tmp`, which is wiped on reboot.

The other settings are explained in the yml itself. The ones you're most likely to change:

| Setting | If omitted | What it does |
|---|---|---|
| `purge_progress_threshold` | `0.10` | Losers below this fraction are purged; above it they're paused |
| `enable_purge` | `true` | `false` = never delete anything, always pause |
| `tracker_overrides` | none | Per-tracker minimum in MB instead of a percentage (for trackers that require a fixed amount downloaded) |
| `excluded_trackers` | none | Trackers whose torrents are never touched |

### 4. Add it to User Scripts

1. Settings → User Scripts → **Add New Script**, name it `queue_manager`.
2. Edit the script and paste in the contents of [`queue_manager.sh`](queue_manager.sh).
3. Set the schedule to **Custom** with `*/5 * * * *` (every 5 minutes). Runs can't overlap; a run that starts while another is still going just skips itself.
4. Click **Run Script** once and check the output for errors.

## Updating

When a new version is on GitHub you get a 🔔 Discord message with the changelog. To update, delete `queue_manager.py`:

```
rm /mnt/user/appdata/scripts/natorr/queue_manager.py
```

The next run of `queue_manager.sh` downloads the latest version automatically. Your `queue_manager.yml` is never touched. If a new version adds a setting you want to change, compare your yml against the [latest one](queue_manager.yml); new settings have sensible defaults, so you don't have to.

If the changelog says `queue_manager.sh` itself changed, paste the new version into User Scripts again. That's rare.

## Logs and troubleshooting

- **Log:** `arr_qui_watcher.log` in your `state_dir` (rotated at 10 MB, 5 backups). Every decision is logged with the qualities, CF scores, progress, and which factor decided it.
- **State:** `paused_superseded.json` in `state_dir` holds the torrents currently being tracked. Delete it to make the script forget everything it's paused (they stay paused in qBittorrent until you resume them).
- **A paused torrent never resumes:** it waits until nothing on any of your qBittorrent clients is actively downloading. If you always have something downloading (e.g. autobrr racing), it'll keep waiting.
- **"Quality position unknown" in the log:** a quality profile couldn't be read, or a release's quality isn't in its profile. CF score decided that one instead.
- **Containers not found:** `resolve_and_patch.py` needs the containers to be running and named `radarr`, `sonarr` and `qui` (or edit the names in `queue_manager.sh`).
