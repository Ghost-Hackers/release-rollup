"""Deduped rollups: an ordinary zip that holds every distinct piece of content once.

Layout of a deduped rollup:

    README.txt                  how to get the files back
    .rollup/blobs/<sha256>      one distinct piece of content
    .rollup/skeleton/<sha256>   what is left of an inner zip with its member data cut out
    .rollup/MANIFEST.json       which file is made of which pieces

Nothing inside is named by anything from a build. An inner zip is cut at the raw
compressed stream of each member, so joining its skeleton and blobs gives back the
original file byte for byte without compressing anything again. Rollup reads both this
layout and the plain zips older versions wrote.
"""
from __future__ import annotations

import binascii
import functools
import json
import os
import re
import shutil
import struct
import tempfile
import hashlib
import zipfile
import zlib
from collections.abc import Callable, Generator, Iterable, Mapping
from typing import IO, BinaryIO, Literal, Protocol, Self, TypedDict, TypeGuard, cast

FORMAT = 2
# Format 1 is a rollup of v<version>/ folders. Format 2 adds the optional "top" rule; only a manifest that
# uses a rule other than the default is written as format 2, so everything older stays byte for byte as it was.
TOPS = ('release', 'flat')
MANIFEST = '.rollup/MANIFEST.json'
README = 'README.txt'
BLOB_DIR = '.rollup/blobs/'
SKELETON_DIR = '.rollup/skeleton/'
STAMP = (1980, 1, 1, 0, 0, 0)
CHUNK = 1 << 20
# Compressed streams shorter than this stay in the skeleton: a zip entry of its own costs more than it can save.
MIN_BLOB = 512
# Refuse to rebuild more than this unless asked to (restore --max-size).
DEFAULT_MAX_SIZE = 64 << 30
MAX_MANIFEST = 256 << 20
MAX_INT = 1 << 62

HEX64 = re.compile(r'[0-9a-f]{64}')
TOP_RE = re.compile(r'v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?')
WIN_RESERVED = re.compile(r'(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?', re.I)
WIN_BAD_CHARS = re.compile(r'[<>:"|?*]')
# What a damaged or hostile zip can raise while it is read.
READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, struct.error,
               ValueError, LookupError, OverflowError)


def reason(e: BaseException) -> str:
    """The text of an error, or its type name when the text is empty. A cut-short zip member raises a bare EOFError."""
    return str(e) or type(e).__name__

class Fail(Exception):
    pass


class Reader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class PlainEntry(TypedDict):
    path: str
    kind: Literal['plain']
    size: int
    sha256: str
    blob: str


class SplitEntry(TypedDict):
    path: str
    kind: Literal['zip-split']
    size: int
    sha256: str
    skeleton: str
    skeleton_size: int
    refs: list[tuple[int, int, str]]


Entry = PlainEntry | SplitEntry


class ManifestData(TypedDict):
    format: int
    tool: object
    top: str
    files: dict[str, Entry]


def n(x: int) -> str:
    return format(x, ',')


def sha_stream(f: Reader) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(CHUNK), b''):
        h.update(chunk)
    return h.hexdigest()


def blob_key(data_sha: str) -> str:
    """The name of a blob in the rollup."""
    return data_sha


# Names

def path_problem(path: object, top: str = 'release') -> str:
    """Why this file name cannot go in a manifest on any platform, or an empty string.

    top 'release' (the default): the path starts with a v<version>/ or build folder.
    top 'flat': a plain file name with no folder, such as outbox-report.zip or INDEX.md."""
    if not isinstance(path, str) or not path:
        return 'empty name'
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in path):
        return 'control character in the name'
    if '\\' in path:
        return 'backslash in the name'
    if path.startswith('/'):
        return 'absolute path'
    if re.match(r'[A-Za-z]:', path):
        return 'drive letter'
    if top == 'flat' and ':' in path:
        return 'colon in the name'
    parts = path.split('/')
    if '..' in parts or '.' in parts or '' in parts:
        return 'path with an empty, . or .. part'
    if top == 'flat':
        return 'folder in the name' if len(parts) > 1 else ''
    if len(parts) < 2 or not TOP_RE.fullmatch(parts[0]):
        return 'path outside a release or build folder'
    return ''


def windows_name_problem(path: str) -> str:
    """Why this file name cannot be written on Windows, or an empty string."""
    for part in path.split('/'):
        if WIN_RESERVED.fullmatch(part):
            return 'reserved Windows name %s' % part
        if WIN_BAD_CHARS.search(part):
            return 'character Windows does not allow in %s' % part
        if part.endswith(('.', ' ')):
            return 'Windows drops the trailing dot or space of %s' % part
    return ''


