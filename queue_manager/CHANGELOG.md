# Changelog

All notable changes to `queue_manager` are documented here. Versions follow
`MAJOR.MINOR.PATCH`. On every run (when `settings.update.check_for_updates`
is enabled), the script compares its own `VERSION` against this file's
latest entry on the configured GitHub branch and, if newer, posts a Discord
notification with the changelog since your version - see the `update.*`
config keys in `queue_manager.yml` for the source repo/branch, and the
"Update check" section in `queue_manager.py`.

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
