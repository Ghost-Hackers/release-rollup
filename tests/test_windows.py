"""Names Windows cannot hold: refused up front on Windows, warned about elsewhere, and clean errors for the rest.

Windows cannot put these names on its own disk (NUL is the null device, a trailing dot or space is dropped, a:b is a
stream of a), so the fixtures carry them the way they reach a Windows machine: as entries of a zip made elsewhere.
The same tests run on Linux and check the warning there. hosts.tsv lists the few that need names on disk.
"""
import contextlib
import io
import os
import subprocess
import sys
import unittest
import zipfile
from collections.abc import Callable, Mapping

import hostcat
import test_dedupe as TD
from release_rollup import cli
from release_rollup.container import Rollup
from release_rollup.core import Fail, apply_pre, move_trash, upgrade

QUIET = TD.QUIET
WINDOWS = hostcat.this_host() == 'windows'
BAD = ('CON', 'COM1', 'aux.txt', 'aux.c', 'NUL', 'lpt3.log', 'trail.', 'trail ', 'dir/prn', 'a:b', 'COM¹',
       'COM².txt')
FINE = ('console.txt', 'com10', 'auxiliary', 'v0.1.0-nul', 'null.txt')


def tree_state(root: str) -> dict[str, str | None]:
    """Names and sha256 of every file and folder below root."""
    out: dict[str, str | None] = {}
    for d, dirs, fs in os.walk(root):
        for name in dirs:
            out[os.path.relpath(os.path.join(d, name), root)] = None
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = TD.sha_file(p)
    return out


