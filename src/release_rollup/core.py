"""Plan and apply the release rollup.

plan() reads the releases folder and returns the steps to take. apply() carries
them out, or only reports them in a dry run. Nothing in plan() touches the disk
beyond reading.
"""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from .container import (DEFAULT_MAX_SIZE, READ_ERRORS, TOP_RE, Fail, Rollup, check_paths, n, path_problem, sha_stream,
                        windows_name_problem, write_container)

VER = r'(\d+)\.(\d+)\.(\d+)'
DIR_RE = re.compile(r'^v' + VER + r'$')
ZIP_RE = re.compile(r'^(?:v' + VER + r'|\d+\.\d+|\d+\.x|pre-1\.0\.0)\.zip$')
# A trailing slash marks a folder, anything else is a file.
DEFAULT_MARKERS = ('RELEASE_NOTES.md', 'SHA256SUMS', 'assets/')
# Pre-release builds: -alpha.N, -beta.N and -rc.N. Only the folder or zip of a published version's builds is cleaned up.
# Tester packages live in a project's prereleases/ folder, which plan_pre() and apply_pre() tidy (the pre command).
STAGES = ('alpha', 'beta', 'rc')
RC_RE = re.compile(r'^v?' + VER + r'-(?:' + '|'.join(STAGES) + r')\.\d+(?:\.zip)?$')
PRE_RE = re.compile(r'^v?' + VER + r'-(' + '|'.join(STAGES) + r')\.(\d+)(?:\.zip)?$')
STAMP = (1980, 1, 1, 0, 0, 0)
# prereleases/<stage>/ holds v<x.y.z>-<stage>.<n>/ folders and v<x.y.z>-<stage>.x.zip rollups.
PRE_DIR_RE = re.compile(r'^v' + VER + r'-(' + '|'.join(STAGES) + r')\.(\d+)$')
PRE_ZIP_RE = re.compile(r'^v' + VER + r'-(' + '|'.join(STAGES) + r')\.x\.zip$')
PRE_MARKERS = ('PRERELEASE.json', 'SHA256SUMS')


def vkey(name):
    """Sort key of a version, folder or zip name: (major, minor, patch, rank, n) with alpha 0, beta 1, rc 2 and a
    final release 3 (n 0).
    None for a name that is not a version."""
    m = PRE_RE.match(name)
    if m:
        return tuple(map(int, m.groups()[:3])) + (STAGES.index(m.group(4)), int(m.group(5)))
    m = re.match(r'^v?' + VER + r'(?:\.zip)?$', name)
    return tuple(map(int, m.groups())) + (3, 0) if m else None


def vs(t):
    return '%d.%d.%d' % t


def folder_files(path):
    out = {}
    for d, _, fs in os.walk(path):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, path).replace(os.sep, '/')] = p
    return out


def is_published(path, markers):
    for m in markers:
        p = os.path.join(path, m.rstrip('/'))
        if m.endswith('/'):
            if not os.path.isdir(p):
                return False
        elif not os.path.isfile(p):
            return False
    return True


def scan(root, markers=DEFAULT_MARKERS):
    """Return (found, zmembers).

    found maps a version tuple to {'dir': path or None, 'zips': [paths]};
    zmembers maps a zip path to the sorted versions it holds.
    """
    found, zmembers = {}, {}
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        m = DIR_RE.match(name)
        if m and os.path.isdir(p):
            if is_published(p, markers):
                found.setdefault(tuple(map(int, m.groups())), {'dir': None, 'zips': []})['dir'] = p
            continue
        if ZIP_RE.match(name) and os.path.isfile(p):
            try:
                with Rollup(p) as z:
                    tops = {i.split('/')[0] for i in z.names()}
            except Fail as e:
                raise Fail('%s is not a readable zip (%s); nothing was changed' % (name, e))
            vers = []
            for t in tops:
                m = DIR_RE.match(t)
                if not m:
                    raise Fail('%s holds an unexpected top-level entry %r' % (name, t))
                vers.append(tuple(map(int, m.groups())))
            zmembers[p] = sorted(vers)
            for v in vers:
                found.setdefault(v, {'dir': None, 'zips': []})['zips'].append(p)
    return found, zmembers


