import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from collections.abc import Container, Iterable
from typing import TypedDict, Unpack
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import hostcat
from release_rollup import core  # noqa: E402
from release_rollup.cli import main
from release_rollup.core import Fail, Files, apply, make_trash, move_trash, plan
from test_dedupe import ApplyOpts

FILES = ('RELEASE_NOTES.md', 'SHA256SUMS', 'assets/pack.bin')


def QUIET(*_: object) -> None:
    return None


def fake(root: str, ver: str, payload: str | None = None) -> None:
    d = os.path.join(root, 'v' + ver)
    os.makedirs(os.path.join(d, 'assets'))
    with open(os.path.join(d, 'RELEASE_NOTES.md'), 'w') as f:
        f.write('notes %s\n' % ver)
    with open(os.path.join(d, 'SHA256SUMS'), 'w') as f:
        f.write('sums %s\n' % ver)
    with open(os.path.join(d, 'assets', 'pack.bin'), 'wb') as f:
        f.write((payload or ver).encode() * 500)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix='rollup-test-')
        self.addCleanup(self._td.cleanup)
        self.root = os.path.join(self._td.name, 'releases')
        self.trash_dir = os.path.join(self._td.name, 'trash')
        os.makedirs(self.root)
        self.trash = move_trash(self.trash_dir)

    def run_apply(self, **kw: Unpack[ApplyOpts]) -> int:
        return apply(self.root, trash=self.trash, say=QUIET, **kw)

    def ls(self, skip: Container[str] = ()) -> list[str]:
        return sorted(x for x in os.listdir(self.root) if x not in skip)

    def trashed(self) -> list[str]:
        return sorted(os.listdir(self.trash_dir)) if os.path.isdir(self.trash_dir) else []


class Timeline(Base):
    SCRIPT = [
        ('0.1.0', ['v0.1.0']), ('0.1.1', ['v0.1.0.zip', 'v0.1.1']),
        ('0.1.2', ['v0.1.0.zip', 'v0.1.1.zip', 'v0.1.2']),
        ('0.1.3', ['v0.1.0.zip', 'v0.1.1.zip', 'v0.1.2.zip', 'v0.1.3']),
        ('0.2.0', ['0.1.zip', 'v0.2.0']), ('0.2.1', ['0.1.zip', 'v0.2.0.zip', 'v0.2.1']),
        ('0.3.0', ['0.1.zip', '0.2.zip', 'v0.3.0']),
        ('1.0.0', ['pre-1.0.0.zip', 'v1.0.0']), ('1.0.1', ['pre-1.0.0.zip', 'v1.0.0.zip', 'v1.0.1']),
        ('1.1.0', ['pre-1.0.0.zip', '1.0.zip', 'v1.1.0']),
        ('2.0.0', ['pre-1.0.0.zip', '1.x.zip', 'v2.0.0']),
    ]

    def test_fifteen_steps(self):
        # ignored entries sit in the folder the whole time
        os.makedirs(os.path.join(self.root, 'v0.0.9-rc.1', 'assets'))
        os.makedirs(os.path.join(self.root, 'v0.0.8'))
        with open(os.path.join(self.root, 'README.md'), 'w') as f:
            f.write('x\n')
        skip = ('README.md', 'v0.0.9-rc.1', 'v0.0.8')
        steps = 0
        for ver, want in self.SCRIPT:
            fake(self.root, ver)
            self.run_apply()
            steps += 1
            self.assertEqual(self.ls(skip), sorted(want), 'after %s' % ver)
            before = self.ls()
            self.assertEqual(self.run_apply(), 0, 'second run after %s' % ver)
            self.assertEqual(self.ls(), before)
        for x in skip:
            self.assertTrue(os.path.exists(os.path.join(self.root, x)), x)
        with zipfile.ZipFile(os.path.join(self.root, 'pre-1.0.0.zip')) as z:
            want = sorted('v%s/%s' % (v, f) for v in ('0.1.0', '0.1.1', '0.1.2', '0.1.3', '0.2.0', '0.2.1', '0.3.0') for f in FILES)
            self.assertEqual(z.namelist(), want)
            self.assertEqual(z.read('v0.2.1/assets/pack.bin'), b'0.2.1' * 500)
        self.assertEqual(steps, 11)

    def test_many_at_once_then_one_more(self):
        for mi, top in ((1, 3), (2, 2), (3, 3)):
            for pa in range(top + 1):
                fake(self.root, '0.%d.%d' % (mi, pa))
        self.run_apply()
        self.assertEqual(self.ls(), ['0.1.zip', '0.2.zip', 'v0.3.0.zip', 'v0.3.1.zip', 'v0.3.2.zip', 'v0.3.3'])
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as z:
            self.assertEqual(sorted({i.split('/')[0] for i in z.namelist()}), ['v0.1.0', 'v0.1.1', 'v0.1.2', 'v0.1.3'])
        self.assertEqual(self.run_apply(), 0)
        fake(self.root, '1.0.0')
        self.run_apply()
        self.assertEqual(self.ls(), ['pre-1.0.0.zip', 'v1.0.0'])

    def test_single_release_line(self):
        fake(self.root, '0.1.0')
        fake(self.root, '0.2.0')
        self.run_apply()
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0'])
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as z:
            self.assertEqual(z.namelist()[0].split('/')[0], 'v0.1.0')

    def test_only_newest_stays(self):
        fake(self.root, '0.1.0')
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.ls(), ['v0.1.0'])

    def test_empty_root(self):
        self.assertEqual(self.run_apply(), 0)


