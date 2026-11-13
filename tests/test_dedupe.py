"""Deduped rollups: round trips, safety promises, hostile input and failure at every step."""
import ast
import contextlib
import hashlib
import io
import json
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from collections.abc import Buffer, Callable, Container, Iterable, Mapping
from typing import NotRequired, TypedDict, Unpack, cast
from unittest import mock

import smallfs

import hostcat

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'src')
sys.path.insert(0, SRC)

from release_rollup import __version__, cli, container, core  # noqa: E402
from release_rollup.container import (BLOB_DIR, MANIFEST, README, SKELETON_DIR, Rollup,  # noqa: E402
                                      long_path, restore, split_zip)
from release_rollup.core import Fail, Files, Trash, apply, apply_pre, move_trash, scan, verify  # noqa: E402


def QUIET(*_: object) -> None:
    return None


type Ref = list[int | str]  # [offset, length, blob] as the JSON holds it


class RawEntry(TypedDict):
    """A manifest file entry as the JSON holds it. Lists both kinds' fields; a test reads only its entry's kind."""
    path: str
    kind: str
    size: int
    sha256: str
    blob: str
    skeleton: str
    skeleton_size: int
    refs: list[Ref]


class RawManifest(TypedDict):
    """The parsed MANIFEST.json a forging test edits. format is loose so a test can break it."""
    format: NotRequired[object]
    tool: object
    top: NotRequired[str]
    files: list[RawEntry]


type ManifestEdit = Callable[[RawManifest], object]


class ApplyOpts(TypedDict, total=False):
    dry: bool
    markers: Iterable[str]
    keep_rc: bool
    zip_newest: bool
    dedupe: bool


class RestoreOpts(TypedDict, total=False):
    check_only: bool
    max_size: int


class RewriteOpts(TypedDict, total=False):
    change: Mapping[str, bytes]
    manifest: ManifestEdit
    drop: Container[str]
    add: Iterable[tuple[str, bytes]]


def noise(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).getrandbits(8 * size).to_bytes(size, 'little') if size else b''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kill_plan() -> tuple[str, int]:
    """(python line that kills the running process at once, return code subprocess reports) for this host.
    Linux: SIGKILL, reported as -9. Windows has no SIGKILL; there os.kill with any other signal number calls
    TerminateProcess and the number becomes the exit code (Python docs, os.kill), so SIGTERM is reported as 15."""
    if hostcat.this_host() == 'windows':
        return 'os.kill(os.getpid(), signal.SIGTERM)', signal.SIGTERM
    return 'os.kill(os.getpid(), signal.SIGKILL)', -signal.SIGKILL


def sha_file(path: str) -> str:
    with open(path, 'rb') as f:
        return sha(f.read())


def inner_zip(members: Mapping[str, bytes], method: int = zipfile.ZIP_DEFLATED, comment: bytes = b'', prefix: bytes = b'',
              trailing: bytes = b'', force_zip64: bool = False, streamed: bool = False) -> bytes:
    """Bytes of a zip with these members. streamed writes through a pipe-like file, so data descriptors get used."""
    raw = io.BytesIO()
    target: io.RawIOBase | io.BytesIO = raw
    if streamed:
        class NoSeek(io.RawIOBase):
            def writable(self) -> bool:
                return True

            def write(self, b: Buffer, /) -> int:
                return raw.write(b)

            def seekable(self) -> bool:
                return False
        target = NoSeek()
    with zipfile.ZipFile(target, 'w', method) as z:
        z.comment = comment
        for name, data in members.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = method
            with z.open(info, 'w', force_zip64=force_zip64) as w:
                w.write(data)
    return prefix + raw.getvalue() + trailing


def release(root: str, ver: str, app: bytes | None = None, extra: Mapping[str, bytes] | None = None) -> str:
    """A published release folder: the three marker files plus these assets."""
    d = os.path.join(root, 'v' + ver)
    os.makedirs(os.path.join(d, 'assets'))
    for name in ('RELEASE_NOTES.md', 'SHA256SUMS'):
        with open(os.path.join(d, name), 'w') as f:
            f.write('%s %s\n' % (name, ver))
    for name, data in dict(extra or {}, **({'assets/app.zip': app} if app is not None else {})).items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(data)
    return d


def app(ver: str, shared: bytes | None = None) -> bytes:
    """An inner zip with a big member every version shares and a small one of its own."""
    return inner_zip({'lib/runtime.bin': shared if shared is not None else noise(60000), 'version.txt': ver.encode() * 300})


def snapshot(root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root).replace(os.sep, '/')] = sha_file(p)
    return out


def rewrite(path: str, change: Mapping[str, bytes] | None = None, manifest: ManifestEdit | None = None,
            drop: Container[str] = (), add: Iterable[tuple[str, bytes]] | None = None) -> None:
    """Rewrite a rollup's entries: change maps an entry name to new bytes, manifest edits the parsed manifest."""
    with zipfile.ZipFile(path) as z:
        items = [(i.filename, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            if name in drop:
                continue
            if change and name in change:
                data = change[name]
            if name == MANIFEST and manifest:
                loaded: object = json.loads(data.decode())
                if not isinstance(loaded, dict):
                    raise AssertionError('%s is not a JSON object' % MANIFEST)
                m = cast(RawManifest, loaded)
                edited = manifest(m) or m
                data = edited if isinstance(edited, bytes) else json.dumps(edited).encode()
            z.writestr(name, data)
        for name, data in (add or []):
            z.writestr(name, data)


def is_deduped(path: str) -> bool:
    with Rollup(path) as r:
        return r.deduped


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix='rollup-dedupe-')
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name
        self.root = os.path.join(self.tmp, 'releases')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.root)
        self.trash: Trash = move_trash(os.path.join(self.tmp, 'trash'))
        self.said: list[str] = []

    def say(self, line: str) -> None:
        self.said.append(line)

    def use_small_fs(self) -> None:
        """Move the releases, output and trash folders onto the small filesystem (see smallfs.py)."""
        self.tmp = smallfs.folder(self)
        self.root = os.path.join(self.tmp, 'releases')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.root)
        self.trash = move_trash(os.path.join(self.tmp, 'trash'))

    def run_apply(self, **kw: Unpack[ApplyOpts]) -> int:
        return apply(self.root, trash=self.trash, say=self.say, **kw)

    def rollup(self, *vers: str, zip_name: str = '0.1.zip', **kw: Unpack[ApplyOpts]) -> str:
        """Three releases sharing content rolled into 0.1.zip, with 0.2.0 as the newest."""
        for v in vers or ('0.1.0', '0.1.1', '0.1.2'):
            release(self.root, v, app(v))
        release(self.root, '0.2.0', app('0.2.0'))
        self.run_apply(**kw)
        return os.path.join(self.root, zip_name)

    def assertRefused(self, path: str, pattern: str | None = None, **kw: Unpack[RestoreOpts]) -> str:
        """restore refuses this rollup and leaves no output at all."""
        with self.assertRaises(Fail) as cm:
            restore(path, out=self.out, say=QUIET, **kw)
        if pattern:
            self.assertRegex(str(cm.exception), pattern)
        self.assertFalse(os.path.exists(self.out) and os.listdir(self.out), 'restore wrote something')
        return str(cm.exception)


