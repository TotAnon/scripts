# Changelog

All notable changes to `queue_manager` are documented here. Versions follow
`MAJOR.MINOR.PATCH`. On every run (when `settings.update.check_for_updates`
is enabled), the script compares its own `VERSION` against this file's
latest entry on the configured GitHub branch and, if newer, updates itself -
see the `update.*` config keys in `queue_manager.yml` for the source repo/
branch, and the "Self-update" section in `queue_manager.py` for exactly what
that does to your files.

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