def check_paths(paths: Iterable[str], windows: bool, top: str = 'release') -> None:
    """Fail unless all these names can be written side by side (windows: also on a Windows disk)."""
    seen: set[str] = set()
    for p in paths:
        why = path_problem(p, top) or (windows and windows_name_problem(p))
        if why:
            raise Fail('unsafe name %r: %s; nothing was written' % (p, why))
        key = p.casefold() if windows else p
        if key in seen:
            raise Fail('name %r appears twice; nothing was written' % p)
        seen.add(key)
    for p in paths:
        parts = p.split('/')
        for i in range(1, len(parts)):
            parent = '/'.join(parts[:i])
            if (parent.casefold() if windows else parent) in seen:
                raise Fail('%r is both a file and a folder; nothing was written' % parent)


def long_path(path: str) -> str:
    """Windows only: a path past the old 260 character limit needs the \\\\?\\ prefix."""
    if os.name != 'nt':
        return path
    if len(path) < 200 or path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


# Manifest

def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise Fail('manifest repeats the key %r' % k)
        out[k] = v
    return out


def _is_int(x: object, low: int = 0) -> TypeGuard[int]:
    return isinstance(x, int) and not isinstance(x, bool) and low <= x <= MAX_INT


def _is_sha(x: object) -> TypeGuard[str]:
    return isinstance(x, str) and HEX64.fullmatch(x) is not None


@functools.lru_cache(maxsize=4)
def parse_manifest(raw: bytes) -> ManifestData:
    """Parse and check a manifest. Returns {'format', 'tool', 'files': {path: entry}}. Raises Fail."""
    try:
        loaded: object = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_dup_keys)
    except (ValueError, RecursionError) as e:
        raise Fail('manifest is not valid JSON (%s)' % e)
    if not isinstance(loaded, dict):
        raise Fail('manifest has no valid format number')
    m = cast(dict[str, object], loaded)
    fmt = m.get('format')
    if not _is_int(fmt) or fmt < 1:
        raise Fail('manifest has no valid format number')
    if fmt > FORMAT:
        raise Fail('format %s was made by a newer release-rollup' % fmt)
    top = m.get('top', 'release')
    if not isinstance(top, str) or top not in TOPS:
        raise Fail('manifest has the unknown top rule %r' % (top,))
    if top != 'release' and fmt < 2:
        raise Fail('manifest uses the top rule %r, which needs format 2' % top)
    listed = m.get('files')
    if not isinstance(listed, list):
        raise Fail('manifest has no file list')
    files: dict[str, Entry] = {}
    for item in cast(list[object], listed):
        if not isinstance(item, dict):
            raise Fail('manifest holds a file entry that is not an object')
        e = cast(dict[str, object], item)
        path = e.get('path')
        why = path_problem(path, top)
        if why or not isinstance(path, str):
            raise Fail('manifest holds the unsafe name %r: %s' % (path, why))
        if path in files:
            raise Fail('manifest lists %r twice' % path)
        size = e.get('size')
        if not _is_int(size) or not _is_sha(e.get('sha256')):
            raise Fail('manifest entry %r has a bad size or sha256' % path)
        if e.get('kind') == 'plain':
            if not _is_sha(e.get('blob')):
                raise Fail('manifest entry %r has a bad blob name' % path)
        elif e.get('kind') == 'zip-split':
            _check_refs(path, e, size)
        else:
            raise Fail('manifest entry %r has the unknown kind %r' % (path, e.get('kind')))
        files[path] = cast(Entry, e)
    check_paths(list(files), False, top)
    return {'format': fmt, 'tool': m.get('tool'), 'top': top, 'files': files}


def _check_refs(path: str, e: dict[str, object], size: int) -> None:
    skeleton_size = e.get('skeleton_size')
    refs = e.get('refs')
    if not _is_sha(e.get('skeleton')) or not _is_int(skeleton_size) or not isinstance(refs, list):
        raise Fail('manifest entry %r has a bad skeleton' % path)
    last, total = -1, skeleton_size
    for r in cast(list[object], refs):
        parts = cast(list[object], r) if isinstance(r, list) else []
        off, length, blob = parts if len(parts) == 3 else (None, None, None)
        if not (_is_int(off) and _is_int(length) and _is_sha(blob)):
            raise Fail('manifest entry %r has a bad reference' % path)
        if off <= last or off > skeleton_size:
            raise Fail('manifest entry %r has references out of order or out of range' % path)
        last = off
        total += length
    if total != size:
        raise Fail('manifest entry %r: its pieces add up to %s bytes, not %s' % (path, n(total), n(size)))