class RoundTrip(Base):
    def test_default_run_writes_a_deduped_rollup(self):
        wanted = {'v%s/assets/app.zip' % v: sha(app(v)) for v in ('0.1.0', '0.1.1', '0.1.2')}
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            self.assertEqual(names[0], README)
            self.assertIn(MANIFEST, names)
            self.assertEqual(zf.testzip(), None)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.0')))
        restore(z, out=self.out, say=QUIET)
        got = snapshot(self.out)
        self.assertEqual(len(got), 9)
        for arc, h in wanted.items():
            self.assertEqual(got[arc], h)

    def test_nothing_inside_is_named_by_a_build(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                self.assertRegex(name, r'^(README\.txt|\.rollup/MANIFEST\.json|\.rollup/(blobs|skeleton)/[0-9a-f]{64})$')

    def test_shared_content_is_stored_once(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            big = [i for i in zf.infolist() if i.file_size > 50000]
        self.assertEqual(len(big), 1)
        self.assertLess(os.path.getsize(z), 70000 + 9000)

    def test_writes_are_deterministic(self):
        z = self.rollup()
        first = sha_file(z)
        self.root = os.path.join(self.tmp, 'again')
        os.makedirs(self.root)
        self.assertEqual(sha_file(self.rollup()), first)

    def test_no_dedupe_gives_the_plain_zip_of_before(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v))
        files = core.member_files(core.plan(self.root)[0])
        expect = os.path.join(self.tmp, 'expect.zip')
        core.write_zip(expect, files)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['run', '--root', self.root, '--no-dedupe', '--trash-dir', os.path.join(self.tmp, 't')]), 0)
        z = os.path.join(self.root, '0.1.zip')
        self.assertEqual(sha_file(z), sha_file(expect))
        with zipfile.ZipFile(z) as zf:
            self.assertNotIn(MANIFEST, zf.namelist())

    def test_cli_default_dedupes_and_restores(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['run', '--root', self.root, '--trash-dir', os.path.join(self.tmp, 't')]), 0)
            z = os.path.join(self.root, '0.1.zip')
            self.assertEqual(cli.main(['restore', z, '--check']), 0)
            self.assertEqual(cli.main(['restore', z, '--out', self.out]), 0)
        self.assertRegex(out.getvalue(), r'deduped\)')
        self.assertRegex(out.getvalue(), r'space: before [\d,]+ bytes, after [\d,]+ bytes, saved [\d,]+ bytes \(\d+\.\d%\)')
        self.assertEqual(snapshot(self.out)['v0.1.1/assets/app.zip'], sha(app('0.1.1')))

    def test_cli_restore_failures_exit_1(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['restore', os.path.join(self.tmp, 'missing.zip'), '--check']), 1)
            z = self.rollup()
            self.assertEqual(cli.main(['restore', z]), 1)
        self.assertIn('--out', err.getvalue())

    def test_check_writes_nothing(self):
        z = self.rollup()
        before = snapshot(self.tmp)
        restore(z, check_only=True, say=QUIET)
        self.assertEqual(snapshot(self.tmp), before)

    def test_one_release_with_nothing_shared_stays_plain(self):
        release(self.root, '0.1.0', app('0.1.0'))
        release(self.root, '0.2.0', app('0.2.0', noise(100, 2)))
        self.run_apply()
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as zf:
            self.assertNotIn(MANIFEST, zf.namelist())
        self.assertTrue(any('shares too little' in s for s in self.said))

    def test_one_release_sharing_inside_itself_dedupes(self):
        release(self.root, '0.1.0', None, {'assets/a.bin': noise(50000), 'assets/b.bin': noise(50000)})
        release(self.root, '0.2.0', None, {'assets/a.bin': noise(5, 3)})
        self.run_apply()
        z = os.path.join(self.root, '0.1.zip')
        self.assertTrue(is_deduped(z))
        self.assertLess(os.path.getsize(z), 55000)

    def test_no_files_at_all_writes_a_plain_zip(self):
        tmp = os.path.join(self.tmp, 'empty.zip')
        self.assertFalse(core.write_rollup(tmp, {}, True, 'empty', QUIET))
        with zipfile.ZipFile(tmp) as zf:
            self.assertEqual(zf.namelist(), [])

    def test_empty_manifest_is_readable(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            pieces = [n for n in zf.namelist() if '/blobs/' in n or '/skeleton/' in n]
        rewrite(z, manifest=lambda m: dict(m, files=[]), drop=pieces)
        restore(z, out=self.out, say=QUIET)

    def test_nothing_shared_is_byte_for_byte_the_plain_zip(self):
        release(self.root, '0.1.0', None, {'assets/a.bin': noise(5000, 1)})
        release(self.root, '0.1.1', None, {'assets/a.bin': noise(5000, 2)})
        release(self.root, '0.2.0', None, {'assets/a.bin': noise(5000, 3)})
        expect = os.path.join(self.tmp, 'expect.zip')
        files = core.member_files(core.plan(self.root)[0])
        # Marker files differ per version, so nothing at all repeats.
        core.write_zip(expect, files)
        self.run_apply()
        self.assertEqual(sha_file(os.path.join(self.root, '0.1.zip')), sha_file(expect))

    def test_small_saving_stays_plain(self):
        release(self.root, '0.1.0', None, {'assets/a.bin': noise(200000, 1), 'assets/x': b'same' * 10})
        release(self.root, '0.1.1', None, {'assets/a.bin': noise(200000, 2), 'assets/x': b'same' * 10})
        release(self.root, '0.2.0', None, {'assets/a.bin': noise(5, 3)})
        self.run_apply()
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as zf:
            self.assertNotIn(MANIFEST, zf.namelist())

    def test_worth_it_margin(self):
        self.assertFalse(core.worth_it(1000, 1000))
        self.assertFalse(core.worth_it(2000, 1000 + 1000))
        self.assertTrue(core.worth_it(3000, 1000))
        self.assertFalse(core.worth_it(1000000, 995000))
        self.assertTrue(core.worth_it(1000000, 980000))

    def test_too_big_for_a_default_restore_stays_plain(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v))
        # The one patched limit in the suite: the real one is 64 GiB, and a real input that size would be
        # read, deflated and checked in full, which takes minutes and as much free disk.
        with mock.patch.object(core, 'DEFAULT_MAX_SIZE', 1000):
            self.run_apply()
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as zf:
            self.assertNotIn(MANIFEST, zf.namelist())
        self.assertTrue(any('plain zip' in s for s in self.said))

    def test_non_zip_files_dedupe_and_crlf_survives(self):
        crlf = b'line one\r\nline two\r\n' + noise(40000)
        lone = b'lf only\n\r mixed\r\n\x00'
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, None, {'assets/crlf.txt': crlf, 'assets/mixed.txt': lone})
        self.run_apply()
        z = os.path.join(self.root, '0.1.zip')
        self.assertTrue(is_deduped(z))
        self.assertLess(os.path.getsize(z), 45000)
        restore(z, out=self.out, say=QUIET)
        with open(os.path.join(self.out, 'v0.1.1', 'assets', 'crlf.txt'), 'rb') as f:
            self.assertEqual(f.read(), crlf)
        with open(os.path.join(self.out, 'v0.1.0', 'assets', 'mixed.txt'), 'rb') as f:
            self.assertEqual(f.read(), lone)

    def test_empty_file_round_trip(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v), {'assets/empty': b''})
        z = os.path.join(self.root, '0.1.zip')
        self.run_apply()
        restore(z, out=self.out, say=QUIET)
        self.assertEqual(os.path.getsize(os.path.join(self.out, 'v0.1.0', 'assets', 'empty')), 0)

    def test_reader_handles_any_read_size(self):
        z = self.rollup()
        with Rollup(z) as r:
            name = 'v0.1.0/assets/app.zip'
            with r.open(name) as f:
                data = f.read(1) + f.read(7) + f.read(100000) + f.read()
            with r.open(name) as f:
                self.assertEqual(f.read(), data)
            self.assertEqual(sha(data), sha(app('0.1.0')))
            self.assertEqual(r.size(name), len(data))

    def test_space_line_counts_what_left_the_disk(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v))
        before = sum(core.disk_size(os.path.join(self.root, 'v' + v)) for v in ('0.1.0', '0.1.1'))
        self.run_apply()
        after = os.path.getsize(os.path.join(self.root, '0.1.zip'))
        line = [s for s in self.said if s.startswith('space:')]
        self.assertEqual(line, ['space: before %s bytes, after %s bytes, saved %s bytes (%.1f%%)' % (
            format(before, ','), format(after, ','), format(before - after, ','), 100.0 * (before - after) / before)])

    def test_dry_run_changes_nothing_and_reports_no_space(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v))
        before = snapshot(self.root)
        apply(self.root, dry=True, say=self.say)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(any(s.startswith('space:') for s in self.said))


