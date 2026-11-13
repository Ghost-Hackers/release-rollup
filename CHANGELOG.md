# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Release zips are built on GitHub from the version tag and come with a signed attestation, so `gh attestation verify` can prove which commit a download was built from. `SECURITY.md` explains how to check one.
- `tools/build_release.py` builds a release zip from a commit. Two builds of one commit are byte for byte the same, so anyone can rebuild a release and compare the sha256.

### Fixed

- `run` and `pre` left out a subfolder they couldn't read, or a link to another folder, and still trashed the release. Both now stop the run before anything changes and name the path (#33).

## [0.1.1] - 2026-11-13

### Changed

- `run` and `pre` now say a deduplicated rollup from release-rollup 0.2 or later needs a newer release-rollup, instead of calling its `.rollup` folder an unexpected entry. They still refuse it and change nothing (#4).

### Fixed

- `run` and `pre` left out a subfolder they couldn't read, or a link to another folder, and still trashed the release. Both now stop the run before anything changes and name the path (#33).

## [0.2.0-beta.2] - 2026-11-12

### Added

- `tools/e2e.sh` runs the end-to-end checks a release goes through against the release zip, with `--cross` for a 0.1.x zip and `--self-check` to prove that a changed byte fails them.

### Changed

- The package passes Pyright's strict mode. The settings are in `pyproject.toml`, so editors and the command line use the same rules.

### Fixed

- A zip member whose data was cut short made the check pass with no reason given, so `upgrade` could trash a damaged `.upgrade-old` copy. Every read error now stops the run with a reason.
- When `upgrade` keeps a damaged `.upgrade-old` copy, it now says the copy failed its check and gives the reason, instead of calling it "not a plain rollup".

## [0.2.0-beta.1] - 2026-11-12

### Added

- `run` and `pre` deduplicate by default. Repeated content across the builds in a rollup is stored once, in a normal zip with the same name, and that includes the members of zips inside a build. Three beta builds that are 41,026,579 bytes unpacked come to 13,698,897 bytes.
- `--no-dedupe` on `run` and `pre` writes the plain zip of 0.1.0.
- `release-rollup restore ROLLUP.zip --out DIR` writes the original files back, byte for byte. `--check` proves them and writes nothing. `--max-size BYTES` sets the limit on what a rollup may unpack to.
- `README.txt` in every deduplicated rollup says how to restore and carries a Python script for when the tool isn't around.
- A `space:` line at the end of a run gives bytes before, after and saved.
- `release-rollup upgrade --root DIR [--dry-run] [--trash-dir DIR]` turns the plain rollups in a releases or prereleases folder into deduplicated ones. Every file is compared by sha256 with the old zip, the new zip is moved in place in one step, and the old zip goes to the trash afterwards. A zip that is already deduplicated, that shares too little, or that isn't a rollup is left as it is, and a zip that fails its check is refused and untouched. A second run does nothing.
- `release_rollup.Rollup` for reading a rollup of either format from other tools.
- `release-rollup archive --root DIR --out NAME.zip [--dry-run] [--trash-dir DIR]` rolls the plain files of a folder, such as report zips or test kits, into one deduplicated archive. Content shared between files is stored once and each original comes back byte for byte with `restore`, or is proved with `restore --check`. The `README.txt` in the archive says how. New files are added to the same archive on a later run, each file is compared by sha256 with its source before the source goes to the trash, and a repeat run prints `nothing to do`. `INDEX.md` is archived but stays in the folder.
- The manifest takes an optional `top` rule. `"flat"` allows a plain file name where the default rule needs a `v<version>/` folder. An archive is written as format 2 with `"top": "flat"`. Rollups from `run`, `pre` and `upgrade` keep format 1.

### Changed

- A rollup is verified through the same reader that restores it, so every file is rebuilt and compared by sha256 with its source before a source is trashed.
- A rollup stays a plain zip, with the same bytes as `--no-dedupe`, when sharing saves under 1,024 bytes or under 1%, or when nothing new is added. An existing plain rollup becomes deduplicated when it's rewritten for a new member or by `upgrade`, after every file is compared with the old zip.
- On Windows, `run`, `pre` and `upgrade` refuse file names Windows cannot hold (`CON`, `COM1`, `aux.txt`, `NUL`, a trailing dot or space, `:` and similar) before writing anything. The error lists the files, the sources stay where they are, and `--dry-run` refuses too. `upgrade` refuses just the zip that holds one. In 0.1.0 `run` would zip such a tree and trash the sources, and only `restore` on Windows then refused the zip.
- On other systems the same names print one `warning:` line and the rollup is written, because a rollup may be restored on Windows later.
- `COM` and `LPT` followed by a superscript digit count as reserved names.
- Format 2 is the highest format that is read. A rollup with a higher number is refused by name.

### Fixed

- A full disk, an unreadable source or a file name the system refuses to open (`NUL` or a trailing dot on Windows) now ends in one `error:` line and exit code 1 instead of a traceback.

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