def group_of(v, newest, zip_newest=False):
    a, b, _ = v
    if v == newest:
        return ('one', v) if zip_newest else None
    if a == 0:
        if newest[0] >= 1:
            return ('pre',)
        return ('line', 0, b) if newest[1] > b else ('one', v)
    if newest[0] > a:
        return ('major', a)
    return ('line', a, b) if newest[1] > b else ('one', v)


def target_name(key, members):
    if key[0] == 'one':
        return 'v%s.zip' % vs(members[0])
    if key[0] == 'line':
        return '%d.%d.zip' % (key[1], key[2])
    if key[0] == 'major':
        return '%d.x.zip' % key[1]
    return 'pre-1.0.0.zip'


def plan(root, markers=DEFAULT_MARKERS, zip_newest=False):
    """List of steps: {'target', 'members', 'sources', 'built', 'found'}."""
    found, zmembers = scan(root, markers)
    if not found:
        return []
    newest = max(found)
    groups = {}
    for v in found:
        k = group_of(v, newest, zip_newest)
        if k:
            groups.setdefault(k, []).append(v)
    out = []
    for k, mem in sorted(groups.items(), key=lambda kv: min(kv[1])):
        mem = sorted(mem)
        target = os.path.join(root, target_name(k, mem))
        srcs = []
        for v in mem:
            for p in ([found[v]['dir']] if found[v]['dir'] else []) + found[v]['zips']:
                if p != target and p not in [s for s, _ in srcs]:
                    srcs.append((p, 'dir' if os.path.isdir(p) else 'zip'))
        done = os.path.isfile(target) and zmembers.get(target) == mem
        if done and not srcs:
            continue
        out.append({'target': target, 'members': mem, 'sources': srcs, 'built': done, 'found': found})
    return out


def host_is_windows():
    return os.name == 'nt'


def windows_findings(names):
    """[(name, reason)] for every name Windows cannot write or restore."""
    return [(a, why) for a, why in ((a, windows_name_problem(a)) for a in sorted(names)) if why]


def screen_names(groups, say, verb='roll up'):
    """groups: [(label, names)]. Windows cannot hold names such as CON, aux.txt, NUL, or a name ending in a dot or space.
    On Windows, any such name raises Fail before anything is written; elsewhere it is a warning, since the
    rollup would be refused by restore on a Windows machine. Returns how many names were flagged."""
    bad = [(label, a, why) for label, names in groups for a, why in windows_findings(names)]
    if not bad:
        return 0
    windows = host_is_windows()
    shown = '; '.join('%s: %s (%s)' % x for x in bad[:5])
    if len(bad) > 5:
        shown += '; and %s more' % n(len(bad) - 5)
    if windows and verb == 'upgrade':
        raise Fail('%s name%s Windows cannot hold, so Windows could not restore it. %s'
                   % (n(len(bad)), '' if len(bad) == 1 else 's', shown))
    if windows:
        raise Fail('cannot %s: %s name%s Windows cannot hold. %s. Rename or remove %s first. '
                   'Nothing was written, the sources are untouched'
                   % (verb, n(len(bad)), '' if len(bad) == 1 else 's', shown, 'it' if len(bad) == 1 else 'them'))
    say('warning: %s name%s Windows cannot hold, so restoring on Windows would be refused. %s'
        % (n(len(bad)), '' if len(bad) == 1 else 's', shown))
    return len(bad)


def member_files(step):
    """arcname -> (kind, location) for every member; the folder wins, else the first zip holding it."""
    files = {}
    for v in step['members']:
        e = step['found'][v]
        if e['dir']:
            for arc, p in folder_files(e['dir']).items():
                files['v%s/%s' % (vs(v), arc)] = ('file', p)
        else:
            zp = [z for z in e['zips'] if z != step['target']] or e['zips']
            with Rollup(zp[0]) as z:
                for name in z.files():
                    if name.startswith('v%s/' % vs(v)):
                        files[name] = ('zip', (zp[0], name))
    return files


def open_src(src):
    kind, loc = src
    if kind == 'file':
        return open(loc, 'rb'), None
    z = Rollup(loc[0])
    return z.open(loc[1]), z


def sha_of(src):
    f, z = open_src(src)
    try:
        return sha_stream(f)
    finally:
        f.close()
        if z:
            z.close()