class InnerZips(Base):
    """Every inner zip shape must split where it can and always rebuild exactly."""

    SHAPES = {
        'deflated': lambda: inner_zip({'a': noise(9000), 'b': noise(9000, 2)}),
        'stored': lambda: inner_zip({'a': noise(9000), 'b': noise(9000, 2)}, zipfile.ZIP_STORED),
        'data descriptor': lambda: inner_zip({'a': noise(9000), 'b': noise(9000, 2)}, streamed=True),
        'zip64': lambda: inner_zip({'a': noise(9000)}, force_zip64=True),
        'comment': lambda: inner_zip({'a': noise(9000)}, comment=b'a comment PK\x05\x06 with a stray signature'),
        'prefix junk': lambda: inner_zip({'a': noise(9000)}, prefix=b'#!/bin/sh\nexit 0\n' + noise(3000, 4)),
        'trailing junk': lambda: inner_zip({'a': noise(9000)}, trailing=b'trailing'),
        'no entries': lambda: inner_zip({}),
        'only small entries': lambda: inner_zip({'a': b'x', 'b': b'y'}),
        'empty entry': lambda: inner_zip({'empty': b'', 'a': noise(9000)}),
        'hostile names': lambda: inner_zip({'../../evil': noise(9000), '/abs': noise(9000, 2), 'c:\\x': noise(9000, 3),
                                            'aux': noise(9000, 5)}),
    }

    def test_each_shape_rebuilds_exactly(self):
        for label, make in self.SHAPES.items():
            with self.subTest(label):
                data = make()
                root = os.path.join(self.tmp, re.sub(r'\W', '_', label))
                os.makedirs(root)
                for v in ('0.1.0', '0.1.1', '0.2.0'):
                    release(root, v, data, {'assets/filler.bin': noise(5000, 9)})
                apply(root, trash=self.trash, say=QUIET)
                z = os.path.join(root, '0.1.zip')
                self.assertTrue(is_deduped(z))
                out = os.path.join(root, 'out')
                restore(z, out=out, say=QUIET)
                for v in ('v0.1.0', 'v0.1.1'):
                    with open(os.path.join(out, v, 'assets', 'app.zip'), 'rb') as f:
                        self.assertEqual(f.read(), data)
                self.assertEqual(sorted(os.listdir(out)), ['v0.1.0', 'v0.1.1'])

    def split(self, data: bytes) -> list[tuple[int, int, bool]] | None:
        p = os.path.join(self.tmp, 'inner.bin')
        with open(p, 'wb') as f:
            f.write(data)
        return split_zip(p, len(data))

    def test_split_finds_the_streams(self):
        data = inner_zip({'a': noise(9000), 'b': noise(9000, 2), 'small': b'tiny'})
        cuts = self.split(data)
        assert cuts is not None
        self.assertEqual(len(cuts), 2)
        for off, length, packed in cuts:
            self.assertTrue(packed)
            self.assertLessEqual(off + length, len(data))

    def test_split_finds_nothing_in_other_files(self):
        self.assertIsNone(self.split(b''))
        self.assertIsNone(self.split(noise(5000)))
        self.assertIsNone(self.split(b'PK\x05\x06 not a zip'))
        self.assertIsNone(self.split(inner_zip({})))

    def central_offsets(self, data: bytes) -> list[int]:
        """Offsets of the central directory records."""
        out: list[int] = []
        i = data.find(b'PK\x01\x02')
        while i >= 0:
            out.append(i)
            i = data.find(b'PK\x01\x02', i + 4)
        return out

    def test_overlapping_entries_are_not_split(self):
        # Point the second entry at the first one's header, as a zip quine or overlap bomb does.
        data = bytearray(inner_zip({'aa': noise(9000), 'bb': noise(9000, 2)}))
        first, second = self.central_offsets(bytes(data))
        data[second + 42:second + 46] = data[first + 42:first + 46]
        self.assertIsNone(self.split(bytes(data)))

    def test_entry_reaching_past_the_end_is_not_split(self):
        data = bytearray(inner_zip({'aa': noise(9000)}))
        first = self.central_offsets(bytes(data))[0]
        data[first + 20:first + 24] = struct.pack('<I', 0x7fffffff)
        self.assertIsNone(self.split(bytes(data)))

    def test_entry_without_a_local_header_is_not_split(self):
        data = bytearray(inner_zip({'aa': noise(9000)}))
        data[0:4] = b'XXXX'
        self.assertIsNone(self.split(bytes(data)))

    def test_bomb_is_carried_as_bytes(self):
        # A member that claims to inflate to 3 GiB: never inflated, so it just rides along.
        data = bytearray(inner_zip({'bomb': noise(9000)}))
        first = self.central_offsets(bytes(data))[0]
        data[first + 24:first + 28] = struct.pack('<I', 3 << 30)
        data = bytes(data)
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, data, {'assets/filler.bin': noise(5000, 9)})
        self.run_apply()
        restore(os.path.join(self.root, '0.1.zip'), out=self.out, say=QUIET)
        self.assertEqual(snapshot(self.out)['v0.1.0/assets/app.zip'], sha(data))

    def test_depth_one_only(self):
        nested = inner_zip({'inner.zip': inner_zip({'a': noise(9000)}, zipfile.ZIP_STORED)}, zipfile.ZIP_STORED)
        cuts = self.split(nested)
        assert cuts is not None
        self.assertEqual(len(cuts), 1)

    def test_random_payloads_round_trip(self):
        for seed in range(12):
            r = random.Random(seed)
            root = os.path.join(self.tmp, 'r%d' % seed)
            os.makedirs(root)
            pool = [noise(r.randrange(0, 20000), seed * 100 + i) for i in range(5)]
            want = {}
            for v in ('0.1.0', '0.1.1', '0.1.2', '0.2.0'):
                method = r.choice((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))
                data = inner_zip({'m%d' % i: r.choice(pool) for i in range(r.randrange(0, 6))}, method)
                release(root, v, data, {'assets/plain.bin': r.choice(pool)})
                want[v] = (data)
            apply(root, trash=self.trash, say=QUIET)
            out = os.path.join(root, 'out')
            restore(os.path.join(root, '0.1.zip'), out=out, say=QUIET)
            for v in ('0.1.0', '0.1.1', '0.1.2'):
                with open(os.path.join(out, 'v' + v, 'assets', 'app.zip'), 'rb') as f:
                    self.assertEqual(f.read(), want[v], 'seed %d' % seed)


