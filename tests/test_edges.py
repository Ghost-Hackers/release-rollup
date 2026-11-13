"""Runs with no trash command, a failing gio, odd names, and damaged or odd inputs."""
import io
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import hostcat  # noqa: E402
import smallfs  # noqa: E402
import test_archive as TA  # noqa: E402
import test_dedupe as TD  # noqa: E402
import test_rollup as TR  # noqa: E402
from release_rollup import core  # noqa: E402
from release_rollup.container import MANIFEST, SKELETON_DIR, Rollup, check_paths, restore  # noqa: E402
from release_rollup.core import Fail, apply, apply_pre, archive, upgrade, upgrade_zip  # noqa: E402

load_tests = hostcat.load_tests


def QUIET(*_: object) -> None:
    pass


def flip(path: str, name: str, at: int = 0) -> None:
    """Flip one byte of an entry's data on disk, leaving its CRC stale."""
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(name)
    with open(path, 'r+b') as f:
        f.seek(info.header_offset + 26)
        nlen, elen = struct.unpack('<HH', f.read(4))
        f.seek(info.header_offset + 30 + nlen + elen + at)
        byte = f.read(1)
        f.seek(-1, 1)
        f.write(bytes([byte[0] ^ 0xFF]))


class NoTrashCommand(TR.Base):
    """Called without a trash function and with no gio on PATH, every command stops before it writes."""

    def setUp(self) -> None:
        super().setUp()
        empty = os.path.join(self._td.name, 'bin')
        os.makedirs(empty)
        self.only_on_path(empty)

    def test_apply(self):
        for v in ('0.1.0', '0.2.0'):
            TR.make_release(self.root, v)
        with self.assertRaisesRegex(Fail, 'gio is not installed'):
            apply(self.root, say=QUIET)
        self.assertEqual(self.ls(), ['v0.1.0', 'v0.2.0'])

    def test_apply_pre(self):
        for nm in ('v1.0.0-beta.1', 'v1.0.0-beta.2'):
            TR.pre_build(self.root, nm)
        with self.assertRaisesRegex(Fail, 'gio is not installed'):
            apply_pre(self.root, '1.0.0-beta.2', say=QUIET)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, 'beta'))), ['v1.0.0-beta.1', 'v1.0.0-beta.2'])

    def test_upgrade(self):
        with self.assertRaisesRegex(Fail, 'gio is not installed'):
            upgrade(self.root, say=QUIET)

    def test_archive(self):
        TA.write(os.path.join(self.root, 'a.txt'), b'a')
        with self.assertRaisesRegex(Fail, 'gio is not installed'):
            archive(self.root, 'archive.zip', say=QUIET)
        self.assertEqual(sorted(os.listdir(self.root)), ['a.txt'])


