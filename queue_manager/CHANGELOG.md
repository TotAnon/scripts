# Changelog

All notable changes to `queue_manager` are documented here. Versions follow
`MAJOR.MINOR.PATCH`. On every run (when `settings.update.check_for_updates`
is enabled), the script compares its own `VERSION` against this file's
latest entry on the configured GitHub branch and, if newer, posts a Discord
notification with the changelog since your version - see the `update.*`
config keys in `queue_manager.yml` for the source repo/branch, and the
"Update check" section in `queue_manager.py`.

## [1.6.0] - 2026-11-05

### Changed
- **Winner/loser ranking now follows Sonarr/Radarr's own upgrade
  priority** instead of putting CF score first:
  1. Quality position in the item's own quality profile (read from
     `/api/v3/qualityprofile`). Qualities in the same group (e.g.
     WEBDL-1080p and WEBRip-1080p under "WEB 1080p") count as equal, as
     they do in Arr.
  2. CF score, only when quality position is equal.
  3. Revision (`version`, then `real`), so a PROPER/REPACK beats the
     original.
  4. Live qBit progress, only for true duplicates that tie on all of the
     above. qBit is still only queried for the candidates tied at the top.

  Example: a 2160p release at CF 200 now beats a 1080p release at CF 5000
  (if your profile ranks 2160p higher). Before, the 1080p one won.
- Profiles are fetched once per instance per run. Each queue item's
  profile comes from its series/movie, which the queue request now embeds
  (`includeSeries` / `includeMovie`), so there are no extra API calls per
  item.
- If profiles can't be read, or an item's quality isn't in its profile
  (or it has no profile), quality counts as equal for that comparison and
  CF score decides. This is logged. Nothing is guessed.
- The log now says which factor decided each winner/loser (quality, CF,
  revision, or progress) and shows each release's quality name. The
  winner's progress is still shown in the log and Discord.

### Removed
- `settings.source_rank_order` and the hardcoded resolution/source
  ranking. Your profile's order replaces them. A leftover
  `source_rank_order` in an existing yml is ignored.

## [1.5.0] - 2026-11-05

### Fixed (ported from the standalone `arr_qui_watcher2.py` variant)
- **Arr queue is now fetched in full, page by page.** Only the first 100
  records were ever read before. A paused loser (or its winner) sitting
  past that point looked like it "didn't exist" in Arr - so at resume
  time the Arr `changeCategory` call was skipped and the category never
  changed.
- **Post-import category is now verified, not assumed.** After a loser
  resumes, the script reads each download client's configured
  Post-Import Category from Arr (Settings > Download Clients) and
  confirms the torrent actually landed in it, setting it directly in qBit
  if Arr's `changeCategory` didn't take. It keeps re-checking every run
  while the torrent finishes, and won't stop tracking a finished torrent
  until its category is correct. A no-op if you don't use a post-import
  category.
- Each tracked loser now remembers which download client it came from,
  so the right post-import category is picked when an Arr instance has
  more than one client. Older state entries without it still work when
  only one client has a post-import category configured.

## [1.4.0] - 2026-11-05

### Fixed
- **Stuck-paused trumped torrents now resume correctly.** The resume gate
  used to check whether a competing hash was still *present in Arr's
  queue* - but a release that finished downloading and then got stuck on
  an Arr-side import failure (bad naming, a matching error) stays in
  Arr's queue indefinitely even though nothing is downloading it anymore.
  A paused loser waiting on that release could stay paused forever. The
  gate is now a single global check - is anything actually transferring
  bytes right now, on any configured qBit backend (`dlspeed > 0`) -
  computed from qBit's own live state instead of Arr's queue listing, so
  a stuck import can no longer block a resume. Deliberately global
  ("in either client") rather than per-release, per the report that
  prompted this.
- Deliberately speed-based, not state-string-based: a nominally
  "downloading" torrent sitting at 0 throughput (stalled, no peers)
  doesn't count as active either - the whole point is not trading one
  flavor of "stuck forever" for another.
