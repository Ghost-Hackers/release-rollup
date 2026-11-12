"""The upgrade command: plain rollup zips become deduped ones, or stay exactly as they are."""
import os
import re
import shutil
import struct
import subprocess
import sys
import unittest
import zipfile
import zlib
from collections.abc import Buffer

import hostcat
import smallfs
import test_dedupe as TD
from release_rollup import cli, core
from release_rollup.container import Rollup, restore
from release_rollup.core import Fail, upgrade

QUIET = TD.QUIET


def read(path: str) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


def write(path: str, data: Buffer) -> None:
    with open(path, 'wb') as f:
        f.write(data)


def cut_short(path: str, name: str) -> None:
    """Add an entry called name that reads past the end of the zip, so reading it raises a bare EOFError.

    It reuses the local header of the last member in the central directory and claims more stored bytes than the
    file holds. Sharing a header offset is the one layout Python's zip reader lets through with only an
    "Overlapped entries" warning; any other entry that runs past the next header is refused with BadZipFile.
    The new entry keeps the raw name of the header it borrows and gets its own name from a Unicode path extra
    field (0x7075), so the zip still opens. It goes just before the borrowed entry: that one ends at the central
    directory, the new one ends at the shared header and only warns. A name that is already in the zip now reads
    the cut-short entry, as it is listed after the original.
    """
    data = read(path)
    eocd = data.rindex(b'PK\x05\x06')
    count, _, cd_off = struct.unpack('<HII', data[eocd + 10:eocd + 20])
    entries: list[bytes] = []
    pos = cd_off
    for _ in range(count):
        nlen, elen, clen = struct.unpack('<HHH', data[pos + 28:pos + 34])
        entries.append(data[pos:pos + 46 + nlen + elen + clen])
        pos += 46 + nlen + elen + clen
    last = entries[-1]
    nlen = struct.unpack('<H', last[28:30])[0]
    raw = last[46:46 + nlen]
    new = name.encode('utf-8')
    extra = struct.pack('<HHBI', 0x7075, 5 + len(new), 1, zlib.crc32(raw)) + new
    entry = bytearray(last[:46 + nlen])
    struct.pack_into('<HHH', entry, 8, struct.unpack('<H', last[8:10])[0] & ~0x8, zipfile.ZIP_STORED, 0)
    struct.pack_into('<II', entry, 20, len(data), len(data))
    struct.pack_into('<HHH', entry, 30, len(extra), 0, 0)
    entry += extra
    cd = b''.join(entries[:-1]) + bytes(entry) + last
    end = bytearray(data[eocd:])
    struct.pack_into('<HHII', end, 8, count + 1, count + 1, len(cd), cd_off)
    write(path, data[:cd_off] + cd + bytes(end))