class GioTrash(TR.Base):
    """gio_trash runs the real command; these put a shell script named gio first on PATH."""

    def gio(self, body: str) -> None:
        folder = os.path.join(self._td.name, 'bin')
        os.makedirs(folder)
        stub = os.path.join(folder, 'gio')
        with open(stub, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(stub, 0o755)
        self.only_on_path(folder + os.pathsep + '/bin' + os.pathsep + '/usr/bin')

    def test_linux_gio_trash_moves_the_file(self):
        gone = os.path.join(self._td.name, 'gone')
        os.makedirs(gone)
        self.gio('mv "$2" "%s/"\n' % gone)
        p = os.path.join(self.root, 'x')
        TA.write(p, b'x')
        core.gio_trash(p)
        self.assertEqual(os.listdir(gone), ['x'])
        self.assertFalse(os.path.exists(p))

    def test_linux_gio_trash_failing_is_a_clean_error(self):
        self.gio('echo "no trash here" >&2\nexit 2\n')
        p = os.path.join(self.root, 'x')
        TA.write(p, b'x')
        with self.assertRaisesRegex(Fail, 'gio trash .* failed: no trash here'):
            core.gio_trash(p)
        self.assertTrue(os.path.exists(p))


class Names(unittest.TestCase):
    def test_names_differing_in_case_clash_on_windows(self):
        check_paths(['v0.1.0/A', 'v0.1.0/a'], False)
        with self.assertRaisesRegex(Fail, "name 'v0.1.0/a' appears twice"):
            check_paths(['v0.1.0/A', 'v0.1.0/a'], True)

    def test_colon_in_a_flat_name_is_refused(self):
        check_paths(['v0.1.0/ab:c'], False)
        with self.assertRaisesRegex(Fail, 'colon in the name'):
            check_paths(['ab:c'], False, 'flat')


class Upgrade(unittest.TestCase):
    def test_old_copy_in_the_way_is_refused(self):
        with tempfile.TemporaryDirectory(prefix='rollup-edges-') as td:
            path = os.path.join(td, '0.1.zip')
            data = TA.blob(1)
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
                for v in ('v0.1.0', 'v0.1.1'):
                    z.writestr(v + '/a.bin', data)
            before = TA.read(path)
            TA.write(core.old_copy_path(path), b'left over')
            got = upgrade_zip(path, '0.1.zip', False, core.move_trash(os.path.join(td, 'trash')), QUIET)
            self.assertEqual(got[0], 'refused')
            self.assertIn('is in the way; trash it first', got[3])
            self.assertEqual(TA.read(path), before)
            self.assertEqual(sorted(os.listdir(td)), ['.0.1.zip.upgrade-old', '0.1.zip'])


class Oversized(TA.Base):
    def test_linux_archive_over_the_size_limit_is_refused_unread(self):
        # Sparse, so it takes no disk space.
        big = os.path.join(self.root, 'big.bin')
        with open(big, 'wb') as f:
            f.truncate(core.DEFAULT_MAX_SIZE + 1)
        with self.assertRaisesRegex(Fail, 'would hold more than .* bytes; sources kept'):
            self.run_archive()
        self.assertEqual(os.listdir(self.root), ['big.bin'])


class ArchiveTarget(TA.Base):
    def test_out_that_is_a_folder_is_refused(self):
        os.makedirs(os.path.join(self.root, TA.OUT))
        self.put('report.zip', TA.blob(1))
        with self.assertRaisesRegex(Fail, '%s is not a plain file' % TA.OUT):
            self.run_archive()
        self.assertEqual(sorted(os.listdir(self.root)), [TA.OUT, 'report.zip'])

    def test_old_archive_copy_in_the_way_is_refused(self):
        self.put('report.zip', TA.blob(1))
        self.run_archive()
        self.put('later.zip', TA.blob(2))
        left = os.path.join(self.root, '.%s.archive-old' % TA.OUT)
        TA.write(left, b'left over')
        before = TD.snapshot(self.root)
        with self.assertRaisesRegex(Fail, 'is in the way .*Nothing was changed'):
            self.run_archive()
        self.assertEqual(TD.snapshot(self.root), before)


class Scan(TD.Base):
    def test_release_folder_without_assets_is_not_rolled_up(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            TD.release(self.root, v, TD.app(v))
        loose = TD.release(self.root, '0.1.2', TD.app('0.1.2'))
        shutil.rmtree(os.path.join(loose, 'assets'))
        self.run_apply()
        with Rollup(os.path.join(self.root, '0.1.zip')) as r:
            self.assertEqual({nm.split('/')[0] for nm in r.names()}, {'v0.1.0', 'v0.1.1'})
        self.assertTrue(os.path.isdir(loose))

    def test_zip_with_a_stray_top_level_entry_stops_the_run(self):
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip'), 'w') as z:
            z.writestr('v0.1.0/a.bin', b'a')
            z.writestr('notes.txt', b'b')
        TD.release(self.root, '0.2.0', TD.app('0.2.0'))
        before = TD.snapshot(self.root)
        with self.assertRaisesRegex(Fail, "0.1.zip holds an unexpected top-level entry 'notes.txt'"):
            self.run_apply()
        self.assertEqual(TD.snapshot(self.root), before)

    def test_rc_names_of_the_wrong_kind_stay(self):
        TD.release(self.root, '0.1.0', TD.app('0.1.0'))
        TA.write(os.path.join(self.root, 'v0.1.0-rc.1'), b'a file named like a folder')
        os.makedirs(os.path.join(self.root, 'v0.1.0-rc.2.zip'))
        self.run_apply()
        self.assertEqual(sorted(os.listdir(self.root)), ['v0.1.0', 'v0.1.0-rc.1', 'v0.1.0-rc.2.zip'])


class DamagedRollup(TD.Base):
    """An existing rollup is read again, without a full check, when a new release joins it."""

    def add_release(self) -> dict[str, str]:
        TD.release(self.root, '0.1.3', TD.app('0.1.3'))
        return TD.snapshot(self.root)

    def test_damaged_blob_stops_the_run_and_keeps_everything(self):
        z = self.rollup()
        flip(z, TD.Corruption.blob_of(z).filename, 1000)
        before = self.add_release()
        with self.assertRaisesRegex(Fail, 'blob [0-9a-f]{64} of v0.1.0/assets/app.zip cannot be read'):
            self.run_apply()
        self.assertEqual(TD.snapshot(self.root), before)

    def test_damaged_skeleton_stops_the_run_and_keeps_everything(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            skeleton = next(nm for nm in zf.namelist() if nm.startswith(SKELETON_DIR))
        flip(z, skeleton)
        before = self.add_release()
        with self.assertRaisesRegex(Fail, 'skeleton [0-9a-f]{64} of v0.1.[0-2]/assets/app.zip cannot be read'):
            self.run_apply()
        self.assertEqual(TD.snapshot(self.root), before)

    def test_damaged_manifest_is_refused(self):
        z = self.rollup()
        flip(z, MANIFEST)
        self.assertRefused(z, 'Bad CRC-32|Error -3')

    def test_inner_zip_with_a_damaged_directory_is_stored_whole(self):
        good = TD.app('0.1.0')
        at = good.rindex(b'PK\x01\x02')
        bad = good[:at] + b'PK\x01\x00' + good[at + 4:]
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(bad)))
        TD.release(self.root, '0.1.0', bad)
        for v in ('0.1.1', '0.2.0'):
            TD.release(self.root, v, TD.app(v))
        self.run_apply()
        restore(os.path.join(self.root, '0.1.zip'), out=self.out, say=QUIET)
        self.assertEqual(TD.snapshot(self.out)['v0.1.0/assets/app.zip'], TD.sha(bad))

    def test_linux_source_shorter_than_its_size_is_refused(self):
        # sysfs reports 4096 bytes for a file that reads as a few.
        d = TD.release(self.root, '0.1.0', TD.app('0.1.0'))
        os.symlink('/sys/devices/system/cpu/online', os.path.join(d, 'assets', 'online'))
        for v in ('0.1.1', '0.2.0'):
            TD.release(self.root, v, TD.app(v))
        before = TD.snapshot(self.root)
        with self.assertRaisesRegex(Fail, 'a source file got shorter while it was being read; sources kept'):
            self.run_apply()
        self.assertEqual(TD.snapshot(self.root), before)


class NoHardLinks(TD.Base):
    """Where os.link fails, the old copy kept during a swap is a full copy."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = smallfs.nolinks_folder(self)
        self.root = os.path.join(self.tmp, 'releases')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.root)
        self.trash = core.move_trash(os.path.join(self.tmp, 'trash'))

    def test_windows_upgrade_trashes_a_full_copy_of_the_old_zip(self):
        z = self.rollup(dedupe=False)
        old = TD.sha_file(z)
        restore(z, out=os.path.join(self.tmp, 'before'), say=QUIET)
        upgrade(self.root, trash=self.trash, say=QUIET)
        self.assertTrue(TD.is_deduped(z))
        self.assertEqual(TD.sha_file(os.path.join(self.tmp, 'trash', '.0.1.zip.upgrade-old')), old)
        restore(z, out=self.out, say=QUIET)
        self.assertEqual(TD.snapshot(self.out), TD.snapshot(os.path.join(self.tmp, 'before')))

    def test_windows_archive_trashes_a_full_copy_of_the_old_archive(self):
        box = os.path.join(self.tmp, 'outbox')
        os.makedirs(box)
        want = {'one.zip': TD.sha(TA.blob(1)), 'two.zip': TD.sha(TA.blob(2))}
        TA.write(os.path.join(box, 'one.zip'), TA.blob(1))
        archive(box, TA.OUT, trash=self.trash, say=QUIET)
        first = TD.sha_file(os.path.join(box, TA.OUT))
        TA.write(os.path.join(box, 'two.zip'), TA.blob(2))
        archive(box, TA.OUT, trash=self.trash, say=QUIET)
        self.assertEqual(TD.sha_file(os.path.join(self.tmp, 'trash', '.%s.archive-old' % TA.OUT)), first)
        self.assertEqual(sorted(os.listdir(box)), [TA.OUT])
        restore(os.path.join(box, TA.OUT), out=self.out, say=QUIET)
        self.assertEqual(TD.snapshot(self.out), want)


if __name__ == '__main__':
    unittest.main()
