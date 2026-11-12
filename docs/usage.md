# Usage

The commands are `run` for a releases folder and `pre` for a prereleases folder. `release-rollup --version` prints the version.

## run

```
release-rollup run [--root DIR] [--dry-run] [--marker NAME ...] [--trash-dir DIR] [--keep-rc|--keep-pre] [--zip-newest]
```

Every release except the newest gets zipped on its own (`v0.1.3.zip`). When the first release of the next minor line shows up, the zips of the old line merge into one named after the line (`0.1.zip`, holding `v0.1.0/`, `v0.1.1/` and so on). At 1.0.0 everything below it becomes `pre-1.0.0.zip`. After that 1.0.x rolls into `1.0.zip` when 1.1.0 appears, and at 2.0.0 all the 1.x line zips roll into `1.x.zip`.

A folder counts as a published release when it holds `RELEASE_NOTES.md`, `SHA256SUMS` and an `assets/` folder. Pre-release builds (alpha, beta and release candidate) such as `v1.2.0-beta.1` or `v1.2.0-rc.4` never count as a release and are never zipped.

Once `X.Y.Z` is published, in any form (a `v<X.Y.Z>/` folder, its own zip, or inside a line zip or `pre-1.0.0.zip`), every `v<X.Y.Z>-alpha.<N>`, `-beta.<N>` and `-rc.<N>` folder (with or without the `v`) or `.zip` in the releases folder goes to the trash too. A pre-release build of a version that is not published yet stays. This happens last, after the zips of the run were checked.

Each zip is checked before anything is removed: the archive is tested, and every file in it is compared by sha256 with its source. Only then do the old folders and zips go to the trash. If a check fails, the sources stay where they are.

| Option | Meaning |
| --- | --- |
| `--root DIR` | The releases folder. Defaults to the current directory |
| `--dry-run` | Print what would happen and change nothing. Needs neither `gio` nor `--trash-dir` |
| `--marker NAME` | A file a `v<version>/` folder must hold to count as published. Repeat it for several, and end a name with `/` for a folder. Giving any marker replaces the default list, which is `RELEASE_NOTES.md`, `SHA256SUMS` and `assets/` |
| `--trash-dir DIR` | Move replaced folders and zips into `DIR` instead of using `gio trash` |
| `--keep-rc`, `--keep-pre` | Leave the alpha, beta and release candidate builds alone even when their release is published |
| `--zip-newest` | Zip the newest release too, into `v<version>.zip`, after the same check as every other zip |

With `--zip-newest` nothing stays unpacked. The newest release's zip is merged into a line zip only once its line closes, like any other per-release zip. A project that has no `RELEASE_NOTES.md` passes the markers it does have: `--marker SHA256SUMS --marker assets/`.

Replaced folders and zips are sent to the trash with `gio trash`. If `gio` is not installed (it is not on Windows), or you would rather keep them somewhere specific, pass `--trash-dir DIR`. Things are moved into that folder, never deleted. Without `gio` and without `--trash-dir` the tool stops with an error before it touches anything.

## pre

```
release-rollup pre [--root DIR] [--current VERSION] [--dry-run] [--marker NAME ...] [--trash-dir DIR]
```

`pre` tidies a project's `prereleases/` folder, which holds `alpha/`, `beta/` and `rc/` with one folder per tester build (`prereleases/alpha/v1.0.0-alpha.4/`). A build counts as finished when it holds `PRERELEASE.json` and `SHA256SUMS`. Folders that are not finished yet are left alone, so the previous build stays unpacked until the new one is complete.

Older builds of one version in a stage folder go into `v<x.y.z>-<stage>.x.zip`. The newest finished build of each stage stays unpacked while its version is unreleased. Zips are verified like those of `run` before any folder is trashed.

| Option | Meaning |
| --- | --- |
| `--root DIR` | The `prereleases/` folder. Defaults to the current directory |
| `--current VERSION` | The version you are on now. `1.1.0-beta.2` means 1.1.0 and later are unreleased, `1.1.0` means only versions later than 1.1.0 are. Every other version is treated as released, and all of its builds get rolled up. Without `--current` only the newest version found counts as unreleased |
| `--dry-run` | Print the plan and change nothing |
| `--marker NAME` | A file a build folder must hold to count as finished. Repeat it for several, and end a name with `/` for a folder. Giving any marker replaces the default list, which is `PRERELEASE.json` and `SHA256SUMS` |
| `--trash-dir DIR` | Move replaced folders and zips into `DIR` instead of using `gio trash` |

Before:

```
prereleases/alpha/
  v1.0.0-alpha.4/
  v1.0.0-alpha.5/
  v1.0.0-alpha.6/
```

```
$ release-rollup pre --root prereleases --current 1.0.0-alpha.6
```

After:

```
prereleases/alpha/
  v1.0.0-alpha.x.zip      (holds alpha.4 and alpha.5)
  v1.0.0-alpha.6/
```

## Example

Before, with 0.2.0 just published:

```
releases/
  v0.1.0/
  v0.1.1/
  v0.1.2/
  v0.2.0/
```

```
$ release-rollup run --root releases --dry-run
would write 0.1.zip (3 releases, 9 files); trash v0.1.0, v0.1.1, v0.1.2
$ release-rollup run --root releases
wrote 0.1.zip (3 releases, 9 files, 2,914 bytes)
trashed v0.1.0
trashed v0.1.1
trashed v0.1.2
```

After:

```
releases/
  0.1.zip
  v0.2.0/
```

Release candidates of a published version go the same way:

```
$ release-rollup run --root releases --dry-run
would trash 0.2.0-rc.4 (release 0.2.0 is published)
would trash v0.2.0-rc.5 (release 0.2.0 is published)
```

Run it again and it prints `nothing to do`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Done, or nothing to do |
| 1 | A zip failed its check, an existing archive could not be read, or there is no way to trash files. Sources are left in place |
| 2 | Bad command line, or `--root` is not a folder |
