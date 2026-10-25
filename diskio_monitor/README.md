# Unraid Disk I/O Monitor → Discord

A User Scripts add-on for Unraid that watches how busy each array disk is, works out **what** is keeping it busy, and reports to Discord. It also tracks RAM, including how much of each qBittorrent's memory it actually needs and how much is releasable cache. It was built to answer one question: *why do my array disks keep hitting 100%, and do my seeding settings actually change that?*

It measures per-disk utilization every 30 seconds with no gaps, ties disk spikes to the containers and torrents active at the time, and adds a test mode so you can change a setting, run it for a week, and get a side-by-side comparison against previous tests.

## What you get

| Report | When | Contents |
|---|---|---|
| ⚠️ **Disk I/O / RAM alert** | Every interval (default 4 h), only if a disk crossed the threshold or average RAM use crossed its threshold | Each disk's average utilization and share of time over the threshold, qBittorrent upload speed and active uploads, and RAM |
| 🛑 **qBittorrent out of memory** | Immediately | A qBittorrent process was killed for lack of memory, with its memory at the time |
| ✅ **Still monitoring** | After a quiet stretch (default 12 h) | Same layout, as a heartbeat |
| 📊 **Daily summary** | Midnight | Per-disk load, with the containers that had files open on each hot disk · qBittorrent overall vs. during spikes · GB uploaded (array vs. pool, fresh vs. old torrents, new torrents) · upload by tier · array streams by container · RAM: system and per qBittorrent, required vs. cache · 🔔 reminder when a newer version is on GitHub |
| 📡 **Tracker breakdown** | After each daily and rollup report | Every tracker grouped by tier: active uploads, GB uploaded, average speed, time at upload cap |
| 🗓️ **Rollup** | Every 7 days (configurable) | Daily summary over the whole period, plus which factor moves with disk load (active uploads, peers, upload speed, or qBittorrent's cache size) |
| 🧪 **Test comparison** | When a test ends, with each rollup, or on demand | Up to several tests side by side: disk load, demand, upload, RAM, normalized load, tier shares |

Reports are Discord embeds, so they read cleanly on desktop and mobile.

## Features

- **Gap-free utilization.** Reads `/proc/diskstats` continuously, so short spikes between samples aren't missed.
- **Mover-aware.** Time while mover (or the Mover Tuning plugin's `age_mover`) runs is excluded from disk stats and shown separately.
- **Stream attribution.** Uses `lsof` to see which containers have files open on which array disk, the same logic as the Unraid dashboard's stream count, broken down per container and per disk.
- **qBittorrent integration** through [qui](https://github.com/autobrr/qui)'s client proxy, for one or more instances: upload speed, actively uploading torrents, connected peers, swarm demand, per-tracker stats, and new torrents.
- **Array vs. pool.** Works out whether uploaded data came from the array or a cache pool without touching array disks, so sleeping disks aren't woken.
- **Tiers.** If you tag torrents by tier (e.g. with qui automations), reports show each tier's share of upload, the split above and below a ratio target, and how often capped torrents sit at their limit.
- **Test mode with normalization.** Each test is saved with per-day figures, demand indicators (swarm leechers, new torrents) and efficiency ratios (disk minutes over threshold per 10 active uploads, and per 100 GB uploaded from the array). Comparisons flag when demand differed enough between tests that raw totals aren't comparable.
- **Robust state.** Totals and the report schedule survive restarts, edits, and reboots. Offline time is flagged in reports instead of silently skewing them.
- **RAM.** System RAM use, plus each qBittorrent container's memory split into *required* (qBittorrent's own memory and peer buffers) and *cache* (torrent data held in RAM, which Linux frees when something else needs it). Containers are matched to your qui instances automatically, with no container names to configure. Reports show cache size during disk spikes, and rollups show whether cache size moves with disk load: when cache gets squeezed, seeding turns into disk reads.
- **Update reminders.** Once a day the script checks GitHub for a newer version, and while one is available the daily summary starts with a 🔔 reminder linking to what's new and the update steps. If the update changes how results are measured, the reminder warns you so you can finish the current test first. Nothing is downloaded or changed for you.
- **Privacy.** Tracker passkeys are stripped the moment data arrives and are never stored or sent.

## Requirements

- **Unraid** with the **User Scripts** plugin
- **Python 3.8+**, e.g. via the *Python 3 for UNRAID* plugin (only the standard library is used, no pip packages)
- A **Discord webhook** (channel → Edit Channel → Integrations → Webhooks)
- *Optional:* **[qui](https://github.com/autobrr/qui)** running as a container, for the qBittorrent features. Without it, the disk and stream reports still work.

## Installation

The two files you need are in this folder:

- `diskio_monitor.py`: the program
- `user_script.sh`: the settings and launcher, pasted into User Scripts

### 1. Copy the program to your appdata

Create the folder and put `diskio_monitor.py` in it. From the Unraid terminal:

```
mkdir -p /mnt/user/appdata/scripts/diskio_monitor
curl -fsSL https://raw.githubusercontent.com/TotAnon/scripts/main/diskio_monitor/diskio_monitor.py \
  -o /mnt/user/appdata/scripts/diskio_monitor/diskio_monitor.py
```

The same folder will hold the script's state and test history. It should be on a cache pool (appdata), not `/tmp` or `/var/log`, which are wiped on reboot. No file permissions are needed.

### 2. Create the user script

In **Settings → User Scripts**, add a new script and replace its contents with [`user_script.sh`](user_script.sh).

### 3. Configure

Edit the settings block at the top of the user script. At minimum, replace the placeholder `DISCORD_WEBHOOK_URL` with your own webhook. Keep your filled-in copy private: the webhook URL, qui proxy keys and `QUI_API_KEY` work as passwords. For qBittorrent, see the next section, or set `QBIT_ENABLED=false`.

### 4. Schedule and start

Set the schedule to **At Startup of Array**, then click **Run in Background** to start it now. It runs continuously from then on.

Check that it's working:

```
tail -f /var/log/diskio_monitor.log
```

You should see `Started`, your disks listed, and, if enabled, `qBittorrent instance '…' reachable via Qui proxy`.

## qBittorrent setup (via qui)

The script reaches qBittorrent through qui's **client proxy**, which needs no qBittorrent password.

1. In qui, go to **Settings → Client Proxy** and create a client API key for **each** qBittorrent instance.
2. Add one line per instance to `QBIT_INSTANCES`, using `localhost` as the host (the script runs `wget` inside qui's container):
   ```bash
   QBIT_INSTANCES=(
       "qbit-tv|http://localhost:7476/proxy/YOUR_CLIENT_KEY_1"
       "qbit-movies|http://localhost:7476/proxy/YOUR_CLIENT_KEY_2"
   )
   ```
3. Set `QBIT_PATH_MAP` to how your qBittorrent containers map their data path, e.g. `/data=/mnt/user/data`. If one instance maps differently, add a third field to its line: `"label|url|/downloads=/mnt/user/downloads"`.
4. Set `POOL_NAMES` to the pool new downloads land on before mover runs, usually `cache`.

Check that the proxy works:

```
docker exec qui wget -qO- -T 20 "http://localhost:7476/proxy/YOUR_CLIENT_KEY/api/v2/sync/maindata?rid=0" | head -c 200
```

You should see JSON starting with `{"rid":`.

**Tracker names.** By default a tracker is named from its hostname (`tracker.example.org` → `example`). Hosts that share a name are combined automatically, e.g. different subdomains of the same domain. To merge unrelated domains or set nicer names, use `TRACKER_ALIASES`:

```bash
TRACKER_ALIASES=(
    "torrentleech.org|TorrentLeech"
    "tleechreload.org|TorrentLeech"
)
```

Alternatively, set `QUI_API_KEY` (qui → **Settings → API Keys**) to use the display names and domain merges from qui's tracker customizations; names are refreshed with each alert interval. Aliases take priority over qui's names.

**Tiers.** Torrents tagged like `tier 1`, `Tier 2` or `tier3` are grouped by tier. Change `TIER_TAG_REGEX` if your tags differ.

## Configuration reference

All settings live in the user script.

| Setting | Default | Description |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | (required) | Where reports are posted |
| `THRESHOLD_PERCENT` | `85` | Utilization that counts as "over" |
| `SAMPLE_SECONDS` | `30` | Length of each sample |
| `COMPILE_MINUTES` | `240` | Alert window, aligned to midnight |
| `HEALTH_PING_HOURS` | `12` | Heartbeat after this long with no messages; `0` disables |
| `WEEKLY_INTERVAL_DAYS` | `7` | Rollup period in days |
| `MONITOR_NAME_REGEX` | `^disk[0-9]+$` | Which disks to monitor, by Unraid name; add parity with `^(disk[0-9]+\|parity[0-9]*)$` |
| `MOVER_PROCESS_NAMES` | `mover age_mover` | Processes treated as mover |
| `TEST_NAME` | `Test 1` | Label for the current test |
| `TEST_COMPARE_COUNT` | `3` | Tests shown side by side (wider tables wrap on mobile) |
| `STATE_DIR` | `/mnt/user/appdata/scripts/diskio_monitor` | Program, state and history location |
| `QBIT_ENABLED` | `true` | qBittorrent features on or off |
| `QUI_CONTAINER_NAME` | `qui` | Name of the qui container |
| `QUI_API_KEY` | (blank) | Optional; tracker names from qui's tracker customizations |
| `QBIT_INSTANCES` | | One `label\|proxy_url[\|path_map]` per instance |
| `QBIT_PATH_MAP` | `/data=/mnt/user/data` | Container path → host path; `;`-separated for several |
| `POOL_NAMES` | `cache` | Pool(s) downloads land on before mover |
| `TIER_TAG_REGEX` | `^tier ?(?<n>[0-9]+)$` | Tier tag pattern (case-insensitive); must capture the number as `n` |
| `RATIO_SPLIT` | `2.0` | Tier upload is split below / at-or-above this ratio |
| `NEW_TORRENT_DAYS` | `7` | Torrents younger than this count as "fresh" |
| `TRACKER_ALIASES` | (empty) | `domain\|Name` display-name overrides |
| `STREAMS_ENABLED` | `true` | Stream attribution on or off |
| `STREAM_SHARES` | `data` | User shares to attribute streams for (only the array portion is counted) |
| `STREAM_TOP_N` | `8` | Containers listed in the streams field |
| `STREAM_LSOF_TIMEOUT` | `20` | Skip a stream snapshot if `lsof` takes longer |
| `RAM_ENABLED` | `true` | RAM reporting on or off |
| `RAM_THRESHOLD_PERCENT` | `90` | Interval alert when average system RAM use is at least this |
| `QBIT_RAM_THRESHOLD_GB` | `0` (off) | Interval alert when a qBittorrent's average *required* RAM is at least this many GB |
| `UPDATE_CHECK` | `true` | Daily check for a newer version on GitHub, shown as a reminder in the daily summary (see [Updating](#updating)) |
| `LOG_FILE` | `/var/log/diskio_monitor.log` | Log location (rotated daily or at 10 MB) |

## Running tests

1. Set `TEST_NAME` (e.g. `Test 1`) and let it run. A week is a good length.
2. Make your change: upload slots, tier caps, active torrent limits, and so on.
3. Change `TEST_NAME` to `Test 2` and restart the script.

On restart, the previous test is saved to `test_history.jsonl`, a comparison is posted, and the rollup period restarts so it lines up with the new test.

To see the current test so far against previous ones at any time, from a terminal:

```
bash /boot/config/plugins/user.scripts/scripts/<your script name>/script --compare
```

This is read-only and safe while the monitor is running.

## Reading the reports

- **Time ≥ threshold.** The share of sampled time a disk spent at or above `THRESHOLD_PERCENT`. Peak utilization isn't shown because spinning disks briefly hit 100% constantly; how *long* they stay there is what matters.
- **📂 owners on a hot disk.** The average number of streams each container had open on that disk while it was over the threshold.
- **Streams.** Open folders on the array, not bytes. Reads through `/mnt/user` are performed by Unraid's `shfs` process, so per-container byte counts aren't available; streams are the reliable proxy.
- **qBittorrent "during ≥ threshold".** Averages from only the samples where a disk was over the threshold. If these are much higher than the overall averages, qBittorrent is a likely driver of the spikes.
- **At cap.** Of the time capped torrents were uploading, how much was spent at 90% or more of their limit.
- **What moves with array load (r).** A correlation from -1 to 1 between array utilization and each candidate driver. Nearer 1 means it rises and falls with disk load. Shown once there are at least 30 samples.
- **Normalized rows (comparisons).** Disk load relative to workload, so a quiet week doesn't look like an improvement. Lower is better. If swarm demand changed by more than 25% between tests, the comparison says so.
- **🧠 RAM.** *System* is RAM in use (total minus available) and how often it was over `RAM_THRESHOLD_PERCENT`. *Waiting on RAM* appears when processes actually stalled for memory, a better sign of a real shortage than a high percentage. For each qBittorrent: *required* is memory it needs and can't give back; *cache* is torrent data held in RAM, released whenever something else needs it; *Unraid shows* is the figure on the Docker page, which counts required memory plus recently used cache. The per-instance threshold uses *required*, because cache filling free RAM is normal.
- **Cache during ≥ threshold.** qBittorrent's cache while a disk was over the threshold. If it's much lower than the overall cache, disk spikes may come from cache being squeezed out of RAM rather than from seeding demand.
- **Footer flags.** 🚚 time excluded for mover, ⚠️ time the monitor was offline (unsampled), and 🧪 the current test and day.

## Files

| File | Location | Purpose |
|---|---|---|
| `diskio_monitor.py` | `STATE_DIR` | The program |
| `state.json` | `STATE_DIR` | Running totals and report schedule |
| `test_history.jsonl` | `STATE_DIR` | One line per finished test |
| `monitor.pid` | `STATE_DIR` | Prevents two copies from running |
| `diskio_monitor.log` | `/var/log` | Activity log |
| `qbit_*.json` | `/tmp/diskio_monitor` | qBittorrent sync cache (RAM, rebuilt as needed) |

**Resetting:** stop the script, delete `state.json` (and `test_history.jsonl` to also clear past tests), and start it again.

## Updating

When a newer version is on GitHub, the daily summary starts with a 🔔 reminder. See the [changelog](CHANGELOG.md) for what changed.

1. Stop the script in User Scripts.
2. Download the latest program with the `curl` command from [step 1](#1-copy-the-program-to-your-appdata).
3. Start the script again. The log shows `Started v…` with the new version.

Your settings live in the user script, so they aren't touched. `state.json` and `test_history.jsonl` aren't touched either: as long as you don't change `TEST_NAME`, the current test carries on. The minutes the script was stopped are unsampled and show as offline time in the report footer; test figures are averaged over sampled time only, so a short stop doesn't skew them.

If a new version adds settings, copy those lines from the latest [`user_script.sh`](user_script.sh) into yours. Until you do, they use their defaults.

**Updates that change measurements.** Some updates change how disk load, upload or other test figures are measured. Updating in the middle of a test then mixes old and new numbers in that test. The reminder says so (⚠️ *This update changes how results are measured*), and the changelog marks these releases. For clean results, wait until the current test ends and update when you start the next one (change `TEST_NAME` at the same restart). If you're not sure whether to wait, ask on Discord before updating.

### Updating from the bash version

The bash version was a single script in User Scripts and doesn't use `diskio_monitor.py`, so downloading the program alone isn't enough, and the bash version has no update reminder.

1. Stop the old script in User Scripts.
2. Download `diskio_monitor.py` with the `curl` command from [step 1](#1-copy-the-program-to-your-appdata). It must go in the folder where the bash version kept its `state.sh` (`/mnt/user/appdata/scripts/diskio_monitor` by default); if yours is elsewhere, change the path in the command.
3. Replace the whole User Scripts entry with the new [`user_script.sh`](user_script.sh) and copy your settings (webhook URL, threshold, qBittorrent instances and so on) into it. Set `STATE_DIR` to that same folder.
4. Start it. On first start, `state.sh` is converted to `state.json` and the original is kept as `state.sh.migrated`. Your totals and current test carry over.

## Troubleshooting

| Log message | Fix |
|---|---|
| `python3 not found` (User Scripts output) | Install the Python 3 plugin |
| `Missing …/diskio_monitor.py` | Copy the program into `STATE_DIR` |
| `qBittorrent instance '…' NOT reachable` | Check `QUI_CONTAINER_NAME` and the proxy key; run the check command above |
| `Discord send FAILED (HTTP 404)` | The webhook URL is wrong or was deleted |
| `Stream snapshot skipped: lsof exceeded 20s` | Raise `STREAM_LSOF_TIMEOUT`, or set `STREAMS_ENABLED=false` |
| `Could not read Qui tracker customizations` | Check `QUI_API_KEY`, or leave it blank |
| `Update check failed: …` | GitHub couldn't be reached; the reminder is skipped that day. Set `UPDATE_CHECK=false` to stop checking |
| `qBittorrent instance '…': container not found` | The script couldn't match that instance to a container: is qBittorrent running in Docker? It retries every 15 minutes; disk and upload reports aren't affected |
| `No physical disks found` | Check `MONITOR_NAME_REGEX`; the array must be started |

## Notes and limitations

- The script runs as a background process. Measuring costs a single read of `/proc/diskstats` per sample, which never touches the disks; the `lsof` pass runs at the lowest CPU and I/O priority.
- Array vs. pool detection checks only pool paths, so it never wakes array disks. It assumes your shares follow the standard Unraid layout (`/mnt/<pool>/<share>/…`).
- **How containers are matched.** The script finds running `qbittorrent-nox` processes, then looks for each instance's torrent hashes in that container's resume data: `torrents.db` with SQLite storage, or `BT_backup/*.fastresume` files with the default storage. It works with hotio, linuxserver and binhex images and any container names or networking. If neither is found, it falls back to the files each qBittorrent has open. The match is saved and redone when a container is recreated. With appdata on the array instead of a cache pool, this lookup could spin up that disk once at startup.
- **RAM costs nothing extra on disk.** It reads `/proc/meminfo` and each container's memory files under `/sys/fs/cgroup`, which live in RAM. Containers that run other processes alongside qBittorrent (e.g. binhex VPN images) include those in their figures.
- qBittorrent's API reports which tracker URL a torrent is currently using. If a torrent fails over between two URLs that resolve to different names, its upload is split between them unless they're merged with an alias or in qui.