# Reading

class Member:
    """A file being rebuilt: read() streams it and raises Fail as soon as a piece or the whole does not match."""

    def __init__(self, rollup: Rollup, entry: Entry) -> None:
        self._r = rollup
        self._e = entry
        self._gen = self._checked()
        self._buf = b''

    def _blob(self, name: str, length: int) -> Generator[bytes]:
        path = self._e['path']
        h, left = hashlib.sha256(), length
        try:
            with self._r.zip.open(BLOB_DIR + name) as f:
                for chunk in iter(lambda: f.read(CHUNK), b''):
                    h.update(chunk)
                    left -= len(chunk)
                    if left < 0:
                        break
                    yield chunk
        except READ_ERRORS as e:
            raise Fail('blob %s of %s cannot be read (%s)' % (name, path, reason(e)))
        if left or blob_key(h.hexdigest()) != name:
            raise Fail('blob %s of %s is corrupt' % (name, path))

    def _skeleton_part(self, f: Reader, update: Callable[[bytes], object], count: int, skeleton: str) -> Generator[bytes]:
        while count:
            chunk = f.read(min(count, CHUNK))
            if not chunk:
                raise Fail('skeleton %s of %s is too short' % (skeleton, self._e['path']))
            update(chunk)
            count -= len(chunk)
            yield chunk

    def _pieces(self) -> Generator[bytes]:
        e = self._e
        if e['kind'] == 'plain':
            for chunk in self._blob(e['blob'], e['size']):
                yield chunk
            return
        h, pos = hashlib.sha256(), 0
        try:
            with self._r.zip.open(SKELETON_DIR + e['skeleton']) as sk:
                for off, length, blob in e['refs']:
                    for chunk in self._skeleton_part(sk, h.update, off - pos, e['skeleton']):
                        yield chunk
                    pos = off
                    for chunk in self._blob(blob, length):
                        yield chunk
                for chunk in self._skeleton_part(sk, h.update, e['skeleton_size'] - pos, e['skeleton']):
                    yield chunk
        except READ_ERRORS as err:
            raise Fail('skeleton %s of %s cannot be read (%s)' % (e['skeleton'], e['path'], reason(err)))
        if h.hexdigest() != e['skeleton']:
            raise Fail('skeleton %s of %s is corrupt' % (e['skeleton'], e['path']))

    def _checked(self) -> Generator[bytes]:
        h, total = hashlib.sha256(), 0
        for chunk in self._pieces():
            h.update(chunk)
            total += len(chunk)
            yield chunk
        if total != self._e['size'] or h.hexdigest() != self._e['sha256']:
            raise Fail('%s does not match its sha256 in the manifest' % self._e['path'])

    def read(self, size: int = -1) -> bytes:
        out: list[bytes] = []
        have = len(self._buf)
        if self._buf:
            out.append(self._buf)
            self._buf = b''
        while size < 0 or have < size:
            chunk = next(self._gen, None)
            if chunk is None:
                break
            out.append(chunk)
            have += len(chunk)
        data = b''.join(out)
        if size >= 0 and len(data) > size:
            self._buf = data[size:]
            data = data[:size]
        return data

    def close(self) -> None:
        self._gen.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class Rollup:
    """Read access to a rollup zip of either format. Raises Fail when it cannot be read or fails its checks."""

    def __init__(self, path: str | os.PathLike[str], max_size: int = DEFAULT_MAX_SIZE) -> None:
        self.path = path
        self.manifest: ManifestData | None = None
        try:
            self.zip = zipfile.ZipFile(path)
        except READ_ERRORS as e:
            raise Fail(reason(e))
        try:
            names = self.zip.namelist()
            self.deduped = MANIFEST in names
            if self.deduped:
                self._load(names, max_size)
        except READ_ERRORS as e:
            self.zip.close()
            raise Fail(reason(e))
        except Fail:
            self.zip.close()
            raise

    def _load(self, names: list[str], max_size: int) -> None:
        if len(set(names)) != len(names):
            raise Fail('it holds the same entry twice')
        for name in names:
            if name in (MANIFEST, README):
                continue
            prefix = BLOB_DIR if name.startswith(BLOB_DIR) else SKELETON_DIR if name.startswith(SKELETON_DIR) else None
            if prefix is None or not HEX64.fullmatch(name[len(prefix):]):
                raise Fail('unexpected entry %r in a deduped rollup' % name)
        info = self.zip.getinfo(MANIFEST)
        if info.file_size > MAX_MANIFEST:
            raise Fail('manifest is too large (%s bytes)' % n(info.file_size))
        manifest = self.manifest = parse_manifest(self.zip.read(MANIFEST))
        used: set[str] = set()
        total = 0
        for e in manifest['files'].values():
            total += e['size']
            if e['kind'] == 'plain':
                self._need(e['path'], BLOB_DIR + e['blob'], e['size'], used)
            else:
                self._need(e['path'], SKELETON_DIR + e['skeleton'], e['skeleton_size'], used)
                for _, length, blob in e['refs']:
                    self._need(e['path'], BLOB_DIR + blob, length, used)
        if total > max_size:
            raise Fail('it would unpack to more than %s bytes (raise --max-size)' % n(max_size))
        extra = set(names) - used - {MANIFEST, README}
        if extra:
            raise Fail('entry %s is not used by any file' % sorted(extra)[0])

    def _need(self, path: str, name: str, size: int, used: set[str]) -> None:
        try:
            info = self.zip.getinfo(name)
        except KeyError:
            raise Fail('%s needs %s, which is missing' % (path, name))
        if info.file_size != size:
            raise Fail('%s and the entry %s disagree on its size' % (path, name))
        used.add(name)

    @property
    def _manifest(self) -> ManifestData:
        assert self.manifest is not None
        return self.manifest

    @property
    def top(self) -> str:
        """The naming rule of the files: 'release' for v<version>/ folders, 'flat' for plain names. A plain zip counts as 'release'."""
        return self._manifest['top'] if self.deduped else 'release'

    def names(self) -> list[str]:
        """Every name the rollup holds for a reader: its files, plus folder entries in a plain zip."""
        return sorted(self._manifest['files']) if self.deduped else self.zip.namelist()

    def files(self) -> list[str]:
        """The file names, never folder entries."""
        if self.deduped:
            return sorted(self._manifest['files'])
        return [i.filename for i in self.zip.infolist() if not i.is_dir()]

    def size(self, name: str) -> int:
        return self._manifest['files'][name]['size'] if self.deduped else self.zip.getinfo(name).file_size

    def open(self, name: str) -> Member | IO[bytes]:
        if self.deduped:
            return Member(self, self._manifest['files'][name])
        return self.zip.open(name)

    def check(self) -> str:
        """Empty string when every byte of the rollup is proven, else the reason."""
        try:
            bad = self.zip.testzip()
            if bad:
                return 'corrupt entry %s' % bad
            if self.deduped:
                for name in self.files():
                    with self.open(name) as f:
                        sha_stream(f)
        except Fail as e:
            return str(e)
        except READ_ERRORS as e:
            return reason(e)
        return ''

    def close(self) -> None:
        self.zip.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# Writing

