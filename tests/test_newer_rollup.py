"""Rollups written by release-rollup 0.2 or later in its deduplicated layout.

That layout holds README.txt and a .rollup/ folder instead of v<version>/ folders. This version
can't read it, so run and pre refuse, name the zip and say to upgrade, and change nothing.
"""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import hostcat  # noqa: E402
from release_rollup.cli import main  # noqa: E402

UPGRADE = 'was written by release-rollup 0.2 or later (deduplicated); upgrade release-rollup to handle it'


def deduped_rollup(path, files):
    """Write path in the 0.2 deduplicated layout: README.txt, one blob per distinct content, MANIFEST.json."""
    entries, blobs = [], {}
    for name, data in sorted(files.items()):
        sha = hashlib.sha256(data).hexdigest()
        blobs[sha] = data
        entries.append({'blob': sha, 'kind': 'plain', 'path': name, 'sha256': sha, 'size': len(data)})
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README.txt', 'This rollup holds its files deduplicated.\n')
        for sha, data in blobs.items():
            z.writestr('.rollup/blobs/' + sha, data)
        z.writestr('.rollup/MANIFEST.json', json.dumps({'files': entries, 'format': 1, 'tool': 'release-rollup 0.2.0-beta.2'},
                                                         indent=0, sort_keys=True))


def release(root, name, markers):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, 'assets'))
    for m in markers:
        with open(os.path.join(d, m), 'w', encoding='utf-8') as f:
            f.write('%s %s\n' % (m, name))
    with open(os.path.join(d, 'assets', 'app.bin'), 'wb') as f:
        f.write(b'shared' * 500)


def contents(name, markers):
    out = {'%s/%s' % (name, m): ('%s %s\n' % (m, name)).encode() for m in markers}
    out['%s/assets/app.bin' % name] = b'shared' * 500
    return out


class NewerRollup(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix='rollup-newer-')
        self.addCleanup(td.cleanup)
        self.base = td.name
        self.trash = os.path.join(self.base, 'trash')
        os.makedirs(self.trash)

    def snapshot(self):
        out = {}
        for d, dirs, fs in os.walk(self.base):
            for sub in dirs:
                out[os.path.relpath(os.path.join(d, sub), self.base)] = None
            for f in fs:
                p = os.path.join(d, f)
                with open(p, 'rb') as fh:
                    out[os.path.relpath(p, self.base)] = fh.read()
        return out

    def refused(self, args, zip_name):
        before = self.snapshot()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(args)
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), 'error: %s %s\n' % (zip_name, UPGRADE))
        self.assertEqual(self.snapshot(), before)

    def test_run_names_a_deduplicated_line_zip(self):
        rel = os.path.join(self.base, 'releases')
        markers = ('RELEASE_NOTES.md', 'SHA256SUMS')
        files = {}
        for v in ('v0.1.0', 'v0.1.1'):
            files.update(contents(v, markers))
        os.makedirs(rel)
        deduped_rollup(os.path.join(rel, '0.1.zip'), files)
        for v in ('v0.2.0', 'v0.2.1'):
            release(rel, v, markers)
        self.refused(['run', '--root', rel, '--trash-dir', self.trash], '0.1.zip')
        self.refused(['run', '--root', rel, '--dry-run'], '0.1.zip')

    def test_pre_names_a_deduplicated_stage_zip(self):
        pre = os.path.join(self.base, 'prereleases')
        stage = os.path.join(pre, 'beta')
        markers = ('PRERELEASE.json', 'SHA256SUMS')
        files = {}
        for v in ('v0.3.0-beta.1', 'v0.3.0-beta.2'):
            files.update(contents(v, markers))
        os.makedirs(stage)
        deduped_rollup(os.path.join(stage, 'v0.3.0-beta.x.zip'), files)
        release(stage, 'v0.3.0-beta.3', markers)
        self.refused(['pre', '--root', pre, '--current', '0.3.0-beta.3', '--trash-dir', self.trash],
                     'beta/v0.3.0-beta.x.zip')

    def test_other_top_level_entries_keep_the_old_message(self):
        rel = os.path.join(self.base, 'releases')
        os.makedirs(rel)
        with zipfile.ZipFile(os.path.join(rel, '0.1.zip'), 'w') as z:
            z.writestr('notes.txt', 'not a release\n')
        release(rel, 'v0.2.0', ('RELEASE_NOTES.md', 'SHA256SUMS'))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--root', rel, '--dry-run']), 1)
        self.assertEqual(err.getvalue(), "error: 0.1.zip holds an unexpected top-level entry 'notes.txt'\n")


load_tests = hostcat.load_tests

if __name__ == '__main__':
    unittest.main()
