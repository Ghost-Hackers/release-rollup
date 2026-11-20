"""The lock that refuses a second run on a folder while the first one still works on it.

It is an OS file lock on <root>/.release-rollup.lock: flock on Linux and macOS, a byte-range lock on Windows. The OS
drops it when the process ends, killed or not: Linux at once, Windows shortly after. So a run that is gone holds no
lock for long, and no one has to remove it by hand. The file holds one line naming the run that has it.

Checking the lock only needs read access to the file, so in a folder shared by several users a run can tell whose
run holds it. A run that gets the lock on a file someone else left knows the run that made it is gone: it says so if
the file names that run, removes the file and makes its own. A clean end removes the file.
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
    _OPEN = os.O_BINARY | os.O_NOINHERIT

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

    def _clear_locked(path: str) -> None:
        """Nothing to do yet: Windows can't remove the file while it is open."""

    def _clear_closed(path: str) -> None:
        _remove_stale(path)
else:
    import fcntl

    _OPEN = os.O_NOFOLLOW | os.O_CLOEXEC

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

    def _clear_locked(path: str) -> None:
        # Removed while the lock is held, so no other run can have made a new file at that name in the meantime.
        _remove_stale(path)

    def _clear_closed(path: str) -> None:
        """Nothing to do: the file went before it was closed."""

# A new lock file is ours to write; an existing one may belong to another user, and checking its lock needs only
# read access.
_NEW = _OPEN | os.O_RDWR | os.O_CREAT | os.O_EXCL
_OLD = _OPEN | os.O_RDONLY
# Windows error when a file is open in another program and can't be removed.
_SHARING_VIOLATION = 32


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


def _remove_stale(path: str) -> None:
    """Remove a lock file left by a run that is gone. Raises Fail when this user may not remove it."""
    try:
        os.remove(path)
    except PermissionError as e:
        if getattr(e, 'winerror', None) == _SHARING_VIOLATION:
            raise
        raise Fail('%s was left by a run that is no longer running and has to be removed by hand (%s); nothing was '
                   'changed' % (path, e.strerror or reason(e))) from None
    except FileNotFoundError:
        pass


def _acquire(path: str) -> tuple[int, str]:
    """Make and lock a new lock file at path. Returns its fd and the record of a stale lock file it replaced, '' when
    there was none. Raises Fail when another run holds the lock, when path is a link or anything but a plain file, or
    when a stale lock file can't be removed, before writing anything."""
    stale = ''
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
        try:
            fd = os.open(path, _NEW if seen is None else _OLD, 0o640)
        except (FileExistsError, FileNotFoundError):
            continue
        kept = replace = False
        try:
            if seen is not None:
                mine = os.fstat(fd)
                if (mine.st_dev, mine.st_ino) != (seen.st_dev, seen.st_ino):
                    continue
            if not _try_lock(fd):
                raise Fail('%s is held by another run%s; nothing was changed' % (path, _owner(_read(fd))))
            if _ours(fd, path):
                kept = seen is None
                replace = not kept
            if replace:
                # The lock was free, so the run that made this file is gone. Put a file of our own in its place.
                stale = _read(fd)
                _clear_locked(path)
        finally:
            if not kept:
                os.close(fd)
        if kept:
            return fd, stale
        if replace:
            _clear_closed(path)


def _cannot(path: str, e: OSError) -> Fail:
    return Fail('cannot lock %s: %s; nothing was changed' % (path, e.strerror or reason(e)))


@contextlib.contextmanager
def lock(root: str, say: Callable[[str], object]) -> Generator[None]:
    """Hold the lock on root for the length of the block. Raises Fail when another run holds it or when the lock
    file can't be made or a stale one removed, before anything in root is changed."""
    path = os.path.join(root, NAME)
    try:
        fd, stale = _acquire(path)
    except OSError as e:
        raise _cannot(path, e) from None
    try:
        if stale:
            found = _owner(stale)
            if found:
                say('cleared a stale lock in %s left by a run that ended without releasing it%s' % (path, found))
            else:
                say('cleared a stale lock in %s that held an unreadable record' % path)
        try:
            # _try_lock() moved the offset to the locked byte on Windows; the record goes at the start.
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