def split_zip(path: str, size: int) -> list[tuple[int, int, bool]] | None:
    """[(offset, length, stored)] of the raw member data worth cutting out of an inner zip.

    None when the file is not a zip we can cut safely: not a zip, a damaged one, or one
    whose entries share bytes (a zip quine or overlap bomb). Only the offsets are used and
    nothing is inflated, so a member's claimed size cannot do harm.
    """
    try:
        with open(long_path(path), 'rb') as f:
            if not zipfile.is_zipfile(f):
                return None
            f.seek(0)
            with zipfile.ZipFile(f) as z:
                infos = z.infolist()
            spans: list[tuple[int, int, int, bool]] = []
            for i in infos:
                f.seek(i.header_offset)
                head = f.read(30)
                if len(head) != 30 or head[:4] != b'PK\x03\x04':
                    return None
                nlen, elen = struct.unpack('<HH', head[26:30])
                start = i.header_offset + 30 + nlen + elen
                end = start + i.compress_size
                if end > size:
                    return None
                spans.append((i.header_offset, start, end, i.compress_type != zipfile.ZIP_STORED))
    except READ_ERRORS:
        return None
    spans.sort()
    for a, b in zip(spans, spans[1:]):
        if b[0] < a[2]:
            return None
    cuts = [(start, end - start, packed) for _, start, end, packed in spans if end - start >= MIN_BLOB]
    return cuts or None


