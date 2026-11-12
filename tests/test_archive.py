"""The archive command: the plain files of one folder roll into one deduped archive."""
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from collections.abc import Mapping
from unittest import mock

import hostcat
import test_dedupe as TD
from release_rollup import cli, container, core
from release_rollup.container import MANIFEST, README, Rollup, parse_manifest, path_problem, restore
from release_rollup.core import Fail, Src, archive

QUIET = TD.QUIET
OUT = 'archive.zip'


def blob(seed: int, n: int = 40000) -> bytes:
    return random.Random(seed).randbytes(n)


def make_zip(path: str, members: Mapping[str, bytes]) -> None:
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(zipfile.ZipInfo(name, (2026, 1, 2, 3, 4, 6)), data)


def write(path: str, data: bytes) -> None:
    with open(path, 'wb') as f:
        f.write(data)


def read(path: str) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix='rollup-archive-')
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name
        self.root = os.path.join(self.tmp, 'outbox')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.root)
        self.trash_dir = os.path.join(self.tmp, 'trash')
        self.said: list[str] = []

    def say(self, line: str) -> None:
        self.said.append(line)

    def run_archive(self, dry: bool = False) -> int:
        self.said = []
        return archive(self.root, OUT, dry=dry, trash=core.move_trash(self.trash_dir), say=self.say)

    def put(self, name: str, data: bytes) -> str:
        write(os.path.join(self.root, name), data)
        return TD.sha(data)

    def target(self) -> str:
        return os.path.join(self.root, OUT)

    def trashed(self) -> list[str]:
        return sorted(os.listdir(self.trash_dir)) if os.path.isdir(self.trash_dir) else []

    def visible(self) -> list[str]:
        return sorted(n for n in os.listdir(self.root) if not n.startswith('.'))

    def hidden(self) -> list[str]:
        return sorted(n for n in os.listdir(self.root) if n.startswith('.'))

    def restored(self) -> dict[str, str]:
        shutil.rmtree(self.out, ignore_errors=True)
        restore(self.target(), out=self.out, say=QUIET)
        return TD.snapshot(self.out)