def src_size(src):
    kind, loc = src
    if kind == 'file':
        return os.path.getsize(loc)
    with Rollup(loc[0]) as z:
        return z.size(loc[1])


def worth_it(plain, deduped):
    """A deduped rollup asks more of whoever opens it, so it has to win by a margin."""
    return plain - deduped >= max(1024, plain // 100)


def write_zip(tmp, files, beaten_by=None):
    """Write a plain zip. With beaten_by, stop and return False once it is clearly bigger than a file of that size."""
    with open(tmp, 'w+b') as raw, zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc in sorted(files):
            if beaten_by is not None and worth_it(raw.tell(), beaten_by):
                return False
            info = zipfile.ZipInfo(arc, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.file_size = src_size(files[arc])
            f, src_z = open_src(files[arc])
            try:
                with z.open(info, 'w') as w:
                    shutil.copyfileobj(f, w, 1 << 20)
            finally:
                f.close()
                if src_z:
                    src_z.close()
    return True


def local_copy(spool):
    """For write_container: a real, seekable file for a source, spooled out of its zip when it lives in one."""
    def get(src):
        kind, loc = src
        if kind == 'file':
            return loc, False
        fd, path = tempfile.mkstemp(prefix='src.', dir=spool)
        f, z = open_src(src)
        try:
            with os.fdopen(fd, 'wb') as w:
                shutil.copyfileobj(f, w, 1 << 20)
        finally:
            f.close()
            z.close()
        return path, True
    return get


def write_rollup(tmp, files, dedupe, label, say, top='release', force=False):
    """Write tmp as a deduped rollup, or as a plain zip when asked to or when sharing saves nothing.
    With force it is always deduped (an archive stays one format, whatever it holds).
    Returns True when it is deduped."""
    if not force and (not dedupe or not files):
        write_zip(tmp, files)
        return False
    if sum(src_size(s) for s in files.values()) > DEFAULT_MAX_SIZE:
        if force:
            raise Fail('%s would hold more than %s bytes; sources kept' % (label, n(DEFAULT_MAX_SIZE)))
        say('%s holds more than %s bytes; written as a plain zip' % (label, n(DEFAULT_MAX_SIZE)))
        write_zip(tmp, files)
        return False
    for arc in sorted(files):
        why = path_problem(arc, top)
        if why:
            raise Fail('%s cannot hold %r (%s); sources kept. --no-dedupe writes a plain zip' % (label, arc, why))
    spool = tmp + '.spool'
    try:
        shared = write_container(tmp, files, local_copy(spool), spool, top)
    finally:
        shutil.rmtree(spool, ignore_errors=True)
    if force:
        return True
    size = os.path.getsize(tmp)
    fd, plain = tempfile.mkstemp(prefix=os.path.basename(tmp) + '.plain.', suffix='.tmp', dir=os.path.dirname(tmp))
    os.close(fd)
    try:
        finished = write_zip(plain, files, size) if shared else write_zip(plain, files)
        if not shared or (finished and not worth_it(os.path.getsize(plain), size)):
            os.replace(plain, tmp)
            say('%s shares too little to dedupe; written as a plain zip' % label)
            return False
    finally:
        if os.path.exists(plain):
            os.remove(plain)
    return True


def verify(path, files):
    """Empty string when the rollup is sound and holds every file with the same sha256, else the reason."""
    try:
        with Rollup(path) as z:
            why = z.check()
            if why:
                return why
            names = set(z.names())
            for arc, src in files.items():
                if arc not in names:
                    return 'missing %s' % arc
                with z.open(arc) as f:
                    if sha_stream(f) != sha_of(src):
                        return 'sha256 differs for %s' % arc
            extra = names - set(files)
            if extra:
                return 'unexpected entry %s' % sorted(extra)[0]
    except (Fail, OSError) + READ_ERRORS as e:
        return str(e)
    return ''


def build_target(target, files, dedupe, label, what, say, top='release', force=False, before_replace=None):
    """Temp file, write, verify every file against its source, then move in place. Sources are untouched."""
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(target) + '.', suffix='.tmp', dir=os.path.dirname(target))
    os.close(fd)
    try:
        deduped = write_rollup(tmp, files, dedupe, label, say, top, force)
        why = verify(tmp, files)
        if why:
            raise Fail('%s failed the check (%s); sources kept' % (label, why))
        if before_replace:
            before_replace()
        os.replace(tmp, target)
    except OSError as e:
        raise Fail('%s could not be written (%s); sources kept' % (label, e))
    except READ_ERRORS as e:
        raise Fail('%s could not be written, a source is unreadable (%s); sources kept' % (label, e))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    say('wrote %s (%s, %s bytes%s)' % (label, what, n(os.path.getsize(target)), ', deduped' if deduped else ''))


def disk_size(path):
    if os.path.isdir(path):
        return sum(os.path.getsize(p) for p in folder_files(path).values())
    return os.path.getsize(path)


class Space:
    """What a run did to the disk: the sources and old rollups it replaced, against the rollups they became."""

    def __init__(self, say):
        self.say = say
        self.before = self.after = 0

    def report(self):
        if self.before:
            saved = self.before - self.after
            self.say('space: before %s bytes, after %s bytes, saved %s bytes (%.1f%%)' % (
                n(self.before), n(self.after), n(saved), 100.0 * saved / self.before))


def gio_trash(p):
    r = subprocess.run(['gio', 'trash', p], capture_output=True, text=True)
    if r.returncode:
        raise Fail('gio trash %s failed: %s' % (p, r.stderr.strip()))


def move_trash(trash_dir):
    """A trash function that moves things into trash_dir. Never deletes."""
    def trash(p):
        os.makedirs(trash_dir, exist_ok=True)
        dest = os.path.join(trash_dir, os.path.basename(p.rstrip('/')))
        i = 1
        while os.path.lexists(dest):
            i += 1
            dest = os.path.join(trash_dir, '%s.%d' % (os.path.basename(p.rstrip('/')), i))
        shutil.move(p, dest)
    return trash


def make_trash(trash_dir=None):
    """gio trash when it is installed and no trash_dir is given, else move into trash_dir."""
    if trash_dir:
        return move_trash(trash_dir)
    if shutil.which('gio'):
        return gio_trash
    raise Fail('gio is not installed; pass --trash-dir DIR so sources can be moved aside')


def rc_leftovers(root, published):
    """Alpha, beta and release candidate folders and zips whose release is published: [(path, version)], oldest build first."""
    out = []
    for name in sorted(os.listdir(root), key=lambda x: (vkey(x) or (), x)):
        m = RC_RE.match(name)
        p = os.path.join(root, name)
        if not m:
            continue
        if name.endswith('.zip') != os.path.isfile(p) or (not name.endswith('.zip') and not os.path.isdir(p)):
            continue
        v = tuple(map(int, m.groups()))
        if v in published:
            out.append((p, v))
    return out


def run_step(s, root, files, label, what, dedupe, trash, say, space):
    """Write or re-check one rollup, then trash its sources. Raises Fail with the sources in place."""
    target = s['target']
    before = sum(disk_size(p) for p, _ in s['sources'])
    if os.path.isfile(target):
        before += os.path.getsize(target)
    if not s['built']:
        build_target(target, files, dedupe, label, what, say)
    else:
        why = verify(target, files)
        if why:
            raise Fail('%s failed the check (%s); sources kept' % (label, why))
    for p, _ in s['sources']:
        trash(p)
        say('trashed %s' % os.path.relpath(p, root).replace(os.sep, '/'))
    space.before += before
    space.after += os.path.getsize(target)


def clean_rcs(root, published, dry, trash, say):
    """Trash the alpha, beta and rc builds of every published version. Returns how many were found."""
    items = rc_leftovers(root, published)
    for p, v in items:
        name = os.path.basename(p)
        if dry:
            say('would trash %s (release %s is published)' % (name, vs(v)))
        else:
            trash(p)
            say('trashed %s' % name)
    return len(items)


def apply(root, dry=False, trash=None, say=print, markers=DEFAULT_MARKERS, keep_rc=False, zip_newest=False, dedupe=True):
    """Carry out the plan. Returns the number of zip steps.

    Raises Fail when a zip does not verify; that zip's sources stay where they are.
    Once every zip is verified, rc builds of published versions go to the trash
    unless keep_rc is set. With zip_newest the newest release is zipped too,
    so no release stays unpacked. With dedupe off new rollups are plain zips.
    On a Windows host a name Windows cannot hold is refused before anything is written;
    elsewhere it is a warning.
    """
    steps = plan(root, markers, zip_newest)
    published = set() if keep_rc else set(scan(root, markers)[0])
    if not steps and not (published and rc_leftovers(root, published)):
        say('nothing to do')
        return 0
    all_files = [member_files(s) for s in steps]
    screen_names([(os.path.basename(s['target']), f) for s, f in zip(steps, all_files)], say)
    if trash is None and not dry:
        trash = make_trash()
    space = Space(say)
    try:
        for s, files in zip(steps, all_files):
            name = os.path.basename(s['target'])
            what = '%s release%s, %s files' % (n(len(s['members'])), '' if len(s['members']) == 1 else 's', n(len(files)))
            if dry:
                say('would %s %s (%s); trash %s' % ('check' if s['built'] else 'write', name, what,
                    ', '.join(os.path.basename(p) for p, _ in s['sources']) or 'nothing'))
                continue
            run_step(s, root, files, name, what, dedupe, trash, say, space)
    finally:
        space.report()
    clean_rcs(root, published, dry, trash, say)
    return len(steps)


# Pre-release rollup: prereleases/alpha/, beta/ and rc/.

def parse_current(ver):
    """(major, minor, patch, stage or None) of a version such as 1.2.0 or 1.2.0-beta.3 (a leading v is fine)."""
    m = re.match(r'^v?' + VER + r'(?:-(' + '|'.join(STAGES) + r')\.\d+)?$', ver or '')
    if not m:
        raise Fail('--current %r is not a version such as 1.2.0 or 1.2.0-beta.3' % ver)
    return tuple(map(int, m.groups()[:3])) + (m.group(4),)


def scan_pre(stage_dir, stage, markers=PRE_MARKERS):
    """Return (found, zmembers) for one stage folder.

    found maps a build name (v1.2.0-beta.3) to {'dir': path or None, 'zips': [paths]};
    zmembers maps a zip path to the sorted build names it holds. Folders of another
    stage, unfinished folders and other names are left alone.
    """
    found, zmembers = {}, {}
    for name in sorted(os.listdir(stage_dir)):
        p = os.path.join(stage_dir, name)
        m = PRE_DIR_RE.match(name)
        if m and os.path.isdir(p) and not os.path.islink(p):
            if m.group(4) == stage and is_published(p, markers):
                found.setdefault(name, {'dir': None, 'zips': []})['dir'] = p
            continue
        m = PRE_ZIP_RE.match(name)
        if m and m.group(4) == stage and os.path.isfile(p):
            base = tuple(map(int, m.groups()[:3]))
            try:
                with Rollup(p) as z:
                    tops = {i.split('/')[0] for i in z.names()}
            except Fail as e:
                raise Fail('%s/%s is not a readable zip (%s); nothing was changed' % (stage, name, e))
            for t in tops:
                tm = PRE_DIR_RE.match(t)
                if not tm or tm.group(4) != stage or tuple(map(int, tm.groups()[:3])) != base:
                    raise Fail('%s/%s holds an unexpected top-level entry %r' % (stage, name, t))
                found.setdefault(t, {'dir': None, 'zips': []})['zips'].append(p)
            zmembers[p] = sorted(tops, key=vkey)
    return found, zmembers


def plan_pre(root, current=None, markers=PRE_MARKERS):
    """Steps for a prereleases folder, same shape as plan() with build names as members.

    Per stage, the builds of one version go into v<x.y.z>-<stage>.x.zip. The newest
    build of each stage stays unpacked while its version is unreleased: the base of
    a pre-release current or anything later, or anything later than a final current.
    A version at or below a final current, or below a pre-release current's base,
    counts as released (or dropped) and has all its builds rolled up. Without current,
    only the newest version found counts as unreleased.
    """
    cur = parse_current(current) if current else None
    stages = {}
    for st in STAGES:
        d = os.path.join(root, st)
        if os.path.isdir(d) and not os.path.islink(d):
            stages[st] = scan_pre(d, st, markers)
    if cur:
        def open_base(b):
            return b >= cur[:3] if cur[3] else b > cur[:3]
    else:
        bases = [vkey(nm)[:3] for f, _ in stages.values() for nm in f]
        newest = max(bases) if bases else None

        def open_base(b):
            return b == newest
    out = []
    for st, (found, zmembers) in stages.items():
        by_base = {}
        for nm in found:
            by_base.setdefault(vkey(nm)[:3], []).append(nm)
        for base in sorted(by_base):
            names = sorted(by_base[base], key=vkey)
            if open_base(base) and found[names[-1]]['dir']:
                names = names[:-1]
            if not names:
                continue
            target = os.path.join(root, st, 'v%s-%s.x.zip' % (vs(base), st))
            srcs = []
            for nm in names:
                for p in ([found[nm]['dir']] if found[nm]['dir'] else []) + found[nm]['zips']:
                    if p != target and p not in [x for x, _ in srcs]:
                        srcs.append((p, 'dir' if os.path.isdir(p) else 'zip'))
            done = os.path.isfile(target) and zmembers.get(target) == names
            if done and not srcs:
                continue
            out.append({'target': target, 'members': names, 'sources': srcs, 'built': done, 'found': found})
    return out


def member_files_pre(step):
    """arcname -> (kind, location) for every build of a pre-release step; the folder wins, else a zip."""
    files = {}
    for nm in step['members']:
        e = step['found'][nm]
        if e['dir']:
            for arc, p in folder_files(e['dir']).items():
                files['%s/%s' % (nm, arc)] = ('file', p)
        else:
            zp = [z for z in e['zips'] if z != step['target']] or e['zips']
            with Rollup(zp[0]) as z:
                for name in z.files():
                    if name.startswith(nm + '/'):
                        files[name] = ('zip', (zp[0], name))
    return files


def apply_pre(root, current=None, dry=False, trash=None, say=print, markers=PRE_MARKERS, dedupe=True):
    """Carry out plan_pre(). Returns the number of zip steps. Same checks as apply():
    every zip is written to a temp file, verified against its sources and only then
    moved in place, and only then are the sources trashed."""
    steps = plan_pre(root, current, markers)
    if not steps:
        say('nothing to do')
        return 0
    all_files = [member_files_pre(s) for s in steps]
    screen_names([(os.path.relpath(s['target'], root).replace(os.sep, '/'), f) for s, f in zip(steps, all_files)], say)
    if trash is None and not dry:
        trash = make_trash()
    space = Space(say)
    try:
        for s, files in zip(steps, all_files):
            rel = os.path.relpath(s['target'], root).replace(os.sep, '/')
            what = '%s build%s, %s files' % (n(len(s['members'])), '' if len(s['members']) == 1 else 's', n(len(files)))
            if dry:
                say('would %s %s (%s); trash %s' % ('check' if s['built'] else 'write', rel, what,
                    ', '.join(os.path.relpath(p, root).replace(os.sep, '/') for p, _ in s['sources']) or 'nothing'))
                continue
            run_step(s, root, files, rel, what, dedupe, trash, say, space)
    finally:
        space.report()
    return len(steps)


# Upgrade: turn plain rollup zips into deduped ones.

def upgrade_candidates(root):
    """Every .zip directly in root and in its alpha/, beta/ and rc/ folders (a releases or a prereleases folder)."""
    out = []
    for d in [root] + [os.path.join(root, st) for st in STAGES]:
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if name.endswith('.zip') and os.path.isfile(p) and not os.path.islink(p) and not name.startswith('.'):
                out.append(p)
    return out


def rollup_shaped(names):
    """True when every file sits in a v<version>/ folder, which is what a rollup holds."""
    return bool(names) and all('/' in x and TOP_RE.fullmatch(x.split('/')[0]) for x in names)


def old_copy_path(path):
    return os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.upgrade-old')


def clear_leftover(path, trash, say):
    """A crash after the new zip was moved in leaves the old one next to it. Trash it once the new zip proves every file of it."""
    old = old_copy_path(path)
    if not os.path.lexists(old):
        return
    name = os.path.basename(path)
    try:
        with Rollup(old) as z:
            files = {nm: ('zip', (old, nm)) for nm in z.files()}
            ok = not z.deduped and not z.check()
        why = verify(path, files) if ok and files else 'the old copy is not a plain rollup'
    except (Fail, OSError) + READ_ERRORS as e:
        why = str(e)
    if why:
        say('left %s alone: %s' % (os.path.basename(old), why))
        return
    trash(old)
    say('trashed %s (left over from an interrupted upgrade of %s)' % (os.path.basename(old), name))


def upgrade_zip(path, label, dry, trash, say):
    """Returns (status, before, after). status: upgraded, deduped, kept, skipped or refused (with the reason as a 4th item).
    The old zip is read only until the new one is written next to it and every file of it proved by sha256 against the old one;
    then the new zip is moved in place in one step and the old zip goes to the trash."""
    try:
        z = Rollup(path)
    except Fail as e:
        return 'refused', 0, 0, str(e)
    with z:
        if z.deduped:
            return 'deduped', 0, 0, ''
        try:
            names = z.files()
            if not rollup_shaped(names):
                return 'skipped', 0, 0, 'not a rollup (files outside v<version>/ folders)'
            why = z.check()
        except READ_ERRORS as e:
            why = str(e)
        if why:
            return 'refused', 0, 0, why
    try:
        screen_names([(label, names)], say, 'upgrade')
    except Fail as e:
        return 'refused', 0, 0, str(e)
    before = os.path.getsize(path)
    if dry:
        say('would upgrade %s (%s files, %s bytes)' % (label, n(len(names)), n(before)))
        return 'upgraded', before, before, ''
    files = {nm: ('zip', (path, nm)) for nm in names}
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=os.path.dirname(path))
    os.close(fd)
    old = old_copy_path(path)
    try:
        if not write_rollup(tmp, files, True, label, say):
            return 'kept', before, before, ''
        why = verify(tmp, files)
        if why:
            return 'refused', 0, 0, 'the new zip failed the check (%s)' % why
        if os.path.lexists(old):
            return 'refused', 0, 0, '%s is in the way; trash it first' % os.path.basename(old)
        try:
            os.link(path, old)
        except OSError:
            shutil.copy2(path, old)
        os.replace(tmp, path)
    except Fail as e:
        return 'refused', 0, 0, str(e)
    except OSError as e:
        return 'refused', 0, 0, 'could not be written (%s)' % e
    except READ_ERRORS as e:
        return 'refused', 0, 0, 'could not be written, a file is unreadable (%s)' % e
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    try:
        trash(old)
    except (Fail, OSError) as e:
        raise Fail('%s is upgraded, but its old copy %s could not be trashed (%s)' % (label, os.path.basename(old), e))
    return 'upgraded', before, os.path.getsize(path), ''


