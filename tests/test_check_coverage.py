"""Tests for tools/check_coverage.py on small hand-written reports."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import check_coverage  # noqa: E402
import hostcat  # noqa: E402

load_tests = hostcat.load_tests

SOURCE = 'def f(x):\n    if x:\n        return 1\n    return 2\n'
HEADER = '# file\twhere\thost\ttext\tproof\n'


class Check(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix='rollup-cov-')
        self.addCleanup(td.cleanup)
        self.root = td.name
        os.makedirs(os.path.join(self.root, 'pkg'))
        with open(os.path.join(self.root, 'pkg', 'm.py'), 'w') as f:
            f.write(SOURCE)

    def report(self, missing: list[int], missing_branches: list[list[int]]) -> str:
        """Line 1 and 2 always run, 3 and 4 run unless missing; branches 2->3 and 2->4."""
        lines = [1, 2, 3, 4]
        branches = [[2, 3], [2, 4]]
        data = {
            'meta': {'format': 3, 'version': '7.17.3', 'branch_coverage': True},
            'files': {'pkg/m.py': {
                'executed_lines': [n for n in lines if n not in missing],
                'missing_lines': missing,
                'excluded_lines': [],
                'executed_branches': [b for b in branches if b not in missing_branches],
                'missing_branches': missing_branches,
            }},
            'totals': {'num_statements': 4, 'covered_lines': 4 - len(missing),
                       'num_branches': 2, 'covered_branches': 2 - len(missing_branches)},
        }
        path = os.path.join(self.root, 'cov.json')
        with open(path, 'w') as f:
            json.dump(data, f)
        return path

    def exclusions(self, *rows: str) -> str:
        path = os.path.join(self.root, 'excl.tsv')
        with open(path, 'w') as f:
            f.write(HEADER + ''.join(r + '\n' for r in rows))
        return path

    def run_check(self, report: str, excl: str, host: str = 'linux') -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = check_coverage.main([report, '--exclusions', excl, '--root', self.root, '--host', host])
        return rc, out.getvalue(), err.getvalue()

    def test_full_coverage_passes(self):
        rc, out, err = self.run_check(self.report([], []), self.exclusions())
        self.assertEqual((rc, err), (0, ''))
        self.assertIn('6 of 6 lines and branches (100.00%)', out)

    def test_listed_miss_passes_and_covers_the_branch_into_it(self):
        excl = self.exclusions('pkg/m.py\t3\tall\treturn 1\tno caller passes a true x')
        rc, out, err = self.run_check(self.report([3], [[2, 3]]), excl)
        self.assertEqual((rc, err), (0, ''))
        self.assertIn('4 of 4 lines and branches (100.00%)', out)
        self.assertIn('1 lines and 1 branches excluded', out)

    def test_unlisted_miss_fails(self):
        rc, out, err = self.run_check(self.report([3], [[2, 3]]), self.exclusions())
        self.assertEqual(rc, 1)
        self.assertIn('pkg/m.py:3 is not covered on linux and not in the exclusions', err)
        self.assertIn('pkg/m.py:2->3 is not covered on linux', err)
        self.assertIn('(66.67%)', out)
        self.assertIn('below 100%', err)

    def test_unlisted_branch_fails_and_a_listed_branch_passes(self):
        report = self.report([], [[2, 4]])
        rc, _, err = self.run_check(report, self.exclusions())
        self.assertEqual(rc, 1)
        self.assertIn('pkg/m.py:2->4 is not covered', err)
        rc, _, err = self.run_check(report, self.exclusions('pkg/m.py\t2->4\tall\tif x:\tx is always true'))
        self.assertEqual((rc, err), (0, ''))

    def test_listed_but_reached_fails(self):
        excl = self.exclusions('pkg/m.py\t3\tall\treturn 1\tproof', 'pkg/m.py\t2->4\tall\tif x:\tproof')
        rc, _, err = self.run_check(self.report([], []), excl)
        self.assertEqual(rc, 1)
        self.assertIn('pkg/m.py:3 is listed but reached: write the test, drop the entry', err)
        self.assertIn('pkg/m.py:2->4 is listed but reached', err)

    def test_changed_source_text_fails(self):
        excl = self.exclusions('pkg/m.py\t3\tall\treturn 0\tproof')
        rc, _, err = self.run_check(self.report([3], [[2, 3]]), excl)
        self.assertEqual(rc, 1)
        self.assertIn("pkg/m.py:3 reads 'return 1' now, the list says 'return 0'", err)

    def test_row_for_the_other_host_only_checks_its_text(self):
        excl = self.exclusions('pkg/m.py\t3\twindows\treturn 1\tproof')
        rc, _, err = self.run_check(self.report([], []), excl)
        self.assertEqual((rc, err), (0, ''))
        rc, _, err = self.run_check(self.report([3], [[2, 3]]), excl)
        self.assertEqual(rc, 1)
        self.assertIn('pkg/m.py:3 is not covered on linux', err)
        rc, _, err = self.run_check(self.report([3], [[2, 3]]), excl, 'windows')
        self.assertEqual((rc, err), (0, ''))
        excl = self.exclusions('pkg/m.py\t3\twindows\treturn 9\tproof')
        rc, _, err = self.run_check(self.report([], []), excl)
        self.assertIn('the code moved', err)

    def test_line_past_the_end_or_not_a_statement_fails(self):
        rc, _, err = self.run_check(self.report([], []), self.exclusions('pkg/m.py\t9\tall\tx\tproof'))
        self.assertEqual(rc, 1)
        self.assertIn('pkg/m.py:9 is not in the source', err)
        with open(os.path.join(self.root, 'pkg', 'm.py'), 'a') as f:
            f.write('# note\n')
        rc, _, err = self.run_check(self.report([], []), self.exclusions('pkg/m.py\t5\tall\t# note\tproof'))
        self.assertIn('pkg/m.py:5 is not a statement coverage measures', err)

    def test_bad_rows_fail_with_the_line(self):
        report = self.report([], [])
        for row, want in (('pkg/m.py\t3\tall\treturn 1', 'expected 5 tab-separated fields'),
                          ('pkg/m.py\tthree\tall\treturn 1\tp', "bad line or branch 'three'"),
                          ('pkg/m.py\t3\tmac\treturn 1\tp', "host must be one of all, linux, windows, not 'mac'"),
                          ('pkg/m.py\t3\tall\treturn 1\t ', 'no proof given')):
            rc, _, err = self.run_check(report, self.exclusions(row))
            self.assertEqual(rc, 1)
            self.assertIn('excl.tsv:2: ' + want, err)
        row = 'pkg/m.py\t3\tall\treturn 1\tp'
        rc, _, err = self.run_check(self.report([3], [[2, 3]]), self.exclusions(row, row))
        self.assertIn('excl.tsv:3: listed twice', err)

    def test_counts_use_thousands_separators(self):
        report = self.report([], [])
        with open(report) as f:
            data = json.load(f)
        data['totals'].update(num_statements=12345, covered_lines=12345)
        with open(report, 'w') as f:
            json.dump(data, f)
        _, out, _ = self.run_check(report, self.exclusions())
        self.assertIn('12,347 of 12,347', out)
        self.assertIn('12,345 lines', out)

    def test_report_without_branch_coverage_is_refused(self):
        report = self.report([], [])
        with open(report) as f:
            data = json.load(f)
        data['meta']['branch_coverage'] = False
        with open(report, 'w') as f:
            json.dump(data, f)
        rc, _, err = self.run_check(report, self.exclusions())
        self.assertEqual(rc, 1)
        self.assertIn('without branch coverage', err)


if __name__ == '__main__':
    unittest.main()
