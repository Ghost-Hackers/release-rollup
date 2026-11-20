"""The run lock: a second run on a folder that another run holds is refused before anything changes (issue #7).

The other run is a second Python process that holds the lock through release_rollup.lock until we close its stdin.
"""
import contextlib
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import hostcat
import smallfs
import test_dedupe as TD
from release_rollup import cli
from release_rollup.lock import NAME, lock

# Holds the lock on argv[2] until stdin closes. Prints its pid once it has the lock.
HOLD = ('import os, sys\n'
        'sys.path.insert(0, sys.argv[1])\n'
        'from release_rollup.lock import lock\n'
        'with lock(sys.argv[2], print):\n'
        '    print("ready", os.getpid(), flush=True)\n'
        '    sys.stdin.read()\n')

# Takes the lock on argv[2], prints its pid and kills itself without letting go.
KILL = ('import os, signal, sys\n'
        'sys.path.insert(0, sys.argv[1])\n'
        'from release_rollup.lock import lock\n'
        'with lock(sys.argv[2], print):\n'
        '    print("ready", os.getpid(), flush=True)\n'
        '    %s\n' % TD.kill_plan()[0])

TIME = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ'


def tree(base: str) -> dict[str, bytes | None]:
    """Every path under base, with file contents, or 'link:' and its target for a symlink."""
    out: dict[str, bytes | None] = {}
    for d, dirs, fs in os.walk(base):
        for sub in dirs:
            out[os.path.relpath(os.path.join(d, sub), base)] = None
        for f in fs:
            p = os.path.join(d, f)
            if os.path.islink(p):
                out[os.path.relpath(p, base)] = b'link:' + os.fsencode(os.readlink(p))
                continue
            with open(p, 'rb') as fh:
                out[os.path.relpath(p, base)] = fh.read()
    return out