def _copy_range(f: BinaryIO, offset: int, length: int, sink: Callable[[bytes], object]) -> None:
    f.seek(offset)
    while length:
        chunk = f.read(min(length, CHUNK))
        if not chunk:
            raise Fail('a source file got shorter while it was being read; sources kept')
        sink(chunk)
        length -= len(chunk)


class _Writer:
    def __init__(self, z: zipfile.ZipFile) -> None:
        self.z = z
        self.seen: dict[str, tuple[int, int]] = {}
        self.shared = 0

    def _info(self, name: str, deflate: bool, length: int) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, STAMP)
        info.compress_type = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16
        info.file_size = length
        return info

    def text(self, name: str, data: bytes) -> None:
        self.z.writestr(self._info(name, True, len(data)), data)

    def add_range(self, prefix: str, f: BinaryIO, offset: int, length: int, deflate: bool) -> str:
        """Store this byte range once. Returns its name."""
        h, crc = hashlib.sha256(), [0]

        def feed(chunk: bytes) -> None:
            h.update(chunk)
            crc[0] = binascii.crc32(chunk, crc[0])
        _copy_range(f, offset, length, feed)
        key = blob_key(h.hexdigest())
        mark = (length, crc[0])
        if key in self.seen:
            if self.seen[key] != mark:
                raise Fail('sha256 collision on %s; sources kept' % key)
            self.shared += length
            return key
        self.seen[key] = mark
        with self.z.open(self._info(prefix + key, deflate, length), 'w') as w:
            _copy_range(f, offset, length, w.write)
        return key


def readme_text(version: str, fmt: int = 1) -> bytes:
    return ('''This rollup holds its files deduplicated. To get them back, run:

    release-rollup restore THIS.zip --out DIR

or, from a copy of the tool's src folder:

    python3 -m release_rollup restore THIS.zip --out DIR

Plain unzip works but gives you only this file and the .rollup folder, not your
original files: format %d, written by release-rollup %s.

.rollup/blobs/<sha256> holds each distinct piece of content once.
.rollup/skeleton/<sha256> holds what is left of an inner zip with its member data
cut out. .rollup/MANIFEST.json lists every original file: a "plain" file is one blob,
and a "zip-split" file is its skeleton with blobs spliced in at the listed offsets.
"refs" holds [offset in the skeleton, length, blob name]. Every file's size and
sha256 are in the manifest.

Without the tool: unzip this rollup, then run the script below with Python 3 from
the folder that holds README.txt. It writes the files into a new folder "restored"
and stops if a sha256 does not match.

# --- restore script ---
import hashlib, json, os, sys
def read(p):
    with open(p, 'rb') as f:
        return f.read()
with open('.rollup/MANIFEST.json', encoding='utf-8') as f:
    m = json.load(f)
for e in m['files']:
    parts = e['path'].split('/')
    if '..' in parts or e['path'].startswith('/') or '\\\\' in e['path'] or ':' in e['path']:
        sys.exit('unsafe name ' + e['path'])
    out = os.path.join('restored', *parts)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    h = hashlib.sha256()
    with open(out, 'wb') as w:
        def put(b):
            w.write(b)
            h.update(b)
        if e['kind'] == 'plain':
            put(read('.rollup/blobs/' + e['blob']))
        else:
            s = read('.rollup/skeleton/' + e['skeleton'])
            pos = 0
            for off, length, blob in e['refs']:
                put(s[pos:off])
                put(read('.rollup/blobs/' + blob))
                pos = off
            put(s[pos:])
    if h.hexdigest() != e['sha256']:
        sys.exit('sha256 differs for ' + e['path'])
# --- end ---
''' % (fmt, version)).encode('ascii')


def write_container[S](tmp: str | os.PathLike[str], files: Mapping[str, S], open_source: Callable[[S], tuple[str, bool]],
                         spool_dir: str, top: str = 'release') -> int:
    """Write a deduped rollup to tmp. files maps a name to a source, open_source(source) gives
    (seekable real path, is_temporary). top is the naming rule recorded in the manifest.
    Returns the bytes saved by sharing."""
    from . import __version__
    os.makedirs(spool_dir, exist_ok=True)
    entries: list[Entry] = []
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        w = _Writer(z)
        fmt = 1 if top == 'release' else 2
        w.text(README, readme_text(__version__, fmt))
        for arc in sorted(files):
            path, temporary = open_source(files[arc])
            try:
                entries.append(_add_file(w, arc, path, spool_dir))
            finally:
                if temporary:
                    os.remove(path)
        manifest: dict[str, object] = {'format': fmt, 'tool': 'release-rollup ' + __version__, 'files': entries}
        if top != 'release':
            manifest['top'] = top
        w.text(MANIFEST, json.dumps(manifest, sort_keys=True, indent=0, ensure_ascii=True).encode('ascii') + b'\n')
    return w.shared


