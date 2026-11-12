# How it works

The rules behind `run`, `pre`, `upgrade`, `archive` and `restore`. Options and exit codes are in [usage.md](usage.md).

## The rule

Releases live in one folder as `v<version>/` directories. The newest one stays a plain folder so it can be read and uploaded as is. Everything older is zipped, and the zips get coarser as they age.

With `--zip-newest` the newest release is zipped on its own as well (`v0.1.3.zip`), with the same check before the folder goes to the trash, so nothing stays unpacked. From then on it follows the same steps as every other release. A rerun finds the zip and does nothing.

1. Every release except the newest is zipped on its own: `v0.1.3.zip`.
2. When the first release of the next minor line appears, the zips of the old line merge into one zip named after the line: `0.1.zip`, holding `v0.1.0/`, `v0.1.1/` and so on. This also happens for a line with a single release.
3. When 1.0.0 appears, everything below it merges into `pre-1.0.0.zip`.
4. After that the same pattern repeats. `1.0.x` rolls into `1.0.zip` when 1.1.0 appears. When 2.0.0 appears, all the 1.x line zips roll into `1.x.zip`, and so on for each major.

## Pre-release builds

A folder or zip named `v<x.y.z>-<stage>.<n>` or `<x.y.z>-<stage>.<n>` (with or without the `v`) is a pre-release build. The stage is `alpha`, `beta` or `rc`. It is trashed once release x.y.z is published, whichever form that release has: a `v<x.y.z>/` folder with its markers, `v<x.y.z>.zip`, or a member of a line zip, `pre-1.0.0.zip` or a major zip. Which versions a zip holds is read from the zip itself, not from its name. Every build of that version goes, whatever its stage and number. Leftovers are listed oldest first: alpha, then beta, then rc, with the number compared as a number (`rc.9` before `rc.10`). The order is the one `vkey` in `core.py` implements, and the tests pin it. A build of a version that is not published yet, such as `0.2.0-rc.4` while only 0.1.x exists, stays until 0.2.0 is published. Names that only start like a candidate, such as `0.2.0-rc.4-notes/`, are not touched.

This step runs last, after every zip of the run passed its check, so a failed zip leaves the candidates in place. `--keep-rc` (or `--keep-pre`) skips it.

"Newest" means the highest published version. Versions are compared as numbers, so 0.10.0 is newer than 0.9.0.

## Timeline

The folder after each release is published and the tool has run:

| Published | Folder contents |
| --- | --- |
| 0.1.0 | `v0.1.0/` |
| 0.1.1 | `v0.1.0.zip`, `v0.1.1/` |
| 0.1.2 | `v0.1.0.zip`, `v0.1.1.zip`, `v0.1.2/` |
| 0.1.3 | `v0.1.0.zip`, `v0.1.1.zip`, `v0.1.2.zip`, `v0.1.3/` |
| 0.2.0 | `0.1.zip`, `v0.2.0/` |
| 0.2.1 | `0.1.zip`, `v0.2.0.zip`, `v0.2.1/` |
| 0.3.0 | `0.1.zip`, `0.2.zip`, `v0.3.0/` |
| 1.0.0 | `pre-1.0.0.zip`, `v1.0.0/` |
| 1.0.1 | `pre-1.0.0.zip`, `v1.0.0.zip`, `v1.0.1/` |
| 1.1.0 | `pre-1.0.0.zip`, `1.0.zip`, `v1.1.0/` |
| 2.0.0 | `pre-1.0.0.zip`, `1.x.zip`, `v2.0.0/` |

Pre-release builds are not shown in the table. Publishing 0.2.0 also trashes `0.2.0-alpha.4`, `0.2.0-beta.1`, `0.2.0-rc.4`, `0.2.0-rc.5` and so on.

Publishing 0.2.0 is the moment the three-way `v0.1.x.zip` pile becomes `0.1.zip`: the new minor line is what triggers the merge. At 1.0.0 the old `0.1.zip`, `0.2.zip` and the single-release zips all fold into `pre-1.0.0.zip`, which then holds `v0.1.0/` through `v0.3.0/`.

## What counts as published

