"""Which tests run on which host.

hosts.tsv lists every test that runs on one host only, as "<test id><TAB><linux|windows>". Any test not listed
runs on both and checks the behavior of the host it runs on. Each test module sets load_tests = hostcat.load_tests,
so a test meant for the other host is left out of the run (it is not a skip: it never runs here).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOGUE = os.path.join(HERE, 'hosts.tsv')
HOSTS = ('linux', 'windows')


def this_host():
    return 'windows' if os.name == 'nt' else 'linux'


def read_catalogue(path=CATALOGUE):
    """[(line number, test id, host)] for every row; blank lines and # comments are left out."""
    rows = []
    with open(path, encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            line = line.rstrip('\r\n')
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.split('\t')
            rows.append((i, parts[0], parts[1] if len(parts) == 2 else None))
    return rows


def other_host_tests():
    here = this_host()
    return {tid for _, tid, host in read_catalogue() if host != here}


def _keep(suite, drop):
    import unittest
    out = unittest.TestSuite()
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            out.addTest(_keep(t, drop))
        elif t.id() not in drop:
            out.addTest(t)
    return out


def load_tests(loader, standard_tests, pattern):
    return _keep(standard_tests, other_host_tests())