class Safety(Base):
    def test_corrupt_zip_keeps_sources(self):
        fake(self.root, '0.1.0')
        fake(self.root, '0.1.1')
        real = core.write_zip

        def broken(t: str, files: Files) -> None:
            real(t, files)
            with open(t, 'r+b') as f:
                f.seek(40)
                f.write(b'\xff\xff\xff\xff')
        with mock.patch.object(core, 'write_zip', broken):
            with self.assertRaises(Fail):
                self.run_apply()
        self.assertEqual(self.ls(), ['v0.1.0', 'v0.1.1'])
        self.assertEqual(self.trashed(), [])

    def test_existing_bad_zip_stops_the_run(self):
        fake(self.root, '0.1.0')
        with open(os.path.join(self.root, 'v0.0.5.zip'), 'wb') as f:
            f.write(b'not a zip')
        with self.assertRaises(Fail):
            self.run_apply()
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'v0.1.0')))

    def test_dry_run_changes_nothing(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            fake(self.root, v)
        before = self.ls()
        lines: list[str] = []
        self.assertEqual(apply(self.root, dry=True, say=lines.append), 1)
        self.assertEqual(self.ls(), before)
        self.assertEqual(self.trashed(), [])
        self.assertEqual(len(lines), 1)
        self.assertIn('would write 0.1.zip', lines[0])
        self.assertIn('2 releases, 6 files', lines[0])

    def test_dry_run_prints_plan_via_cli(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            fake(self.root, v)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['run', '--root', self.root, '--dry-run']), 0)
        self.assertIn('would write 0.1.zip', out.getvalue())
        self.assertEqual(self.ls(), ['v0.1.0', 'v0.1.1', 'v0.2.0'])

    def test_interrupted_run_is_finished_by_the_next(self):
        # zip is in place but the sources were never trashed
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
        self.run_apply()
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as z:
            data = z.read('v0.1.0/SHA256SUMS')
        fake(self.root, '0.1.0')
        self.assertEqual(self.run_apply(), 1)
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0'])
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as z:
            self.assertEqual(z.read('v0.1.0/SHA256SUMS'), data)

    def test_late_release_in_old_line_joins_the_zip(self):
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
        self.run_apply()
        fake(self.root, '0.1.1')
        self.run_apply()
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as z:
            self.assertEqual(sorted({i.split('/')[0] for i in z.namelist()}), ['v0.1.0', 'v0.1.1'])
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0'])