A `v<version>/` folder is published when it holds all of the marker entries. By default those are the files `RELEASE_NOTES.md` and `SHA256SUMS` and the folder `assets/`. A marker name ending in `/` is a folder, anything else is a file.

The version has to be three plain numbers after the `v`. Folders such as `v0.2.0-beta.1` or `v0.2.0-rc.4` (pre-release builds, see below), `v1.0` or `notes/` are not releases and are never read or zipped. Only pre-release builds are ever moved, and only once their release is published. A `v0.1.5/` folder that is missing a marker is treated the same way: it is left alone and does not count as the newest release.

Zips are only picked up if their name is one the tool writes: `v<x.y.z>.zip`, `<x.y>.zip`, `<x>.x.zip` or `pre-1.0.0.zip`. Anything else, such as `backup.zip`, is ignored.

## Verification and safety

For every zip the tool writes, it does this in order:

1. Write the archive under a hidden temporary name in the same folder.
2. Run `testzip` on it.
3. Read every member back, rebuilding it first in a deduplicated rollup, and compare its sha256 with the file it came from. A missing member or an extra one counts as a failure.
4. Rename it into place.
5. Only now send the source folders and older zips to the trash.

If any check fails, the temporary file is removed, the run stops with exit code 1, and no source is touched. Sources go to the trash via `gio trash`, or are moved into `--trash-dir`. Nothing is deleted outright.

The archive has fixed timestamps and sorted entries, so zipping the same release twice gives the same member list.

## Deduplicated rollups

A rollup is a normal zip, so its name, the rules above and every zip tool keep working. What changes is what's inside:

```
README.txt                  how to restore, with a script for when the tool is gone
.rollup/blobs/<sha256>      one distinct piece of content
.rollup/skeleton/<sha256>   an inner zip with its member data cut out
.rollup/MANIFEST.json       which file is made of which pieces
```

Nothing inside is named after anything in a build, which is why a hostile file name in a build has no way to reach an extraction path.

A file that parses as a zip, whatever its extension, is cut at the raw compressed stream of each member. The streams become blobs, stored as they are since they're already compressed, and what remains (local headers, central directory, comment, any junk before or after) is the skeleton. Joining them gives the original file exactly, without compressing anything again, so the result doesn't depend on the zlib build of the machine. Streams under 512 bytes stay in the skeleton because a zip entry of their own costs more than they save. Only the outer level is cut: a zip inside a zip inside a build rides along as one blob. A zip whose entries overlap or reach past the end of the file is treated as an ordinary file, and so is anything else, which is stored as one deflated blob. Nothing is ever inflated, so a member that claims an absurd size does no harm.

`MANIFEST.json` lists each original file with its size and sha256. A `plain` file names one blob. A `zip-split` file names its skeleton and a list of `[offset in the skeleton, length, blob]`. `"format": 1` is the version of this layout, and a newer number is refused with a message rather than guessed at.

Reading always checks. Each blob's and skeleton's sha256 must equal its name, each file's sha256 must equal the manifest, and a bad one stops the read and is named in the error. A manifest is rejected when a path has `..`, an absolute start, a backslash, a drive letter or a control character, when a path sits outside a `v<x.y.z>/` or build folder, when a name repeats or is both a file and a folder, when a reference is out of order or out of range, when sizes don't add up, or when the zip holds an entry nothing uses. `restore` also adds up the sizes before reading anything and refuses more than `--max-size`. On Windows it also refuses reserved names (`CON`, `aux.txt`), names with `:` and the like, trailing dots and spaces, and names that differ only by case. Those are all fine on Linux, where `aux.c` is an ordinary file, so they don't stop a Linux run.

`restore` proves the whole rollup first and writes each file to a work folder inside `--out`, then moves them into place, so a bad rollup or a full disk leaves nothing behind. It refuses if a file already exists, or if a folder it needs is a symlink or a file.

`run` and `pre` write the deduplicated form to the temporary file, then a plain zip beside it to compare. The plain one wins when nothing repeats, or when the deduplicated one doesn't save at least 1,024 bytes and 1%. So a project with nothing to share gets the same bytes as `--no-dedupe`. A rollup over 64 GiB also stays plain, since it would need `--max-size` to restore. A member name a manifest can't hold, such as a backslash in a Linux file name, stops the run with the sources kept, and `--no-dedupe` is the way past it.