class Promises(Base):
    def test_rerun_says_nothing_to_do(self):
        z = self.rollup()
        first = sha_file(z)
        self.said = []
        self.run_apply()
        self.assertEqual(self.said, ['nothing to do'])
        self.run_apply()
        self.assertEqual(sha_file(z), first)

    def test_unpacked_build_is_compared_and_trashed(self):
        z = self.rollup()
        first = sha_file(z)
        restore(z, out=self.root, say=QUIET)
        self.said = []
        self.run_apply()
        self.assertEqual(sha_file(z), first)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.0')))
        self.assertTrue(any(s == 'trashed v0.1.0' for s in self.said))
        self.said = []
        self.run_apply()
        self.assertEqual(self.said, ['nothing to do'])

    def test_changed_unpacked_build_keeps_the_folder(self):
        z = self.rollup()
        restore(z, out=self.root, say=QUIET)
        with open(os.path.join(self.root, 'v0.1.0', 'SHA256SUMS'), 'a') as f:
            f.write('changed')
        with self.assertRaisesRegex(Fail, 'sha256 differs for v0.1.0/SHA256SUMS'):
            self.run_apply()
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'v0.1.0')))

    def test_new_member_gives_the_same_bytes_as_from_scratch(self):
        z = self.rollup('0.1.0', '0.1.1')
        release(self.root, '0.1.2', app('0.1.2'))
        self.run_apply()
        got = sha_file(z)
        self.root = os.path.join(self.tmp, 'scratch')
        os.makedirs(self.root)
        self.assertEqual(sha_file(self.rollup('0.1.0', '0.1.1', '0.1.2')), got)

    def plain_with_new_member(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v))
        apply(self.root, trash=self.trash, say=QUIET, dedupe=False)
        z = os.path.join(self.root, '0.1.zip')
        self.assertFalse(is_deduped(z))
        release(self.root, '0.1.2', app('0.1.2'))
        return z

    def test_plain_rollup_upgrades_when_it_is_rewritten(self):
        z = self.plain_with_new_member()
        with zipfile.ZipFile(z) as zf:
            old = {i: sha(zf.read(i)) for i in zf.namelist()}
        self.run_apply()
        with Rollup(z) as r:
            self.assertTrue(r.deduped)
            for arc, h in old.items():
                with r.open(arc) as f:
                    self.assertEqual(sha(f.read()), h)
            self.assertIn('v0.1.2/assets/app.zip', r.files())

    def test_rewrite_of_a_corrupt_plain_rollup_refuses(self):
        z = self.plain_with_new_member()
        with zipfile.ZipFile(z) as zf:
            info = zf.getinfo('v0.1.0/assets/app.zip')
        with open(z, 'r+b') as f:
            f.seek(info.header_offset + 90)
            f.write(b'\xff' * 500)
        before = snapshot(self.root)
        with self.assertRaises(Fail):
            self.run_apply()
        self.assertEqual(snapshot(self.root), before)

    def test_plain_rollup_stays_plain_on_a_rerun(self):
        z = self.plain_with_new_member()
        shutil.rmtree(os.path.join(self.root, 'v0.1.2'))
        first = sha_file(z)
        self.said = []
        self.run_apply()
        self.assertEqual((self.said, sha_file(z)), (['nothing to do'], first))

    def test_mixed_formats_side_by_side(self):
        release(self.root, '0.1.0', app('0.1.0'))
        release(self.root, '0.1.1', app('0.1.1'))
        release(self.root, '0.2.0', app('0.2.0'))
        release(self.root, '0.2.1', app('0.2.1'))
        release(self.root, '0.3.0', app('0.3.0'))
        for s in core.plan(self.root):
            if s['target'].endswith('0.1.zip'):
                core.write_zip(s['target'], core.member_files(s))
        self.run_apply()
        self.assertFalse(is_deduped(os.path.join(self.root, '0.1.zip')))
        self.assertTrue(is_deduped(os.path.join(self.root, '0.2.zip')))
        self.said = []
        self.run_apply()
        self.assertEqual(self.said, ['nothing to do'])
        restore(os.path.join(self.root, '0.1.zip'), check_only=True, say=QUIET)
        restore(os.path.join(self.root, '0.2.zip'), check_only=True, say=QUIET)

    def test_no_dedupe_rewrite_turns_a_deduped_rollup_plain(self):
        z = self.rollup('0.1.0', '0.1.1')
        release(self.root, '0.1.2', app('0.1.2'))
        self.run_apply(dedupe=False)
        self.assertFalse(is_deduped(z))
        with zipfile.ZipFile(z) as zf:
            self.assertEqual(sha(zf.read('v0.1.0/assets/app.zip')), sha(app('0.1.0')))

    def test_folder_wins_when_a_zip_holds_the_same_release(self):
        z = self.rollup()
        restore(z, out=self.root, say=QUIET)
        s = core.plan(self.root)[0]
        files = core.member_files(s)
        self.assertEqual(files['v0.1.0/SHA256SUMS'][0], 'file')

    def test_trash_failure_after_the_swap_recovers_on_rerun(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v))

        def broken(_: str) -> None:
            raise Fail('trash failed')
        with self.assertRaises(Fail):
            apply(self.root, trash=broken, say=QUIET)
        z = os.path.join(self.root, '0.1.zip')
        self.assertTrue(is_deduped(z))
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'v0.1.0')))
        self.run_apply()
        self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.0')))

    def test_verify_refuses_missing_and_extra_files(self):
        z = self.rollup()
        with Rollup(z) as r:
            files: Files = {name: ('zip', (z, name)) for name in r.files()}
        self.assertEqual(verify(z, files), '')
        self.assertRegex(verify(z, {**files, 'v0.1.0/more': ('zip', (z, 'v0.1.0/SHA256SUMS'))}), 'missing')
        fewer = dict(files)
        fewer.pop('v0.1.0/SHA256SUMS')
        self.assertRegex(verify(z, fewer), 'unexpected entry')

    def test_deduped_zips_merge_into_a_line_zip(self):
        for v in ('0.1.0', '0.2.0'):
            release(self.root, v, None, {'assets/a.bin': noise(50000), 'assets/b.bin': noise(50000)})
        self.run_apply(zip_newest=True)
        one = os.path.join(self.root, 'v0.2.0.zip')
        self.assertTrue(is_deduped(one))
        release(self.root, '0.2.1', None, {'assets/a.bin': noise(50000), 'assets/b.bin': noise(50000)})
        release(self.root, '0.3.0', None, {'assets/a.bin': noise(5, 8)})
        self.run_apply()
        self.assertFalse(os.path.exists(one))
        line = os.path.join(self.root, '0.2.zip')
        self.assertTrue(is_deduped(line))
        self.assertEqual(os.path.getsize(line) < 56000, True)
        restore(line, check_only=True, say=QUIET)

    def test_tool_never_sees_rollup_internals_as_releases(self):
        z = self.rollup()
        _, members = scan(self.root)
        self.assertEqual(members[z], [(0, 1, 0), (0, 1, 1), (0, 1, 2)])

    def test_zip_newest_dedupes_too(self):
        for v in ('0.1.0', '0.2.0'):
            release(self.root, v, None, {'assets/a.bin': noise(50000), 'assets/b.bin': noise(50000)})
        self.run_apply(zip_newest=True)
        self.assertTrue(is_deduped(os.path.join(self.root, 'v0.2.0.zip')))
        self.assertEqual(sorted(os.listdir(self.root)), ['0.1.zip', 'v0.2.0.zip'])


class Pre(Base):
    def build(self, stage: str, name: str, data: bytes) -> None:
        d = os.path.join(self.root, stage, name)
        os.makedirs(os.path.join(d, 'assets'))
        for f, text in (('PRERELEASE.json', '{"v": "%s"}' % name), ('SHA256SUMS', name)):
            with open(os.path.join(d, f), 'w') as w:
                w.write(text)
        with open(os.path.join(d, 'assets', 'app.zip'), 'wb') as w:
            w.write(data)

    def test_pre_dedupes_by_default_and_keeps_the_newest(self):
        for i in (1, 2, 3):
            self.build('beta', 'v1.0.0-beta.%d' % i, app(str(i)))
        apply_pre(self.root, current='1.0.0-beta.3', trash=self.trash, say=self.say)
        z = os.path.join(self.root, 'beta', 'v1.0.0-beta.x.zip')
        self.assertTrue(is_deduped(z))
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'beta', 'v1.0.0-beta.3')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'beta', 'v1.0.0-beta.1')))
        self.assertTrue(any(s.startswith('space:') for s in self.said))
        restore(z, out=self.out, say=QUIET)
        self.assertEqual(snapshot(self.out)['v1.0.0-beta.2/assets/app.zip'], sha(app('2')))
        self.said = []
        apply_pre(self.root, current='1.0.0-beta.3', trash=self.trash, say=self.say)
        self.assertEqual(self.said, ['nothing to do'])

    def test_pre_new_member_rewrites_and_unpack_is_trashed(self):
        for i in (1, 2, 3):
            self.build('beta', 'v1.0.0-beta.%d' % i, app(str(i)))
        apply_pre(self.root, current='1.0.0-beta.3', trash=self.trash, say=QUIET)
        z = os.path.join(self.root, 'beta', 'v1.0.0-beta.x.zip')
        self.build('beta', 'v1.0.0-beta.4', app('4'))
        apply_pre(self.root, current='1.0.0-beta.4', trash=self.trash, say=QUIET)
        with Rollup(z) as r:
            self.assertTrue(r.deduped)
            self.assertIn('v1.0.0-beta.3/assets/app.zip', r.files())
        first = sha_file(z)
        restore(z, out=os.path.join(self.root, 'beta'), say=QUIET)
        apply_pre(self.root, current='1.0.0-beta.4', trash=self.trash, say=QUIET)
        self.assertEqual(sha_file(z), first)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'beta', 'v1.0.0-beta.1')))

    def test_pre_no_dedupe_and_plain_upgrade(self):
        for i in (1, 2, 3):
            self.build('rc', 'v1.0.0-rc.%d' % i, app(str(i)))
        apply_pre(self.root, current='1.0.0-rc.3', trash=self.trash, say=QUIET, dedupe=False)
        z = os.path.join(self.root, 'rc', 'v1.0.0-rc.x.zip')
        self.assertFalse(is_deduped(z))
        self.build('rc', 'v1.0.0-rc.4', app('4'))
        apply_pre(self.root, current='1.0.0-rc.4', trash=self.trash, say=QUIET)
        self.assertTrue(is_deduped(z))

    def test_pre_rejects_a_foreign_build_in_a_manifest(self):
        for i in (1, 2, 3):
            self.build('beta', 'v1.0.0-beta.%d' % i, app(str(i)))
        apply_pre(self.root, current='1.0.0-beta.3', trash=self.trash, say=QUIET)
        z = os.path.join(self.root, 'beta', 'v1.0.0-beta.x.zip')

        def retop(m: RawManifest) -> None:
            m['files'][0]['path'] = m['files'][0]['path'].replace('v1.0.0-beta.1', 'v9.0.0-beta.1')
        rewrite(z, manifest=retop)
        before = snapshot(self.root)
        with self.assertRaisesRegex(Fail, 'unexpected top-level'):
            apply_pre(self.root, current='1.0.0-beta.3', trash=self.trash, say=QUIET)
        self.assertEqual(snapshot(self.root), before)


