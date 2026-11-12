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

Full form: `release-rollup pre [--root DIR] [--current VERSION] [--dry-run] [--marker NAME ...] [--trash-dir DIR]`. The suite has 69 tests, all in `tests/test_rollup.py`, 15 of them in the `Prereleases` class at the end of `tests/test_rollup.py`.

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
- `VERSION` and `src/release_rollup/VERSION` must match. `pyproject.toml` carries the same number in PEP 440 form (`1.2.0b1` for `1.2.0-beta.1`).
- Zip members have a fixed 1980-01-01 timestamp and mode 0644, and are sorted by name.
- The temporary zip is named `.<target>.<random>.tmp` in the releases folder and is removed on failure.
- A directory marker needs a trailing slash (`--marker assets/`). Without it the marker must be a file.
- Version comparison is numeric on the three parts, so 0.10.0 sorts above 0.9.0.