## Upgrading plain rollups

`upgrade` goes through the same write path as `run` and `pre`. For each `.zip` in the folder and in `alpha/`, `beta/` and `rc/`, it first opens the zip and checks every entry. A zip that is already deduplicated is skipped. So is a zip with files outside `v<version>/` folders, since that isn't a rollup. A zip that fails its check is refused: nothing is written, and the run reports it, goes on with the others and exits with 1.

The new rollup is written to a temporary file in the same folder and every file in it is compared by sha256 with the old zip. If sharing doesn't pay (the same limits as `run`), the temporary file is dropped and the plain zip stays. Otherwise the old zip is hard-linked (or copied, where links don't work) to `.<name>.upgrade-old`, the temporary file is renamed over the zip in one step, and the `.upgrade-old` file goes to the trash. The zip is never missing or half written, and the old bytes are in the trash.

If the run dies before the rename, the old zip is untouched and only a temporary file is left. If it dies after the rename, the new zip is in place and `.<name>.upgrade-old` is left.

The next `upgrade` trashes that file once the zip in place proves every file of it. When it doesn't, the file stays and a message says so. A zip that stays plain because nothing is shared is rewritten into a temporary file again on each run, then dropped.

## Archiving a folder of files

`archive` uses the same container as `run` and `pre`, with a different rule for the names inside. A rollup manifest accepts only paths that start with a `v<x.y.z>/` or build folder. The manifest of an archive carries `"top": "flat"`, which allows exactly one path part: a plain file name such as `outbox-report.zip` or `INDEX.md`. A manifest with no `top` key keeps the rule from 0.1.0, so every earlier rollup reads and restores as before.

An archive is written as `"format": 2`. A release-rollup older than 0.2.0 refuses it by name as written by a newer version, since it would otherwise find paths it does not accept. Rollups from `run`, `pre` and `upgrade` stay `"format": 1`, byte for byte as before.

A flat name is refused on every system when it is empty, `.` or `..`, holds a slash, a backslash, a colon, a control character, or starts with a drive letter. Reserved Windows names are checked the same way as for `run`: refused on Windows, a warning elsewhere. A name that differs only by case from another is refused on Windows.

A run reads the files directly in the folder and skips hidden files, the archive and anything that is not a plain file. An existing archive is read in full: the zip test, then every file against its sha256. A zip that is not an archive made by this command is never replaced.

The new files and the archive's own files are written to a temporary file in the same folder, and every file in it is compared with its source. Then the old archive is linked to `.<name>.archive-old`, the temporary file is moved over the archive in one step, `.<name>.archive-old` goes to the trash, and then the archived source files do. A run that dies before the move leaves the old archive and a temporary file. A run that dies after it leaves the sources in place, and the next run finds them in the archive with the same sha256, proves the archive again and trashes them.

A source that has the same name as a file already in the archive but other bytes stops the run. `INDEX.md` is taken into the archive but never trashed, and when its bytes change the archive copy is replaced.

Only the outer level of a zip is cut open, as for rollups. A kit zip holds its release zip as one deflated member, so a second kit with a rebuilt release zip shares what is stored identically (the runtime files) and not the release zip. Storing that inner zip uncompressed in the kit would not change this, since it is still a member of the kit and is cut only as a whole.

## Names Windows cannot hold

A name such as `CON`, `COM1`, `aux.txt`, `NUL`, `lpt3.log` (with or without an extension, in any case), a name with `:`, `?`, `*`, `<`, `>`, `|` or `"`, and a name ending in a dot or space can sit in a tree on Linux without trouble. `restore` refuses all of them on Windows, so a rollup that holds one could not be restored there.

`run`, `pre` and `upgrade` look at every file name that would go into a rollup before they write or move anything. On Windows a hit raises an error that lists up to five of the names with the zip they would go into, and nothing is written or trashed. That also holds for `--dry-run`. `upgrade` refuses only the zip that holds such a name, leaves it as it is and goes on with the others; it ends with exit code 1 like any refused zip. On any other host the same hit is a single `warning:` line, and the rollup is written as usual.