class Selection(Base):
    def test_rc_ignored(self):
        fake(self.root, '0.1.0')
        fake(self.root, '0.2.0-rc.1')
        fake(self.root, '0.1.1')
        self.run_apply()
        self.assertEqual(self.ls(), ['v0.1.0.zip', 'v0.1.1', 'v0.2.0-rc.1'])

    def test_rc_alone_changes_nothing(self):
        fake(self.root, '0.1.0-rc.1')
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.ls(), ['v0.1.0-rc.1'])

    def test_other_folders_and_files_left_alone(self):
        fake(self.root, '0.1.0')
        fake(self.root, '0.2.0')
        os.makedirs(os.path.join(self.root, 'notes'))
        os.makedirs(os.path.join(self.root, 'v0.1.5'))  # unpublished
        for name in ('README.md', 'extra.zip', 'v1.zip'):
            with open(os.path.join(self.root, name), 'w') as f:
                f.write('keep')
        self.run_apply()
        self.assertEqual(self.ls(), ['0.1.zip', 'README.md', 'extra.zip', 'notes', 'v0.1.5', 'v0.2.0', 'v1.zip'])

    def test_unpublished_missing_each_marker(self):
        fake(self.root, '0.1.0')
        fake(self.root, '0.2.0')
        os.remove(os.path.join(self.root, 'v0.1.0', 'SHA256SUMS'))
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.ls(), ['v0.1.0', 'v0.2.0'])

    def test_marker_option(self):
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
            os.remove(os.path.join(self.root, 'v' + v, 'SHA256SUMS'))
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.run_apply(markers=('RELEASE_NOTES.md',)), 1)
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0'])

    def test_marker_option_folder_marker_and_cli(self):
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
        with open(os.path.join(self.root, 'v0.1.0', 'MARK'), 'w') as f:
            f.write('x')
        with open(os.path.join(self.root, 'v0.2.0', 'MARK'), 'w') as f:
            f.write('x')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['run', '--root', self.root, '--trash-dir', self.trash_dir, '--marker', 'MARK', '--marker', 'assets/'])
        self.assertEqual(code, 0)
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0'])


class Trash(Base):
    def test_trash_dir_fallback_moves_sources(self):
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['run', '--root', self.root, '--trash-dir', self.trash_dir]), 0)
        self.assertEqual(self.trashed(), ['v0.1.0'])
        self.assertTrue(os.path.isfile(os.path.join(self.trash_dir, 'v0.1.0', 'SHA256SUMS')))
        self.assertIn('trashed v0.1.0', out.getvalue())

    def test_trash_dir_name_clash(self):
        os.makedirs(os.path.join(self.trash_dir, 'v0.1.0'))
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
        self.run_apply()
        self.assertEqual(self.trashed(), ['v0.1.0', 'v0.1.0.2'])

    def test_no_gio_and_no_trash_dir_is_an_error(self):
        with mock.patch('shutil.which', return_value=None):
            with self.assertRaises(Fail):
                make_trash()
            fake(self.root, '0.1.0')
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(['run', '--root', self.root]), 1)
            self.assertIn('--trash-dir', err.getvalue())

    def test_gio_used_when_present(self):
        with mock.patch('shutil.which', return_value='/usr/bin/gio'):
            self.assertIs(make_trash(), core.gio_trash)


class Output(Base):
    def test_thousands_separators(self):
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
        for i in range(1000):
            with open(os.path.join(self.root, 'v0.1.0', 'assets', 'f%d' % i), 'w') as f:
                f.write('x')
        lines: list[str] = []
        apply(self.root, trash=self.trash, say=lines.append)
        self.assertIn('1,003 files', lines[0])
        self.assertEqual(core.n(1234567), '1,234,567')

    def test_plan_is_read_only(self):
        for v in ('0.1.0', '0.2.0'):
            fake(self.root, v)
        before = self.ls()
        steps = plan(self.root)
        self.assertEqual([os.path.basename(s['target']) for s in steps], ['0.1.zip'])
        self.assertEqual(self.ls(), before)

    def test_cli_missing_root(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(['run', '--root', os.path.join(self.root, 'nope')]), 2)

    def test_cli_nothing_to_do(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['run', '--root', self.root, '--trash-dir', self.trash_dir]), 0)
        self.assertEqual(out.getvalue().strip(), 'nothing to do')


load_tests = hostcat.load_tests


if __name__ == '__main__':
    unittest.main()


def rc_dir(root: str, name: str) -> None:
    os.makedirs(os.path.join(root, name, 'assets'))


def rc_zip(root: str, name: str) -> None:
    with zipfile.ZipFile(os.path.join(root, name), 'w') as z:
        z.writestr('x.txt', 'rc')