def upgrade(root, dry=False, trash=None, say=print):
    """Turn the plain rollup zips of a releases or prereleases folder into deduped ones. Returns how many were upgraded.

    A zip that shares too little stays as it is, a zip that is already deduped is left alone, and one that does not
    prove (corrupt, or a name no rollup can hold) is reported and left untouched. Raises Fail at the end when any was refused.
    """
    zips = upgrade_candidates(root)
    if trash is None and not dry:
        trash = make_trash()
    space = Space(say)
    done, refused = 0, []
    try:
        for p in zips:
            label = os.path.relpath(p, root).replace(os.sep, '/')
            if not dry:
                clear_leftover(p, trash, say)
            status, before, after, why = upgrade_zip(p, label, dry, trash, say)
            if status == 'upgraded':
                done += 1
                if not dry:
                    say('upgraded %s: %s bytes to %s bytes' % (label, n(before), n(after)))
                    space.before += before
                    space.after += after
            elif status == 'kept':
                say('kept %s as a plain zip' % label)
            elif status == 'skipped':
                say('skipped %s: %s' % (label, why))
            elif status == 'refused':
                refused.append(label)
                say('refused %s: %s; left untouched' % (label, why))
    finally:
        space.report()
    if not done and not refused:
        say('nothing to do')
    if refused:
        raise Fail('%s not upgraded: %s' % (n(len(refused)), ', '.join(refused)))
    return done


