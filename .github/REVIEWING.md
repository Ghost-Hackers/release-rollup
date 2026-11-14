# Reviewing

What a change to release-rollup has to meet before it merges.

## Safety

release-rollup zips users' release folders and moves the originals to the trash. A change must never lose a file: every zip is checked against its sources before anything is trashed, and a run that can't finish leaves the folder as it was.

Paths from zips, manifests and arguments stay inside the folder given. `..`, absolute paths, drive letters, links, junctions and names Windows can't hold are refused.

## Tests

- Every change comes with tests, and they cause failures with real input: damaged zips, a full disk (`RR_SMALLFS`), read-only files. No mocks or patches. The one exception is the 64 GiB limit in `tests/test_dedupe.py`, which would need 64 GiB of test data.
- Coverage stays at 100% of lines and branches on Linux and Windows. A line no real input can reach goes in `tools/coverage_exclusions.tsv` with the proof, not under a forced test.
- Tests never skip. A test for one system is listed in `tests/hosts.tsv`.

## Changes

- Every fix and feature has an issue, and its commit says `Fixes #n` or `Refs #n`. A problem found later gets its own issue and its own commit.
- A change under `src/` adds a line under `[Unreleased]` in `CHANGELOG.md`, or the pull request gets the `no changelog` label when users won't notice it.
- Versions follow semver with alpha, beta and rc stages. A version number is never used twice.
- Workflows pin actions to a commit and ask for the least permissions they need.

## Writing

Comments, docs, commit messages and pull request text read like a developer wrote them: short and plain, saying what the code does or why. No em dashes or emoji, no openers that announce and no closing summaries. Words to leave out: seamless, robust, comprehensive, leverage, utilize, ensure, streamline, delve, crucial, essential, facilitate, enhance, showcase, various. Tests are described by what they check, never as real or not.