def zip_entries(path: str, entries: Mapping[str, bytes]) -> None:
    """A plain zip with these {entry name: bytes}. The names go into the zip as they are, on any host."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in sorted(entries):
            z.writestr(name, entries[name])


def zip_rollup(path: str, members: Mapping[str, Mapping[str, bytes] | None]) -> str:
    """A plain rollup zip as an older run or another machine left it. members: {version: extra assets or None}."""
    entries: dict[str, bytes] = {}
    for v, extra in members.items():
        files = {'RELEASE_NOTES.md': b'notes ' + v.encode(), 'SHA256SUMS': b'sums ' + v.encode(), 'assets/app.zip': TD.app(v)}
        files.update(extra or {})
        entries.update({'v%s/%s' % (v, k): data for k, data in files.items()})
    zip_entries(path, entries)
    return path


class Names(TD.Base):
    def releases(self, extra: Mapping[str, bytes]) -> None:
        """0.1.0 and 0.1.1 sit in a plain 0.1.zip, the odd names in 0.1.0; 0.1.2 and 0.2.0 are folders.
        A run adds 0.1.2 to 0.1.zip, so it reads every name of the zip first."""
        zip_rollup(os.path.join(self.root, '0.1.zip'), {'0.1.0': extra, '0.1.1': None})
        for v in ('0.1.2', '0.2.0'):
            TD.release(self.root, v, TD.app(v))

    def trashed(self) -> list[str]:
        d = os.path.join(self.tmp, 'trash')
        return os.listdir(d) if os.path.isdir(d) else []

    def per_host(self, run: Callable[[], object], pattern: str) -> str:
        """On Windows run() raises Fail matching pattern and nothing moves; elsewhere it says one warning and goes on.
        Returns the error or the warning."""
        before = tree_state(self.root)
        if WINDOWS:
            with self.assertRaisesRegex(Fail, pattern) as cm:
                run()
            self.assertEqual(tree_state(self.root), before)
            self.assertEqual(self.trashed(), [])
            return str(cm.exception)
        self.said = []
        run()
        warn = [s for s in self.said if s.startswith('warning:')]
        self.assertEqual(len(warn), 1, self.said)
        self.assertIn('restoring on Windows would be refused', warn[0])
        return warn[0]

    def test_each_name_is_refused_on_windows_and_warned_about_elsewhere(self):
        for bad in BAD:
            with self.subTest(bad):
                self.setUp()
                self.releases({'assets/' + bad: b'x'})
                self.per_host(self.run_apply, r'Windows cannot hold.*Nothing was written, the sources are untouched')
                if not WINDOWS:
                    with Rollup(os.path.join(self.root, '0.1.zip')) as r:
                        self.assertIn('v0.1.0/assets/' + bad, r.files())
                        self.assertIn('v0.1.2/SHA256SUMS', r.files())
                    self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.2')))

    def test_message_names_the_file_and_counts(self):
        self.releases({'assets/CON': b'x', 'assets/aux.txt': b'y'})
        msg = self.per_host(self.run_apply, 'Windows cannot hold')
        self.assertIn('2 names Windows cannot hold', msg)
        self.assertIn('0.1.zip: v0.1.0/assets/CON (reserved Windows name CON)', msg)
        self.assertIn('0.1.zip: v0.1.0/assets/aux.txt', msg)
        self.assertNotIn('more', msg)
        self.assertTrue(msg.startswith('cannot roll up' if WINDOWS else 'warning: '), msg)

    def test_a_long_list_is_cut_after_five(self):
        self.releases(dict({'assets/con.%d' % i: b'x' for i in range(8)}, **{'assets/prn.txt': b'x'}))
        msg = self.per_host(self.run_apply, 'Windows cannot hold')
        self.assertIn('9 names Windows cannot hold', msg)
        self.assertIn('and 4 more', msg)

    def test_dry_run_refuses_or_warns_too(self):
        self.releases({'assets/CON': b'x'})
        before = tree_state(self.root)
        self.per_host(lambda: self.run_apply(dry=True), 'Windows cannot hold')
        self.assertEqual(tree_state(self.root), before)

    def test_names_that_only_look_alike_pass(self):
        for v in ('0.1.0', '0.1.1', '0.1.2', '0.2.0'):
            TD.release(self.root, v, TD.app(v), {'assets/' + name: b'x' for name in FINE} if v == '0.1.0' else None)
        self.run_apply()
        self.assertTrue(os.path.isfile(os.path.join(self.root, '0.1.zip')))
        self.assertEqual([s for s in self.said if s.startswith('warning:')], [])

    def test_linux_rolls_up_folders_holding_such_names(self):
        # Linux only: these names exist on a Linux disk. On Windows they come in zips (the tests above).
        bad = ('CON', 'NUL', 'trail.', 'trail ', 'a:b')
        for v in ('0.1.0', '0.1.1', '0.1.2', '0.2.0'):
            TD.release(self.root, v, TD.app(v), {'assets/' + b: b'x' for b in bad} if v == '0.1.0' else None)
        self.run_apply()
        z = os.path.join(self.root, '0.1.zip')
        warn = [s for s in self.said if s.startswith('warning:')]
        self.assertEqual(len(warn), 1, self.said)
        self.assertIn('5 names Windows cannot hold', warn[0])
        self.assertIn('restoring on Windows would be refused', warn[0])
        TD.restore(z, out=self.out, say=QUIET)
        for b in bad:
            self.assertTrue(os.path.isfile(os.path.join(self.out, 'v0.1.0', 'assets', b)), b)

    def test_restore_of_such_a_rollup_per_host(self):
        z = zip_rollup(os.path.join(self.root, '0.1.zip'), {'0.1.0': {'assets/CON': b'x', 'assets/trail.': b'y'}})
        if WINDOWS:
            self.assertRegex(self.assertRefused(z), 'unsafe name .*reserved Windows name CON')
        else:
            TD.restore(z, out=self.out, say=QUIET)
            self.assertEqual(sorted(TD.snapshot(os.path.join(self.out, 'v0.1.0', 'assets'))), ['CON', 'app.zip', 'trail.'])

    def test_run_checks_every_step_before_the_first_write(self):
        # the 0.1 line is clean and would be written first; the bad name sits in 0.2.zip
        for v in ('0.1.0', '0.1.1', '0.1.2', '0.2.2', '0.3.0'):
            TD.release(self.root, v, TD.app(v))
        zip_rollup(os.path.join(self.root, '0.2.zip'), {'0.2.0': None, '0.2.1': {'assets/CON': b'x'}})
        msg = self.per_host(self.run_apply, '0.2.zip: v0.2.1/assets/CON')
        self.assertIn('0.2.zip: v0.2.1/assets/CON', msg)
        self.assertEqual(os.path.isfile(os.path.join(self.root, '0.1.zip')), not WINDOWS)

    def build(self, name: str, extra: Mapping[str, bytes] | None = None) -> None:
        d = os.path.join(self.root, 'beta', name)
        os.makedirs(os.path.join(d, 'assets'))
        for f in ('PRERELEASE.json', 'SHA256SUMS'):
            with open(os.path.join(d, f), 'w') as w:
                w.write(name)
        with open(os.path.join(d, 'assets', 'app.zip'), 'wb') as w:
            w.write(TD.app(name))
        for k, v in (extra or {}).items():
            with open(os.path.join(d, k), 'wb') as w:
                w.write(v)

    def test_pre_refuses_on_windows_and_warns_elsewhere(self):
        entries: dict[str, bytes] = {}
        for i in (1, 2):
            nm = 'v1.0.0-beta.%d' % i
            entries.update({nm + '/PRERELEASE.json': nm.encode(), nm + '/SHA256SUMS': nm.encode(),
                            nm + '/assets/app.zip': TD.app(nm)})
        entries['v1.0.0-beta.2/assets/aux.txt'] = b'x'
        zip_entries(os.path.join(self.root, 'beta', 'v1.0.0-beta.x.zip'), entries)
        for i in (3, 4):
            self.build('v1.0.0-beta.%d' % i)
        msg = self.per_host(lambda: apply_pre(self.root, current='1.0.0-beta.4', trash=self.trash, say=self.say),
                            r'beta/v1.0.0-beta.x.zip: v1.0.0-beta.2/assets/aux.txt.*Nothing was written')
        self.assertIn('1 name Windows cannot hold', msg)
        self.assertIn('beta/v1.0.0-beta.x.zip: v1.0.0-beta.2/assets/aux.txt', msg)
        self.assertEqual(os.path.isdir(os.path.join(self.root, 'beta', 'v1.0.0-beta.3')), WINDOWS)

    def plain_rollup(self, extra: Mapping[str, bytes]) -> str:
        return zip_rollup(os.path.join(self.root, '0.1.zip'), {'0.1.0': extra, '0.1.1': None, '0.1.2': None})

    def plain_rollup_second(self) -> str:
        for v in ('0.3.0', '0.3.1', '0.3.2', '0.4.0'):
            TD.release(self.root, v, TD.app(v))
        self.run_apply(dedupe=False)
        return os.path.join(self.root, '0.3.zip')

    def up(self, dry: bool = False) -> int:
        self.said = []
        return upgrade(self.root, dry=dry, trash=move_trash(os.path.join(self.tmp, 'old-zips')), say=self.say)

    def test_upgrade_refuses_such_a_zip_on_windows_and_warns_elsewhere(self):
        z = self.plain_rollup({'assets/NUL': b'x'})
        good = self.plain_rollup_second()
        self.assertFalse(TD.is_deduped(good))
        before = tree_state(self.root)
        if WINDOWS:
            with self.assertRaisesRegex(Fail, '1 not upgraded: 0.1.zip'):
                self.up()
            self.assertTrue(any(s.startswith('refused 0.1.zip: 1 name Windows cannot hold') and s.endswith('left untouched')
                                for s in self.said), self.said)
            # the zip is untouched, the unrelated plain zip beside it still upgrades
            self.assertFalse(TD.is_deduped(z))
            self.assertEqual(tree_state(self.root).pop('0.1.zip'), before['0.1.zip'])
        else:
            self.assertEqual(self.up(), 2)
            self.assertTrue(any(s.startswith('warning: 1 name Windows cannot hold') for s in self.said), self.said)
            self.assertTrue(TD.is_deduped(z))
        self.assertTrue(TD.is_deduped(good))
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith('.')], [])

    def test_upgrade_dry_run_refuses_or_warns_too(self):
        z = self.plain_rollup({'assets/CON': b'x'})
        before = tree_state(self.root)
        if WINDOWS:
            with self.assertRaisesRegex(Fail, 'not upgraded'):
                self.up(dry=True)
        else:
            self.assertEqual(self.up(dry=True), 1)
            self.assertTrue(any(s.startswith('warning: 1 name Windows cannot hold') for s in self.said), self.said)
        self.assertEqual(tree_state(self.root), before)
        self.assertFalse(TD.is_deduped(z))


class Command(TD.Base):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def releases(self, extra: Mapping[str, bytes] | None = None) -> None:
        for v in ('0.1.0', '0.1.1', '0.1.2', '0.2.0'):
            TD.release(self.root, v, TD.app(v), extra if v == '0.1.0' else None)

    def test_names_windows_cannot_hold_on_the_command_line(self):
        zip_rollup(os.path.join(self.root, '0.1.zip'), {'0.1.0': {'assets/CON': b'x'}, '0.1.1': None})
        for v in ('0.1.2', '0.2.0'):
            TD.release(self.root, v, TD.app(v))
        before = tree_state(self.root)
        trash = ('--trash-dir', os.path.join(self.tmp, 't'))
        if WINDOWS:
            # one error line and exit 1 each time, nothing touched
            for cmd, want in ((['run'], 'cannot roll up: 1 name Windows cannot hold'),
                              (['run', '--dry-run'], 'cannot roll up: 1 name Windows cannot hold'),
                              (['upgrade'], '1 not upgraded: 0.1.zip')):
                code, out, err = self.run_cli(cmd[0], '--root', self.root, *trash, *cmd[1:])
                self.assertEqual(code, 1, cmd)
                self.assertTrue(err.startswith('error: ' + want), err)
                self.assertEqual(err.count('\n'), 1, err)
            self.assertEqual(tree_state(self.root), before)
            return
        code, out, err = self.run_cli('run', '--root', self.root, '--dry-run')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('warning: 1 name Windows cannot hold', out)
        self.assertEqual(tree_state(self.root), before)
        code, out, err = self.run_cli('run', '--root', self.root, *trash)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('warning: 1 name Windows cannot hold', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.2')))

    def assertOneLine(self, err: str, start: str) -> None:
        self.assertTrue(err.startswith(start), err)
        self.assertEqual(err.count('\n'), 1, err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('Errno', err)

    def test_unexpected_file_errors_are_one_clean_line(self):
        # --trash-dir names a file, so the first move into the trash fails with an OSError that nothing
        # below the command line turns into a refusal. The command prints one line. The new zip was
        # written and checked before that move, so it stays, and so does everything it holds.
        in_the_way = os.path.join(self.tmp, 'trash-is-a-file')
        with open(in_the_way, 'w') as f:
            f.write('x')
        # (upgrade and pre turn the same failure into their own refusal; their one-line OSErrors are
        # in test_linux_unreadable_folders_are_one_clean_line.)
        self.releases()
        before = tree_state(self.root)
        code, _, err = self.run_cli('run', '--root', self.root, '--trash-dir', in_the_way)
        self.assertEqual(code, 1)
        self.assertOneLine(err, 'error: %s: ' % in_the_way)
        after = tree_state(self.root)
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        with Rollup(os.path.join(self.root, '0.1.zip')) as r:
            self.assertEqual(r.check(), '')
            self.assertEqual({f.split('/')[0] for f in r.files()}, {'v0.1.0', 'v0.1.1', 'v0.1.2'})
        code, _, err = self.run_cli('run', '--root', self.root, '--trash-dir', os.path.join(self.tmp, 't'))  # the same folder still works
        self.assertEqual(code, 0)

    def test_linux_unreadable_folders_are_one_clean_line(self):
        # A folder this user may not list: the releases folder for run and upgrade, a stage folder for pre.
        self.releases()
        pre = os.path.join(self.tmp, 'pre')
        os.makedirs(os.path.join(pre, 'alpha'))
        trash = os.path.join(self.tmp, 't')
        for cmd, root, locked in (('run', self.root, self.root), ('upgrade', self.root, self.root),
                                  ('pre', pre, os.path.join(pre, 'alpha'))):
            with self.subTest(cmd):
                os.chmod(locked, 0o300)
                try:
                    code, _, err = self.run_cli(cmd, '--root', root, '--trash-dir', trash)
                finally:
                    os.chmod(locked, 0o755)
                self.assertEqual(code, 1)
                self.assertOneLine(err, 'error: %s: Permission denied' % locked)

    def test_linux_restore_output_failing_is_one_clean_line(self):
        # restore prints its summary after the files are written, past the part that turns OSErrors
        # into a refusal. With stdout unbuffered on a full device that print fails with ENOSPC.
        z = self.rollup()
        with open('/dev/full', 'w') as full:
            r = subprocess.run([sys.executable, '-u', '-m', 'release_rollup', 'restore', z, '--out', self.out],
                               stdout=full, stderr=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONPATH=TD.SRC))
        self.assertEqual(r.returncode, 1)
        self.assertOneLine(r.stderr, 'error: a file: No space left on device')
        self.assertEqual(sorted(os.listdir(self.out)), ['v0.1.0', 'v0.1.1', 'v0.1.2'])

    def test_no_command_prints_the_usage(self):
        code, out, err = self.run_cli()
        self.assertEqual((code, out), (2, ''))
        self.assertTrue(err.startswith('usage: release-rollup'), err)


load_tests = hostcat.load_tests


if __name__ == '__main__':
    unittest.main()
