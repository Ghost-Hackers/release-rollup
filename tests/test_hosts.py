"""hosts.tsv names real tests, each once, with a known host, and every test module filters by it."""
import glob
import importlib
import os
import tempfile
import unittest

import hostcat


class Catalogue(unittest.TestCase):
    def test_every_row_names_a_test_that_exists(self):
        seen = set()
        rows = hostcat.read_catalogue()
        self.assertTrue(rows)
        for line, tid, host in rows:
            with self.subTest(line=line, test=tid):
                self.assertIn(host, hostcat.HOSTS)
                self.assertNotIn(tid, seen, 'listed twice')
                seen.add(tid)
                mod, cls, meth = tid.split('.')
                case = getattr(importlib.import_module(mod), cls)
                self.assertTrue(issubclass(case, unittest.TestCase))
                self.assertTrue(meth.startswith('test') and callable(getattr(case, meth, None)), 'no such test')

    def test_every_test_module_uses_the_catalogue(self):
        for path in sorted(glob.glob(os.path.join(hostcat.HERE, 'test*.py'))):
            mod = importlib.import_module(os.path.basename(path)[:-3])
            self.assertIs(getattr(mod, 'load_tests', None), hostcat.load_tests, path)

    def test_the_other_hosts_tests_are_left_out(self):
        loader = unittest.TestLoader()
        ids = set()
        stack = [loader.discover(hostcat.HERE, top_level_dir=hostcat.HERE)]
        while stack:
            for t in stack.pop():
                if isinstance(t, unittest.TestSuite):
                    stack.append(t)
                else:
                    ids.add(t.id())
        rows = {tid: host for _, tid, host in hostcat.read_catalogue()}
        here = hostcat.this_host()
        for tid, host in rows.items():
            self.assertEqual(tid in ids, host == here, tid)
        self.assertIn('test_hosts.Catalogue.test_the_other_hosts_tests_are_left_out', ids)

    def test_a_bad_row_is_read_as_bad(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'hosts.tsv')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('# comment\n\nmod.Cls.test_a\tlinux\nmod.Cls.test_b\n')
            self.assertEqual(hostcat.read_catalogue(p), [(3, 'mod.Cls.test_a', 'linux'), (4, 'mod.Cls.test_b', None)])


load_tests = hostcat.load_tests


if __name__ == '__main__':
    unittest.main()
