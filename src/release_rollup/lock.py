"""The lock that refuses a second run on a folder while the first one still works on it.

It is an OS file lock on <root>/.release-rollup.lock: flock on Linux and macOS, a byte-range lock on Windows. The OS
drops it the moment the process ends, killed or not, so a lock is never held by a run that is gone. The file holds
one line naming the run that has it. A run that gets the lock and still finds a line there knows that run ended
without cleaning up, says so and goes on. A clean end removes the file.
"""
import contextlib
import datetime
import os
import re
import socket
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
    _FLAGS = os.O_RDWR | os.O_CREAT | os.O_BINARY

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _AT, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _release(fd: int, path: str) -> None:
        # Windows can't remove an open file, so close it first. A run that opened it in the meantime keeps it from
        # being removed; it stays behind empty, and the next run takes an empty file as free.
        os.ftruncate(fd, 0)
        os.lseek(fd, _AT, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(path)
else:
    import fcntl

    _FLAGS = os.O_RDWR | os.O_CREAT

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _release(fd: int, path: str) -> None:
        # Remove the file while the lock is still held. A run that opened it just before gets the lock on a removed
        # file once we close, sees that in _ours() and starts over on a new one.
        if _ours(fd, path):
            os.remove(path)
        os.close(fd)


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


def _acquire(path: str) -> int:
    """Open and lock path. Raises Fail when another run holds it."""
    while True:
        fd = os.open(path, _FLAGS, 0o644)
        try:
            if not _try_lock(fd):
                raise Fail('%s is held by another run%s; nothing was changed' % (path, _owner(_read(fd))))
            if _ours(fd, path):
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


@contextlib.contextmanager
def lock(root: str, say: Callable[[str], object]) -> Generator[None]:
    """Hold the lock on root for the length of the block. Raises Fail when another run holds it or when the lock
    file can't be made, before anything in root is changed."""
    path = os.path.join(root, NAME)
    try:
        fd = _acquire(path)
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
        except BaseException:
            _release(fd, path)
            raise
    except OSError as e:
        raise Fail('cannot lock %s: %s; nothing was changed' % (path, e.strerror or reason(e))) from None
    try:
        yield
    finally:
        _release(fd, path)
