# Facts

Commands that work, and behavior worth knowing before you change something.

## Contents

- [Commands](#commands)
- [CI](#ci)
- [Releasing](#releasing)
- [Behavior](#behavior)

## Commands

Run the tests:

```sh
cd release-rollup
python3 -m unittest discover -s tests -v
```

On Windows use `py -3.14` instead of `python3` in every command here.

Tests plus byte-compile:

```sh
bash tools/check.sh
```

On Windows `python3` is the Microsoft Store shortcut, so pass the real interpreter: `PY="py -3.14" bash tools/check.sh` (Git Bash).

`check.sh` needs:

- coverage.py 7.17.3, the version CI pins: `python3 -m pip install coverage==7.17.3`
- `RR_SMALLFS`, a folder on a filesystem of a few MB, for the tests that fill a disk. On Linux `check.sh` makes one itself, a 4 MB tmpfs in a private mount namespace. On Windows set it to a folder on a small NTFS drive (CI uses an 8 MB VHD).
- on Windows, `RR_NOLINKS_FS`, a folder on a FAT32 drive, for the tests of a filesystem without hard links
- a normal user. Root reads past file permissions, so the tests that lock files can't see the refusal they check for, and `check.sh` stops when run as root.

Coverage has to reach 100% of lines and branches on every host. A line no test can reach goes in [`tools/coverage_exclusions.tsv`](../tools/coverage_exclusions.tsv) with the file, the line or `from->to` branch, the host, the line's text and the proof. [`tools/check_coverage.py`](../tools/check_coverage.py) fails when a missed line isn't listed, when a listed line did run, when a listed line's text no longer matches the source, or when the total is below 100%.

End-to-end checks on a release zip (0.2.0 or newer; the zip is unpacked into a temp folder):

```sh
bash tools/e2e.sh release-rollup-<version>.zip
bash tools/e2e.sh release-rollup-<version>.zip --cross release-rollup-0.1.0.zip
bash tools/e2e.sh --self-check release-rollup-<version>.zip
```

On Windows from Git Bash set `PY="py -3.14"` first. A SHA256SUMS file next to the zip is verified if present. The gio trash checks write under `~/.cache/release-rollup-e2e` and trash it afterwards; without gio the script prints an INFO line, since the no-gio refusal is that system's trash check.

Run from a checkout without installing:

```sh
PYTHONPATH=src python3 -m release_rollup run --root /path/to/releases --dry-run
```

Install the console script into a virtualenv:

```sh
python3 -m venv .venv && .venv/bin/pip install . && .venv/bin/release-rollup run --root /path/to/releases
```

Try it on made-up releases:

```sh
T=$(mktemp -d)
for v in 0.1.0 0.1.1 0.2.0 0.2.1; do
  mkdir -p $T/v$v/assets; echo n > $T/v$v/RELEASE_NOTES.md; echo s > $T/v$v/SHA256SUMS; echo $v > $T/v$v/assets/a
done
PYTHONPATH=src python3 -m release_rollup run --root $T --trash-dir $T.trash
```

Expected result: `0.1.zip`, `v0.2.0.zip` and `v0.2.1/` in `$T`, the old folders in `$T.trash`.

Tidy the tester builds after each publish:

```sh
release-rollup pre --root prereleases --current <VERSION>
```

Full form: `release-rollup pre [--root DIR] [--current VERSION] [--dry-run] [--marker NAME ...] [--trash-dir DIR]`. The `pre` tests are the `Prereleases` class at the end of `tests/test_rollup.py`.

Archive a folder of plain files: `PYTHONPATH=src python3 -m release_rollup archive --root DIR --out NAME.zip [--dry-run] [--trash-dir DIR]`. Prove it with `restore NAME.zip --check`.

## CI

A pull request into master needs these checks, all from GitHub Actions:

| Check | Workflow | What it runs |
|---|---|---|
| Tests (ubuntu-latest), Tests (windows-latest), Tests (windows-11-arm) | [`ci.yml`](../.github/workflows/ci.yml) | `check.sh` with coverage, the release zip build and the end-to-end checks, one job per runner |
| Pyright strict | [`ci.yml`](../.github/workflows/ci.yml) | Pyright on `src/`, `tests/` and `tools/`, the folders `[tool.pyright]` in `pyproject.toml` includes |
| Changelog / Entry | [`changelog-check.yml`](../.github/workflows/changelog-check.yml) | a change under `src/` needs a line under `## [Unreleased]`, or the `no changelog` label |
| Version numbers | [`release-check.yml`](../.github/workflows/release-check.yml) | `tools/check_release.py versions` |
| CodeQL (python), CodeQL (actions) | [`codeql.yml`](../.github/workflows/codeql.yml) | code scanning |
| Review | [`claude-review.yml`](../.github/workflows/claude-review.yml) | the Claude review |
| Unchanged | [`claude-copy-check.yml`](../.github/workflows/claude-copy-check.yml) | the files copied from ci-workflows weren't edited here |

When every changed file is Markdown or under `docs/`, the "Changed files" job reports `code=false`: the three Tests jobs still start but skip their steps, and Pyright skips. Pushes to master always run everything.

`changed-files.yml`, `changelog.yml`, `review-retarget.yml`, the Claude workflows and `.github/scripts/check_copy.py` are copies from [Ghost-Hackers/ci-workflows](https://github.com/Ghost-Hackers/ci-workflows), made with its `tools/sync.py`. Change them there and sync again. `.github/ci-workflows-copy.txt` lists them, and `.github/dependabot.yml` leaves them out.

`release/0.1.x` has its own ruleset: Tests (ubuntu-latest), Tests (windows-latest), Changelog entry and the two CodeQL checks, from that branch's own workflows.

## Releasing

A release starts as a pull request that sets the new version everywhere `tools/check_release.py versions` looks: `VERSION`, `pyproject.toml` in its PEP 440 form (`0.2.0b3` for `0.2.0-beta.3`), the fallback `__version__` in `src/release_rollup/__init__.py`, and a `## [x.y.z] - date` heading in `CHANGELOG.md` with its link at the bottom. After it merges, tag the merge commit and push the tag on its own:

```sh
git tag -a v0.2.0 <merge commit> -m "Release 0.2.0"
git push origin v0.2.0
```

The tag has to be annotated, and it has to go up alone: a tag pushed together with a new branch didn't start the tag workflow. Tags matching `v*` can't be moved or deleted once pushed.

The tag runs [`release.yml`](../.github/workflows/release.yml). It checks the versions against the tag, runs the tests, builds the zip with `tools/build_release.py`, attests it, and opens a draft release with the zip and `SHA256SUMS`, marked as a pre-release when the version has a `-`. The notes are written on the draft before it's published.

To check a published zip, compare it with a local build of the tag and verify the attestation:

```sh
python3 tools/build_release.py --out rebuilt --ref v0.2.0
gh attestation verify release-rollup-0.2.0.zip --repo Ghost-Hackers/release-rollup
```

`gh attestation verify` prints nothing when its output isn't a terminal, so check its exit code or pass `--format json`.

`release/0.1.x` holds fixes for the 0.1 line; 0.1.1 shipped from it. Pull requests into it go through its own ruleset and its own workflows, and they don't close issues on merge, so close those by hand with "Done in #N". CI on master checks each new build against the newest published 0.1.x release with `tools/e2e.sh --cross`.

## Behavior

Rules of `pre`:

- `--root` is the `prereleases/` folder with `alpha/`, `beta/`, `rc/`. Builds are `<stage>/v<x.y.z>-<stage>.<n>/`. A build is finished when it holds the markers, default `PRERELEASE.json` and `SHA256SUMS`. Unfinished folders are left alone.
- Older builds of one version go into `<stage>/v<x.y.z>-<stage>.x.zip`. Later runs rewrite the zip (temp file, verified, replaced) and trash only the folders that went in.
- The newest finished build of each stage stays unpacked while its version is unreleased. With `--current` as a pre-release (1.1.0-beta.2), its base 1.1.0 and later are unreleased. With a final `--current` (1.1.0), only later versions are. Anything below is rolled up completely, so a released version is at most one zip per stage. Without `--current` only the newest version found is unreleased.
- Zips are verified like `run` (testzip, sha256 of every member) before a source is trashed. A failure keeps the sources.
- An unreadable rollup zip, or one with a top-level entry that is not a build of that version and stage, stops the run with nothing changed.
- Folders of another stage, other names and unknown stage folders (`gamma/`) are ignored.
- A build unpacked again from the zip is checked against the zip on the next run, then trashed.
- A bad `--current` (`1.0`, `1.0.0-gamma.1`) is an error. A rerun prints `nothing to do`.

General:

- The tests have no dependency on `gio`. They use `--trash-dir` style moves.
- `VERSION` and `src/release_rollup/VERSION` must match. `pyproject.toml` carries the same number in PEP 440 form (`0.2.0b1` for `0.2.0-beta.1`), and `tests/test_dedupe.py` checks all of them agree.
- `host_is_windows()` in `core.py` decides whether a reserved name refuses the run. Nothing switches it from outside: the tests check how the host they run on behaves.
- Names Windows can't hold on disk (`NUL`, `aux.c`, `trail.`, `a:b`, case clashes) reach the tool inside a zip or an archive manifest, so the same test runs on both systems. Windows refuses them with nothing written or trashed; Linux prints a warning and carries on.
- `tests/hosts.tsv` lists the few tests that only make sense on one system, and every test module's `load_tests` leaves out the other system's rows. `tests/test_hosts.py` checks that every row names a test that exists. All other tests run on both systems.
- `zipfile` on Windows turns a backslash in an entry name into a slash when it reads the zip, so `v0.1.0\evil` is read as `v0.1.0/evil`: a plain file inside the release folder.
- `os.kill(pid, signal.SIGTERM)` on Windows ends the process at once with exit code 15. There is no `SIGKILL`; the kill tests use `kill_plan()` in `tests/test_dedupe.py` to pick each system's call and return code.
- The zip interop test reads a rollup with `unzip` on Linux and with Windows' own `tar.exe` (in `System32`) on Windows.
- The symlink tests create symlinks on both systems. On Windows that needs Developer Mode or an elevated shell, and the tests fail without it.
- Zip members have a fixed 1980-01-01 timestamp and mode 0644, and are sorted by name.
- The temporary zip is named `.<target>.<random>.tmp` in the releases folder and is removed on failure.
- A directory marker needs a trailing slash (`--marker assets/`). Without it the marker must be a file.
- Version comparison is numeric on the three parts, so 0.10.0 sorts above 0.9.0.
- Three beta builds of one app (15 files, 41,026,579 bytes unpacked) come to 13,698,897 bytes deduplicated, in 113 entries.
- Other tools that open a rollup with `zipfile` directly see only `README.txt` and `.rollup/`. They should use `release_rollup.Rollup`: `names()`, `files()`, `open(name)`.
- `Rollup` on a deduplicated zip reads the manifest on every open, parsed once per content through a small cache. `verify` opens the source zip once per file, so this matters.
- The temporary files of a write are `.<target>.<random>.tmp`, `.tmp.spool/` and `.tmp.plain.<random>.tmp`. A killed run can leave them behind; the tool ignores them.
- `restore` work folders are named `.rollup-restore.<random>.tmp`, in the `--out` folder.
- `upgrade` leaves `.<zip>.upgrade-old` next to a zip when a run dies between the rename and the trash. The next `upgrade` trashes it once the zip in place proves all its files.
- A reader that doesn't want this package as a dependency can read `.rollup/MANIFEST.json` with the standard library. The file list is `files[].path`. The bytes are a blob, or a skeleton plus blobs, proved by the `sha256` of the entry.

[![Back to top](https://img.shields.io/badge/Back%20to%20top-222?style=plastic&logo=github)](#facts)
