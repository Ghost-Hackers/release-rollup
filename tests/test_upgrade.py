"""The upgrade command: plain rollup zips become deduped ones, or stay exactly as they are."""
import os
import signal
import subprocess
import sys
import unittest
import zipfile
from unittest import mock

import hostcat
import test_dedupe as TD
from release_rollup import cli, container, core
from release_rollup.container import Rollup, restore
from release_rollup.core import Fail, upgrade

QUIET = TD.QUIET


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def write(path, data):
    with open(path, 'wb') as f:
        f.write(data)


class Upgrade(TD.Base):
    def plain(self, name='0.1.zip'):
        """A plain rollup of three releases that share a big file."""
        return self.rollup(zip_name=name, dedupe=False)

    def up(self, **kw):
        self.said = []
        return upgrade(self.root, trash=core.move_trash(os.path.join(self.tmp, 'old-zips')), say=self.say, **kw)

    def trashed(self):
        d = os.path.join(self.tmp, 'old-zips')
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def hidden(self):
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
        for v in ('0.1.0', '0.1.1'):
            TD.release(self.root, v, TD.inner_zip({'a.bin': TD.noise(5000, hash(v) % 1000)}))
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
        z = self.plain()
        before = TD.snapshot(self.tmp)
        with mock.patch.object(container, '_copy_range', side_effect=OSError(28, 'No space left')):
            with self.assertRaises(Fail):
                self.up()
        self.assertEqual(TD.snapshot(self.tmp), before)
        self.assertEqual(self.hidden(), [])
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

        def refuse(p):
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
        z = self.plain()
        self.up()
        left = os.path.join(self.root, '.0.1.zip.upgrade-old')
        with zipfile.ZipFile(left, 'w') as w:
            w.writestr('v0.1.0/other', b'something else')
        self.up()
        self.assertTrue(os.path.exists(left))
        self.assertTrue(any('left .0.1.zip.upgrade-old alone' in s for s in self.said), self.said)

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
