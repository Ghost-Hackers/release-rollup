"""Plan and apply the release rollup.

plan() reads the releases folder and returns the steps to take. apply() carries
them out, or only reports them in a dry run. Nothing in plan() touches the disk
beyond reading.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

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
# release-rollup 0.2 and later can write deduplicated rollups: README.txt plus a .rollup/ folder of
# shared content instead of v<version>/ folders. This version can't read them.
NEWER_TOP = '.rollup'
NEWER = '%s was written by release-rollup 0.2 or later (deduplicated); upgrade release-rollup to handle it'


class Fail(Exception):
    pass


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


def n(x):
    return format(x, ',')


def sha_stream(f):
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
    return h.hexdigest()


def folder_files(path):
    """arcname -> path for every file under path. Raises Fail for a subfolder that can't be read or a
    link to another folder (a symlink, or a junction on Windows): os.walk passes over both quietly,
    and their files would be missing from the rollup while the folder still went to the trash."""
    def refuse(e):
        raise Fail('cannot read %s (%s); nothing was changed' % (e.filename or path, e.strerror or type(e).__name__))

    out = {}
    for d, dirs, fs in os.walk(path, onerror=refuse):
        for sub in dirs:
            p = os.path.join(d, sub)
            if os.path.islink(p) or os.path.isjunction(p):
                raise Fail('%s links to another folder, which a rollup can\'t hold; nothing was changed' % p)
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
                with zipfile.ZipFile(p) as z:
                    tops = {i.split('/')[0] for i in z.namelist()}
            except zipfile.BadZipFile as e:
                raise Fail('%s is not a readable zip (%s); nothing was changed' % (name, e))
            if NEWER_TOP in tops:
                raise Fail(NEWER % name)
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
            with zipfile.ZipFile(zp[0]) as z:
                for i in z.infolist():
                    if i.filename.startswith('v%s/' % vs(v)) and not i.is_dir():
                        files[i.filename] = ('zip', (zp[0], i.filename))
    return files


def open_src(src):
    kind, loc = src
    if kind == 'file':
        return open(loc, 'rb'), None
    z = zipfile.ZipFile(loc[0])
    return z.open(loc[1]), z


def sha_of(src):
    f, z = open_src(src)
    try:
        return sha_stream(f)
    finally:
        f.close()
        if z:
            z.close()


def write_zip(tmp, files):
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc in sorted(files):
            info = zipfile.ZipInfo(arc, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            f, src_z = open_src(files[arc])
            try:
                with z.open(info, 'w') as w:
                    shutil.copyfileobj(f, w, 1 << 20)
            finally:
                f.close()
                if src_z:
                    src_z.close()


def verify(path, files):
    """Empty string when the zip is sound and holds every file with the same sha256, else the reason."""
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad:
                return 'corrupt member %s' % bad
            names = set(z.namelist())
            for arc, src in files.items():
                if arc not in names:
                    return 'missing %s' % arc
                with z.open(arc) as f:
                    if sha_stream(f) != sha_of(src):
                        return 'sha256 differs for %s' % arc
            extra = names - set(files)
            if extra:
                return 'unexpected entry %s' % sorted(extra)[0]
    except (zipfile.BadZipFile, OSError) as e:
        return str(e)
    return ''


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


def apply(root, dry=False, trash=None, say=print, markers=DEFAULT_MARKERS, keep_rc=False, zip_newest=False):
    """Carry out the plan. Returns the number of zip steps.

    Raises Fail when a zip does not verify; that zip's sources stay where they are.
    Once every zip is verified, rc builds of published versions go to the trash
    unless keep_rc is set. With zip_newest the newest release is zipped too,
    so no release stays unpacked.
    """
    steps = plan(root, markers, zip_newest)
    published = set() if keep_rc else set(scan(root, markers)[0])
    if not steps and not (published and rc_leftovers(root, published)):
        say('nothing to do')
        return 0
    # Every step's files are listed before anything is written, so a folder that can't be
    # listed stops the run with nothing changed.
    all_files = [member_files(s) for s in steps]
    if trash is None and not dry:
        trash = make_trash()
    for s, files in zip(steps, all_files):
        name = os.path.basename(s['target'])
        what = '%s release%s, %s files' % (n(len(s['members'])), '' if len(s['members']) == 1 else 's', n(len(files)))
        if dry:
            say('would %s %s (%s); trash %s' % ('check' if s['built'] else 'write', name, what,
                ', '.join(os.path.basename(p) for p, _ in s['sources']) or 'nothing'))
            continue
        if not s['built']:
            fd, tmp = tempfile.mkstemp(prefix='.' + name + '.', suffix='.tmp', dir=root)
            os.close(fd)
            try:
                write_zip(tmp, files)
                why = verify(tmp, files)
                if why:
                    raise Fail('%s failed the check (%s); sources kept' % (name, why))
                os.replace(tmp, s['target'])
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            say('wrote %s (%s, %s bytes)' % (name, what, n(os.path.getsize(s['target']))))
        else:
            why = verify(s['target'], files)
            if why:
                raise Fail('%s failed the check (%s); sources kept' % (name, why))
        for p, _ in s['sources']:
            trash(p)
            say('trashed %s' % os.path.relpath(p, root).replace(os.sep, '/'))
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
                with zipfile.ZipFile(p) as z:
                    tops = {i.split('/')[0] for i in z.namelist()}
            except zipfile.BadZipFile as e:
                raise Fail('%s/%s is not a readable zip (%s); nothing was changed' % (stage, name, e))
            if NEWER_TOP in tops:
                raise Fail(NEWER % ('%s/%s' % (stage, name)))
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
            with zipfile.ZipFile(zp[0]) as z:
                for i in z.infolist():
                    if i.filename.startswith(nm + '/') and not i.is_dir():
                        files[i.filename] = ('zip', (zp[0], i.filename))
    return files


def apply_pre(root, current=None, dry=False, trash=None, say=print, markers=PRE_MARKERS):
    """Carry out plan_pre(). Returns the number of zip steps. Same checks as apply():
    every zip is written to a temp file, verified against its sources and only then
    moved in place, and only then are the sources trashed."""
    steps = plan_pre(root, current, markers)
    if not steps:
        say('nothing to do')
        return 0
    all_files = [member_files_pre(s) for s in steps]
    if trash is None and not dry:
        trash = make_trash()
    for s, files in zip(steps, all_files):
        rel = os.path.relpath(s['target'], root).replace(os.sep, '/')
        d = os.path.dirname(s['target'])
        what = '%s build%s, %s files' % (n(len(s['members'])), '' if len(s['members']) == 1 else 's', n(len(files)))
        if dry:
            say('would %s %s (%s); trash %s' % ('check' if s['built'] else 'write', rel, what,
                ', '.join(os.path.relpath(p, root).replace(os.sep, '/') for p, _ in s['sources']) or 'nothing'))
            continue
        if not s['built']:
            fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(rel) + '.', suffix='.tmp', dir=d)
            os.close(fd)
            try:
                write_zip(tmp, files)
                why = verify(tmp, files)
                if why:
                    raise Fail('%s failed the check (%s); sources kept' % (rel, why))
                os.replace(tmp, s['target'])
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            say('wrote %s (%s, %s bytes)' % (rel, what, n(os.path.getsize(s['target']))))
        else:
            why = verify(s['target'], files)
            if why:
                raise Fail('%s failed the check (%s); sources kept' % (rel, why))
        for p, _ in s['sources']:
            trash(p)
            say('trashed %s' % os.path.relpath(p, root).replace(os.sep, '/'))
    return len(steps)
