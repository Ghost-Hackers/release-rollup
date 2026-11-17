"""tools/check_release.py on temp folders and temp git repositories."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from types import ModuleType

import hostcat

HERE = os.path.dirname(os.path.abspath(__file__))


def _load() -> ModuleType:
    path = os.path.join(HERE, '..', 'tools', 'check_release.py')
    spec = importlib.util.spec_from_file_location('check_release', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_release = _load()

CHANGELOG = '# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Old entry.\n\n## [{v}] - 2026-01-01\n\n- Something.\n'


def write(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def make_tree(root: str, version: str, pep: str) -> None:
    write(root, 'VERSION', version + '\n')
    write(root, 'src/release_rollup/VERSION', version + '\n')
    write(root, 'src/release_rollup/__init__.py', f"try:\n    pass\nexcept OSError:\n    __version__ = '{version}'\n")
    write(root, 'pyproject.toml', f'[project]\nname = "x"\nversion = "{pep}"\n')
    write(root, 'CHANGELOG.md', CHANGELOG.format(v=version))


class Versions(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def check(self, version: str = '0.2.0-beta.2', pep: str = '0.2.0b2', tag: str | None = None) -> list[str]:
        make_tree(self.root, version, pep)
        return check_release.check_versions(self.root, tag)

    def test_agreeing_versions_pass_for_every_stage(self) -> None:
        for version, pep in [('0.2.0-alpha.1', '0.2.0a1'), ('0.2.0-beta.12', '0.2.0b12'),
                             ('1.0.0-rc.3', '1.0.0rc3'), ('1.4.10', '1.4.10')]:
            with self.subTest(version=version):
                self.assertEqual(self.check(version, pep, tag='v' + version), [])

    def test_pep440_mapping(self) -> None:
        self.assertEqual(check_release.pep440('0.2.0-beta.2'), '0.2.0b2')
        self.assertEqual(check_release.pep440('3.1.4'), '3.1.4')
        for bad in ['0.2', 'v1.0.0', '1.0.0-beta', '1.0.0-gamma.1', '1.0.0-beta.1.2', '']:
            with self.subTest(bad=bad):
                self.assertIsNone(check_release.pep440(bad))

    def test_pyproject_must_use_the_pep440_form(self) -> None:
        for version, wrong in [('0.2.0-alpha.1', '0.2.0-alpha.1'), ('0.2.0-beta.2', '0.2.0a2'),
                               ('1.0.0-rc.1', '1.0.0b1'), ('1.0.0', '1.0.1')]:
            with self.subTest(version=version, wrong=wrong):
                problems = self.check(version, wrong)
                self.assertEqual(len(problems), 1)
                self.assertIn('pyproject.toml', problems[0])

    def test_inner_version_file_mismatch(self) -> None:
        make_tree(self.root, '0.2.0-beta.2', '0.2.0b2')
        write(self.root, 'src/release_rollup/VERSION', '0.2.0-beta.1\n')
        problems = check_release.check_versions(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn('src/release_rollup/VERSION', problems[0])

    def test_dunder_version_mismatch(self) -> None:
        make_tree(self.root, '0.2.0-beta.2', '0.2.0b2')
        write(self.root, 'src/release_rollup/__init__.py', "    __version__ = '0.1.0'\n")
        problems = check_release.check_versions(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn('__version__', problems[0])

    def test_missing_changelog_heading(self) -> None:
        make_tree(self.root, '0.2.0-beta.2', '0.2.0b2')
        write(self.root, 'CHANGELOG.md', CHANGELOG.format(v='0.2.0-beta.1'))
        problems = check_release.check_versions(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn('## [0.2.0-beta.2]', problems[0])

    def test_changelog_heading_must_match_exactly(self) -> None:
        make_tree(self.root, '0.2.0', '0.2.0')
        write(self.root, 'CHANGELOG.md', CHANGELOG.format(v='0.2.0-beta.2'))
        self.assertEqual(len(check_release.check_versions(self.root)), 1)

    def test_tag_mismatch(self) -> None:
        self.assertEqual(self.check(tag='v0.2.0-beta.2'), [])
        for tag in ['v0.2.0-beta.3', '0.2.0-beta.2', 'v0.2.0']:
            with self.subTest(tag=tag):
                problems = self.check(tag=tag)
                self.assertEqual(len(problems), 1)
                self.assertIn(tag, problems[0])

    def test_missing_files_are_reported(self) -> None:
        make_tree(self.root, '0.2.0-beta.2', '0.2.0b2')
        for rel in ['CHANGELOG.md', 'pyproject.toml', 'src/release_rollup/VERSION', 'src/release_rollup/__init__.py']:
            with self.subTest(rel=rel):
                saved = os.path.join(self.root, 'saved')
                shutil.move(os.path.join(self.root, *rel.split('/')), saved)
                self.assertTrue(check_release.check_versions(self.root))
                shutil.move(saved, os.path.join(self.root, *rel.split('/')))
        os.remove(os.path.join(self.root, 'VERSION'))
        self.assertEqual(check_release.check_versions(self.root), ['VERSION is missing'])

    def test_bad_version_string(self) -> None:
        make_tree(self.root, 'banana', '0.2.0b2')
        problems = check_release.check_versions(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn('banana', problems[0])

    def test_several_problems_are_all_listed(self) -> None:
        make_tree(self.root, '0.2.0-beta.2', '0.1.0')
        write(self.root, 'VERSION', '0.2.0-beta.2\n')
        write(self.root, 'src/release_rollup/VERSION', '0.0.1\n')
        self.assertEqual(len(check_release.check_versions(self.root, 'v9.9.9')), 3)

    def test_command_line_exit_codes(self) -> None:
        make_tree(self.root, '0.2.0-beta.2', '0.2.0b2')
        script = os.path.join(HERE, '..', 'tools', 'check_release.py')
        run = subprocess.run(['python', script, '--root', self.root, 'versions'], capture_output=True, text=True)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, 'versions ok'))
        run = subprocess.run(['python', script, '--root', self.root, 'versions', '--tag', 'v1.0.0'],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertIn('v1.0.0', run.stdout)

    def test_this_repository_passes(self) -> None:
        self.assertEqual(check_release.check_versions(os.path.join(HERE, '..')), [])


load_tests = hostcat.load_tests


if __name__ == '__main__':
    unittest.main()
