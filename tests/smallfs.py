"""Folders on a small filesystem, for tests that fill a disk.

RR_SMALLFS names a folder on a filesystem of a few MB. tools/check.sh makes one by itself on
Linux (a tmpfs in a private mount namespace). CI makes one on both systems before the tests,
see .github/workflows/ci.yml. Without it these tests fail and say how to set it up.

RR_NOLINKS_FS names a folder on a filesystem without hard links, for the Windows tests of the
copy that stands in for a link. CI makes one on a FAT32 VHD.
"""
import os
import random
import shutil
import tempfile
import unittest

ENV = 'RR_SMALLFS'
LIMIT = 64 << 20
HOWTO = ('%s is not set. This test fills a real disk, so it needs a folder on a filesystem of a few MB. '
         'Run the tests through tools/check.sh (on Linux it makes one itself), or mount a small filesystem '
         '(Linux: tmpfs with size=4m; Windows: a fixed VHD of 8 MB formatted NTFS) and set %s to a folder on it.'
         % (ENV, ENV))


def folder(case: unittest.TestCase) -> str:
    """A fresh, empty folder on the small filesystem, removed when the test ends."""
    base = os.environ.get(ENV, '')
    if not base:
        case.fail(HOWTO)
    if not os.path.isdir(base):
        case.fail('%s=%s is not a folder. %s' % (ENV, base, HOWTO))
    total = shutil.disk_usage(base).total
    if total > LIMIT:
        case.fail('%s=%s is on a filesystem of %d bytes; the ballast would fill it, so it must be %d bytes or less'
                  % (ENV, base, total, LIMIT))
    d = tempfile.mkdtemp(prefix='rr-', dir=base)
    case.addCleanup(shutil.rmtree, d, True)
    return d


NOLINKS_ENV = 'RR_NOLINKS_FS'
NOLINKS_HOWTO = ('This test needs a folder on a filesystem without hard links. On Windows, as an '
                 'administrator, attach a fixed VHD of 64 MB formatted FAT32 (diskpart: create vdisk file=... '
                 'maximum=64 type=fixed, attach vdisk, create partition primary, format fs=fat32 quick, '
                 'assign letter=T) and set %s to a folder on it.' % NOLINKS_ENV)


def nolinks_folder(case: unittest.TestCase) -> str:
    """A fresh, empty folder where os.link fails, removed when the test ends."""
    base = os.environ.get(NOLINKS_ENV, '')
    if not base:
        case.fail('%s is not set. %s' % (NOLINKS_ENV, NOLINKS_HOWTO))
    if not os.path.isdir(base):
        case.fail('%s=%s is not a folder. %s' % (NOLINKS_ENV, base, NOLINKS_HOWTO))
    d = tempfile.mkdtemp(prefix='rr-', dir=base)
    case.addCleanup(shutil.rmtree, d, True)
    probe = os.path.join(d, 'probe')
    open(probe, 'wb').close()
    try:
        os.link(probe, probe + '.link')
    except OSError:
        pass
    else:
        case.fail('%s=%s takes hard links. %s' % (NOLINKS_ENV, base, NOLINKS_HOWTO))
    os.remove(probe)
    return d


def leave_free(case: unittest.TestCase, near: str, free: int) -> str:
    """Fill the filesystem holding near until about free bytes are left. Returns the ballast file,
    which a test may remove to get the space back. It sits in RR_SMALLFS itself, outside the test's
    own folders, so their snapshots don't see it."""
    have = shutil.disk_usage(near).free
    if have < free:
        case.fail('the small filesystem has %d bytes free, the test needs %d' % (have, free))
    fd, path = tempfile.mkstemp(prefix='ballast-', dir=os.environ[ENV])
    case.addCleanup(_remove, path)
    left = have - free
    chunk = b'\0' * (1 << 20)
    with os.fdopen(fd, 'wb') as f:
        while left > 0:
            step = min(left, len(chunk))
            f.write(chunk[:step])
            left -= step
    return path


def _remove(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def noise(size: int, seed: int) -> bytes:
    """Bytes that don't compress, so a zip of them takes about as much disk as they do."""
    return random.Random(seed).randbytes(size)


# What a full disk says: "No space left on device" from the C library, or Windows' own
# "There is not enough space on the disk".
FULL = r'(No space left|not enough space)'