- A loser can no longer resume in the same run it was just paused in,
  regardless of how the global check happens to read at that instant
  (restores a safety property the old per-release check had as a side
  effect, now provided explicitly).

### Changed
- **Duplicate/near-duplicate grab ranking now also considers source.**
  Previously ranked by `(customFormatScore, resolution)` only, with live
  qBit progress as the tiebreaker on an exact tie. Two releases that tie
  on score and resolution but differ in `quality.quality.source` (e.g.
  the same 1080p episode as both a WEBDL and a Bluray grab) are no longer
  treated as a pure duplicate decided by progress alone - source now
  breaks that tie too, ranked by the new `settings.source_rank_order`
  (default: bluray > web > webdl > webrip > hdtv > dvd > sdtv). A source
  Arr doesn't report, or that isn't in the list, is neutral and never
  invents a ranking out of missing data - no behavior change for anyone
  without `quality.quality.source` in their Arr responses.
- **Winner's progress is now always captured and shown**, not just the
  loser's - in both the log line at decision time and the Discord
  embed's Winner box. Previously only the loser's progress was visible
  anywhere, so there was no way to tell after the fact whether
  pausing/purging a partially-downloaded loser was actually justified by
  a genuinely-further-along winner. Costs one extra qBit lookup, but only
  when an actual supersede decision is being made, not on every idle run.
