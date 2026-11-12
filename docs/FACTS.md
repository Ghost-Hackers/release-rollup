# Facts

Commands that work, and behavior worth knowing before you change something.

## Commands

Run the tests:

```
cd release-rollup
python3 -m unittest discover -s tests -v
```

On Windows use `py -3.14` instead of `python3` in every command here.

Tests plus byte-compile:

```
bash tools/check.sh
```

On Windows `python3` is the Microsoft Store shortcut, so pass the real interpreter: `PY="py -3.14" bash tools/check.sh` (Git Bash).

Run from a checkout without installing:

```
PYTHONPATH=src python3 -m release_rollup run --root /path/to/releases --dry-run
```

Install the console script into a virtualenv:

```
python3 -m venv .venv && .venv/bin/pip install . && .venv/bin/release-rollup run --root /path/to/releases
```

Try it on made-up releases:

```
T=$(mktemp -d)
for v in 0.1.0 0.1.1 0.2.0 0.2.1; do
  mkdir -p $T/v$v/assets; echo n > $T/v$v/RELEASE_NOTES.md; echo s > $T/v$v/SHA256SUMS; echo $v > $T/v$v/assets/a
done
PYTHONPATH=src python3 -m release_rollup run --root $T --trash-dir $T.trash
```

Expected result: `0.1.zip`, `v0.2.0.zip` and `v0.2.1/` in `$T`, the old folders in `$T.trash`.

Tidy the tester builds after each publish:

```
release-rollup pre --root prereleases --current <VERSION>
```

Full form: `release-rollup pre [--root DIR] [--current VERSION] [--dry-run] [--marker NAME ...] [--trash-dir DIR]`. The `pre` tests are the `Prereleases` class at the end of `tests/test_rollup.py`.

Archive a folder of plain files: `PYTHONPATH=src python3 -m release_rollup archive --root DIR --out NAME.zip [--dry-run] [--trash-dir DIR]`. Prove it with `restore NAME.zip --check`.

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
- `host_is_windows()` in `core.py` decides whether a reserved name refuses the run. Nothing switches it from outside: the tests check the real behavior of the host they run on.
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