class Hostile(Base):
    """A forged rollup is refused by name, on restore and by run, and nothing gets written."""

    def forged(self, edit: ManifestEdit) -> str:
        z = self.rollup()
        rewrite(z, manifest=edit)
        return z

    def entry(self, m: RawManifest, kind: str = 'zip-split') -> RawEntry:
        return next(e for e in m['files'] if e['kind'] == kind)

    def test_bad_paths_in_a_manifest(self):
        for bad in ('../evil', 'v0.1.0/../../evil', '/etc/passwd', 'v0.1.0\\..\\evil', 'C:/evil', 'c:evil', 'v0.1.0/a\x00b',
                    'v0.1.0/a\nb', '', 'evil.txt', 'other/evil', 'v0.1.0', 'v0.1.0/', 'v0.1.0//x', 'v0.1.0/./x',
                    '\\\\server\\share\\x', 'v0.1.0/\ud800'):
            with self.subTest(bad):
                root = os.path.join(self.tmp, 'c')
                shutil.rmtree(root, ignore_errors=True)
                os.makedirs(root)
                self.root = root

                def edit(m: RawManifest, bad: str = bad) -> None:
                    self.entry(m)['path'] = bad
                z = self.forged(edit)
                self.assertRefused(z)
                with self.assertRaises(Fail):
                    scan(root)
                self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil')))

    def refuse_edit(self, edit: ManifestEdit, pattern: str | None = None) -> None:
        root = os.path.join(self.tmp, 'c')
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root)
        self.root = root
        z = self.forged(edit)
        msg = self.assertRefused(z, pattern)
        self.assertIn(os.path.basename(z), msg)
        before = snapshot(root)
        with self.assertRaises(Fail):
            self.run_apply()
        self.assertEqual(snapshot(root), before)

    def test_bad_manifest_structure(self):
        cases: dict[str, ManifestEdit] = {
            'duplicate path': lambda m: m['files'].append(m['files'][0].copy()),
            'file and folder': lambda m: m['files'].append({**m['files'][0], 'path': m['files'][0]['path'] + '/below'}),
            'blob not hex': lambda m: self.entry(m, 'plain').update(blob='../../evil'),
            'upper-case blob': lambda m: self.entry(m, 'plain').update(blob='A' * 64),
            'sha with newline': lambda m: self.entry(m).update(sha256='a' * 64 + '\n'),
            'skeleton not hex': lambda m: self.entry(m).update(skeleton='x'),
            'missing blob': lambda m: self.entry(m, 'plain').update(blob='a' * 64),
            'negative size': lambda m: self.entry(m).update(size=-1),
            'bool size': lambda m: self.entry(m, 'plain').update(size=True),
            'absurd size': lambda m: self.entry(m).update(size=1 << 70),
            'unknown kind': lambda m: self.entry(m).update(kind='script'),
            'ref past skeleton': lambda m: self.entry(m)['refs'][0].__setitem__(0, 1 << 40),
            'ref negative': lambda m: self.entry(m)['refs'][0].__setitem__(0, -5),
            'ref not a triple': lambda m: self.entry(m)['refs'].__setitem__(0, [1, 2]),
            'ref to missing blob': lambda m: self.entry(m)['refs'][0].__setitem__(2, 'b' * 64),
            'refs repeat an offset': lambda m: self.entry(m)['refs'].append(list(self.entry(m)['refs'][-1])),
            'sizes disagree': lambda m: self.entry(m).update(size=self.entry(m)['size'] + 1),
            'skeleton size lies': lambda m: self.entry(m).update(skeleton_size=self.entry(m)['skeleton_size'] + 1,
                                                                  size=self.entry(m)['size'] + 1),
            # The callback's return value replaces the manifest, so these two can hold what RawManifest can't.
            'not a list': lambda m: dict(m, files='x'),
            'entry not an object': lambda m: dict(m, files=[*m['files'], 3]),
            'format text': lambda m: m.update(format='1'),
            'format zero': lambda m: m.update(format=0),
            'format bool': lambda m: m.update(format=True),
            'no format': lambda m: m.pop('format'),
        }
        for label, edit in cases.items():
            with self.subTest(label):
                self.refuse_edit(edit)

    def test_newer_format_is_refused_by_name(self):
        self.refuse_edit(lambda m: m.update(format=container.FORMAT + 1), 'newer release-rollup')

    def test_unparsable_manifests(self):
        for raw in (b'not json', b'[]', b'{"format":1,"format":1,"files":[]}', b'\xff\xfe', b'[' * 100000):
            with self.subTest(raw[:10]):
                self.refuse_edit(lambda m, raw=raw: raw)

    def rewrite_outer(self, **kw: Unpack[RewriteOpts]) -> str:
        z = self.rollup()
        rewrite(z, **kw)
        return z

    def test_unexpected_entries(self):
        for name in ('v0.1.0/evil', '../evil', '/abs', '.rollup/other', BLOB_DIR + 'short', BLOB_DIR + '../../evil',
                     SKELETON_DIR + 'a' * 63, BLOB_DIR + 'a' * 64):
            with self.subTest(name):
                self.root = tempfile.mkdtemp(dir=self.tmp)
                msg = self.assertRefused(self.rewrite_outer(add=[(name, b'x')]))
                self.assertRegex(msg, 'unexpected entry|not used by any file')

    def test_repeated_entry_is_refused(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            first = next(n for n in zf.namelist() if n.startswith(BLOB_DIR))
        with zipfile.ZipFile(z, 'a') as zf, self.assertWarns(UserWarning):
            zf.writestr(first, b'other')
        self.assertRefused(z, 'twice')

    def test_missing_blob_named(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            blob = next(n for n in zf.namelist() if n.startswith(BLOB_DIR))
        rewrite(z, drop=[blob])
        self.assertRefused(z, re.escape(blob))

    def hostile_plain(self, names: Iterable[str]) -> str:
        z = os.path.join(self.root, 'hostile.zip')
        with zipfile.ZipFile(z, 'w') as zf:
            for name in names:
                zf.writestr(name, b'x')
        return z

    def test_plain_zip_slip_is_refused(self):
        for bad in ('v0.1.0/../../evil', '../evil', '/abs/evil', 'v0.1.0\\evil', 'evil'):
            with self.subTest(bad):
                # Own folders per name: what one name leaves behind must not reach the next.
                self.root = tempfile.mkdtemp(dir=self.tmp)
                self.out = os.path.join(self.root, 'out')
                z = self.hostile_plain(['v0.1.0/good', bad])
                # zipfile cleans some names on the way in, so patch them straight into the file bytes.
                self.patch_name(z, bad)
                if os.sep == '\\' and '\\' in bad:
                    self.assertBackslashStaysInside(z, bad)
                else:
                    self.assertRefused(z, 'unsafe name')
                self.assertNothingOutside()

    def assertNothingOutside(self):
        """Whatever restore did, the only things next to the output folder are the zip and the output folder."""
        self.assertLessEqual(set(os.listdir(self.root)), {'hostile.zip', 'out'})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil')))

    def assertBackslashStaysInside(self, z: str, bad: str) -> None:
        """Windows zipfile reads a backslash as a slash, so the name is a plain file inside the release folder."""
        restore(z, out=self.out, say=QUIET)
        self.assertEqual(sorted(snapshot(self.out)), ['v0.1.0/evil', 'v0.1.0/good'])

    def test_kill_plan_kills_for_real(self):
        kill, killed = kill_plan()
        r = subprocess.run([sys.executable, '-c', 'import os, signal, sys\n%s\nsys.exit(3)' % kill])
        self.assertEqual(r.returncode, killed)
        self.assertEqual(killed, signal.SIGTERM if hostcat.this_host() == 'windows' else -9)

    def patch_name(self, z: str, bad: str) -> None:
        with zipfile.ZipFile(z) as zf:
            got = zf.namelist()[1]
        if got != bad and len(got) == len(bad):
            with open(z, 'r+b') as f:
                data = f.read().replace(got.encode(), bad.encode())
                f.seek(0)
                f.write(data)

    def test_windows_names_refused_only_for_windows(self):
        for bad in ('v0.1.0/aux.txt', 'v0.1.0/CON', 'v0.1.0/lpt1.log', 'v0.1.0/nul/x', 'v0.1.0/a:b', 'v0.1.0/stream.txt:evil',
                    'v0.1.0/trail.', 'v0.1.0/trail ', 'v0.1.0/a?b', 'v0.1.0/a*b'):
            with self.subTest(bad):
                self.root = tempfile.mkdtemp(dir=self.tmp)
                self.out = os.path.join(self.root, 'out')
                z = self.rollup()

                def edit(m: RawManifest, bad: str = bad) -> None:
                    m['files'][0]['path'] = bad
                rewrite(z, manifest=edit)
                if hostcat.this_host() == 'windows':
                    self.assertRefused(z, 'unsafe name')
                else:
                    restore(z, out=self.out, say=QUIET)
                    self.assertTrue(os.path.isfile(os.path.join(self.out, *bad.split('/'))))

    def test_windows_case_collision(self):
        z = self.rollup()

        def edit(m: RawManifest) -> None:
            m['files'].append({**m['files'][0], 'path': m['files'][0]['path'].upper().replace('V0', 'v0')})
        rewrite(z, manifest=edit)
        if hostcat.this_host() == 'windows':
            self.assertRefused(z, 'twice')
        else:
            restore(z, out=self.out, say=QUIET)
            got = sorted(snapshot(self.out))
            self.assertEqual(len(got), len({g.casefold() for g in got}) + 1)

    def test_linux_keeps_names_only_windows_minds(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v), {'assets/aux.c': b'kernel file'})
        self.run_apply()
        z = os.path.join(self.root, '0.1.zip')
        restore(z, out=self.out, say=QUIET)
        self.assertTrue(os.path.isfile(os.path.join(self.out, 'v0.1.0', 'assets', 'aux.c')))

    def test_name_a_rollup_cannot_hold_keeps_the_sources(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v), {'assets/back\\slash': b'x'})
        before = snapshot(self.root)
        with self.assertRaisesRegex(Fail, 'no-dedupe'):
            self.run_apply()
        self.assertEqual(snapshot(self.root), before)
        self.run_apply(dedupe=False)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.0')))

    def test_name_a_rollup_cannot_hold_from_a_zip_keeps_the_sources(self):
        # Every host: the name comes in a plain zip made elsewhere. A tab is a control character, which no
        # manifest may hold, and Windows' zipfile reads it as it is (it only turns a backslash into a slash).
        apps = {v: app(v) for v in ('0.1.0', '0.1.1')}
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip'), 'w', zipfile.ZIP_DEFLATED) as zf:
            for v, data in apps.items():
                zf.writestr('v%s/assets/app.zip' % v, data)
            zf.writestr('v0.1.0/assets/tab\there', b'x')
        for v in ('0.1.2', '0.2.0'):
            release(self.root, v, app(v))
        before = snapshot(self.root)
        with self.assertRaisesRegex(Fail, re.escape('cannot hold %r' % 'v0.1.0/assets/tab\there') + '.*no-dedupe'):
            self.run_apply()
        self.assertEqual(snapshot(self.root), before)
        self.run_apply(dedupe=False)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.2')))
        with zipfile.ZipFile(os.path.join(self.root, '0.1.zip')) as zf:
            self.assertIn('v0.1.0/assets/tab\there', zf.namelist())

    def test_symlink_in_out_is_refused(self):
        z = self.rollup()
        os.makedirs(self.out)
        elsewhere = os.path.join(self.tmp, 'elsewhere')
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.out, 'v0.1.0'), target_is_directory=True)
        with self.assertRaisesRegex(Fail, 'symlink'):
            restore(z, out=self.out, say=QUIET)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(os.listdir(self.out), ['v0.1.0'])

    def test_dangling_symlink_target_is_refused(self):
        z = self.rollup()
        os.makedirs(os.path.join(self.out, 'v0.1.0'))
        victim = os.path.join(self.tmp, 'victim')
        os.symlink(victim, os.path.join(self.out, 'v0.1.0', 'SHA256SUMS'))
        with self.assertRaisesRegex(Fail, 'already exists'):
            restore(z, out=self.out, say=QUIET)
        self.assertFalse(os.path.exists(victim))

    def test_existing_file_is_never_overwritten(self):
        z = self.rollup()
        os.makedirs(os.path.join(self.out, 'v0.1.0'))
        keep = os.path.join(self.out, 'v0.1.0', 'SHA256SUMS')
        with open(keep, 'w') as f:
            f.write('mine')
        with self.assertRaisesRegex(Fail, 'already exists'):
            restore(z, out=self.out, say=QUIET)
        with open(keep) as f:
            self.assertEqual(f.read(), 'mine')
        self.assertEqual(sorted(os.listdir(self.out)), ['v0.1.0'])

    def test_restore_of_a_file_where_a_folder_goes(self):
        # Regression: the second release's folder used to fail after the first was already moved into place.
        z = self.rollup()
        os.makedirs(self.out)
        with open(os.path.join(self.out, 'v0.1.2'), 'w') as f:
            f.write('mine')
        with self.assertRaisesRegex(Fail, 'file where a folder goes'):
            restore(z, out=self.out, say=QUIET)
        self.assertEqual(sorted(os.listdir(self.out)), ['v0.1.2'])

    def test_max_size_refuses_before_reading(self):
        # A damaged blob as well: had any blob been read, the refusal would name the damage instead.
        z = self.rollup()
        info = Corruption.blob_of(z)
        rewrite(z, change={info.filename: noise(info.file_size, 77)})
        self.assertRefused(z, 'unpack to more', max_size=100)

    def test_reused_blob_cannot_inflate_past_the_cap(self):
        # A small manifest asking for one blob many times: the sizes add up before any byte is read.
        z = self.rollup()

        def edit(m: RawManifest) -> None:
            e = self.entry(m)
            big = max(e['refs'], key=lambda r: r[1])
            length = big[1]
            assert isinstance(length, int)
            e['refs'] = [[i, length, big[2]] for i in range(min(e['skeleton_size'], 2000))]
            e['size'] = e['skeleton_size'] + length * len(e['refs'])
        info = Corruption.blob_of(z)
        rewrite(z, manifest=edit, change={info.filename: noise(info.file_size, 77)})
        self.assertRefused(z, 'unpack to more', max_size=1 << 20)

    def test_plain_zip_bomb_is_refused_by_size(self):
        z = os.path.join(self.root, 'bomb.zip')
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('v0.1.0/zeros', b'\0' * (1 << 22))
        self.assertLess(os.path.getsize(z), 20000)
        self.assertRefused(z, 'unpack to more', max_size=1 << 20)

    def test_manifest_size_cap(self):
        # A real manifest one byte past the cap. Spaces are valid JSON padding and deflate to almost
        # nothing, so the zip stays small; the size is refused before the manifest is read.
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            items = [(i.filename, zf.read(i.filename)) for i in zf.infolist() if i.filename != MANIFEST]
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name, data in items:
                zf.writestr(name, data)
            left = container.MAX_MANIFEST + 1
            with zf.open(MANIFEST, 'w') as f:
                chunk = b' ' * (1 << 20)
                while left:
                    step = min(left, len(chunk))
                    f.write(chunk[:step])
                    left -= step
        self.assertRefused(z, 'too large')

    def test_scan_stays_fast_on_absurd_plain_sizes(self):
        # An old rollup past the restore cap still scans: only restore enforces it. Its central
        # directory claims almost 4 GiB for every entry, over 64 GiB in all.
        for v in ('0.1.0', '0.1.1', '0.1.2'):
            release(self.root, v, app(v), extra={'assets/f%d' % i: b'x' for i in range(5)})
        release(self.root, '0.2.0', app('0.2.0'))
        self.run_apply(dedupe=False)
        z = os.path.join(self.root, '0.1.zip')
        with zipfile.ZipFile(z) as zf:
            count, start = len(zf.infolist()), zf.start_dir
        with open(z, 'r+b') as f:
            data = bytearray(f.read())
            at = start
            for _ in range(count):
                self.assertEqual(data[at:at + 4], b'PK\x01\x02')
                struct.pack_into('<I', data, at + 24, 0xFFFFFFF0)
                name_len, extra_len, comment_len = struct.unpack_from('<HHH', data, at + 28)
                at += 46 + name_len + extra_len + comment_len
            f.seek(0)
            f.write(data)
        self.assertGreater(count * 0xFFFFFFF0, container.DEFAULT_MAX_SIZE)
        self.assertIn(z, scan(self.root)[1])
        self.assertRefused(z, 'unpack to more')


