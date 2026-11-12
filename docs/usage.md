# Usage

`run` works on a releases folder, `pre` on a prereleases folder, `upgrade` converts older rollups, `archive` rolls up a folder of plain files and `restore` gets the original files back. `release-rollup --version` prints the version.

## run

```
release-rollup run [--root DIR] [--dry-run] [--marker NAME ...] [--trash-dir DIR] [--keep-rc|--keep-pre] [--zip-newest] [--no-dedupe]
```

Every release except the newest gets zipped on its own (`v0.1.3.zip`). When the first release of the next minor line shows up, the zips of the old line merge into one named after the line (`0.1.zip`, holding `v0.1.0/`, `v0.1.1/` and so on). At 1.0.0 everything below it becomes `pre-1.0.0.zip`. After that 1.0.x rolls into `1.0.zip` when 1.1.0 appears, and at 2.0.0 all the 1.x line zips roll into `1.x.zip`.

A folder counts as a published release when it holds `RELEASE_NOTES.md`, `SHA256SUMS` and an `assets/` folder. Pre-release builds (alpha, beta and release candidate) such as `v0.2.0-beta.1` or `v0.2.0-rc.4` never count as a release and are never zipped.

Once `X.Y.Z` is published, in any form (a `v<X.Y.Z>/` folder, its own zip, or inside a line zip or `pre-1.0.0.zip`), every `v<X.Y.Z>-alpha.<N>`, `-beta.<N>` and `-rc.<N>` folder (with or without the `v`) or `.zip` in the releases folder goes to the trash too. A pre-release build of a version that is not published yet stays. This happens last, after the zips of the run were checked.

