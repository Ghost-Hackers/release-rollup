"""Release checks that run locally and in the release-check workflow.

    python tools/check_release.py versions [--tag vX.Y.Z]

"versions" checks that the version strings agree and that CHANGELOG.md has a heading for the version.
The changelog entry for a change under src/ is checked by changelog-check.yml, which calls the shared
changelog.yml from ci-workflows.
"""
import argparse
import os
import re
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Release checks for the version numbers.')
    parser.add_argument('--root', default=ROOT, help='repository folder (default: this one)')
    sub = parser.add_subparsers(dest='command', required=True)
    v = sub.add_parser('versions', help='version strings agree and the changelog has the version')
    v.add_argument('--tag', help='a release tag such as v0.2.0, which must match VERSION')
    args = parser.parse_args(argv)

    root: str = args.root
    problems = check_versions(root, args.tag)
    ok = 'versions ok'
    for p in problems:
        print(p)
    if problems:
        return 1
    print(ok)
    return 0


if __name__ == '__main__':
    sys.exit(main())