class Lock(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix='rollup-lock-')
        self.addCleanup(td.cleanup)
        self.tmp = td.name
        self.root = os.path.join(self.tmp, 'releases')
        self.trash = os.path.join(self.tmp, 'trash')
        self.out = os.path.join(self.tmp, 'archive.zip')
        self.path = os.path.join(self.root, NAME)
        os.makedirs(self.root)
        os.makedirs(self.trash)
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            TD.release(self.root, v, TD.app(v))
        self.said: list[str] = []

    def hold(self, script: str = HOLD) -> tuple[subprocess.Popen[str], int]:
        """Start a process that takes the lock on root. Returns it and its pid once it holds the lock."""
        p = subprocess.Popen([sys.executable, '-c', script, TD.SRC, self.root], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, p)
        assert p.stdout is not None
        line = p.stdout.readline()
        m = re.fullmatch(r'ready (\d+)\n', line)
        if not m:
            self.fail('the holder did not get the lock: %r' % line)
        return p, int(m.group(1))

    def stop(self, p: subprocess.Popen[str]) -> None:
        for f in (p.stdin, p.stdout):
            if f is not None and not f.closed:
                with contextlib.suppress(OSError):
                    f.close()
        p.wait(timeout=60)

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def cli_process(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, '-m', 'release_rollup', *argv], capture_output=True, text=True,
                              env=dict(os.environ, PYTHONPATH=TD.SRC))

    def commands(self) -> dict[str, list[str]]:
        t = ['--trash-dir', self.trash]
        return {'run': ['run', '--root', self.root, *t], 'pre': ['pre', '--root', self.root, *t],
                'upgrade': ['upgrade', '--root', self.root, *t], 'archive': ['archive', '--root', self.root, '--out', self.out, *t]}

    def test_a_second_run_is_refused_while_another_holds_the_folder(self):
        _, pid = self.hold()
        before = tree(self.tmp)
        with open(self.path, encoding='utf-8') as f:
            self.assertRegex(f.read(), r'^pid=%d host=%s started=%s\n$' % (pid, re.escape(socket.gethostname()), TIME))
        for cmd, argv in self.commands().items():
            with self.subTest(cmd):
                r = self.cli_process(*argv)
                self.assertEqual((r.returncode, r.stdout), (1, ''))
                self.assertRegex(r.stderr, r'^error: %s is held by another run \(pid %d on %s, started %s\); nothing was changed\n$'
                                 % (re.escape(self.path), pid, re.escape(socket.gethostname()), TIME))
                self.assertEqual(tree(self.tmp), before)

    def test_a_held_lock_with_an_unreadable_record_is_still_refused(self):
        self.hold()
        with open(self.path, 'r+b') as f:
            f.write(b'\xff not a record')
        before = tree(self.tmp)
        code, out, err = self.cli(*self.commands()['run'])
        self.assertEqual((code, out), (1, ''))
        self.assertEqual(err, 'error: %s is held by another run; nothing was changed\n' % self.path)
        self.assertEqual(tree(self.tmp), before)

    def test_dry_run_goes_ahead_while_another_run_holds_the_folder(self):
        self.hold()
        before = tree(self.tmp)
        for cmd, argv in self.commands().items():
            with self.subTest(cmd):
                code, _, err = self.cli(*argv, '--dry-run')
                self.assertEqual((code, err), (0, ''))
                self.assertEqual(tree(self.tmp), before)

    def test_the_lock_is_free_again_once_the_holder_ends(self):
        p, _ = self.hold()
        self.stop(p)
        self.assertFalse(os.path.exists(self.path))
        code, out, err = self.cli(*self.commands()['run'])
        self.assertEqual((code, err), (0, ''))
        self.assertNotIn('stale', out)
        self.assertTrue(os.path.isfile(os.path.join(self.root, '0.1.zip')))

    def test_every_command_finishes_and_leaves_no_lock_file(self):
        for cmd, argv in self.commands().items():
            with self.subTest(cmd):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, err), (0, ''), out)
                for base in (self.tmp,):
                    found = [os.path.join(d, f) for d, _, fs in os.walk(base) for f in fs if f == NAME]
                    self.assertEqual(found, [])

    def test_a_stale_lock_from_a_killed_run_is_cleared(self):
        p, pid = self.hold(KILL)
        self.assertEqual(p.wait(timeout=60), TD.kill_plan()[1])
        with open(self.path, encoding='utf-8') as f:
            self.assertRegex(f.read(), r'^pid=%d ' % pid)
        # Windows drops the locks of a killed process soon after it ends, not at once (LockFileEx docs).
        deadline = time.monotonic() + 30
        while True:
            code, out, err = self.cli(*self.commands()['run'])
            if code == 0 or 'is held by another run' not in err or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('cleared a stale lock in %s left by a run that ended without releasing it (pid %d on %s, '
                      % (self.path, pid, socket.gethostname()), out)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.isfile(os.path.join(self.root, '0.1.zip')))

    def test_a_leftover_lock_file_with_no_holder(self):
        for content, said in ((b'', None), (b'\x00\x01garbage', 'cleared a stale lock in %s that held an unreadable record'),
                              (b'pid=12 host=old-box started=2026-01-02T03:04:05Z\n',
                               'cleared a stale lock in %s left by a run that ended without releasing it '
                               '(pid 12 on old-box, started 2026-01-02T03:04:05Z)')):
            with self.subTest(content):
                with open(self.path, 'wb') as f:
                    f.write(content)
                with lock(self.root, self.said.append):
                    pass
                self.assertEqual(self.said, [said % self.path] if said else [])
                self.assertFalse(os.path.exists(self.path))
                self.said.clear()

    def test_the_lock_is_let_go_when_the_run_stops_with_an_error(self):
        for exc in (KeyboardInterrupt, ValueError):
            with self.subTest(exc), self.assertRaises(exc):
                with lock(self.root, self.said.append):
                    raise exc()
            self.assertFalse(os.path.exists(self.path))
        with lock(self.root, self.said.append):
            self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(self.said, [])

    def refused_as_not_plain(self) -> None:
        """Run on root and check it was refused because the lock path is not a plain file, with nothing changed."""
        before = tree(self.tmp)
        code, out, err = self.cli(*self.commands()['run'])
        self.assertEqual((code, out), (1, ''))
        self.assertEqual(err, 'error: %s is not a plain file; nothing was changed\n' % self.path)
        self.assertEqual(tree(self.tmp), before)

    def test_a_lock_path_that_is_a_folder_is_refused(self):
        os.mkdir(self.path)
        self.refused_as_not_plain()

    def test_a_lock_path_that_is_a_hard_link_is_refused(self):
        # Writing the record through a second name would overwrite the other file.
        target = os.path.join(self.tmp, 'notes.txt')
        with open(target, 'wb') as f:
            f.write(b'keep me\n')
        os.link(target, self.path)
        self.refused_as_not_plain()
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'keep me\n')
        self.assertTrue(os.path.samefile(target, self.path))

    def test_linux_a_lock_path_that_is_a_symlink_is_refused(self):
        target = os.path.join(self.tmp, 'notes.txt')
        with open(target, 'wb') as f:
            f.write(b'keep me\n')
        os.symlink(target, self.path)
        self.refused_as_not_plain()
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'keep me\n')
        self.assertEqual(os.readlink(self.path), target)
        # A link to a file that isn't there is refused too, and the file is not made.
        os.remove(self.path)
        os.remove(target)
        os.symlink(target, self.path)
        self.refused_as_not_plain()
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(os.readlink(self.path), target)

    def test_the_lock_file_is_private_to_its_owner(self):
        with lock(self.root, self.said.append):
            mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600 if hostcat.this_host() == 'linux' else 0o666)

    def test_the_lock_file_removed_by_hand_during_a_run(self):
        # Linux lets anyone remove the file; the run then leaves alone whatever sits at that name when it ends.
        # Windows refuses to remove a file the run has open.
        for replace in (False, True):
            with self.subTest(replace=replace):
                with lock(self.root, self.said.append):
                    if hostcat.this_host() == 'windows':
                        self.assertRaises(PermissionError, os.remove, self.path)
                        continue
                    os.remove(self.path)
                    if replace:
                        with open(self.path, 'w', encoding='utf-8') as f:
                            f.write('other')
                if replace and hostcat.this_host() == 'linux':
                    with open(self.path, encoding='utf-8') as f:
                        self.assertEqual(f.read(), 'other')
                    os.remove(self.path)
                self.assertFalse(os.path.exists(self.path))

    def test_another_program_reading_the_lock_file(self):
        # A virus scanner or a refused run may have the file open when the run ends. Windows can't remove it then,
        # so it stays behind empty, and the next run takes it as free without a word.
        with lock(self.root, self.said.append):
            reader = open(self.path, 'rb')
            self.addCleanup(reader.close)
            self.assertRegex(reader.read().decode(), r'^pid=%d ' % os.getpid())
        reader.close()
        if hostcat.this_host() == 'windows':
            with open(self.path, 'rb') as f:
                self.assertEqual(f.read(), b'')
        else:
            self.assertFalse(os.path.exists(self.path))
        code, out, err = self.cli(*self.commands()['run'])
        self.assertEqual((code, err), (0, ''))
        self.assertNotIn('stale', out)
        self.assertFalse(os.path.exists(self.path))

    def test_linux_read_only_folder_is_refused(self):
        os.chmod(self.root, 0o555)
        self.addCleanup(os.chmod, self.root, 0o755)
        if os.access(self.root, os.W_OK):
            self.fail('%s is still writable after chmod 555: run the tests as a normal user' % self.root)
        before = tree(self.tmp)
        code, out, err = self.cli(*self.commands()['run'])
        self.assertEqual((code, out), (1, ''))
        self.assertEqual(err, 'error: cannot lock %s: Permission denied; nothing was changed\n' % self.path)
        self.assertEqual(tree(self.tmp), before)

    def test_linux_a_symlink_to_a_held_folder_is_refused(self):
        _, pid = self.hold()
        link = os.path.join(self.tmp, 'link')
        os.symlink(self.root, link)
        r = self.cli_process('run', '--root', link, '--trash-dir', self.trash)
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stderr, r'^error: %s is held by another run \(pid %d ' % (re.escape(os.path.join(link, NAME)), pid))

    def full_disk(self, base: str) -> None:
        """Fill the disk under base, then run on an empty folder there: the lock record can't be written."""
        root = os.path.join(base, 'releases')
        os.mkdir(root)
        with open(os.path.join(base, 'ballast'), 'wb', buffering=0) as f:
            smallfs.fill(f)
        code, out, err = self.cli('run', '--root', root, '--trash-dir', self.trash)
        self.assertEqual((code, out), (1, ''))
        self.assertRegex(err, r'^error: cannot lock %s: %s[^\n]*; nothing was changed\n$'
                         % (re.escape(os.path.join(root, NAME)), smallfs.FULL))
        self.assertEqual(os.listdir(root), [])

    def test_linux_a_full_disk_is_refused_and_leaves_no_lock_file(self):
        self.full_disk(smallfs.folder(self))

    def test_windows_a_full_fat32_disk_is_refused_and_leaves_no_lock_file(self):
        # NTFS keeps a file this small inside its MFT record, so only a FAT32 disk runs out of room for the record.
        self.full_disk(smallfs.nolinks_folder(self))


load_tests = hostcat.load_tests

if __name__ == '__main__':
    unittest.main()
