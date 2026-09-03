# Changelog

All notable changes to `queue_manager` are documented here. Versions follow
`MAJOR.MINOR.PATCH`. On every run (when `settings.update.check_for_updates`
is enabled), the script compares its own `VERSION` against this file's
latest entry on the configured GitHub branch and, if newer, posts a Discord
notification with the changelog since your version - see the `update.*`
config keys in `queue_manager.yml` for the source repo/branch, and the
"Update check (notify-only)" section in `queue_manager.py`. It never
downloads or changes any file - you still update by hand.

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