class RcCleanup(Base):
    def test_bare_and_v_folders_go_when_release_is_a_folder(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.1.0-rc.1')
        rc_dir(self.root, 'v0.1.0-rc.2')
        self.run_apply()
        self.assertEqual(self.ls(), ['v0.1.0'])
        self.assertEqual(self.trashed(), ['0.1.0-rc.1', 'v0.1.0-rc.2'])

    def test_rc_zips_go(self):
        fake(self.root, '0.1.0')
        rc_zip(self.root, '0.1.0-rc.1.zip')
        rc_zip(self.root, 'v0.1.0-rc.2.zip')
        self.run_apply()
        self.assertEqual(self.ls(), ['v0.1.0'])
        self.assertEqual(len(self.trashed()), 2)

    def test_release_as_per_release_zip(self):
        fake(self.root, '0.1.0')
        fake(self.root, '0.1.1')
        rc_dir(self.root, 'v0.1.0-rc.1')
        self.run_apply()
        self.assertEqual(self.ls(), ['v0.1.0.zip', 'v0.1.1'])
        # second run: the release now only exists inside the zip
        rc_dir(self.root, 'v0.1.0-rc.3')
        self.run_apply()
        self.assertEqual(self.ls(), ['v0.1.0.zip', 'v0.1.1'])

    def test_release_inside_line_zip(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            fake(self.root, v)
        self.run_apply()
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0'])
        rc_dir(self.root, '0.1.1-rc.1')
        self.run_apply()
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0'])
        self.assertIn('0.1.1-rc.1', self.trashed())

    def test_release_inside_pre_1_0_0_zip(self):
        for v in ('0.1.0', '0.2.0', '1.0.0'):
            fake(self.root, v)
        self.run_apply()
        rc_dir(self.root, 'v0.2.0-rc.1')
        self.run_apply()
        self.assertEqual(self.ls(), ['pre-1.0.0.zip', 'v1.0.0'])

    def test_release_inside_major_zip(self):
        for v in ('1.0.0', '1.1.0', '2.0.0'):
            fake(self.root, v)
        self.run_apply()
        self.assertEqual(self.ls(), ['1.x.zip', 'v2.0.0'])
        rc_dir(self.root, '1.1.0-rc.1')
        self.run_apply()
        self.assertNotIn('1.1.0-rc.1', self.ls())

    def test_unpublished_release_keeps_its_rc(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.2.0-rc.1')
        rc_zip(self.root, 'v0.2.0-rc.2.zip')
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.ls(), ['0.2.0-rc.1', 'v0.1.0', 'v0.2.0-rc.2.zip'])
        fake(self.root, '0.2.0')
        self.run_apply()
        self.assertNotIn('0.2.0-rc.1', self.ls())
        self.assertNotIn('v0.2.0-rc.2.zip', self.ls())

    def test_rc_of_other_version_stays(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.1.0-rc.1')
        rc_dir(self.root, '0.1.1-rc.1')
        self.run_apply()
        self.assertEqual(self.ls(), ['0.1.1-rc.1', 'v0.1.0'])

    def test_unpublished_folder_does_not_publish(self):
        os.makedirs(os.path.join(self.root, 'v0.1.0'))
        rc_dir(self.root, '0.1.0-rc.1')
        self.run_apply()
        self.assertEqual(self.ls(), ['0.1.0-rc.1', 'v0.1.0'])

    def test_alpha_and_beta_go_with_the_rc_builds(self):
        fake(self.root, '0.1.0')
        for name in ('0.1.0-alpha.1', 'v0.1.0-alpha.10', '0.1.0-beta.2', 'v0.1.0-rc.1'):
            rc_dir(self.root, name)
        rc_zip(self.root, 'v0.1.0-beta.3.zip')
        self.run_apply()
        self.assertEqual(self.ls(), ['v0.1.0'])
        self.assertEqual(len(self.trashed()), 5)

    def test_alpha_beta_of_unpublished_release_stay(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.2.0-alpha.1')
        rc_dir(self.root, 'v0.2.0-beta.1')
        before = self.ls()
        self.run_apply()
        self.assertEqual(self.ls(), before)

    def test_keep_rc_keeps_every_stage(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.1.0-alpha.1')
        rc_dir(self.root, '0.1.0-beta.1')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--root', self.root, '--keep-pre', '--trash-dir', self.trash_dir]), 0)
        self.assertEqual(self.ls(), ['0.1.0-alpha.1', '0.1.0-beta.1', 'v0.1.0'])

    def test_stage_folders_are_never_releases(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, 'v0.1.1-alpha.1')
        rc_dir(self.root, 'v0.1.1-beta.1')
        self.assertFalse(core.DIR_RE.match('v0.1.1-alpha.1'))
        self.assertFalse(core.DIR_RE.match('v0.1.1-beta.1'))
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.ls(), ['v0.1.0', 'v0.1.1-alpha.1', 'v0.1.1-beta.1'])

    def test_vkey_order(self):
        # alpha < beta < rc < final, build number compared as a number
        order = ['v0.9.0', '1.0.0-alpha.2', '1.0.0-alpha.10', 'v1.0.0-beta.1', '1.0.0-beta.3', '1.0.0-rc.1', 'v1.0.0-rc.9.zip',
                 '1.0.0-rc.10', 'v1.0.0', 'v1.0.0.zip', '1.0.1-alpha.1', '1.0.1']
        def key(name: str) -> core.VKey:
            k = core.vkey(name)
            assert k is not None, name
            return k
        keys = [key(x) for x in order]
        self.assertEqual(keys, sorted(keys))
        self.assertLess(key('1.0.0-rc.9'), key('1.0.0-rc.10'))
        self.assertLess(key('1.0.0-alpha.2'), key('1.0.0-alpha.10'))
        self.assertLess(key('1.0.0-beta.9'), key('1.0.0-rc.1'))
        self.assertLess(key('1.0.0-rc.99'), key('1.0.0'))
        self.assertEqual(core.vkey('1.0.0'), (1, 0, 0, 3, 0))
        self.assertIsNone(core.vkey('1.0.0-gamma.1'))
        self.assertIsNone(core.vkey('notes'))

    def test_leftovers_listed_oldest_build_first(self):
        fake(self.root, '0.1.0')
        for name in ('0.1.0-rc.10', '0.1.0-rc.9', '0.1.0-alpha.2', '0.1.0-beta.1'):
            rc_dir(self.root, name)
        names = [os.path.basename(p) for p, _ in core.rc_leftovers(self.root, {(0, 1, 0)})]
        self.assertEqual(names, ['0.1.0-alpha.2', '0.1.0-beta.1', '0.1.0-rc.9', '0.1.0-rc.10'])

    def test_lookalike_names_untouched(self):
        fake(self.root, '0.1.0')
        for name in ('0.1.0-rc.1-notes', '0.1.0-rc.x', '0.1.0-rc.1.bak', 'v0.1.0-rc.', '0.1.0-beta.x', '0.1.0-alpha.1-notes', '0.1.0-gamma.1'):
            rc_dir(self.root, name)
        rc_zip(self.root, '0.1.0-rc.1-notes.zip')
        before = self.ls()
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.ls(), before)

    def test_dry_run_changes_nothing_and_prints(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.1.0-rc.1')
        before = self.ls()
        out: list[str] = []
        apply(self.root, dry=True, say=out.append)
        self.assertEqual(self.ls(), before)
        self.assertEqual(out, ['would trash 0.1.0-rc.1 (release 0.1.0 is published)'])

    def test_real_run_prints_trashed(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, 'v0.1.0-rc.1')
        out: list[str] = []
        apply(self.root, trash=self.trash, say=out.append)
        self.assertEqual(out, ['trashed v0.1.0-rc.1'])

    def test_second_run_is_a_no_op(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.1.0-rc.1')
        self.run_apply()
        out: list[str] = []
        apply(self.root, trash=self.trash, say=out.append)
        self.assertEqual(out, ['nothing to do'])
        self.assertEqual(self.trashed(), ['0.1.0-rc.1'])

    def test_cli_trash_dir_and_keep_rc(self):
        fake(self.root, '0.1.0')
        rc_dir(self.root, '0.1.0-rc.1')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--root', self.root, '--keep-rc', '--trash-dir', self.trash_dir]), 0)
        self.assertEqual(self.ls(), ['0.1.0-rc.1', 'v0.1.0'])
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(main(['run', '--root', self.root, '--trash-dir', self.trash_dir]), 0)
        self.assertEqual(buf.getvalue().strip(), 'trashed 0.1.0-rc.1')
        self.assertEqual(self.ls(), ['v0.1.0'])
        self.assertEqual(self.trashed(), ['0.1.0-rc.1'])

    def test_failed_zip_leaves_rcs_in_place(self):
        fake(self.root, '0.1.0')
        fake(self.root, '0.1.1')
        rc_dir(self.root, '0.1.0-rc.1')
        with mock.patch.object(core, 'verify', return_value='boom'):
            with self.assertRaises(Fail):
                self.run_apply()
        self.assertEqual(self.ls(), ['0.1.0-rc.1', 'v0.1.0', 'v0.1.1'])


class ZipNewest(Base):
    def test_newest_zipped_with_flag(self):
        fake(self.root, '0.1.0')
        self.assertEqual(self.run_apply(zip_newest=True), 1)
        self.assertEqual(self.ls(), ['v0.1.0.zip'])
        with zipfile.ZipFile(os.path.join(self.root, 'v0.1.0.zip')) as z:
            self.assertEqual(sorted(z.namelist()), ['v0.1.0/' + f for f in sorted(FILES)])

    def test_untouched_without_flag(self):
        fake(self.root, '0.1.0')
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.ls(), ['v0.1.0'])

    def test_verification_failure_keeps_folder(self):
        fake(self.root, '0.1.0')
        with mock.patch.object(core, 'verify', return_value='boom'):
            with self.assertRaises(Fail):
                self.run_apply(zip_newest=True)
        self.assertEqual(self.ls(), ['v0.1.0'])
        self.assertEqual(self.trashed(), [])

    def test_rerun_is_idempotent(self):
        fake(self.root, '0.1.0')
        self.run_apply(zip_newest=True)
        before = os.path.getmtime(os.path.join(self.root, 'v0.1.0.zip'))
        self.assertEqual(self.run_apply(zip_newest=True), 0)
        self.assertEqual(self.ls(), ['v0.1.0.zip'])
        self.assertEqual(os.path.getmtime(os.path.join(self.root, 'v0.1.0.zip')), before)

    def test_custom_markers(self):
        fake(self.root, '0.1.0')
        os.remove(os.path.join(self.root, 'v0.1.0', 'RELEASE_NOTES.md'))
        self.assertEqual(self.run_apply(zip_newest=True), 0)
        self.assertEqual(self.ls(), ['v0.1.0'])
        self.assertEqual(self.run_apply(zip_newest=True, markers=('SHA256SUMS', 'assets/')), 1)
        self.assertEqual(self.ls(), ['v0.1.0.zip'])

    def test_lines_still_roll_up_and_newest_zip_stays_separate(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            fake(self.root, v)
        self.run_apply(zip_newest=True)
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0.zip'])
        fake(self.root, '0.2.1')
        self.run_apply(zip_newest=True)
        self.assertEqual(self.ls(), ['0.1.zip', 'v0.2.0.zip', 'v0.2.1.zip'])

    def test_rc_cleanup_when_release_only_exists_as_zip(self):
        fake(self.root, '0.1.0')
        self.run_apply(zip_newest=True)
        rc_dir(self.root, 'v0.1.0-rc.4')
        self.run_apply(zip_newest=True)
        self.assertEqual(self.ls(), ['v0.1.0.zip'])

    def test_dry_run_and_cli(self):
        fake(self.root, '0.1.0')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(['run', '--root', self.root, '--dry-run', '--zip-newest']), 0)
        self.assertIn('would write v0.1.0.zip', buf.getvalue())
        self.assertEqual(self.ls(), ['v0.1.0'])
        with contextlib.redirect_stdout(io.StringIO()):
            main(['run', '--root', self.root, '--zip-newest', '--trash-dir', self.trash_dir])
        self.assertEqual(self.ls(), ['v0.1.0.zip'])


def pre_build(root: str, name: str, payload: str | None = None, finished: bool = True) -> str:
    """A tester build in prereleases/<stage>/<name>/ with its marker files."""
    stage = name.split('-')[1].split('.')[0]
    d = os.path.join(root, stage, name)
    os.makedirs(os.path.join(d, 'assets'))
    with open(os.path.join(d, 'RELEASE_NOTES.md'), 'w') as f:
        f.write('notes %s\n' % name)
    with open(os.path.join(d, 'assets', 'pack.mcpack'), 'wb') as f:
        f.write((payload or name).encode() * 300)
    if finished:
        for m in core.PRE_MARKERS:
            with open(os.path.join(d, m), 'w') as f:
                f.write('%s %s\n' % (m, name))
    return d


class PreOpts(TypedDict, total=False):
    dry: bool
    markers: Iterable[str]
    dedupe: bool


class Prereleases(Base):
    def setUp(self) -> None:
        super().setUp()
        self.pre = os.path.join(self._td.name, 'prereleases')
        os.makedirs(self.pre)

    def run_pre(self, current: str | None = None, **kw: Unpack[PreOpts]) -> int:
        return core.apply_pre(self.pre, current=current, trash=self.trash, say=QUIET, **kw)

    def tree(self) -> dict[str, list[str]]:
        return {st: sorted(os.listdir(os.path.join(self.pre, st)))
                for st in sorted(os.listdir(self.pre)) if os.path.isdir(os.path.join(self.pre, st))}

    def members(self, rel: str) -> list[str]:
        with zipfile.ZipFile(os.path.join(self.pre, rel)) as z:
            return sorted({i.split('/')[0] for i in z.namelist()})

    def test_newest_per_stage_stays(self):
        for nm in ('v1.0.0-alpha.1', 'v1.0.0-alpha.2', 'v1.0.0-alpha.3', 'v1.0.0-beta.1', 'v1.0.0-beta.2'):
            pre_build(self.pre, nm)
        self.assertEqual(self.run_pre('1.0.0-beta.2'), 2)
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.3', 'v1.0.0-alpha.x.zip'],
                                       'beta': ['v1.0.0-beta.2', 'v1.0.0-beta.x.zip']})
        self.assertEqual(self.members('alpha/v1.0.0-alpha.x.zip'), ['v1.0.0-alpha.1', 'v1.0.0-alpha.2'])
        self.assertEqual(self.trashed(), ['v1.0.0-alpha.1', 'v1.0.0-alpha.2', 'v1.0.0-beta.1'])
        self.assertEqual(self.run_pre('1.0.0-beta.2'), 0)

    def test_one_build_is_left_alone(self):
        pre_build(self.pre, 'v1.0.0-alpha.1')
        self.assertEqual(self.run_pre('1.0.0-alpha.1'), 0)
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.1']})

    def test_zip_grows_with_each_build(self):
        pre_build(self.pre, 'v1.0.0-beta.1')
        for i in range(2, 6):
            pre_build(self.pre, 'v1.0.0-beta.%d' % i)
            self.run_pre('1.0.0-beta.%d' % i)
            self.assertEqual(self.tree(), {'beta': ['v1.0.0-beta.%d' % i, 'v1.0.0-beta.x.zip']})
            self.assertEqual(self.members('beta/v1.0.0-beta.x.zip'), ['v1.0.0-beta.%d' % k for k in range(1, i)])
        with zipfile.ZipFile(os.path.join(self.pre, 'beta', 'v1.0.0-beta.x.zip')) as z:
            self.assertEqual(z.read('v1.0.0-beta.1/assets/pack.mcpack'), b'v1.0.0-beta.1' * 300)
        # The old zip that was replaced in place is not trashed, only the folders that went into it.
        self.assertEqual(self.trashed(), ['v1.0.0-beta.%d' % k for k in range(1, 5)])

    def test_release_rolls_up_everything(self):
        for nm in ('v1.0.0-alpha.1', 'v1.0.0-alpha.2', 'v1.0.0-beta.1', 'v1.0.0-rc.1'):
            pre_build(self.pre, nm)
        self.run_pre('1.0.0-rc.1')
        self.assertEqual(self.tree()['alpha'], ['v1.0.0-alpha.2', 'v1.0.0-alpha.x.zip'])
        self.run_pre('1.0.0')
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.x.zip'], 'beta': ['v1.0.0-beta.x.zip'],
                                       'rc': ['v1.0.0-rc.x.zip']})
        self.assertEqual(self.members('alpha/v1.0.0-alpha.x.zip'), ['v1.0.0-alpha.1', 'v1.0.0-alpha.2'])
        self.assertEqual(self.run_pre('1.0.0'), 0)

    def test_next_cycle_keeps_old_zip(self):
        pre_build(self.pre, 'v1.0.0-alpha.1')
        self.run_pre('1.0.0')
        pre_build(self.pre, 'v1.1.0-alpha.1')
        pre_build(self.pre, 'v1.1.0-alpha.2')
        self.run_pre('1.1.0-alpha.2')
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.x.zip', 'v1.1.0-alpha.2', 'v1.1.0-alpha.x.zip']})
        self.assertEqual(self.members('alpha/v1.0.0-alpha.x.zip'), ['v1.0.0-alpha.1'])

    def test_unreleased_later_version_keeps_newest(self):
        # VERSION still says 1.0.0 while builds of 1.1.0 exist: 1.1.0 is not released yet.
        pre_build(self.pre, 'v1.1.0-beta.1')
        pre_build(self.pre, 'v1.1.0-beta.2')
        self.run_pre('1.0.0')
        self.assertEqual(self.tree(), {'beta': ['v1.1.0-beta.2', 'v1.1.0-beta.x.zip']})

    def test_older_base_than_current_is_rolled(self):
        pre_build(self.pre, 'v1.0.1-alpha.1')
        pre_build(self.pre, 'v1.1.0-alpha.1')
        self.run_pre('v1.1.0-alpha.1')
        self.assertEqual(self.tree(), {'alpha': ['v1.0.1-alpha.x.zip', 'v1.1.0-alpha.1']})

    def test_without_current_newest_version_is_open(self):
        pre_build(self.pre, 'v1.0.0-rc.1')
        pre_build(self.pre, 'v1.1.0-rc.1')
        pre_build(self.pre, 'v1.1.0-rc.2')
        self.run_pre()
        self.assertEqual(self.tree(), {'rc': ['v1.0.0-rc.x.zip', 'v1.1.0-rc.2', 'v1.1.0-rc.x.zip']})

    def test_unfinished_and_foreign_left_alone(self):
        pre_build(self.pre, 'v1.0.0-beta.1')
        pre_build(self.pre, 'v1.0.0-beta.2')
        pre_build(self.pre, 'v1.0.0-beta.3', finished=False)
        os.makedirs(os.path.join(self.pre, 'beta', 'v1.0.0-alpha.9'))
        with open(os.path.join(self.pre, 'beta', 'notes.txt'), 'w') as f:
            f.write('x')
        os.makedirs(os.path.join(self.pre, 'gamma', 'v1.0.0-gamma.1'))
        self.run_pre('1.0.0-beta.3')
        # beta.3 is still being written, so beta.2 is the newest finished build and stays.
        self.assertEqual(self.tree()['beta'], ['notes.txt', 'v1.0.0-alpha.9', 'v1.0.0-beta.2', 'v1.0.0-beta.3',
                                               'v1.0.0-beta.x.zip'])
        self.assertEqual(self.tree()['gamma'], ['v1.0.0-gamma.1'])

    def test_bad_current_and_bad_zip(self):
        pre_build(self.pre, 'v1.0.0-alpha.1')
        with self.assertRaises(Fail):
            self.run_pre('1.0')
        with self.assertRaises(Fail):
            self.run_pre('1.0.0-gamma.1')
        with open(os.path.join(self.pre, 'alpha', 'v1.0.0-alpha.x.zip'), 'w') as f:
            f.write('not a zip')
        before = self.tree()
        with self.assertRaises(Fail):
            self.run_pre('1.0.0')
        self.assertEqual(self.tree(), before)

    def test_zip_with_wrong_member_refused(self):
        pre_build(self.pre, 'v1.0.0-alpha.1')
        with zipfile.ZipFile(os.path.join(self.pre, 'alpha', 'v1.0.0-alpha.x.zip'), 'w') as z:
            z.writestr('v2.0.0-alpha.1/RELEASE_NOTES.md', 'x')
        with self.assertRaises(Fail):
            self.run_pre('1.0.0')
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.1', 'v1.0.0-alpha.x.zip']})

    def test_corrupt_write_keeps_sources(self):
        pre_build(self.pre, 'v1.0.0-alpha.1')
        with mock.patch.object(core, 'verify', return_value='forced'):
            with self.assertRaises(Fail):
                self.run_pre('1.0.0')
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.1']})
        self.assertEqual(self.trashed(), [])

    def test_folder_and_zip_copy_folder_wins(self):
        pre_build(self.pre, 'v1.0.0-alpha.1')
        pre_build(self.pre, 'v1.0.0-alpha.2')
        self.run_pre('1.0.0-alpha.2')
        # Someone unpacks alpha.1 again to test it; the next run puts it back.
        with zipfile.ZipFile(os.path.join(self.pre, 'alpha', 'v1.0.0-alpha.x.zip')) as z:
            z.extractall(os.path.join(self.pre, 'alpha'))
        self.run_pre('1.0.0-alpha.2')
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.2', 'v1.0.0-alpha.x.zip']})

    def test_no_stage_folders(self):
        self.assertEqual(self.run_pre('1.0.0'), 0)
        self.assertEqual(self.tree(), {})

    def test_cli_pre(self):
        pre_build(self.pre, 'v1.0.0-alpha.1')
        pre_build(self.pre, 'v1.0.0-alpha.2')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(['pre', '--root', self.pre, '--current', '1.0.0-alpha.2', '--dry-run']), 0)
        self.assertIn('would write alpha/v1.0.0-alpha.x.zip (1 build, 4 files)', buf.getvalue())
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.1', 'v1.0.0-alpha.2']})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['pre', '--root', self.pre, '--current', '1.0.0-alpha.2', '--trash-dir', self.trash_dir]), 0)
        self.assertEqual(self.tree(), {'alpha': ['v1.0.0-alpha.2', 'v1.0.0-alpha.x.zip']})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(main(['pre', '--root', self.pre, '--current', 'nope']), 0)