class Corruption(Base):
    @staticmethod
    def blob_of(z: str, big: bool = True) -> zipfile.ZipInfo:
        with zipfile.ZipFile(z) as zf:
            blobs = [i for i in zf.infolist() if i.filename.startswith(BLOB_DIR)]
        blobs.sort(key=lambda i: i.file_size, reverse=big)
        return blobs[0]

    def test_flipped_byte_in_a_stored_blob(self):
        z = self.rollup()
        info = self.blob_of(z)
        with open(z, 'r+b') as f:
            f.seek(info.header_offset + 30 + len(info.filename) + 1000)
            byte = f.read(1)
            f.seek(-1, 1)
            f.write(bytes([byte[0] ^ 1]))
        self.assertRefused(z, re.escape(info.filename))

    def test_replaced_blob_with_a_good_crc(self):
        z = self.rollup()
        info = self.blob_of(z)
        rewrite(z, change={info.filename: noise(info.file_size, 77)})
        self.assertRefused(z, 'blob %s .*is corrupt' % info.filename[len(BLOB_DIR):])

    def test_replaced_skeleton_with_a_good_crc(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            name = next(n for n in zf.namelist() if n.startswith(SKELETON_DIR))
            size = zf.getinfo(name).file_size
        rewrite(z, change={name: noise(size, 78)})
        self.assertRefused(z, 'skeleton %s .*corrupt' % name[len(SKELETON_DIR):])

    def test_short_blob_is_named(self):
        z = self.rollup()
        info = self.blob_of(z)
        rewrite(z, change={info.filename: b'short'})
        self.assertRefused(z, re.escape(info.filename))

    def test_wrong_file_sha_is_named(self):
        z = self.rollup()

        victim = ''

        def edit(m: RawManifest) -> None:
            nonlocal victim
            m['files'][0]['sha256'] = 'd' * 64
            victim = m['files'][0]['path']
        rewrite(z, manifest=edit)
        self.assertRefused(z, re.escape(victim))

    def test_truncated_rollup(self):
        z = self.rollup()
        with open(z, 'r+b') as f:
            f.truncate(os.path.getsize(z) // 2)
        self.assertRefused(z)
        with self.assertRaisesRegex(Fail, 'not a readable zip'):
            scan(self.root)

    def test_garbage_rollup(self):
        z = os.path.join(self.tmp, 'v1.0.0.zip')
        with open(z, 'wb') as f:
            f.write(noise(1000))
        self.assertRefused(z)

    def test_corrupt_rollup_with_a_leftover_folder_keeps_the_folder(self):
        z = self.rollup()
        restore(z, out=self.root, say=QUIET)
        info = self.blob_of(z)
        rewrite(z, change={info.filename: noise(info.file_size, 77)})
        with self.assertRaisesRegex(Fail, 'sources kept'):
            self.run_apply()
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'v0.1.0')))

    def test_corrupt_rollup_is_never_rewritten(self):
        z = self.rollup('0.1.0', '0.1.1')
        info = self.blob_of(z)
        rewrite(z, change={info.filename: noise(info.file_size, 77)})
        release(self.root, '0.1.2', app('0.1.2'))
        before = snapshot(self.root)
        with self.assertRaises(Fail):
            self.run_apply()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith('.tmp')], [])

    def test_corrupt_manifest_stops_the_run_before_any_change(self):
        z = self.rollup()
        release(self.root, '0.3.0', app('0.3.0'))
        rewrite(z, change={MANIFEST: b'garbage'})
        before = snapshot(self.root)
        with self.assertRaisesRegex(Fail, '0.1.zip is not a readable zip'):
            self.run_apply()
        self.assertEqual(snapshot(self.root), before)

    def test_encrypted_flag_is_a_refusal_not_a_crash(self):
        z = self.rollup()
        info = self.blob_of(z)
        with open(z, 'rb') as f:
            data = bytearray(f.read())
        # Set the encrypted bit in both headers of the blob.
        data[info.header_offset + 6] |= 1
        i = data.find(b'PK\x01\x02')
        while i >= 0:
            head = struct.unpack('<H', bytes(data[i + 28:i + 30]))[0]
            if bytes(data[i + 46:i + 46 + head]).decode() == info.filename:
                data[i + 8] |= 1
            i = data.find(b'PK\x01\x02', i + 4)
        with open(z, 'wb') as f:
            f.write(data)
        self.assertRefused(z)