The `restore` check is stricter on Windows than the check at write time: it also refuses names that differ only by case.

Any other error that comes from the file system, such as a file the system cannot open, ends in one `error:` line and exit code 1 instead of a traceback. Nothing that was already verified is lost: a zip is written beside its sources and checked before a source goes to the trash.

## Running twice

The tool looks at what is on disk and works out the target layout from that, so a second run finds everything already in place and prints `nothing to do`. If a run was interrupted after a zip was renamed into place but before the sources were trashed, the next run sees the finished zip, checks it again against the leftover sources, and trashes them.

If a release is published late into an old line (say 0.1.4 appears after `0.1.zip` exists), the next run rebuilds `0.1.zip` with the new member and trashes the old one.

A plain rollup is rebuilt as a deduplicated one the same way when a new member arrives, or on `upgrade`, with every file compared against the old zip before it's replaced. With nothing new and no `upgrade` it stays plain and the run prints `nothing to do`. To unpack a build from a deduplicated rollup, use `release-rollup restore`, not `unzip`; the next run compares the folder with the zip and trashes the folder, as it does for a plain rollup. The bytes of a rollup depend only on its files and the tool version, so rebuilding with a new member gives the same zip as building it from scratch.

## Edge cases

- A line with only one release still becomes a line zip: `0.1.0` alone, followed by `0.2.0`, gives `0.1.zip`.
- Several releases at once are fine. With 0.1.0 to 0.3.3 all present on the first run, you get `0.1.zip`, `0.2.zip`, `v0.3.0.zip`, `v0.3.1.zip`, `v0.3.2.zip` and the folder `v0.3.3/`.
- If only one release exists, there is nothing to do.
- Pre-release builds never count as the newest release, so publishing `0.2.0-rc.4` or `0.2.0-alpha.4` does not close the 0.1 line.
- `--dry-run` also lists the candidates it would trash, and a second run finds none left.
- A zip in the folder that is not readable stops the run with an error before anything is changed.
- A zip with an unexpected top-level entry (anything not named `v<x.y.z>/`) also stops the run.
- Name clashes in `--trash-dir` get a numeric suffix, so earlier trashed items are never overwritten.
- `--dry-run` reads the folder and prints the plan. It writes and moves nothing, and it does not need `gio` or `--trash-dir`.

## Tester builds and the pre command

`release-rollup pre` works on a project's `prereleases/` folder, separate from the releases folder that `run` handles. It holds `alpha/`, `beta/` and `rc/`, and in each of them one folder per tester build, named `v<x.y.z>-<stage>.<n>`. A build counts as finished when it holds `PRERELEASE.json` and `SHA256SUMS`. Unfinished folders are not touched, so the previous finished build stays unpacked while a new one is still being written.

In each stage folder the older builds of one version go into `v<x.y.z>-<stage>.x.zip`, for example `alpha/v1.0.0-alpha.x.zip` holding `v1.0.0-alpha.4/` and `v1.0.0-alpha.5/`. A later run rewrites that zip with the new members, through a temp file that is verified before it replaces the old one. Only the folders that went into the zip are trashed.

While a version is unreleased, the newest finished build of each stage stays unpacked. `--current` decides what is unreleased:

- A pre-release such as `1.1.0-beta.2`: its base 1.1.0 and anything later is unreleased.
- A final version such as `1.1.0`: only versions later than 1.1.0 are unreleased.
- Without `--current`: only the newest version found.

Versions at or below a final current, or below the base of a pre-release current, count as released or dropped. All their builds are rolled up, so a released version ends up as at most one zip per stage. A `--current` like `1.0` or `1.0.0-gamma.1` is an error.

Zips are checked as in `run`: `testzip` and a sha256 comparison of every member with its source, before any source is trashed. If that fails the sources stay. A rollup zip that cannot be read, or that holds a top-level entry which is not a build of that version and stage, stops the run with nothing changed.

Folders of another stage inside a stage folder, other names, and unknown stage folders such as `gamma/` are left alone. If someone unpacks a build from the zip again, the next run compares it with the zip and trashes the folder. A second run prints `nothing to do`.
