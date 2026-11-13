# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1-rc.1] - 2026-11-13

No changes since 0.1.1-beta.1.

## [0.1.1-beta.1] - 2026-11-13

### Changed

- `run` and `pre` now say a deduplicated rollup from release-rollup 0.2 or later needs a newer release-rollup, instead of calling its `.rollup` folder an unexpected entry. They still refuse it and change nothing (#4).

### Fixed

- `run` and `pre` left out a subfolder they couldn't read, or a link to another folder, and still trashed the release. Both now stop the run before anything changes and name the path (#33).

## [0.1.0] - 2026-11-12

### Added

- `release-rollup run` command, also available as `python3 -m release_rollup run`. Needs Python 3.14 or later. Tested on Linux and on Windows 11 (Git Bash).
- Per-release zips (`v0.1.3.zip`) for every published release except the newest.
- Line zips (`0.1.zip`) once the next minor line starts, also for a line with one release.
- `pre-1.0.0.zip` once 1.0.0 is published, then `1.0.zip` and `1.x.zip` as later lines and majors close.
- Pre-release builds (`x.y.z-alpha.N`, `-beta.N`, `-rc.N`) and unpublished folders never count as releases and are never zipped.
- Alpha, beta and release candidate folders and zips (`v1.2.0-beta.1`, `1.2.0-rc.4.zip`) go to the trash once their release is published, after the zips of the run are verified, oldest build first. `--keep-rc` (alias `--keep-pre`) turns this off.
- `--zip-newest` also zips the newest release into `v<version>.zip`, so nothing stays unpacked. Same verification as the other zips; rerunning is a no-op.
- Verification before anything is removed: `testzip` plus a sha256 comparison of every member against its source. A failed zip leaves its sources in place.
- `--dry-run` to print the plan without changing anything.
- `--marker NAME` to change which files and folders make a release count as published. The default is `RELEASE_NOTES.md`, `SHA256SUMS` and `assets/`.
- Trash through `gio trash`, or `--trash-dir DIR` to move sources aside when `gio` is missing.
- Safe to run twice, and to run after several releases landed at once.
- `release-rollup pre` command for a project's `prereleases/` folder (`alpha/`, `beta/`, `rc/`). Older tester builds of a version are zipped into `v<x.y.z>-<stage>.x.zip` after the usual verification. The newest finished build of each stage stays unpacked while its version is unreleased. Options: `--root`, `--current`, `--dry-run`, `--marker`, `--trash-dir`.
- Counts and sizes in the output use thousands separators.