- Added explicit logging of the full tie comparison (every tied
  candidate's progress) whenever score/resolution/source all tie, so a
  future "why did it pick that one" question is answerable straight from
  the log.

### Removed
- `settings.wait_for_empty_queue_to_resume` - its "wait for the whole
  system to go quiet before resuming" intent is now what the fixed global
  resume gate does unconditionally; the old opt-in flag read the same
  broken Arr-queue-presence signal as the per-release check above, so
  keeping it around no longer made sense. A leftover key in an existing
  yml is simply ignored, not an error.

## [1.3.0] - 2026-09-03

### Removed
- `update.delete_self_on_update` (added in 1.2.0), and the auto-delete
  logic behind it. On real Unraid deployments (`/mnt/user`'s FUSE
  union filesystem), `unlink()` on the running script could return
  successfully without the file actually disappearing - 1.2.2 would have
  patched that by verifying after the fact, but the whole approach is
  more moving parts than warranted. Dropped in favor of the plan below.

### Changed
- The update notification now tells you to delete `queue_manager.py`
  yourself to update - exactly TRaSH-Guides' own mover-tuning script's
  wording for `mover.py` ("Delete mover.py and re-run script to update").
  `queue_manager.sh` already re-fetches `queue_manager.py` from GitHub
  whenever it's missing (kept from 1.2.0), so a manual delete is now all
  updating ever takes - the script itself never deletes anything.

## [1.2.1] - 2026-09-03

Test release - no functional change. Version bump only, to exercise the
update-check/notify/delete_self_on_update path end-to-end against a real
deployment.

## [1.2.0] - 2026-09-03

### Added
- Opt-in `update.delete_self_on_update` (default `false`). When enabled,
  once a newer version is detected, `queue_manager.py` deletes itself
  (never `queue_manager.yml`) - the same pattern TRaSH-Guides' own
  mover-tuning script uses for `mover.py`. **Requires a launcher that
  checks for the file being missing and re-downloads it before invoking
  Python again** - `queue_manager.sh` (as shipped) now does this. If you
  invoke `queue_manager.py` directly (a different wrapper, a raw cron
  line) without that check, enabling this will break future runs the
  moment it deletes itself, until the file is manually restored. That's
  why it defaults off - only turn it on if you're using `queue_manager.sh`
  (or an equivalent launcher) as your actual entry point.
- `queue_manager.sh`: fetches `queue_manager.py` fresh from GitHub if it's
  missing, before running it. A no-op when `delete_self_on_update` is left
  at its default.
- Delete attempts retry on every run (independent of the Discord notify
  dedup) so a transient failure (e.g. a permissions hiccup) self-heals on
  the next run instead of leaving the old version running indefinitely.

## [1.1.1] - 2026-09-03

### Changed
- Skip qBit progress lookups entirely for a superseded group unless
  candidates are actually tied on `customFormatScore` + quality - the
  common case (a real upgrade) is now decided from Arr's own queue data
  alone, with zero extra qBit API calls. When there is a genuine tie, only
  the tied candidates get a live-progress lookup, not every item in the
  group.
- `datetime.utcnow()` (deprecated since Python 3.12) replaced with
  `datetime.now(timezone.utc)` throughout.

### Fixed / Refactored
- The purge-vs-track decision (percentage or tracker-minimum-MB threshold)
  was duplicated verbatim in two places and had already drifted slightly
  in wording between them - extracted into a single `decide_purge()`.
- The `tracked_state` entry dict was built as an identical 12-field inline
  literal in two places - extracted into `make_tracked_entry()`.
- The `progress if progress <= 1.0 else progress / 100.0` normalization
  was repeated three times - extracted into `normalize_progress()`.
- No behavior change from any of the above - verified against the
  original inline logic. Added a comment where two textually-identical
  set comprehensions in `process_service` were confirmed to be
  intentionally *not* mergeable (the second one must be recomputed
  because `tracked_state` gains entries between the two points).

## [1.1.0] - 2026-09-03

### Changed
- Replaced the auto-apply update mechanism (1.0.1/1.0.2) with a
  notify-only check, matching TRaSH-Guides' own mover-tuning script:
  compares `VERSION` against the configured branch and posts a Discord
  notification (once per new version, not every run) when one is newer.
  Nothing is downloaded, nothing on disk is touched - you update by hand.
- Dropped the `ruamel.yaml` dependency entirely. Only `requests` + `pyyaml`
  are needed now, same as before any of this existed.

### Removed
- The config-diff/merge machinery and atomic file-replacement logic from
  1.0.1 - no longer applicable now that updates aren't auto-applied.

## [1.0.2] - 2026-09-03

### Fixed
- `main()` was casting `settings.*` values (`int()`/`float()`/`bool()`)
  *before* `check_for_updates()` ran. A value that no longer parsed under
  an old config (exactly the case the update mechanism exists to heal)
  crashed the run before self-update ever got a chance to fix it. The
  update check now runs first; the strict settings casts only happen once
  no update was applied.

## [1.0.1] - 2026-09-03

### Added
- Auto-update: on each run, the script checks the configured GitHub branch
  for a newer `VERSION`. If one is found, it downloads the new
  `queue_manager.py` and `queue_manager.yml`, verifies the new `.py`
  actually compiles before touching anything, merges your existing config
  values onto the new `.yml` template (new settings pick up their shipped
  default silently; any setting whose type changed or that was removed
  falls back to the new template's own default and is called out by name),
  atomically replaces both files, and sends a Discord changelog
  notification. The run that performs an update stops right there - the
  *next* scheduled run picks up the new code and config.
- New `update` config section: `check_for_updates`, `repo`, `branch`,
  `path_prefix`.
- `VERSION` constant.

### Notes
- The comment-preserving yml merge requires `ruamel.yaml`
  (`pip install ruamel.yaml`). Without it, the update check is skipped
  (logged once per run) and everything else runs as before.

## [1.0.0] - 2026-09-03

### Added
- Initial versioned baseline: watches Radarr/Sonarr queues for superseded
  (quality-upgraded) downloads still in progress in qBittorrent (via qui's
  per-instance proxy), pauses the loser, and either purges it below a
  progress threshold or tracks it for resume once the winner finishes.
- Tie-break fix: when two queued candidates have identical
  `customFormatScore` and quality resolution (e.g. the same release grabbed
  from two different indexers), the winner/loser choice now also considers
  live qBit download progress, so a fresh duplicate grab can no longer
  arbitrarily "win" over a release that's already mostly downloaded.