class Upgrade(TD.Base):
    def plain(self, name: str = '0.1.zip') -> str:
        """A plain rollup of three releases that share a big file."""
        return self.rollup(zip_name=name, dedupe=False)

    def up(self, dry: bool = False) -> int:
        self.said = []
        return upgrade(self.root, dry=dry, trash=core.move_trash(os.path.join(self.tmp, 'old-zips')), say=self.say)

    def trashed(self) -> list[str]:
        d = os.path.join(self.tmp, 'old-zips')
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def hidden(self) -> list[str]:
        return [n for n in os.listdir(self.root) if n.startswith('.')]

    def test_plain_becomes_deduped_and_every_file_is_proved(self):
        z = self.plain()
        old = TD.sha_file(z)
        with zipfile.ZipFile(z) as p:
            want = {i.filename: TD.sha(p.read(i.filename)) for i in p.infolist() if not i.is_dir()}
        size = os.path.getsize(z)
        self.assertFalse(TD.is_deduped(z))
        self.assertEqual(self.up(), 1)
        self.assertTrue(TD.is_deduped(z))
        self.assertLess(os.path.getsize(z), size)
        with Rollup(z) as r:
            self.assertEqual({nm: TD.sha(r.open(nm).read()) for nm in r.files()}, want)
            self.assertEqual(r.check(), '')
        self.assertTrue(any(s.startswith('space:') for s in self.said), self.said)
        # the old zip went to the trash, byte for byte, and nothing else is left behind
        self.assertEqual(self.trashed(), ['.0.1.zip.upgrade-old'])
        self.assertEqual(TD.sha_file(os.path.join(self.tmp, 'old-zips', '.0.1.zip.upgrade-old')), old)
        self.assertEqual(self.hidden(), [])
        restore(z, out=self.out, say=QUIET)
        self.assertEqual(TD.snapshot(self.out)['v0.1.1/assets/app.zip'], TD.sha(TD.app('0.1.1')))

    def test_a_repeat_run_does_nothing(self):
        z = self.plain()
        self.up()
        before = TD.snapshot(self.tmp)
        self.assertEqual(self.up(), 0)
        self.assertEqual(self.said, ['nothing to do'])
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertTrue(TD.is_deduped(z))

    def test_an_already_deduped_zip_is_left_alone(self):
        z = self.rollup()
        self.assertTrue(TD.is_deduped(z))
        before = TD.snapshot(self.tmp)
        self.assertEqual(self.up(), 0)
        self.assertEqual(TD.snapshot(self.tmp), before)

    def test_corrupt_source_is_refused_and_untouched(self):
        z = self.plain()
        data = bytearray(read(z))
        with zipfile.ZipFile(z) as p:
            info = p.getinfo('v0.1.0/assets/app.zip')
        data[info.header_offset + 30 + len(info.filename) + 20000] ^= 0xFF
        write(z, data)
        before = TD.snapshot(self.tmp)
        with self.assertRaisesRegex(Fail, '0.1.zip'):
            self.up()
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertEqual(self.trashed(), [])
        self.assertTrue(any(s.startswith('refused 0.1.zip') for s in self.said), self.said)

    def test_a_refused_zip_does_not_stop_the_others(self):
        good = self.plain()
        bad = os.path.join(self.root, '0.0.zip')
        with zipfile.ZipFile(bad, 'w') as w:
            w.writestr('v0.0.1/a', b'x')
        data = bytearray(read(bad))
        data[40] ^= 0xFF
        write(bad, data)
        keep = TD.sha_file(bad)
        with self.assertRaises(Fail):
            self.up()
        self.assertEqual(TD.sha_file(bad), keep)
        self.assertTrue(TD.is_deduped(good))

    def test_no_shared_content_keeps_the_plain_zip(self):
        # Fixed seeds that differ: the two releases have to share no content for this test to hold.
        blobs = {'0.1.0': TD.noise(5000, 101), '0.1.1': TD.noise(5000, 202)}
        self.assertNotEqual(blobs['0.1.0'], blobs['0.1.1'])
        for v, blob in blobs.items():
            TD.release(self.root, v, TD.inner_zip({'a.bin': blob}))
        TD.release(self.root, '0.2.0', TD.app('0.2.0'))
        self.run_apply(dedupe=False)
        z = os.path.join(self.root, '0.1.zip')
        before = TD.snapshot(self.tmp)
        self.assertEqual(self.up(), 0)
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertTrue(any('kept 0.1.zip' in s for s in self.said), self.said)
        self.assertFalse(TD.is_deduped(z))
        self.assertEqual(self.trashed(), [])

    def test_dry_run_writes_nothing(self):
        self.plain()
        before = TD.snapshot(self.tmp)
        self.assertEqual(self.up(dry=True), 1)
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertTrue(any(s.startswith('would upgrade 0.1.zip') for s in self.said), self.said)
        self.assertEqual(self.hidden(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'old-zips')))

    def test_prereleases_folder(self):
        pre = TD.Pre()
        pre.root, pre.trash, pre.tmp = self.root, self.trash, self.tmp
        for i in (1, 2, 3):
            TD.Pre.build(pre, 'beta', 'v1.0.0-beta.%d' % i, TD.app(str(i)))
        core.apply_pre(self.root, current='1.0.0-beta.3', trash=self.trash, say=QUIET, dedupe=False)
        z = os.path.join(self.root, 'beta', 'v1.0.0-beta.x.zip')
        self.assertFalse(TD.is_deduped(z))
        self.assertEqual(self.up(), 1)
        self.assertTrue(TD.is_deduped(z))
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'beta', 'v1.0.0-beta.3')))   # the unpacked newest build is not touched
        self.assertEqual(self.up(), 0)

    def test_a_zip_that_is_not_a_rollup_is_skipped(self):
        p = os.path.join(self.root, 'notes.zip')
        with zipfile.ZipFile(p, 'w') as w:
            w.writestr('readme.txt', b'hello')
        before = TD.snapshot(self.tmp)
        self.assertEqual(self.up(), 0)
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertTrue(any(s.startswith('skipped notes.zip') for s in self.said), self.said)

    def test_a_failure_while_writing_leaves_the_old_zip(self):
        # The disk runs full while the deduped copy is written.
        self.use_small_fs()
        z = self.plain()
        before = TD.snapshot(self.tmp)
        ballast = smallfs.leave_free(self, self.root, 16 << 10)
        with self.assertRaises(Fail):
            self.up()
        self.assertTrue(any(re.search(smallfs.FULL, s) for s in self.said), self.said)
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertEqual(self.hidden(), [])
        os.remove(ballast)
        self.assertEqual(self.up(), 1)
        self.assertTrue(TD.is_deduped(z))

    def test_a_kill_while_writing_leaves_the_old_zip_and_a_rerun_finishes(self):
        z = self.plain()
        before = TD.sha_file(z)
        kill, killed = TD.kill_plan()
        code = '''
import sys
sys.path.insert(0, %r)
import os, signal
from release_rollup import container, core
real, count = container._copy_range, []
def die(*a):
    count.append(1)
    if len(count) > 4:
        %s
    return real(*a)
container._copy_range = die
core.upgrade(%r, trash=core.move_trash(%r), say=lambda *_: None)
''' % (os.path.join(TD.HERE, '..', 'src'), kill, self.root, os.path.join(self.tmp, 'old-zips'))
        r = subprocess.run([sys.executable, '-c', code])
        self.assertEqual(r.returncode, killed)
        self.assertEqual(TD.sha_file(z), before)
        self.assertTrue(self.hidden(), 'the kill should have left a temp file')
        self.assertEqual(self.up(), 1)
        self.assertTrue(TD.is_deduped(z))

    def test_a_failed_trash_after_the_move_is_finished_by_a_rerun(self):
        z = self.plain()
        old = TD.sha_file(z)

        def refuse(p: str) -> None:
            raise Fail('trash is full')
        with self.assertRaisesRegex(Fail, 'upgraded, but its old copy'):
            upgrade(self.root, trash=refuse, say=QUIET)
        self.assertTrue(TD.is_deduped(z))
        left = os.path.join(self.root, '.0.1.zip.upgrade-old')
        self.assertEqual(TD.sha_file(left), old)
        self.assertEqual(self.up(), 0)
        self.assertFalse(os.path.exists(left))
        self.assertEqual(self.trashed(), ['.0.1.zip.upgrade-old'])
        self.assertTrue(TD.is_deduped(z))

    def test_a_leftover_that_does_not_match_is_not_trashed(self):
        self.plain()
        self.up()
        left = os.path.join(self.root, '.0.1.zip.upgrade-old')
        with zipfile.ZipFile(left, 'w') as w:
            w.writestr('v0.1.0/other', b'something else')
        self.up()
        self.assertTrue(os.path.exists(left))
        self.assertTrue(any('left .0.1.zip.upgrade-old alone' in s for s in self.said), self.said)

    def test_a_member_cut_short_is_refused_with_a_reason_and_untouched(self):
        # A bare EOFError has no text: the refusal must still say why, before anything is written.
        z = self.plain()
        cut_short(z, 'v0.1.0/assets/short.bin')
        with zipfile.ZipFile(z) as p, self.assertWarnsRegex(UserWarning, 'Overlapped entries'):
            with self.assertRaises(EOFError) as raised:
                p.read('v0.1.0/assets/short.bin')
        self.assertEqual(str(raised.exception), '')
        before = TD.snapshot(self.tmp)
        with self.assertWarnsRegex(UserWarning, 'Overlapped entries'), self.assertRaisesRegex(Fail, '0.1.zip'):
            self.up()
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertEqual(self.trashed(), [])
        self.assertTrue(any(s.startswith('refused 0.1.zip: EOFError') for s in self.said), self.said)

    def test_a_leftover_with_a_member_cut_short_is_not_trashed(self):
        # The old copy reads fine up to one member that ends early: it does not prove the new zip, so it stays.
        z = self.plain()
        left = os.path.join(self.root, '.0.1.zip.upgrade-old')
        with zipfile.ZipFile(z) as p:
            first = p.infolist()[0].filename
        self.up()
        shutil.copy2(os.path.join(self.tmp, 'old-zips', '.0.1.zip.upgrade-old'), left)
        cut_short(left, first)
        keep = TD.sha_file(left)
        with self.assertWarnsRegex(UserWarning, 'Overlapped entries'):
            self.up()
        self.assertTrue(os.path.exists(left), 'the damaged old copy was trashed')
        self.assertEqual(TD.sha_file(left), keep)
        self.assertTrue(any(s.startswith('left .0.1.zip.upgrade-old alone') for s in self.said), self.said)

    def test_a_leftover_that_fails_its_check_says_so_with_the_reason(self):
        z = self.plain()
        left = os.path.join(self.root, '.0.1.zip.upgrade-old')
        with zipfile.ZipFile(z) as p:
            first = p.infolist()[0].filename
        self.up()
        shutil.copy2(os.path.join(self.tmp, 'old-zips', '.0.1.zip.upgrade-old'), left)
        cut_short(left, first)
        keep = read(left)
        with self.assertWarnsRegex(UserWarning, 'Overlapped entries'):
            self.up()
        lines = [s for s in self.said if s.startswith('left .0.1.zip.upgrade-old alone')]
        self.assertEqual(len(lines), 1, self.said)
        self.assertIn('the old copy failed its check: ', lines[0])
        self.assertTrue(lines[0].split('failed its check: ', 1)[1].strip(), lines[0])
        self.assertNotIn('is not a plain rollup', lines[0])
        self.assertEqual(read(left), keep)

    def test_a_leftover_that_is_not_a_plain_rollup_keeps_its_wording(self):
        self.plain()
        self.up()
        left = os.path.join(self.root, '.0.1.zip.upgrade-old')
        shutil.copy2(os.path.join(self.root, '0.1.zip'), left)
        self.assertTrue(TD.is_deduped(left))
        keep = read(left)
        self.up()
        self.assertTrue(any(s == 'left .0.1.zip.upgrade-old alone: the old copy is not a plain rollup' for s in self.said), self.said)
        self.assertEqual(read(left), keep)

    def test_cli(self):
        self.plain()
        trash = os.path.join(self.tmp, 'cli-trash')
        self.assertEqual(cli.main(['upgrade', '--root', self.root, '--dry-run']), 0)
        self.assertEqual(cli.main(['upgrade', '--root', self.root, '--trash-dir', trash]), 0)
        self.assertEqual(os.listdir(trash), ['.0.1.zip.upgrade-old'])
        self.assertEqual(cli.main(['upgrade', '--root', os.path.join(self.tmp, 'nope')]), 2)
        bad = os.path.join(self.root, '0.0.zip')
        with zipfile.ZipFile(bad, 'w') as w:
            w.writestr('v0.0.1/a', b'x' * 50)
        data = bytearray(read(bad))
        data[40] ^= 0xFF
        write(bad, data)
        self.assertEqual(cli.main(['upgrade', '--root', self.root, '--trash-dir', trash]), 1)


load_tests = hostcat.load_tests


if __name__ == '__main__':
    unittest.main()