# Archive: roll the plain files of one folder into one deduped archive.

ARCHIVE_KEEP = ('INDEX.md',)


def archive_target(root, out):
    if not out.endswith('.zip'):
        raise Fail('--out must be a .zip name, not %r' % out)
    target = os.path.join(root, out) if not os.path.dirname(out) else os.path.abspath(out)
    why = path_problem(os.path.basename(target), 'flat') or (os.path.basename(target).startswith('.') and 'hidden name')
    if why:
        raise Fail('--out %r cannot be used: %s' % (out, why))
    if os.path.lexists(target) and (os.path.islink(target) or not os.path.isfile(target)):
        raise Fail('%s is not a plain file' % target)
    return target


def archive_locals(root, target, say):
    """{name: path} of every file directly in root, except hidden files, the archive and what is not a plain file."""
    out = {}
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if name.startswith('.') or os.path.abspath(p) == os.path.abspath(target):
            continue
        if os.path.islink(p) or not os.path.isfile(p):
            if not os.path.isdir(p):
                say('skipped %s: not a plain file' % name)
            continue
        out[name] = p
    return out


def archive(root, out, dry=False, trash=None, say=print):
    """Roll every file directly in root into one deduped archive named out (inside root unless it has a folder part).

    Zips inside are walked and content shared between files is stored once. A later run adds the new files to
    the same archive. Every file is proved by sha256 against its source before the source goes to the trash;
    any failure leaves everything as it was. INDEX.md is archived too but stays where it is. Returns how many
    files were added or refreshed (0 when there was nothing to do). Raises Fail.
    """
    target = archive_target(root, out)
    label = os.path.basename(target)
    local = archive_locals(root, target, say)
    for name in local:
        why = path_problem(name, 'flat')
        if why:
            raise Fail('cannot archive %r: %s; nothing was written, the sources are untouched' % (name, why))
    files, old_names, same, add = {}, {}, [], []
    old = os.path.lexists(target)
    if old:
        with Rollup(target) as z:
            if not z.deduped or z.top != 'flat':
                raise Fail('%s is not an archive made by this command; nothing was changed' % label)
            why = z.check()
            if why:
                raise Fail('%s failed the check (%s); nothing was changed' % (label, why))
            old_names = {nm: z.manifest['files'][nm]['sha256'] for nm in z.files()}
        files = {nm: ('zip', (target, nm)) for nm in old_names}
    for name, p in local.items():
        src = ('file', p)
        if name not in old_names:
            add.append(name)
        elif sha_of(src) == old_names[name]:
            same.append(name)
            continue
        elif name in ARCHIVE_KEEP:
            add.append(name)
        else:
            raise Fail('%s differs from the copy of the same name in %s; rename it or remove one. '
                       'Nothing was changed' % (name, label))
        files[name] = src
    screen_names([(label, list(files))], say, 'archive')
    check_paths(list(files), host_is_windows(), 'flat')
    gone = [nm for nm in add + same if nm not in ARCHIVE_KEEP]
    if not add and not gone:
        say('nothing to do')
        return 0
    if dry:
        say('would %s %s (%s files); trash %s' % ('add to' if old else 'write', label, n(len(add)),
            ', '.join(gone) or 'nothing'))
        return len(add)
    if trash is None:
        trash = make_trash()
    space = Space(say)
    sources = [os.path.getsize(local[nm]) for nm in add + same]
    space.before = sum(sources) + (os.path.getsize(target) if old else 0)
    keep_old = os.path.join(os.path.dirname(target), '.' + label + '.archive-old')
    if old and os.path.lexists(keep_old):
        raise Fail('%s is in the way (an interrupted run); trash it first. Nothing was changed' % os.path.basename(keep_old))

    def link_old():
        try:
            os.link(target, keep_old)
        except OSError:
            shutil.copy2(target, keep_old)
    try:
        if add:
            build_target(target, files, True, label, '%s files' % n(len(files)), say, 'flat', True,
                         link_old if old else None)
        else:
            why = verify(target, files)
            if why:
                raise Fail('%s failed the check (%s); sources kept' % (label, why))
        if old and os.path.lexists(keep_old):
            trash(keep_old)
        for nm in gone:
            trash(local[nm])
            say('trashed %s' % nm)
        space.after = os.path.getsize(target) + sum(os.path.getsize(local[nm]) for nm in local if nm in ARCHIVE_KEEP)
        space.before += sum(os.path.getsize(local[nm]) for nm in local if nm in ARCHIVE_KEEP and nm not in add + same)
    finally:
        space.report()
    return len(add)
