import argparse
import os
import sys

from . import __version__
from .core import DEFAULT_MARKERS, PRE_MARKERS, Fail, apply, apply_pre, make_trash


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
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.cmd not in ('run', 'pre'):
        ap.print_usage(sys.stderr)
        return 2
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print('error: %s is not a folder' % root, file=sys.stderr)
        return 2
    try:
        trash = None if a.dry_run else make_trash(a.trash_dir)
        if a.cmd == 'pre':
            apply_pre(root, current=a.current, dry=a.dry_run, trash=trash, markers=tuple(a.marker) if a.marker else PRE_MARKERS)
        else:
            apply(root, dry=a.dry_run, trash=trash, markers=tuple(a.marker) if a.marker else DEFAULT_MARKERS,
                  keep_rc=a.keep_rc, zip_newest=a.zip_newest)
    except Fail as e:
        print('error: %s' % e, file=sys.stderr)
        return 1
    return 0