class Failure(Base):
    """A write that stops at any point leaves the old rollup and the sources exactly as they were."""

    def prepared(self) -> tuple[str, dict[str, str]]:
        z = self.rollup('0.1.0', '0.1.1')
        release(self.root, '0.1.2', app('0.1.2'))
        return z, snapshot(self.root)

    def assertUntouched(self, before: dict[str, str]) -> None:
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual([n for n in os.listdir(self.root) if '.tmp' in n], [])

    def rollup_size(self) -> int:
        """Size of the rollup that prepared() and a run lead to, written on the normal disk first."""
        z, _ = self.prepared()
        self.run_apply()
        return os.path.getsize(z)

    def full_disk(self, free: int) -> dict[str, str]:
        """prepared() on the small filesystem, then filled until about free bytes are left."""
        self.use_small_fs()
        _, before = self.prepared()
        smallfs.leave_free(self, self.root, free)
        return before

    def test_disk_full_while_writing(self):
        before = self.full_disk(16 << 10)
        with self.assertRaisesRegex(Fail, smallfs.FULL + '.*sources kept'):
            self.run_apply()
        self.assertUntouched(before)

    def test_disk_full_midway_through_a_blob(self):
        before = self.full_disk(self.rollup_size() // 2)
        with self.assertRaisesRegex(Fail, smallfs.FULL + '.*sources kept'):
            self.run_apply()
        self.assertUntouched(before)

    def test_disk_full_while_writing_the_plain_comparison(self):
        # Room for the rollup and half as much again: the rollup is written and checked, then the
        # plain zip it is compared with runs out of space. That one is bigger, the releases share most bytes.
        before = self.full_disk(self.rollup_size() * 3 // 2)
        with self.assertRaisesRegex(Fail, smallfs.FULL + '.*sources kept'):
            self.run_apply()
        self.assertUntouched(before)

    def test_swap_failure_keeps_everything(self):
        # A folder sits where the rollup goes, so the last step, moving the checked temp file in
        # place, fails. Linux says "Is a directory", Windows "Access is denied".
        self.prepared()
        target = os.path.join(self.root, 'in-the-way.zip')
        os.makedirs(target)
        with open(os.path.join(target, 'keep.txt'), 'w') as f:
            f.write('keep')
        before = snapshot(self.root)
        src = os.path.join(self.root, 'v0.1.2', 'assets', 'app.zip')
        files: Files = {'v0.1.2/assets/app.zip': ('file', src)}
        with self.assertRaisesRegex(Fail, 'could not be written.*sources kept'):
            core.build_target(target, files, True, 'in-the-way.zip', '1 release', self.say)
        self.assertUntouched(before)

    def test_windows_swap_onto_a_rollup_in_use_keeps_everything(self):
        # Another program has the old rollup open (here: this test). Windows refuses to replace it.
        z, before = self.prepared()
        with open(z, 'rb'), self.assertRaisesRegex(Fail, 'could not be written.*sources kept'):
            self.run_apply()
        self.assertUntouched(before)

    def test_killed_mid_write_old_rollup_survives_and_rerun_finishes(self):
        z, before = self.prepared()
        kill, killed = kill_plan()
        code = '''
import os, signal, sys
sys.path.insert(0, %r)
from release_rollup import container, core
real = container._copy_range
count = []
def die(*a):
    count.append(1)
    if len(count) > 8:
        %s
    return real(*a)
container._copy_range = die
core.apply(%r, trash=core.move_trash(%r), say=lambda *_: None)
''' % (SRC, kill, self.root, os.path.join(self.tmp, 'trash'))
        r = subprocess.run([sys.executable, '-c', code])
        self.assertEqual(r.returncode, killed)
        self.assertEqual(sha_file(z), before['0.1.zip'])
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'v0.1.2')))
        leftovers = [n for n in os.listdir(self.root) if '.tmp' in n]
        self.assertTrue(leftovers, 'the kill should have left a temp file')
        self.run_apply()
        self.assertFalse(os.path.exists(os.path.join(self.root, 'v0.1.2')))
        with Rollup(z) as r2:
            self.assertIn('v0.1.2/assets/app.zip', r2.files())

    def test_restore_disk_full_writes_nothing(self):
        z = self.rollup()
        self.out = os.path.join(smallfs.folder(self), 'out')
        smallfs.leave_free(self, os.path.dirname(self.out), 64 << 10)
        with self.assertRaisesRegex(Fail, smallfs.FULL):
            restore(z, out=self.out, say=QUIET)
        self.assertEqual(os.listdir(self.out), [])