class Rolling(Base):
    def reports(self) -> dict[str, str]:
        """Two report zips that share a runtime file, plus a log that is not a zip."""
        want: dict[str, str] = {}
        for i, tag in enumerate(('a', 'b')):
            name = 'outbox-2026110%dT0%d1910-%s.zip' % (i + 1, i + 1, tag)
            make_zip(os.path.join(self.root, name), {'runtime/py.bin': blob(1), 'report-%s.md' % tag: blob(10 + i, 2000)})
            want[name] = TD.sha(read(os.path.join(self.root, name)))
        want['log.txt'] = self.put('log.txt', b'plain log\r\nwith CRLF\r\n')
        return want

    def test_duplicates_across_files_are_stored_once_and_restore_matches(self):
        want = self.reports()
        before = sum(os.path.getsize(os.path.join(self.root, n)) for n in want)
        self.assertEqual(self.run_archive(), 3)
        self.assertEqual(self.visible(), [OUT])
        self.assertEqual(self.restored(), want)
        self.assertLess(os.path.getsize(self.target()), before - 30000)
        with Rollup(self.target()) as r:
            self.assertEqual((r.top, r.deduped, r.check()), ('flat', True, ''))
        self.assertTrue(any(s.startswith('space:') and ',' in s for s in self.said), self.said)

    def test_nested_zips_are_walked(self):
        inner = os.path.join(self.tmp, 'inner.zip')
        make_zip(inner, {'deep/x.bin': blob(2)})
        make_zip(os.path.join(self.root, 'kit.zip'), {'rt/py.bin': blob(1), 'inner.zip': read(inner)})
        make_zip(os.path.join(self.root, 'kit2.zip'), {'rt/py.bin': blob(1), 'other.txt': blob(3, 900)})
        want = {n: TD.sha(read(os.path.join(self.root, n))) for n in ('kit.zip', 'kit2.zip')}
        self.run_archive()
        with Rollup(self.target()) as r:
            assert r.manifest is not None
            kinds = {e['path']: e['kind'] for e in r.manifest['files'].values()}
        self.assertEqual(kinds, {'kit.zip': 'zip-split', 'kit2.zip': 'zip-split'})
        self.assertEqual(self.restored(), want)
        with zipfile.ZipFile(os.path.join(self.out, 'kit.zip')) as z:
            self.assertEqual(z.read('inner.zip'), read(inner))

    def test_no_duplicates_still_an_archive_and_restores(self):
        want = {}
        for i in range(3):
            want['f%d.zip' % i] = self.put('f%d.zip' % i, blob(100 + i, 3000))
        self.run_archive()
        with Rollup(self.target()) as r:
            self.assertTrue(r.deduped)
        self.assertEqual(self.restored(), want)

    def test_new_file_joins_the_same_archive(self):
        want = self.reports()
        self.run_archive()
        first = read(self.target())
        want['later.zip'] = self.put('later.zip', blob(7, 1500))
        self.assertEqual(self.run_archive(), 1)
        self.assertEqual(self.restored(), want)
        self.assertEqual(self.visible(), [OUT])
        self.assertIn('.%s.archive-old' % OUT, self.trashed())
        self.assertEqual(read(os.path.join(self.trash_dir, '.%s.archive-old' % OUT)), first)
        # the same archive built from scratch is byte for byte the same
        scratch = os.path.join(self.tmp, 'scratch')
        os.makedirs(scratch)
        for _ in self.reports_again(scratch).items():
            pass
        write(os.path.join(scratch, 'later.zip'), blob(7, 1500))
        archive(scratch, OUT, trash=core.move_trash(os.path.join(self.tmp, 't2')), say=QUIET)
        self.assertEqual(read(os.path.join(scratch, OUT)), read(self.target()))

    def reports_again(self, dest: str) -> dict[str, str]:
        for i, tag in enumerate(('a', 'b')):
            name = 'outbox-2026110%dT0%d1910-%s.zip' % (i + 1, i + 1, tag)
            make_zip(os.path.join(dest, name), {'runtime/py.bin': blob(1), 'report-%s.md' % tag: blob(10 + i, 2000)})
        write(os.path.join(dest, 'log.txt'), b'plain log\r\nwith CRLF\r\n')
        return {}

    def test_repeat_run_says_nothing_to_do(self):
        self.reports()
        self.run_archive()
        data = read(self.target())
        self.assertEqual(self.run_archive(), 0)
        self.assertEqual(self.said, ['nothing to do'])
        self.assertEqual(read(self.target()), data)

    def test_leftover_source_of_an_interrupted_run_is_proved_then_trashed(self):
        want = self.reports()
        self.run_archive()
        write(os.path.join(self.root, 'log.txt'), b'plain log\r\nwith CRLF\r\n')   # trash step never happened
        self.assertEqual(self.run_archive(), 0)
        self.assertEqual(self.visible(), [OUT])
        self.assertEqual(self.restored(), want)

    def test_index_md_stays_readable_in_place(self):
        want = self.reports()
        want['INDEX.md'] = self.put('INDEX.md', b'# index\n')
        self.run_archive()
        self.assertEqual(self.visible(), ['INDEX.md', OUT])
        self.assertEqual(read(os.path.join(self.root, 'INDEX.md')), b'# index\n')
        self.assertEqual(self.restored(), want)
        self.assertEqual(self.run_archive(), 0)
        # a changed INDEX.md is refreshed in the archive and still stays in place
        want['INDEX.md'] = self.put('INDEX.md', b'# index v2\n')
        self.assertEqual(self.run_archive(), 1)
        self.assertEqual(self.restored(), want)
        self.assertEqual(self.visible(), ['INDEX.md', OUT])

    def test_same_name_other_bytes_is_refused_not_overwritten(self):
        self.put('r.zip', b'one' * 400)
        self.run_archive()
        self.put('r.zip', b'two' * 400)
        before = read(self.target())
        with self.assertRaisesRegex(Fail, 'differs from the copy'):
            self.run_archive()
        self.assertEqual(read(self.target()), before)
        self.assertEqual(read(os.path.join(self.root, 'r.zip')), b'two' * 400)

    def test_dry_run_changes_nothing(self):
        self.reports()
        names = self.visible()
        self.run_archive(dry=True)
        self.assertEqual(self.visible(), names)
        self.assertEqual(self.trashed(), [])
        self.assertTrue(self.said[0].startswith('would write'), self.said)

    def test_hidden_files_folders_and_symlinks_are_left_alone(self):
        self.put('a.bin', b'x' * 900)
        self.put('.hidden.tmp', b'y')
        os.makedirs(os.path.join(self.root, 'sub'))
        self.put('sub/inner.txt', b'z')
        os.symlink('a.bin', os.path.join(self.root, 'ln'))
        self.run_archive()
        self.assertEqual(self.visible(), [OUT, 'ln', 'sub'])
        self.assertEqual(self.hidden(), ['.hidden.tmp'])
        self.assertEqual(list(self.restored()), ['a.bin'])


