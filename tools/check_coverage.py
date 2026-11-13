#!/usr/bin/env python3
"""Check a coverage.py JSON report against tools/coverage_exclusions.tsv.

Usage: check_coverage.py COVERAGE_JSON [--exclusions FILE] [--root DIR] [--host linux|windows]

Fails when:
  - a line or branch is missed on this host and the list doesn't name it,
  - a listed line or branch did run (write the test, drop the entry),
  - a listed line's text no longer matches the source (the code moved; fix the row),
  - the total after exclusions is below 100%.
A missed branch that jumps to a listed missed line needs no row of its own.
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, cast

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXCLUSIONS = os.path.join(HERE, 'coverage_exclusions.tsv')
HOSTS = ('all', 'linux', 'windows')

Branch = tuple[int, int]


def this_host() -> str:
    return 'windows' if os.name == 'nt' else 'linux'


@dataclass(frozen=True)
class Row:
    lineno: int  # line in the exclusions file
    path: str
    line: int
    to: int | None  # None for a line row, the target line for a branch row
    host: str
    text: str
    proof: str

    @property
    def where(self) -> str:
        spot = f'{self.line}' if self.to is None else f'{self.line}->{self.to}'
        return f'{self.path}:{spot}'


class BadList(Exception):
    pass


def parse_where(where: str) -> tuple[int, int | None]:
    if '->' in where:
        a, b = where.split('->', 1)
        return int(a), int(b)
    return int(where), None


def read_exclusions(path: str) -> list[Row]:
    rows: list[Row] = []
    seen: set[tuple[str, int, int | None, str]] = set()
    with open(path, encoding='utf-8') as f:
        for i, raw in enumerate(f, 1):
            raw = raw.rstrip('\r\n')
            if not raw.strip() or raw.startswith('#'):
                continue
            parts = raw.split('\t')
            if len(parts) != 5:
                raise BadList(f'{path}:{i}: expected 5 tab-separated fields, found {len(parts)}')
            file, where, host, text, proof = parts
            try:
                line, to = parse_where(where)
            except ValueError:
                raise BadList(f'{path}:{i}: bad line or branch {where!r}') from None
            if host not in HOSTS:
                raise BadList(f'{path}:{i}: host must be one of {", ".join(HOSTS)}, not {host!r}')
            if not proof.strip():
                raise BadList(f'{path}:{i}: no proof given')
            key = (file, line, to, host)
            if key in seen:
                raise BadList(f'{path}:{i}: listed twice')
            seen.add(key)
            rows.append(Row(i, file, line, to, host, text, proof))
    return rows


@dataclass
class FileData:
    executed: set[int]
    missing: set[int]
    missing_branches: set[Branch]
    executed_branches: set[Branch]


def _ints(v: Any) -> set[int]:
    return {int(x) for x in cast(list[Any], v)}


def _branches(v: Any) -> set[Branch]:
    return {(int(a), int(b)) for a, b in cast(list[list[Any]], v)}


def read_report(path: str) -> tuple[dict[str, FileData], dict[str, int]]:
    with open(path, encoding='utf-8') as f:
        data = cast(dict[str, Any], json.load(f))
    if not data.get('meta', {}).get('branch_coverage'):
        raise BadList(f'{path}: measured without branch coverage; run with branch = true')
    files: dict[str, FileData] = {}
    for name, entry in cast(dict[str, dict[str, Any]], data['files']).items():
        files[name.replace('\\', '/')] = FileData(
            _ints(entry['executed_lines']), _ints(entry['missing_lines']),
            _branches(entry['missing_branches']), _branches(entry['executed_branches']))
    totals = {k: int(v) for k, v in cast(dict[str, Any], data['totals']).items()
              if k in ('num_statements', 'covered_lines', 'num_branches', 'covered_branches')}
    return files, totals


def source_line(root: str, path: str, line: int, cache: dict[str, list[str]]) -> str | None:
    if path not in cache:
        try:
            with open(os.path.join(root, path), encoding='utf-8') as f:
                cache[path] = f.read().splitlines()
        except OSError:
            cache[path] = []
    lines = cache[path]
    return lines[line - 1].strip() if 0 < line <= len(lines) else None


def check(report: str, exclusions: str, root: str, host: str) -> tuple[list[str], str]:
    """(problems, summary line)."""
    files, totals = read_report(report)
    rows = read_exclusions(exclusions)
    problems: list[str] = []
    cache: dict[str, list[str]] = {}

    for r in rows:
        got = source_line(root, r.path, r.line, cache)
        if got is None:
            problems.append(f'exclusions:{r.lineno}: {r.where} is not in the source')
        elif got != r.text:
            problems.append(f'exclusions:{r.lineno}: {r.where} reads {got!r} now, the list says {r.text!r}; '
                            'the code moved, fix the row')

    mine = [r for r in rows if r.host in ('all', host)]
    lines: dict[str, set[int]] = {}
    branches: dict[str, set[Branch]] = {}
    for r in mine:
        fd = files.get(r.path)
        if fd is None:
            problems.append(f'exclusions:{r.lineno}: {r.path} was not measured')
            continue
        if r.to is None:
            lines.setdefault(r.path, set()).add(r.line)
            if r.line in fd.executed:
                problems.append(f'{r.where} is listed but reached: write the test, drop the entry')
            elif r.line not in fd.missing:
                problems.append(f'exclusions:{r.lineno}: {r.where} is not a statement coverage measures')
        else:
            b = (r.line, r.to)
            branches.setdefault(r.path, set()).add(b)
            if b in fd.executed_branches:
                problems.append(f'{r.where} is listed but reached: write the test, drop the entry')
            elif b not in fd.missing_branches:
                problems.append(f'exclusions:{r.lineno}: {r.where} is not a branch coverage measures')

    excl_lines = excl_branches = 0
    for name in sorted(files):
        fd = files[name]
        listed = lines.get(name, set())
        listed_b = branches.get(name, set())
        for n in sorted(fd.missing - listed):
            problems.append(f'{name}:{n} is not covered on {host} and not in the exclusions')
        excl_lines += len(fd.missing & listed)
        for b in sorted(fd.missing_branches):
            if b in listed_b or b[1] in listed:
                excl_branches += 1
            else:
                problems.append(f'{name}:{b[0]}->{b[1]} is not covered on {host} and not in the exclusions')

    stmts, branch_n = totals['num_statements'], totals['num_branches']
    need = stmts - excl_lines + branch_n - excl_branches
    have = totals['covered_lines'] + totals['covered_branches']
    pct = 100.0 if need == 0 else 100.0 * have / need
    if have < need:
        problems.append(f'coverage after exclusions is {pct:.2f}%, below 100%')
    summary = (f'coverage on {host}: {have:,} of {need:,} lines and branches ({pct:.2f}%); '
               f'{stmts:,} lines, {branch_n:,} branches, '
               f'{excl_lines:,} lines and {excl_branches:,} branches excluded')
    return problems, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Check coverage JSON against the exclusions list.')
    ap.add_argument('report', help='output of "coverage json"')
    ap.add_argument('--exclusions', default=EXCLUSIONS)
    ap.add_argument('--root', default=ROOT, help='folder the report paths are relative to')
    ap.add_argument('--host', choices=('linux', 'windows'), default=this_host())
    a = ap.parse_args(argv)
    try:
        problems, summary = check(a.report, a.exclusions, a.root, a.host)
    except (OSError, ValueError, KeyError, TypeError, BadList) as e:
        print(f'check_coverage: {e}', file=sys.stderr)
        return 1
    for p in problems:
        print(f'check_coverage: {p}', file=sys.stderr)
    print(summary)
    if problems:
        print(f'check_coverage: {len(problems):,} problem(s)', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