class Removal(Base):
    """Without the tool a deduped rollup is still a zip that says how to get the files back."""

    def script(self, z: str) -> tuple[str, str]:
        with zipfile.ZipFile(z) as zf:
            text = zf.read(README).decode('ascii')
        return text, text[text.index('# --- restore script ---'):text.index('# --- end ---')]

    def test_readme_says_how(self):
        text, _ = self.script(self.rollup())
        self.assertIn('release-rollup restore THIS.zip --out DIR', text.splitlines()[2])
        self.assertIn('format 1', text)
        self.assertIn(__version__, text)

    def test_readme_script_rebuilds_every_file_without_the_tool(self):
        z = self.rollup()
        _, script = self.script(z)
        work = os.path.join(self.tmp, 'unzipped')
        with zipfile.ZipFile(z) as zf:
            zf.extractall(work)
        tool = os.path.join(self.tmp, 'by_hand.py')
        with open(tool, 'w') as f:
            f.write(script)
        r = subprocess.run([sys.executable, '-I', tool], cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 0, r.stdout)
        restore(z, out=self.out, say=QUIET)
        self.assertEqual(snapshot(os.path.join(work, 'restored')), snapshot(self.out))

    def test_readme_script_refuses_bad_names_and_bad_hashes(self):
        z = self.rollup()

        def edit(m: RawManifest) -> None:
            m['files'][0]['sha256'] = 'd' * 64
        rewrite(z, manifest=edit)
        _, script = self.script(z)
        work = os.path.join(self.tmp, 'unzipped')
        with zipfile.ZipFile(z) as zf:
            zf.extractall(work)
        tool = os.path.join(self.tmp, 'by_hand.py')
        with open(tool, 'w') as f:
            f.write(script)
        r = subprocess.run([sys.executable, '-I', tool], cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertNotEqual(r.returncode, 0)

    def test_the_systems_own_unzip_reads_it(self):
        # Linux: Info-ZIP unzip. Windows: its built-in tar.exe (bsdtar, libarchive), which reads zips and checks
        # each entry's CRC as it extracts. The full path keeps Git for Windows' GNU tar out of the way.
        z = self.rollup()
        work = os.path.join(self.tmp, 'u')
        if hostcat.this_host() == 'windows':
            tar = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'tar.exe')
            os.makedirs(work)
            self.assertEqual(subprocess.run([tar, '-tf', z], stdout=subprocess.DEVNULL).returncode, 0)
            self.assertEqual(subprocess.run([tar, '-xf', z, '-C', work]).returncode, 0)
        else:
            self.assertEqual(subprocess.run(['unzip', '-tq', z], stdout=subprocess.DEVNULL).returncode, 0)
            self.assertEqual(subprocess.run(['unzip', '-q', z, '-d', work]).returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(work, 'README.txt')))
        self.assertTrue(os.path.isfile(os.path.join(work, '.rollup', 'MANIFEST.json')))

    def test_only_stdlib_and_python_3_14_syntax(self):
        stdlib: frozenset[str] | None = getattr(sys, 'stdlib_module_names', None)
        src = os.path.join(SRC, 'release_rollup')
        for name in os.listdir(src):
            if not name.endswith('.py'):
                continue
            with open(os.path.join(src, name), encoding='utf-8') as f:
                tree = ast.parse(f.read(), feature_version=(3, 14))
            for node in ast.walk(tree):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module is not None:
                    mods = [node.module]
                for m in mods:
                    if stdlib:
                        self.assertIn(m.split('.')[0], stdlib, name)

    def test_never_opens_rollup_data_in_text_mode(self):
        with open(os.path.join(SRC, 'release_rollup', 'container.py'), encoding='utf-8') as f:
            source = f.read()
        for call in re.findall(r"\bopen\([^)]*\)", source):
            if "'w" in call or "'r" in call:
                self.assertRegex(call, r"'[rw]b'|zip", call)


class Paths(Base):
    def test_long_path_prefix_on_windows(self):
        short = 'C:\\x\\y'
        far = 'C:\\' + 'd' * 300
        self.assertEqual(long_path(short), short)
        self.assertEqual(long_path(far), '\\\\?\\' + far)
        self.assertEqual(long_path('\\\\?\\' + far), '\\\\?\\' + far)
        self.assertEqual(long_path('\\\\srv\\share\\' + 'd' * 300), '\\\\?\\UNC\\srv\\share\\' + 'd' * 300)

    def test_long_path_is_left_alone_off_windows(self):
        far = '/' + 'd' * 300
        self.assertEqual(long_path(far), far)
        self.assertEqual(long_path('\\\\srv\\share\\' + 'd' * 300), '\\\\srv\\share\\' + 'd' * 300)

    def test_entry_names_stay_short(self):
        z = self.rollup()
        with zipfile.ZipFile(z) as zf:
            self.assertLessEqual(max(len(n) for n in zf.namelist()), len(SKELETON_DIR) + 64)

    def test_deep_long_names_restore(self):
        # The sources stay short, so this also runs on Windows, where the restored path passes 260 characters.
        deep = 'v0.1.0/' + '/'.join(['d' * 40] * 8 + ['f.bin'])
        src = os.path.join(self.tmp, 'src.bin')
        with open(src, 'wb') as f:
            f.write(noise(4000))
        z = os.path.join(self.tmp, 'long.zip')
        spool = os.path.join(self.tmp, 'spool')
        files: Files = {deep: ('file', src)}
        container.write_container(z, files, core.local_copy(spool), spool)
        restore(z, out=self.out, say=QUIET)
        got = long_path(os.path.join(os.path.realpath(self.out), *deep.split('/')))
        with open(got, 'rb') as f:
            self.assertEqual(f.read(), noise(4000))

    def test_unicode_names_round_trip(self):
        for v in ('0.1.0', '0.1.1', '0.2.0'):
            release(self.root, v, app(v), {'assets/caf\u00e9 \u4e16\u754c.txt': b'x'})
        self.run_apply()
        restore(os.path.join(self.root, '0.1.zip'), out=self.out, say=QUIET)
        self.assertIn('v0.1.1/assets/caf\u00e9 \u4e16\u754c.txt', snapshot(self.out))


class Version(unittest.TestCase):
    def test_without_its_version_file_the_package_reports_the_same_version(self):
        # pyproject.toml maps the copy's folder onto src/release_rollup for coverage.
        with open(os.path.join(SRC, 'release_rollup', 'VERSION')) as f:
            ver = f.read().strip()
        with tempfile.TemporaryDirectory(prefix='rr-noversion-') as td:
            pkg = os.path.join(td, 'release_rollup')
            shutil.copytree(os.path.join(SRC, 'release_rollup'), pkg, ignore=shutil.ignore_patterns('VERSION', '__pycache__'))
            r = subprocess.run([sys.executable, '-c', 'import release_rollup as r; print(r.__version__); print(r.__file__)'],
                               capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=td), check=True)
        got, where = r.stdout.splitlines()
        self.assertEqual(got, ver)
        self.assertEqual(os.path.dirname(where), pkg)

    def test_every_place_agrees(self):
        top = os.path.join(HERE, '..')
        with open(os.path.join(top, 'VERSION')) as f:
            ver = f.read().strip()
        with open(os.path.join(SRC, 'release_rollup', 'VERSION')) as f:
            self.assertEqual(f.read().strip(), ver)
        self.assertEqual(__version__, ver)
        with open(os.path.join(SRC, 'release_rollup', '__init__.py')) as f:
            self.assertIn("__version__ = '%s'" % ver, f.read())
        m = re.fullmatch(r'(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?', ver)
        self.assertTrue(m, ver)
        assert m is not None
        pep440 = m.group(1) + ({'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[m.group(2)] + m.group(3) if m.group(2) else '')
        with open(os.path.join(top, 'pyproject.toml')) as f:
            self.assertIn('version = "%s"' % pep440, f.read())


load_tests = hostcat.load_tests


if __name__ == '__main__':
    unittest.main()