class Safety(Base):
    def test_interrupted_write_leaves_sources_and_no_archive(self):
        self.put('a.bin', b'a' * 900)
        self.put('b.bin', b'b' * 900)
        with mock.patch.object(container, 'sha_stream', side_effect=OSError('disk full')):
            with self.assertRaises((Fail, OSError)):
                self.run_archive()
        self.assertEqual(self.visible(), ['a.bin', 'b.bin'])
        self.assertEqual(self.hidden(), [])
        self.assertEqual(self.trashed(), [])
        self.run_archive()
        self.assertEqual(self.visible(), [OUT])

    def test_write_failing_after_an_earlier_archive_keeps_the_old_one(self):
        self.put('a.bin', b'a' * 900)
        self.run_archive()
        data = read(self.target())
        self.put('b.bin', b'b' * 900)
        with mock.patch.object(core, 'verify', return_value='boom'):
            with self.assertRaisesRegex(Fail, 'failed the check'):
                self.run_archive()
        self.assertEqual(read(self.target()), data)
        self.assertEqual(self.visible(), sorted([OUT, 'b.bin']))
        self.assertEqual(self.hidden(), [])

    def test_corrupt_source_zip_is_refused_and_untouched(self):
        make_zip(os.path.join(self.root, 'good.zip'), {'x': blob(1)})
        p = os.path.join(self.root, 'bad.zip')
        make_zip(p, {'x': blob(1), 'y': blob(2)})
        data = bytearray(read(p))
        data[300] ^= 0xFF
        write(p, bytes(data))
        before = read(p)
        # a damaged zip is not walked: its bytes are stored as they are and come back identical
        self.run_archive()
        self.assertEqual(self.restored()['bad.zip'], TD.sha(before))

    def test_source_that_does_not_match_after_writing_is_refused_and_untouched(self):
        make_zip(os.path.join(self.root, 'good.zip'), {'x': blob(1)})
        self.put('b.bin', b'b' * 900)
        before = {n: read(os.path.join(self.root, n)) for n in self.visible()}
        real = core.sha_of
        calls: list[Src] = []

        def lying(src: Src) -> str:
            calls.append(src)
            return 'f' * 64 if len(calls) > 1 else real(src)
        with mock.patch.object(core, 'sha_of', lying):
            with self.assertRaisesRegex(Fail, 'failed the check'):
                self.run_archive()
        self.assertEqual({n: read(os.path.join(self.root, n)) for n in self.visible()}, before)
        self.assertEqual((self.hidden(), self.trashed()), ([], []))

    def test_corrupt_existing_archive_is_refused_and_nothing_moves(self):
        self.put('a.bin', b'a' * 900)
        self.run_archive()
        data = bytearray(read(self.target()))
        z = zipfile.ZipFile(self.target())
        info = [i for i in z.infolist() if i.filename.startswith('.rollup/blobs/')][0]
        z.close()
        # flip the first byte of the stored data: past the 30 byte local header, its name and its extra field
        name_len, extra_len = struct.unpack('<HH', data[info.header_offset + 26:info.header_offset + 30])
        data[info.header_offset + 30 + name_len + extra_len] ^= 0xFF
        write(self.target(), bytes(data))
        self.put('b.bin', b'b' * 900)
        with self.assertRaises(Fail):
            self.run_archive()
        self.assertEqual(read(self.target()), bytes(data))
        self.assertEqual(self.visible(), sorted([OUT, 'b.bin']))

    def test_a_zip_that_is_no_archive_is_never_replaced(self):
        make_zip(self.target(), {'x': b'1'})
        self.put('a.bin', b'a' * 900)
        before = read(self.target())
        with self.assertRaisesRegex(Fail, 'not an archive'):
            self.run_archive()
        self.assertEqual(read(self.target()), before)
        self.assertEqual(self.visible(), ['a.bin', OUT])

    def test_linux_refuses_hostile_names_on_disk(self):
        # Linux only: these names exist on a Linux disk. Windows cannot hold them (backslash, drive colon,
        # control characters); there they only come inside an archive, see the next test.
        for bad in ('a\\b.zip', 'new\nline.zip', 'c:evil.zip', 'ctl\x01.zip', 'tab\t.zip'):
            with self.subTest(bad):
                d = os.path.join(self.tmp, 'h%d' % len(os.listdir(self.tmp)))
                os.makedirs(d)
                write(os.path.join(d, 'ok.bin'), b'1' * 900)
                write(os.path.join(d, bad), b'2')
                with self.assertRaisesRegex(Fail, 'cannot archive'):
                    archive(d, OUT, trash=core.move_trash(self.trash_dir), say=QUIET)
                self.assertEqual(sorted(os.listdir(d)), sorted(['ok.bin', bad]))

    def archive_holding(self, name: str, data: bytes = b'q' * 900) -> bytes:
        """An archive in self.root whose manifest names a file `name`, as an archive made elsewhere could."""
        self.put('x.bin', data)
        self.run_archive()

        def edit(m: TD.RawManifest) -> None:
            for e in m['files']:
                if e['path'] == 'x.bin':
                    e['path'] = name
        TD.rewrite(self.target(), manifest=edit)
        return read(self.target())

    def test_hostile_names_inside_an_archive_are_refused_on_every_host(self):
        for bad in ('a\\b.zip', 'new\nline.zip', 'c:evil.zip', 'ctl\x01.zip', 'tab\t.zip', '../up.zip', 'dir/x.zip'):
            with self.subTest(bad):
                self.setUp()
                before = self.archive_holding(bad)
                self.put('new.bin', b'n' * 900)
                with self.assertRaises(Fail):
                    self.run_archive()
                self.assertEqual(read(self.target()), before)
                self.assertEqual(self.visible(), sorted([OUT, 'new.bin']))
                self.assertLessEqual(set(os.listdir(self.tmp)), {'outbox', 'trash'})

    def test_name_rules_for_the_flat_top(self):
        for good in ('INDEX.md', 'outbox-20261105T051910-fixes.zip', 'kit-0.1.0-beta.4.zip'):
            self.assertEqual(path_problem(good, 'flat'), '', good)
        for bad in ('', '.', '..', 'a/b', '/abs', 'a/../b', 'C:x', 'a:b', 'a\\b', 'x\x00y', 'x\ud800'):
            self.assertNotEqual(path_problem(bad, 'flat'), '', repr(bad))
        # the default rule is unchanged
        self.assertNotEqual(path_problem('INDEX.md'), '')
        self.assertEqual(path_problem('v0.1.0/a.txt'), '')

    def test_linux_archives_reserved_names_on_disk_with_a_warning(self):
        # Linux only: these names exist on a Linux disk; on Windows they come inside an archive (next test).
        for bad in ('CON', 'aux.txt', 'COM1.zip', 'trail.'):
            with self.subTest(bad):
                d = os.path.join(self.tmp, 'w_' + bad.replace('.', '_'))
                os.makedirs(d)
                write(os.path.join(d, bad), b'q' * 900)
                said: list[str] = []
                archive(d, OUT, trash=core.move_trash(self.trash_dir), say=said.append)
                self.assertTrue(any(s.startswith('warning: 1 name Windows cannot hold') for s in said), said)
                self.assertEqual(os.listdir(d), [OUT])
                restore(os.path.join(d, OUT), out=os.path.join(d + '-out'), say=QUIET)
                self.assertEqual(os.listdir(d + '-out'), [bad])

    def test_reserved_names_in_an_archive_are_refused_on_windows_and_warned_elsewhere(self):
        for bad in ('CON', 'aux.txt', 'COM1.zip', 'trail.', 'trail ', 'NUL', 'lpt1'):
            with self.subTest(bad):
                self.setUp()
                before = self.archive_holding(bad)
                self.put('new.bin', b'n' * 900)
                if hostcat.this_host() == 'windows':
                    with self.assertRaisesRegex(Fail, 'Windows cannot hold'):
                        self.run_archive()
                    self.assertEqual(read(self.target()), before)
                    self.assertEqual(self.visible(), sorted([OUT, 'new.bin']))
                    with self.assertRaisesRegex(Fail, 'unsafe name'):
                        restore(self.target(), out=self.out, say=QUIET)
                    self.assertFalse(os.path.exists(self.out) and os.listdir(self.out))
                else:
                    self.run_archive()
                    self.assertTrue(any(s.startswith('warning: 1 name Windows cannot hold') for s in self.said), self.said)
                    self.assertEqual(self.visible(), [OUT])
                    self.assertEqual(sorted(self.restored()), sorted([bad, 'new.bin']))

    def test_linux_archives_names_that_differ_only_in_case(self):
        # Linux only: A.bin and a.bin are two files on a Linux disk, one file on NTFS.
        self.put('A.bin', b'1' * 900)
        self.put('a.bin', b'2' * 900)
        self.run_archive()
        self.assertEqual(self.visible(), [OUT])
        self.assertEqual(sorted(self.restored()), ['A.bin', 'a.bin'])

    def test_case_clash_with_an_archived_name_per_host(self):
        before = self.archive_holding('A.bin', b'1' * 900)
        self.put('a.bin', b'2' * 900)
        if hostcat.this_host() == 'windows':
            with self.assertRaisesRegex(Fail, 'twice'):
                self.run_archive()
            self.assertEqual(read(self.target()), before)
            self.assertEqual(self.visible(), sorted([OUT, 'a.bin']))
        else:
            self.run_archive()
            self.assertEqual(self.visible(), [OUT])
            self.assertEqual(sorted(self.restored()), ['A.bin', 'a.bin'])

    def test_out_must_be_a_zip_name(self):
        with self.assertRaises(Fail):
            archive(self.root, 'x.txt', say=QUIET)
        with self.assertRaises(Fail):
            archive(self.root, '.hid.zip', say=QUIET)

    def test_forged_manifest_names_are_refused_on_read(self):
        self.put('a.bin', b'a' * 900)
        self.run_archive()
        with zipfile.ZipFile(self.target()) as z:
            m = json.loads(z.read(MANIFEST))
        for bad in ('../x', 'a/b', 'C:x', '/abs', 'v0.1.0/x'):
            e = json.loads(json.dumps(m))
            e['files'][0]['path'] = bad
            with self.assertRaises(Fail, msg=bad):
                parse_manifest(json.dumps(e).encode())
        e = json.loads(json.dumps(m))
        e['top'] = 'anything'
        with self.assertRaisesRegex(Fail, 'unknown top'):
            parse_manifest(json.dumps(e).encode())
        e = json.loads(json.dumps(m))
        e['format'] = 1
        with self.assertRaisesRegex(Fail, 'needs format 2'):
            parse_manifest(json.dumps(e).encode())

    def test_restore_check_and_unzip_readme(self):
        self.put('a.bin', b'a' * 900)
        self.run_archive()
        said: list[str] = []
        restore(self.target(), check_only=True, say=said.append)
        self.assertRegex(said[0], 'every file matches')
        with zipfile.ZipFile(self.target()) as z:
            text = z.read(README).decode()
        self.assertIn('release-rollup restore', text)
        self.assertIn('format 2', text)
        # the by-hand recipe in the README restores an archive too
        d = os.path.join(self.tmp, 'byhand')
        with zipfile.ZipFile(self.target()) as z:
            z.extractall(d)
        script = text.split('# --- restore script ---\n')[1].split('# --- end ---')[0]
        # Run it the way README.txt says: a plain Python process in the unzipped folder. Every file it
        # opens has to be closed again, so a ResourceWarning on stderr fails the test.
        r = subprocess.run([sys.executable, '-I', '-W', 'always::ResourceWarning', '-c', script],
                           cwd=d, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('ResourceWarning', r.stderr)
        self.assertEqual(read(os.path.join(d, 'restored', 'a.bin')), b'a' * 900)


class Compatible(Base):
    def test_old_rollups_read_as_before(self):
        rel = os.path.join(self.tmp, 'rel')
        os.makedirs(rel)
        for v in ('0.1.0', '0.1.1'):
            TD.release(rel, v, TD.app(v))
        TD.release(rel, '0.2.0', TD.app('0.2.0'))
        core.apply(rel, trash=core.move_trash(self.trash_dir), say=QUIET)
        z = os.path.join(rel, '0.1.zip')
        with zipfile.ZipFile(z) as zf:
            m = json.loads(zf.read(MANIFEST))
        self.assertEqual(m['format'], 1)
        self.assertNotIn('top', m)
        with Rollup(z) as r:
            self.assertEqual((r.top, r.check()), ('release', ''))

    def test_archive_names_are_not_taken_for_releases(self):
        # the release scan only looks at v<version>/ folders and v<version>.zip style names
        self.put('a.bin', b'a' * 900)
        self.run_archive()
        for nm in ('archive.zip', 'outbox-20261105T051910-x.zip', 'kit-0.1.0-beta.4.zip'):
            self.assertIsNone(core.ZIP_RE.match(nm), nm)
            self.assertIsNone(core.PRE_ZIP_RE.match(nm), nm)
        # a releases scan and an upgrade over a folder holding an archive leave it alone
        data = read(self.target())
        core.upgrade(self.root, trash=core.move_trash(self.trash_dir), say=QUIET)
        self.assertEqual(read(self.target()), data)
        self.assertEqual(core.scan(self.root)[0], {})

    def test_cli(self):
        self.put('a.bin', b'a' * 900)
        self.assertEqual(cli.main(['archive', '--root', self.root, '--out', OUT, '--dry-run']), 0)
        self.assertEqual(self.visible(), ['a.bin'])
        self.assertEqual(cli.main(['archive', '--root', self.root, '--out', OUT, '--trash-dir', self.trash_dir]), 0)
        self.assertEqual(self.visible(), [OUT])
        self.assertEqual(cli.main(['restore', self.target(), '--check']), 0)
        self.assertEqual(cli.main(['restore', self.target(), '--out', self.out]), 0)
        self.assertEqual(cli.main(['archive', '--root', self.root, '--out', 'x.txt']), 1)


load_tests = hostcat.load_tests


if __name__ == '__main__':
    unittest.main()
