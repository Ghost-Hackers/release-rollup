"""Build the release zip for one commit, the same way every time.

    python tools/build_release.py --out DIR [--ref REF] [--root DIR]

Reads the files straight from git at REF (default HEAD), so changes that aren't committed
never end up in a release. Everything under .github/ is left out. The zip is
release-rollup-<version>.zip with every file under release-rollup-<version>/, where the
version comes from VERSION at REF. Next to it goes SHA256SUMS, which `sha256sum -c` reads.

Two builds of the same commit give the same bytes: files go in sorted by path, every entry
gets the commit's date (zip dates count in steps of 2 seconds), and the permissions come from git. So anyone can rebuild a release
from its tag and compare the sha256 with the published one.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time
import zipfile

LEFT_OUT = ('.github/',)
FILE = '100644'
EXECUTABLE = '100755'
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # the earliest date a zip entry can hold


class BuildError(Exception):
    pass


def _git(root: str, *args: str) -> bytes:
    run = subprocess.run(['git', '-C', root, *args], capture_output=True)
    if run.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {run.stderr.decode(errors='replace').strip()}")
    return run.stdout


def tree(root: str, ref: str) -> list[tuple[str, str, str]]:
    """(path, mode, blob id) for every file at ref that goes into the zip, sorted by path."""
    entries: list[tuple[str, str, str]] = []
    for item in _git(root, 'ls-tree', '-r', '-z', '--full-tree', ref).split(b'\0'):
        if not item:
            continue
        meta, raw_path = item.split(b'\t', 1)
        mode, kind, blob = meta.decode().split(' ')
        path = raw_path.decode('utf-8')
        if path.startswith(LEFT_OUT):
            continue
        if kind != 'blob' or mode not in (FILE, EXECUTABLE):
            # Symlinks and submodules don't unpack the same way on every system.
            raise BuildError(f'{path}: a release holds plain files only, this is a {kind} with mode {mode}')
        entries.append((path, mode, blob))
    return sorted(entries)


def commit_date(root: str, ref: str) -> tuple[int, int, int, int, int, int]:
    """The commit's date in UTC, as a zip entry date."""
    stamp = int(_git(root, 'log', '-1', '--format=%ct', ref).strip())
    t = time.gmtime(stamp)
    return max(ZIP_EPOCH, (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec))


def build(root: str, ref: str, out: str) -> str:
    """Write the zip and SHA256SUMS to out. Returns the zip's path."""
    version = _git(root, 'show', f'{ref}:VERSION').decode().strip()
    if not version:
        raise BuildError(f'VERSION is empty at {ref}')
    name = f'release-rollup-{version}'
    zip_path = os.path.join(out, f'{name}.zip')
    sums_path = os.path.join(out, 'SHA256SUMS')
    for path in (zip_path, sums_path):
        if os.path.exists(path):
            raise BuildError(f'{path} already exists; move it aside first')

    # Everything that can refuse the build runs before the zip is opened.
    files = tree(root, ref)
    date = commit_date(root, ref)
    os.makedirs(out, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'x') as z:
            for path, mode, blob in files:
                info = zipfile.ZipInfo(f'{name}/{path}', date_time=date)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3  # Unix, so the permissions below are read on unpack
                info.external_attr = int(mode, 8) << 16
                z.writestr(info, _git(root, 'cat-file', 'blob', blob), compresslevel=9)
    except BaseException:
        # A half-written zip must never look like a finished release.
        os.remove(zip_path)
        raise

    with open(zip_path, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(sums_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'{digest}  {name}.zip\n')
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Build the release zip for one commit.')
    parser.add_argument('--out', required=True, help='folder for the zip and SHA256SUMS')
    parser.add_argument('--ref', default='HEAD', help='commit or tag to build (default HEAD)')
    parser.add_argument('--root', default='.', help='the repository (default the current folder)')
    args = parser.parse_args(argv)
    try:
        zip_path = build(args.root, args.ref, args.out)
    except BuildError as e:
        print(f'build_release: {e}', file=sys.stderr)
        return 1
    with open(os.path.join(args.out, 'SHA256SUMS'), encoding='utf-8') as f:
        print(f'built {zip_path}\n{f.read().strip()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
