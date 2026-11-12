# release-rollup

A small command line tool that keeps a releases folder tidy. You publish releases as `v<version>/` folders, and release-rollup zips the old ones by version line so only the newest release stays unpacked. It is for any project that publishes releases as folders and doesn't want the old ones piling up. Each zip is tested and compared by sha256 with its source before anything goes to the trash.

Before, with 0.2.0 just published:

```
releases/
  v0.1.0/
  v0.1.1/
  v0.1.2/
  v0.2.0/
```

After `release-rollup run --root releases`:

```
releases/
  0.1.zip
  v0.2.0/
```

## Requirements

Python 3.14 or later and the standard library, nothing else. Tested on Linux and on Windows 11 (Git Bash for the shell scripts). On Windows `python3` is the Microsoft Store shortcut, so use `py -3.14`. There is no `gio` there either, so pass `--trash-dir` to say where replaced files go.

## Install

From an unpacked release zip or a clone:

```
pip install .
```

Or run it without installing, from the `src/` folder:

```
cd src
python3 -m release_rollup run --root path/to/releases --dry-run
```

## Quick start

```
# see what would happen, nothing is changed
release-rollup run --root releases --dry-run
# do it
release-rollup run --root releases
# on Windows, name a folder for the replaced files
release-rollup run --root releases --trash-dir C:/trash
# tidy a prereleases/ folder of alpha, beta and rc builds
release-rollup pre --root prereleases --current 1.0.0-alpha.6 --dry-run
```

## Commands

| Command | Purpose |
| --- | --- |
| `run` | Zip the old releases in a releases folder and trash the pre-release builds of published versions |
| `pre` | Zip the older alpha, beta and rc builds in a prereleases folder |

## Documentation

| Document | What's in it |
| --- | --- |
| [docs/usage.md](docs/usage.md) | Every command and option, a worked example, exit codes |
| [docs/how-it-works.md](docs/how-it-works.md) | The rollup rules, a timeline, verification and edge cases |
| [docs/FACTS.md](docs/FACTS.md) | Working commands and behavior to know before changing the code |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release |
| [SECURITY.md](SECURITY.md) | How to report a security problem privately |

## Development

```
bash tools/check.sh
```

runs the unit tests and byte-compiles the package. On Windows use `PY="py -3.14" bash tools/check.sh` from Git Bash.

## License

Apache License 2.0, see [LICENSE](LICENSE). Copies and changed versions have to keep the license and the credit in [NOTICE](NOTICE).
