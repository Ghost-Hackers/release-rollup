"""Release checks that run locally and in the release-check workflow.

    python tools/check_release.py versions [--tag vX.Y.Z]
    python tools/check_release.py changelog --base REF --head REF

"versions" checks that the version strings agree and that CHANGELOG.md has a heading for the version.
"changelog" checks that a change under src/ comes with a new entry under "## [Unreleased]".
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$')
STAGE_TAG = {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}


def pep440(version: str) -> str | None:
    """0.2.0-beta.2 becomes 0.2.0b2, 1.0.0 stays 1.0.0. None when the string isn't a version."""
    m = VERSION_RE.match(version)
    if not m:
        return None
    major, minor, patch, stage, n = m.groups()
    base = f'{major}.{minor}.{patch}'
    return base if stage is None else f'{base}{STAGE_TAG[stage]}{n}'


def _read(root: str, *parts: str) -> str | None:
    try:
        with open(os.path.join(root, *parts), encoding='utf-8') as f:
            return f.read()
    except OSError:
        return None


def check_versions(root: str, tag: str | None = None) -> list[str]:
    problems: list[str] = []
    text = _read(root, 'VERSION')
    if text is None:
        return ['VERSION is missing']
    version = text.strip()
    expected = pep440(version)
    if expected is None:
        return [f'VERSION holds "{version}", which is not X.Y.Z or X.Y.Z-alpha|beta|rc.N']

    inner = _read(root, 'src', 'release_rollup', 'VERSION')
    if inner is None:
        problems.append('src/release_rollup/VERSION is missing')
    elif inner.strip() != version:
        problems.append(f'src/release_rollup/VERSION is {inner.strip()}, VERSION is {version}')

    init = _read(root, 'src', 'release_rollup', '__init__.py')
    m = re.search(r"^\s*__version__ = '([^']*)'", init or '', re.M)
    if m is None:
        problems.append('no fallback __version__ found in src/release_rollup/__init__.py')
    elif m.group(1) != version:
        problems.append(f'__version__ in src/release_rollup/__init__.py is {m.group(1)}, VERSION is {version}')

    pyproject = _read(root, 'pyproject.toml')
    m = re.search(r'^version = "([^"]*)"', pyproject or '', re.M)
    if m is None:
        problems.append('no version found in pyproject.toml')
    elif m.group(1) != expected:
        problems.append(f'pyproject.toml version is {m.group(1)}, expected {expected} for {version}')

    changelog = _read(root, 'CHANGELOG.md')
    if changelog is None:
        problems.append('CHANGELOG.md is missing')
    elif not re.search(rf'^## \[{re.escape(version)}\]', changelog, re.M):
        problems.append(f'CHANGELOG.md has no "## [{version}]" heading')

    if tag is not None and tag != f'v{version}':
        problems.append(f'tag {tag} does not match VERSION {version} (expected v{version})')
    return problems


def _git(root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['git', '-C', root, *args], capture_output=True, text=True, encoding='utf-8')


def unreleased_entries(changelog: str) -> list[str]:
    """The entry lines ("- ..." or "* ...") between "## [Unreleased]" and the next "## " heading."""
    entries: list[str] = []
    inside = False
    for line in changelog.splitlines():
        if line.startswith('## '):
            inside = line.strip().lower() == '## [unreleased]'
        elif inside and re.match(r'\s*[-*] \S', line):
            entries.append(' '.join(line.split()))
    return entries


def check_changelog(root: str, base: str, head: str) -> list[str]:
    merge_base = _git(root, 'merge-base', base, head)
    start = merge_base.stdout.strip() if merge_base.returncode == 0 else base
    # --no-renames lists both paths of a moved file, so a file moved out of src/ still counts.
    diff = _git(root, 'diff', '--name-only', '--no-renames', start, head)
    if diff.returncode != 0:
        return [f'git diff {start} {head} failed: {diff.stderr.strip()}']
    changed = [p for p in diff.stdout.splitlines() if p.startswith('src/')]
    if not changed:
        return []

    def entries(ref: str) -> list[str] | None:
        shown = _git(root, 'show', f'{ref}:CHANGELOG.md')
        return unreleased_entries(shown.stdout) if shown.returncode == 0 else None

    new = entries(head)
    if new is None:
        return [f'CHANGELOG.md does not exist at {head}']
    old = entries(start) or []
    added = list(new)
    for line in old:
        if line in added:
            added.remove(line)
    gone = len(old) - (len(new) - len(added))
    # A line that replaces an old one is a rewording, so it only counts when it adds to the total.
    if len(added) > gone:
        return []
    shown = ', '.join(changed[:3]) + (f' and {len(changed) - 3} more' if len(changed) > 3 else '')
    return [f'{shown} changed under src/, but "## [Unreleased]" in CHANGELOG.md has no new entry. '
            'Add one, or put the "no changelog" label on the pull request if users won\'t notice the change.']


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Release checks for the changelog and version numbers.')
    parser.add_argument('--root', default=ROOT, help='repository folder (default: this one)')
    sub = parser.add_subparsers(dest='command', required=True)
    v = sub.add_parser('versions', help='version strings agree and the changelog has the version')
    v.add_argument('--tag', help='a release tag such as v0.2.0, which must match VERSION')
    c = sub.add_parser('changelog', help='a change under src/ adds an Unreleased entry')
    c.add_argument('--base', required=True)
    c.add_argument('--head', required=True)
    args = parser.parse_args(argv)

    root: str = args.root
    if args.command == 'versions':
        problems = check_versions(root, args.tag)
        ok = 'versions ok'
    else:
        problems = check_changelog(root, args.base, args.head)
        ok = 'changelog ok'
    for p in problems:
        print(p)
    if problems:
        return 1
    print(ok)
    return 0


if __name__ == '__main__':
    sys.exit(main())
