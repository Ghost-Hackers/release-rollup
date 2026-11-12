import argparse
import os
import sys

from . import __version__
from .container import DEFAULT_MAX_SIZE, restore
from .core import DEFAULT_MARKERS, PRE_MARKERS, Fail, apply, apply_pre, make_trash, upgrade, archive


def describe(e):
    """One line for an error the code did not expect, such as a name the file system refuses (NUL, a trailing dot or space)."""
    if isinstance(e, OSError):
        what = e.strerror or str(e)
        return '%s: %s; check for a name this system cannot open (on Windows: CON, NUL, a trailing dot or space)' % (
            e.filename if e.filename is not None else 'a file', what)
    return 'a file name was refused (%s); check for a name this system cannot open (on Windows: CON, NUL, a trailing dot or space)' % e


def build_parser():
    ap = argparse.ArgumentParser(prog='release-rollup', description='Roll published releases up into zips.')
    ap.add_argument('--version', action='version', version='release-rollup ' + __version__)
    sub = ap.add_subparsers(dest='cmd')
    run = sub.add_parser('run', help='zip old releases in a releases folder')
    run.add_argument('--root', default='.', help='the releases folder (default: current directory)')
    run.add_argument('--dry-run', action='store_true', help='print the plan, change nothing')
    run.add_argument('--marker', action='append', metavar='NAME',
                     help='file a v<version>/ folder must hold to count as published; end with / for a folder. '
                          'Repeat for several. Default: ' + ', '.join(DEFAULT_MARKERS))
    run.add_argument('--trash-dir', metavar='DIR',
                     help='move replaced folders and zips here instead of using gio trash')
    run.add_argument('--keep-rc', '--keep-pre', dest='keep_rc', action='store_true',
                     help='leave alpha, beta and release candidate builds in place once their release is published')
    run.add_argument('--zip-newest', action='store_true',
                     help='also zip the newest release into v<version>.zip, so nothing stays unpacked')
    run.add_argument('--no-dedupe', action='store_true',
                     help='write plain zips instead of storing shared content once')
    pre = sub.add_parser('pre', help='zip older alpha, beta and rc builds in a prereleases folder')
    pre.add_argument('--root', default='.', help='the prereleases folder holding alpha/, beta/ and rc/ (default: current directory)')
    pre.add_argument('--current', metavar='VERSION',
                     help="the project's VERSION; the newest build of each stage of an unreleased version stays unpacked. "
                          'Default: the newest version found counts as unreleased')
    pre.add_argument('--dry-run', action='store_true', help='print the plan, change nothing')
    pre.add_argument('--marker', action='append', metavar='NAME',
                     help='file a build folder must hold to count as finished; end with / for a folder. '
                          'Repeat for several. Default: ' + ', '.join(PRE_MARKERS))
    pre.add_argument('--trash-dir', metavar='DIR',
                     help='move replaced folders and zips here instead of using gio trash')
    pre.add_argument('--no-dedupe', action='store_true',
                     help='write plain zips instead of storing shared content once')
    up = sub.add_parser('upgrade', help='turn the plain rollup zips of a releases or prereleases folder into deduped ones')
    up.add_argument('--root', default='.', help='the releases folder, or the prereleases folder holding alpha/, beta/ and rc/ (default: current directory)')
    up.add_argument('--dry-run', action='store_true', help='print what would be upgraded, change nothing')
    up.add_argument('--trash-dir', metavar='DIR',
                    help='move the old zips here instead of using gio trash')
    ar = sub.add_parser('archive', help='roll the files of a folder into one deduped archive')
    ar.add_argument('--root', default='.', help='the folder whose files are rolled up (default: current directory)')
    ar.add_argument('--out', required=True, metavar='NAME.zip',
                    help='the archive; a bare name is created inside --root. Files already in it are kept, new ones are added')
    ar.add_argument('--dry-run', action='store_true', help='print what would happen, change nothing')
    ar.add_argument('--trash-dir', metavar='DIR',
                    help='move the archived files here instead of using gio trash')
    res = sub.add_parser('restore', help='prove a rollup and write its original files back, byte for byte')
    res.add_argument('rollup', metavar='ROLLUP.zip', help='the rollup, deduped or plain')
    res.add_argument('--out', metavar='DIR', help='folder to write into; existing files are never overwritten')
    res.add_argument('--check', action='store_true', help='only prove every file, write nothing')
    res.add_argument('--max-size', type=int, default=DEFAULT_MAX_SIZE, metavar='BYTES',
                     help='refuse a rollup that unpacks to more than this. Default: %s' % format(DEFAULT_MAX_SIZE, ','))
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.cmd == 'restore':
        try:
            restore(a.rollup, out=a.out, check_only=a.check, max_size=a.max_size)
        except Fail as e:
            print('error: %s' % e, file=sys.stderr)
            return 1
        except (OSError, ValueError) as e:
            print('error: %s' % describe(e), file=sys.stderr)
            return 1
        return 0
    if a.cmd not in ('run', 'pre', 'upgrade', 'archive'):
        ap.print_usage(sys.stderr)
        return 2
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print('error: %s is not a folder' % root, file=sys.stderr)
        return 2
    try:
        trash = None if a.dry_run else make_trash(a.trash_dir)
        if a.cmd == 'archive':
            archive(root, a.out, dry=a.dry_run, trash=trash)
        elif a.cmd == 'upgrade':
            upgrade(root, dry=a.dry_run, trash=trash)
        elif a.cmd == 'pre':
            apply_pre(root, current=a.current, dry=a.dry_run, trash=trash, markers=tuple(a.marker) if a.marker else PRE_MARKERS,
                      dedupe=not a.no_dedupe)
        else:
            apply(root, dry=a.dry_run, trash=trash, markers=tuple(a.marker) if a.marker else DEFAULT_MARKERS,
                  keep_rc=a.keep_rc, zip_newest=a.zip_newest, dedupe=not a.no_dedupe)
    except Fail as e:
        print('error: %s' % e, file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print('error: %s' % describe(e), file=sys.stderr)
        return 1
    return 0
