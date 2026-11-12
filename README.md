# release-rollup

[![CI](https://github.com/Ghost-Hackers/release-rollup/actions/workflows/ci.yml/badge.svg)](https://github.com/Ghost-Hackers/release-rollup/actions/workflows/ci.yml)

A small command line tool that keeps a releases folder tidy. You publish releases as `v<version>/` folders, and release-rollup zips the old ones by version line so only the newest release stays unpacked. It is for any project that publishes releases as folders and doesn't want the old ones piling up. Content that repeats across the builds in a zip is stored once, and each zip is tested and compared by sha256 with its source before anything goes to the trash.

Before, with 0.2.0 just published:

```text
releases/
  v0.1.0/
  v0.1.1/
  v0.1.2/
  v0.2.0/
```

After `release-rollup run --root releases`:

```text
releases/
  0.1.zip
  v0.2.0/
```

## Requirements

Python 3.14 or later and the standard library, nothing else. Tested on Linux and on Windows 11 (Git Bash for the shell scripts). On Windows `python3` is the Microsoft Store shortcut, so use `py -3.14`. There is no `gio` there either, so pass `--trash-dir` to say where replaced files go.

## Install

From an unpacked release zip or a clone:

```sh
pip install .
```

Or run it without installing, from the `src/` folder:

```sh
cd src
python3 -m release_rollup run --root path/to/releases --dry-run
```

## Quick start

```sh
# see what would happen, nothing is changed
release-rollup run --root releases --dry-run
# do it
release-rollup run --root releases
# on Windows, name a folder for the replaced files
release-rollup run --root releases --trash-dir C:/trash
# get the original files back out of a rollup
release-rollup restore releases/0.1.zip --out restored
```

## Commands

| Command | Purpose |
| --- | --- |
| `run` | Zip the old releases in a releases folder and trash the pre-release builds of published versions |
| `pre` | Zip the older alpha, beta and rc builds in a prereleases folder |
| `restore` | Prove a rollup and write its original files back, byte for byte |
| `upgrade` | Turn plain rollup zips into deduplicated ones |
| `archive` | Roll the files of a folder into one deduplicated archive |

## Documentation

| Document | What's in it |
| --- | --- |
| [docs/usage.md](docs/usage.md) | Every command and option, a worked example, exit codes |
| [docs/how-it-works.md](docs/how-it-works.md) | The rollup rules, a timeline, verification and edge cases |
| [docs/FACTS.md](docs/FACTS.md) | Working commands and behavior to know before changing the code |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release |
| [SECURITY.md](SECURITY.md) | How to report a security problem privately |

## Development

```sh
bash tools/check.sh
```

runs the unit tests and byte-compiles the package. On Windows use `PY="py -3.14" bash tools/check.sh` from Git Bash.

`tools/e2e.sh` runs the end-to-end checks a release goes through, on a built release zip:

```sh
bash tools/e2e.sh release-rollup-0.2.0.zip [--cross release-rollup-0.1.0.zip]
bash tools/e2e.sh --self-check release-rollup-0.2.0.zip
```

It prints one PASS or FAIL line per check and exits 0 only when none failed. `--cross` adds the checks against a 0.1.x release, and `--self-check` makes sure a zip with a changed byte fails.

## License

Apache License 2.0, see [LICENSE](LICENSE). Copies and changed versions have to keep the license and the credit in [NOTICE](NOTICE).