def _add_file(w: _Writer, arc: str, path: str, spool_dir: str) -> Entry:
    size = os.path.getsize(long_path(path))
    with open(long_path(path), 'rb') as f:
        whole = sha_stream(f)
        cuts = split_zip(path, size)
        if not cuts:
            blob = w.add_range(BLOB_DIR, f, 0, size, True)
            return {'path': arc, 'kind': 'plain', 'size': size, 'sha256': whole, 'blob': blob}
        refs: list[tuple[int, int, str]] = []
        fd, skel = tempfile.mkstemp(prefix='skel.', dir=spool_dir)
        try:
            with os.fdopen(fd, 'wb') as s:
                pos = 0
                for off, length, packed in cuts:
                    _copy_range(f, pos, off - pos, s.write)
                    refs.append((s.tell(), length, w.add_range(BLOB_DIR, f, off, length, not packed)))
                    pos = off + length
                _copy_range(f, pos, size - pos, s.write)
                skel_size = s.tell()
            with open(skel, 'rb') as s:
                name = w.add_range(SKELETON_DIR, s, 0, skel_size, True)
        finally:
            os.remove(skel)
    return {'path': arc, 'kind': 'zip-split', 'size': size, 'sha256': whole, 'skeleton': name,
            'skeleton_size': skel_size, 'refs': refs}


# Restoring

def restore(path: str, out: str | None = None, check_only: bool = False, max_size: int = DEFAULT_MAX_SIZE,
            say: Callable[[str], object] = print) -> int:
    """Prove a rollup of either format and write its files under out. Writes nothing unless every byte is proven.
    Existing files are never overwritten. Raises Fail."""
    name = os.path.basename(path)
    if not os.path.isfile(path):
        raise Fail('%s is not a file' % path)
    try:
        with Rollup(path, max_size) as r:
            files = r.files()
            check_paths(files, os.name == 'nt', r.top)
            total = sum(r.size(f) for f in files)
            if total > max_size:
                raise Fail('it would unpack to more than %s bytes (raise --max-size)' % n(max_size))
            why = r.check()
            if why:
                raise Fail('%s failed the check (%s); nothing was written' % (name, why))
            kind = 'deduped' if r.deduped else 'plain zip'
            if check_only:
                say('checked %s (%s, %s files, %s bytes): every file matches' % (name, kind, n(len(files)), n(total)))
                return len(files)
            if not out:
                raise Fail('restore needs --out DIR, or --check to only prove the rollup')
            _write_out(r, files, os.path.realpath(out))
    except Fail as e:
        msg = str(e)
        raise Fail(msg if name in msg else '%s: %s' % (name, msg))
    except OSError as e:
        raise Fail('%s could not be restored (%s)' % (name, e))
    say('restored %s (%s, %s files, %s bytes) to %s' % (name, kind, n(len(files)), n(total), out))
    return len(files)


def _write_out(r: Rollup, files: list[str], out: str) -> None:
    targets: dict[str, str] = {}
    for f in files:
        parts = f.split('/')
        for i in range(1, len(parts)):
            parent = long_path(os.path.join(out, *parts[:i]))
            if os.path.islink(parent):
                raise Fail('%s is a symlink; nothing was written' % '/'.join(parts[:i]))
            if os.path.lexists(parent) and not os.path.isdir(parent):
                raise Fail('%s is a file where a folder goes; nothing was written' % '/'.join(parts[:i]))
        dest = os.path.join(out, *parts)
        if os.path.lexists(long_path(dest)):
            raise Fail('%s already exists; nothing was written' % f)
        targets[f] = dest
    os.makedirs(long_path(out), exist_ok=True)
    work = tempfile.mkdtemp(prefix='.rollup-restore.', suffix='.tmp', dir=long_path(out))
    try:
        # Short numbered names, so the work folder never adds to the path length.
        temp: dict[str, str] = {}
        for i, f in enumerate(files):
            temp[f] = os.path.join(work, str(i))
            with r.open(f) as src, open(temp[f], 'wb') as dst:
                shutil.copyfileobj(src, dst, CHUNK)
        for f in files:
            os.makedirs(long_path(os.path.dirname(targets[f])), exist_ok=True)
            os.replace(temp[f], long_path(targets[f]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
