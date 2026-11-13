"""Release folders that can't be listed in full: run and pre stop before anything changes.

os.walk passes over a subfolder it can't open, and doesn't go into a link to another folder.
Both used to leave those files out of the rollup while the release folder still went to the trash.
"""
import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator

import hostcat
import lowrights
from release_rollup.cli import main

# Exits 1 if the parent of argv[1] can't be listed; prints denied if argv[1] can't be.
PROBE = ("import os, sys\n"
         "os.listdir(os.path.dirname(sys.argv[1]))\n"
         "try:\n"
         "    os.listdir(sys.argv[1])\n"
         "except PermissionError:\n"
         "    print('denied')\n")


def release(root: str, name: str, markers: tuple[str, ...]) -> str:
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, 'assets'))
    for m in markers:
        with open(os.path.join(d, m), 'w', encoding='utf-8') as f:
            f.write('%s %s\n' % (m, name))
    with open(os.path.join(d, 'assets', 'app.bin'), 'wb') as f:
        f.write(name.encode() * 200)
    return d


class Walk(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix='rollup-walk-')
        self.addCleanup(td.cleanup)
        self.base = td.name
        self.rel = os.path.join(self.base, 'releases')
        self.trash = os.path.join(self.base, 'trash')
        os.makedirs(self.trash)
        for v in ('v0.1.0', 'v0.1.1', 'v0.2.0'):
            release(self.rel, v, ('RELEASE_NOTES.md', 'SHA256SUMS'))
        stage = os.path.join(self.base, 'prereleases', 'beta')
        for v in ('v0.3.0-beta.1', 'v0.3.0-beta.2'):
            release(stage, v, ('RELEASE_NOTES.md', 'PRERELEASE.json', 'SHA256SUMS'))

    def snapshot(self) -> dict[str, bytes | None]:
        """Every path under the temp folder, with file contents, read as root would see it."""
        out: dict[str, bytes | None] = {}
        for d, dirs, fs in os.walk(self.base):
            for sub in dirs:
                out[os.path.relpath(os.path.join(d, sub), self.base)] = None
            for f in fs:
                p = os.path.join(d, f)
                with open(p, 'rb') as fh:
                    out[os.path.relpath(p, self.base)] = fh.read()
        return out

    def refused(self, args: list[str], expect: str) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(args)
        self.assertEqual(code, 1)
        self.assertIn(expect, err.getvalue())
        self.assertIn('nothing was changed', err.getvalue())

    @contextlib.contextmanager
    def locked(self, path: str) -> Generator[None]:
        """Deny listing path until the block ends."""
        if sys.platform == 'win32':
            # mkdtemp gives only SYSTEM, Administrators and the owner access, and Administrators owns what we make.
            lowrights.grant(self.base)
            # Deny Everyone (S-1-1-0): a deny for the user alone doesn't stop an elevated administrator.
            subprocess.run(['icacls', path, '/deny', '*S-1-1-0:(RD)'], capture_output=True, check=True)
            unlock = ['icacls', path, '/remove:d', '*S-1-1-0']
        else:
            os.chmod(path, 0)
        try:
            if self.listable(path):
                # Root reads past file modes, so the run would go ahead and trash the test folder.
                self.fail('%s can still be listed after locking it: run the tests as a normal user' % path)
            yield
        finally:
            if sys.platform == 'win32':
                subprocess.run(unlock, capture_output=True, check=True)
            else:
                os.chmod(path, stat.S_IRWXU)

    def listable(self, path: str) -> bool:
        """Whether the user running the command under test can list path."""
        if sys.platform == 'win32':
            code, out = lowrights.run(['-c', PROBE, path])
            self.assertEqual(code, 0, out)
            return out.split() != ['denied']
        try:
            os.listdir(path)
        except PermissionError:
            return False
        return True

    def check_unchanged(self, args: list[str], expect: str, locked: str) -> None:
        before = self.snapshot()
        with self.locked(locked):
            if sys.platform == 'win32':
                # An elevated administrator lists the locked folder anyway, so the command runs without those rights.
                code, out = lowrights.run(['-m', 'release_rollup', *args])
                self.assertEqual(code, 1, out)
                self.assertIn(expect, out)
                self.assertIn('nothing was changed', out)
            else:
                self.refused(args, expect)
        self.assertEqual(self.snapshot(), before)

    def test_run_stops_on_a_subfolder_it_cant_read(self) -> None:
        assets = os.path.join(self.rel, 'v0.1.0', 'assets')
        self.check_unchanged(['run', '--root', self.rel, '--trash-dir', self.trash], 'cannot read', assets)

    def test_run_dry_run_stops_on_a_subfolder_it_cant_read(self) -> None:
        assets = os.path.join(self.rel, 'v0.1.0', 'assets')
        self.check_unchanged(['run', '--root', self.rel, '--dry-run'], 'cannot read', assets)

    def test_pre_stops_on_a_subfolder_it_cant_read(self) -> None:
        assets = os.path.join(self.base, 'prereleases', 'beta', 'v0.3.0-beta.1', 'assets')
        self.check_unchanged(['pre', '--root', os.path.join(self.base, 'prereleases'), '--current', '0.3.0-beta.2',
                              '--trash-dir', self.trash], 'cannot read', assets)

    def test_linux_run_stops_on_a_symlink_to_a_folder(self) -> None:
        elsewhere = os.path.join(self.base, 'elsewhere')
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, 'data.bin'), 'wb') as f:
            f.write(b'kept elsewhere')
        os.symlink(elsewhere, os.path.join(self.rel, 'v0.1.0', 'assets', 'linked'))
        before = self.snapshot()
        self.refused(['run', '--root', self.rel, '--trash-dir', self.trash], 'links to another folder')
        self.assertEqual(self.snapshot(), before)

    def test_windows_run_stops_on_a_junction(self) -> None:
        if sys.platform == 'win32':
            import _winapi
            elsewhere = os.path.join(self.base, 'elsewhere')
            os.makedirs(elsewhere)
            with open(os.path.join(elsewhere, 'data.bin'), 'wb') as f:
                f.write(b'kept elsewhere')
            # A junction needs no extra rights, unlike a symlink.
            _winapi.CreateJunction(elsewhere, os.path.join(self.rel, 'v0.1.0', 'assets', 'linked'))
            before = self.snapshot()
            self.refused(['run', '--root', self.rel, '--trash-dir', self.trash], 'links to another folder')
            self.assertEqual(self.snapshot(), before)

    def test_a_readable_tree_still_rolls_up(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['run', '--root', self.rel, '--trash-dir', self.trash]), 0)
        self.assertEqual(sorted(os.listdir(self.rel)), ['0.1.zip', 'v0.2.0'])
        self.assertIn('wrote 0.1.zip (2 releases, 6 files', out.getvalue())


load_tests = hostcat.load_tests

if __name__ == '__main__':
    unittest.main()
