"""tools/build_release.py on temp git repositories."""
import hashlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from types import ModuleType

import hostcat

HERE = os.path.dirname(os.path.abspath(__file__))


def _load() -> ModuleType:
    path = os.path.join(HERE, '..', 'tools', 'build_release.py')
    spec = importlib.util.spec_from_file_location('build_release', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_release = _load()


class Build(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.out = os.path.join(self.root, '..', os.path.basename(self.root) + '.out')
        self.addCleanup(shutil.rmtree, self.out, True)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        self.commit({'VERSION': '1.2.3-rc.1\n', 'README.md': 'readme\n', 'src/pkg/a.py': 'a = 1\n',
                     '.github/workflows/ci.yml': 'name: CI\n'}, date='2026-03-04T05:06:07Z')

    def git(self, *args: str, date: str | None = None) -> str:
        # Throwaway repos ignore the developer's own git config (hooks, identity, signing, line endings),
        # so the tests behave the same on every machine.
        empty = os.path.join(self.root, '..', os.path.basename(self.root) + '.gitconfig')
        if not os.path.exists(empty):
            open(empty, 'w').close()
            self.addCleanup(os.remove, empty)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=empty, GIT_CONFIG_NOSYSTEM='1')
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        run = subprocess.run(['git', '-C', self.root, *args], capture_output=True, text=True, check=True, env=env)
        return run.stdout.strip()

    def commit(self, files: dict[str, str], date: str = '2026-03-04T05:06:07Z') -> None:
        for rel, text in files.items():
            path = os.path.join(self.root, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change', date=date)

    def build(self, ref: str = 'HEAD', out: str | None = None) -> zipfile.ZipFile:
        path = build_release.build(self.root, ref, out or self.out)
        z = zipfile.ZipFile(path)
        self.addCleanup(z.close)
        return z

    def refused(self, ref: str = 'HEAD') -> str:
        with self.assertRaises(build_release.BuildError) as caught:
            build_release.build(self.root, ref, self.out)
        return str(caught.exception)

    def test_zip_holds_the_committed_files_under_the_versioned_folder(self) -> None:
        z = self.build()
        self.assertEqual(z.namelist(), ['release-rollup-1.2.3-rc.1/README.md', 'release-rollup-1.2.3-rc.1/VERSION',
                                        'release-rollup-1.2.3-rc.1/src/pkg/a.py'])
        self.assertEqual(z.read('release-rollup-1.2.3-rc.1/src/pkg/a.py'), b'a = 1\n')
        self.assertIsNone(z.testzip())

    def test_github_folder_is_left_out(self) -> None:
        self.assertFalse([n for n in self.build().namelist() if '/.github/' in n])

    def test_two_builds_of_one_commit_are_byte_identical(self) -> None:
        self.build()
        other = self.out + '2'
        self.addCleanup(shutil.rmtree, other, True)
        self.build(out=other)
        for name in ('release-rollup-1.2.3-rc.1.zip', 'SHA256SUMS'):
            with open(os.path.join(self.out, name), 'rb') as a, open(os.path.join(other, name), 'rb') as b:
                self.assertEqual(a.read(), b.read(), name)

    def test_build_ignores_changes_that_are_not_committed(self) -> None:
        with open(os.path.join(self.root, 'README.md'), 'w', encoding='utf-8') as f:
            f.write('edited\n')
        with open(os.path.join(self.root, 'new.txt'), 'w', encoding='utf-8') as f:
            f.write('new\n')
        z = self.build()
        self.assertEqual(z.read('release-rollup-1.2.3-rc.1/README.md'), b'readme\n')
        self.assertNotIn('release-rollup-1.2.3-rc.1/new.txt', z.namelist())

    def test_an_older_ref_builds_that_commit_with_its_own_version(self) -> None:
        self.git('tag', 'v1.2.3-rc.1')
        self.commit({'VERSION': '1.2.3\n', 'later.txt': 'later\n'})
        z = self.build('v1.2.3-rc.1')
        self.assertNotIn('release-rollup-1.2.3-rc.1/later.txt', z.namelist())
        self.assertTrue(os.path.exists(os.path.join(self.out, 'release-rollup-1.2.3-rc.1.zip')))

    def test_every_entry_gets_the_commit_date_in_utc(self) -> None:
        # The commit is at 05:06:07. Zip dates count in steps of 2 seconds, so it's stored as 05:06:06.
        self.assertEqual({i.date_time for i in self.build().infolist()}, {(2026, 3, 4, 5, 6, 6)})

    def test_a_commit_older_than_1980_gets_the_earliest_zip_date(self) -> None:
        self.commit({'README.md': 'old\n'}, date='1975-01-01T00:00:00Z')
        self.assertEqual({i.date_time for i in self.build().infolist()}, {(1980, 1, 1, 0, 0, 0)})

    def test_permissions_come_from_git(self) -> None:
        self.commit({'tools/run.sh': '#!/bin/sh\n'})
        self.git('update-index', '--chmod=+x', 'tools/run.sh')
        self.git('commit', '-q', '-m', 'exec')
        modes = {i.filename.split('/', 1)[1]: i.external_attr >> 16 for i in self.build().infolist()}
        self.assertEqual(modes['tools/run.sh'], 0o100755)
        self.assertEqual(modes['README.md'], 0o100644)

    def test_sha256sums_matches_the_zip_and_names_it_without_a_folder(self) -> None:
        self.build()
        with open(os.path.join(self.out, 'release-rollup-1.2.3-rc.1.zip'), 'rb') as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(self.out, 'SHA256SUMS'), encoding='utf-8') as f:
            self.assertEqual(f.read(), f'{digest}  release-rollup-1.2.3-rc.1.zip\n')

    def test_names_with_spaces_and_non_ascii_letters_keep_their_spelling(self) -> None:
        self.commit({'docs/notes and tips.md': 'x\n', 'docs/café.md': 'y\n'})
        z = self.build()
        self.assertEqual(z.read('release-rollup-1.2.3-rc.1/docs/café.md'), b'y\n')
        self.assertIn('release-rollup-1.2.3-rc.1/docs/notes and tips.md', z.namelist())

    def test_an_existing_zip_or_sums_file_is_not_overwritten(self) -> None:
        for name in ('release-rollup-1.2.3-rc.1.zip', 'SHA256SUMS'):
            with self.subTest(name=name):
                os.makedirs(self.out, exist_ok=True)
                path = os.path.join(self.out, name)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('keep\n')
                self.assertIn('already exists', self.refused())
                with open(path, encoding='utf-8') as f:
                    self.assertEqual(f.read(), 'keep\n')
                os.remove(path)

    def test_a_symlink_is_refused(self) -> None:
        # A link's blob holds its target; any blob will do for git to store it as a link.
        self.git('update-index', '--add', '--cacheinfo', f"120000,{self.git('rev-parse', 'HEAD:README.md')},link")
        self.git('commit', '-q', '-m', 'link')
        self.assertIn('link: a release holds plain files only', self.refused())
        self.assertFalse(os.path.exists(os.path.join(self.out, 'release-rollup-1.2.3-rc.1.zip')))

    def test_a_failure_while_writing_leaves_no_zip_behind(self) -> None:
        # A damaged repository: the file list still reads, but one file's content is gone.
        self.commit({'zz-last.txt': 'only this file has this text\n'})
        blob = self.git('rev-parse', 'HEAD:zz-last.txt')
        obj = os.path.join(self.root, '.git', 'objects', blob[:2], blob[2:])
        os.chmod(obj, stat.S_IWRITE)  # git stores objects read-only, and Windows won't delete those
        os.remove(obj)
        self.assertIn('failed', self.refused())
        self.assertFalse(os.path.exists(os.path.join(self.out, 'release-rollup-1.2.3-rc.1.zip')))

    def test_a_submodule_is_refused(self) -> None:
        head = self.git('rev-parse', 'HEAD')
        self.git('update-index', '--add', '--cacheinfo', f'160000,{head},vendor/lib')
        self.git('commit', '-q', '-m', 'sub')
        self.assertIn('vendor/lib: a release holds plain files only, this is a commit', self.refused())

    def test_an_empty_version_is_refused(self) -> None:
        self.commit({'VERSION': '\n'})
        self.assertIn('VERSION is empty', self.refused())

    def test_a_missing_version_file_or_unknown_ref_is_refused(self) -> None:
        self.assertIn('failed', self.refused('no-such-ref'))
        self.git('rm', '-q', 'VERSION')
        self.git('commit', '-q', '-m', 'drop')
        self.assertIn('failed', self.refused())

    def test_command_line_prints_the_sum_and_returns_0(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = build_release.main(['--root', self.root, '--out', self.out])
        self.assertEqual(code, 0)
        self.assertIn('release-rollup-1.2.3-rc.1.zip', out.getvalue())

    def test_command_line_reports_a_refusal_and_returns_1(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = build_release.main(['--root', self.root, '--out', self.out, '--ref', 'no-such-ref'])
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().startswith('build_release: git '))


load_tests = hostcat.load_tests

if __name__ == '__main__':
    unittest.main()
