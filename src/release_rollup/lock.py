"""The lock that refuses a second run on a folder while the first one still works on it.

It is an OS file lock on <root>/.release-rollup.lock: flock on Linux and macOS, a byte-range lock on Windows. The OS
drops it when the process ends, killed or not: Linux at once, Windows shortly after. So a run that is gone holds no
lock for long, and no one has to remove it by hand. The file holds one line naming the run that has it. A run that
gets the lock and still finds a line there knows that run ended without cleaning up, says so and goes on. A clean
end removes the file.
"""
import contextlib
import datetime
import os
import re
import socket
import stat
import sys
from collections.abc import Callable, Generator

from .container import Fail, reason

NAME = '.release-rollup.lock'
RECORD_RE = re.compile(r'pid=(\d+) host=(.*) started=(\S+)\n?')

if sys.platform == 'win32':
    import msvcrt

    # A Windows byte lock is mandatory: no other process can read the locked byte. Locking one byte far past the
    # record keeps the record readable for the run that gets refused.
    _AT = 1 << 20
    _FLAGS = os.O_RDWR | os.O_BINARY | os.O_NOINHERIT

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _AT, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int, path: str) -> None:
        os.ftruncate(fd, 0)
        os.lseek(fd, _AT, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _remove_closed(path: str) -> None:
        # Windows can't remove an open file, so this runs once it is closed. A run that opened it in the meantime
        # keeps it from being removed; it stays behind empty, and the next run takes an empty file as free.
        with contextlib.suppress(OSError):
            os.remove(path)
else:
    import fcntl

    _FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int, path: str) -> None:
        # Remove the file while the lock is still held; closing the file lets go of the lock. A run that opened it
        # just before gets the lock on a removed file once we close, sees that in _ours() and starts over.
        if _ours(fd, path):
            os.remove(path)

    def _remove_closed(path: str) -> None:
        """Nothing to do: the file went before it was closed."""


def _ours(fd: int, path: str) -> bool:
    """Whether path still names the file open as fd."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    mine = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (mine.st_dev, mine.st_ino)


def _read(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 4096).decode('utf-8', 'replace')


def _owner(record: str) -> str:
    """' (pid N on HOST, started TIME)' for a record line, or '' when it can't be read."""
    m = RECORD_RE.fullmatch(record)
    return ' (pid %s on %s, started %s)' % m.groups() if m else ''


def _plain(st: os.stat_result) -> bool:
    """Whether st is a regular file with no other name and no reparse point, so writing to it reaches nothing else."""
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            and not getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _acquire(path: str) -> int:
    """Open and lock path. Raises Fail when another run holds it or when path is a link or anything but a plain
    file, before reading or writing it."""
    while True:
        try:
            seen = os.lstat(path)
        except FileNotFoundError:
            seen = None
        if seen is not None and not _plain(seen):
            raise Fail('%s is not a plain file; nothing was changed' % path)
        # A new file is made with O_EXCL, which never follows a link. An existing one is opened without O_CREAT,
        # with O_NOFOLLOW where the OS has it; Windows follows links in os.open, so the fstat check below catches a
        # file swapped for a link since the lstat.
        flags = _FLAGS | os.O_CREAT | os.O_EXCL if seen is None else _FLAGS
        try:
            fd = os.open(path, flags, 0o600)
        except (FileExistsError, FileNotFoundError):
            continue
        kept = False
        try:
            if seen is not None:
                mine = os.fstat(fd)
                if (mine.st_dev, mine.st_ino) != (seen.st_dev, seen.st_ino):
                    continue
            if not _try_lock(fd):
                raise Fail('%s is held by another run%s; nothing was changed' % (path, _owner(_read(fd))))
            kept = _ours(fd, path)
        finally:
            if not kept:
                os.close(fd)
        if kept:
            return fd


def _cannot(path: str, e: OSError) -> Fail:
    return Fail('cannot lock %s: %s; nothing was changed' % (path, e.strerror or reason(e)))


@contextlib.contextmanager
def lock(root: str, say: Callable[[str], object]) -> Generator[None]:
    """Hold the lock on root for the length of the block. Raises Fail when another run holds it or when the lock
    file can't be made, before anything in root is changed."""
    path = os.path.join(root, NAME)
    try:
        fd = _acquire(path)
    except OSError as e:
        raise _cannot(path, e) from None
    try:
        try:
            owner = _read(fd)
            if owner:
                found = _owner(owner)
                if found:
                    say('cleared a stale lock in %s left by a run that ended without releasing it%s' % (path, found))
                else:
                    say('cleared a stale lock in %s that held an unreadable record' % path)
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            started = datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
            os.write(fd, ('pid=%d host=%s started=%s\n' % (os.getpid(), socket.gethostname(), started)).encode())
        except OSError as e:
            raise _cannot(path, e) from None
        yield
    finally:
        try:
            _unlock(fd, path)
        finally:
            os.close(fd)
        _remove_closed(path)