Rollups are deduplicated: content that repeats across the builds in a zip, including the members of zips inside it, is stored once. Three beta builds that are 41,026,579 bytes unpacked take 13,698,897 bytes, about a third. See [Deduplicated rollups](#deduplicated-rollups).

Each zip is checked before anything is removed: the archive is tested, and every file in it is compared by sha256 with its source. Only then do the old folders and zips go to the trash. If a check fails, the sources stay where they are.

| Option | Meaning |
| --- | --- |
| `--root DIR` | The releases folder. Defaults to the current directory |
| `--dry-run` | Print what would happen and change nothing. Needs neither `gio` nor `--trash-dir` |
| `--marker NAME` | A file a `v<version>/` folder must hold to count as published. Repeat it for several, and end a name with `/` for a folder. Giving any marker replaces the default list, which is `RELEASE_NOTES.md`, `SHA256SUMS` and `assets/` |
| `--trash-dir DIR` | Move replaced folders and zips into `DIR` instead of using `gio trash` |
| `--keep-rc`, `--keep-pre` | Leave the alpha, beta and release candidate builds alone even when their release is published |
| `--zip-newest` | Zip the newest release too, into `v<version>.zip`, after the same check as every other zip |
| `--no-dedupe` | Write plain zips instead of storing shared content once. See [Deduplicated rollups](#deduplicated-rollups) |

With `--zip-newest` nothing stays unpacked. The newest release's zip is merged into a line zip only once its line closes, like any other per-release zip. A project that has no `RELEASE_NOTES.md` passes the markers it does have: `--marker SHA256SUMS --marker assets/`.

Replaced folders and zips are sent to the trash with `gio trash`. If `gio` is not installed (it is not on Windows), or you would rather keep them somewhere specific, pass `--trash-dir DIR`. Things are moved into that folder, never deleted. Without `gio` and without `--trash-dir` the tool stops with an error before it touches anything.

## pre

```
release-rollup pre [--root DIR] [--current VERSION] [--dry-run] [--marker NAME ...] [--trash-dir DIR] [--no-dedupe]
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
| `--no-dedupe` | Write plain zips instead of storing shared content once. See [Deduplicated rollups](#deduplicated-rollups) |

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

## Deduplicated rollups

`run` and `pre` store shared content once unless you pass `--no-dedupe`. The result is still an ordinary `.zip` with the same name, but unzipping it gives `README.txt` and a `.rollup/` folder of pieces named by their sha256, not your files. To get the files back, byte for byte, use `restore`.

A rollup where sharing saves under 1,024 bytes or under 1% stays a plain zip, byte for byte what `--no-dedupe` writes, and the run says so. Existing plain rollups keep working and are rebuilt when a new member arrives or when you run `upgrade`.

Each run that changes something ends with what it did to the disk: the sources and old zips it replaced against the zips they became.

```
space: before 41,026,579 bytes, after 13,698,897 bytes, saved 27,327,682 bytes (66.6%)
```

Names Windows cannot hold (`CON`, `aux.txt`, `NUL`, a name ending in a dot or space, a `:` or `?` in a name) are checked before anything is written. On Windows, `run`, `pre` and `upgrade` refuse them with a message that lists the files, and nothing is touched, because a rollup holding such a name could not be restored there.

On Linux the same names only produce a `warning:` line and the rollup is written, since the zip is fine there and `restore` works. Rename such files first if the rollup may be restored on Windows.

## restore

```
release-rollup restore ROLLUP.zip [--out DIR] [--check] [--max-size BYTES]
```

```
release-rollup restore ROLLUP.zip --out DIR
release-rollup restore ROLLUP.zip --check
```

| Option | Meaning |
| --- | --- |
| `ROLLUP.zip` | The rollup, deduplicated or plain. `restore` reads plain zips too |
| `--out DIR` | Write the files under `DIR` only after every file is proven. It refuses to overwrite anything |
| `--check` | Prove every file and write nothing |
| `--max-size BYTES` | Refuse a rollup that unpacks to more than this. The default is 64 GiB (68,719,476,736 bytes) |

If you don't have the tool, `README.txt` in the zip has a short Python script that does the same from an unzipped copy.

## upgrade

```
release-rollup upgrade [--root DIR] [--dry-run] [--trash-dir DIR]
```

`upgrade` converts rollups that were written plain. It looks at the `.zip` files directly in the root folder and in its `alpha/`, `beta/` and `rc/` folders. Each plain rollup is rewritten as a deduplicated one next to the old zip, every file is compared by sha256 with the old zip, and only then is the new zip moved in place and the old one trashed. A zip that is already deduplicated is left alone, so a second run prints `nothing to do`.

A zip that fails its check, or can't be stored in a rollup, is reported and not touched. The other zips are still upgraded and the exit code is 1. The unpacked newest builds are never touched.

| Option | Meaning |
| --- | --- |
| `--root DIR` | The releases folder, or the prereleases folder holding `alpha/`, `beta/` and `rc/`. Defaults to the current directory |
| `--dry-run` | List what would be upgraded and write nothing |
| `--trash-dir DIR` | Move the old zips into `DIR` instead of using `gio trash` |

## archive

```
release-rollup archive --out NAME.zip [--root DIR] [--dry-run] [--trash-dir DIR]
```

`run` and `pre` need `v<version>/` folders. For a folder of plain files, such as report zips in an outbox or test kits, use `archive`.

Every file directly in the root folder (`outbox-report.zip`, `kit-0.1.0.zip`, a log) goes into one deduplicated archive. A bare `NAME.zip` is created inside the root folder. Zips among the files are cut open the same way as in a rollup, so content that repeats across files is stored once, and each original comes back byte for byte. Hidden files, folders and symlinks are left alone.

Run it again whenever new files turn up. They are added to the same archive, which is rewritten next to the old one, checked, and moved in place. Each file is compared by sha256 with its source before the source goes to the trash, and a failed check leaves everything as it was. A second run with nothing new prints `nothing to do`. A file whose name is already in the archive with other bytes is refused rather than replaced.

`INDEX.md` is the exception: it goes into the archive too but stays in the root folder, so it can be read without unzipping, and a changed one is refreshed in the archive.

`restore NAME.zip --check` and `restore NAME.zip --out DIR` (see [restore](#restore)) work on an archive like on any rollup. Names with a folder part, a backslash, a colon, a control character or `..` are refused on every system. On Windows the reserved names (`CON`, `aux.txt`, a trailing dot) are refused too, and elsewhere they give the usual warning.

An archive always uses the deduplicated form, even when nothing is shared, so adding files later works the same way. With nothing to share it comes out slightly larger than the files, about half a percent on a set of eight unrelated report zips. Sharing only happens where the compressed bytes are identical: two test kits that carry the same Python runtime save that runtime once, but the release zip inside each kit is compressed a second time by the kit and is not shared.

| Option | Meaning |
| --- | --- |
| `--out NAME.zip` | The archive. Required. A bare name is created inside `--root`. Files already in it are kept and new ones are added |
| `--root DIR` | The folder whose files are rolled up. Defaults to the current directory |
| `--dry-run` | Print what would happen and change nothing |
| `--trash-dir DIR` | Move the archived files into `DIR` instead of using `gio trash` |

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
| 1 | A zip failed its check, an existing archive could not be read, a file name was refused (Windows names, or one the system cannot open), a `restore` was refused, an `upgrade` refused a zip, or there is no way to trash files. Sources are left in place |
| 2 | Bad command line, or `--root` is not a folder |
