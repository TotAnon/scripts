# Changelog

Changes to `diskio_monitor`. Releases marked **⚠️ Measurement change** change how test figures are measured or calculated; updating in the middle of a test mixes old and new numbers, so it's best to update when you start the next test. See [Updating](README.md#updating).

## 2.1.2 ⚠️ Measurement change

- Untagged torrents (e.g. new ones not yet tagged by an automation) now count toward their tracker's tier instead of No tier. The tier is taken from the tracker's tagged torrents, matched by hostname, then by tracker name to cover a tracker's other announce domains. No tier now only holds trackers with no tagged torrents.
- Upload already counted under No tier stays there, so tests running across this update show a higher No tier share than tests started after it.

## 2.1.1

- The update reminder warns when an update changes how results are measured, with the current test and day, and suggests waiting until the test ends.
- The update reminder links to this changelog.

## 2.1

- Daily update reminder: the daily summary checks GitHub for a newer version and shows a 🔔 reminder while one is available. Turn it off with `UPDATE_CHECK=false`.
- Optional `QUI_API_KEY`: tracker names and domain merges from qui's tracker customizations. Affects only tracker names in reports, not test figures.

## 2.0

- Rewritten in Python. A `state.sh` from the bash version is migrated to `state.json` on first start.
