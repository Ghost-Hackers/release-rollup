# Copied from Ghost-Hackers/ci-workflows@7340a08b2258208d0d7152966707baf4eee8ee99 (sha256 bddf7737d12cfdb6aea8979cf7ca1bf4d2bb3b0de9e1cd840c0fd3433517455e). Edit it there, not here.
"""Check that files copied from Ghost-Hackers/ci-workflows weren't edited by hand.

A copied file starts with a line naming its source commit and a sha256 of that source, commit and the rest of the
file. The sync also
writes MANIFEST, the list of every copied file, with the same kind of line. This fails when a copied file was
edited, lost its line or is missing, when the manifest was edited, or when a copy has no manifest. In the source
repo nothing is copied, so there is nothing to check.

Anyone can recompute the hash, so this catches accidental edits, not deliberate ones. `tools/sync.py --verify`, run
where both repos can be read, compares each copy with the file at the commit its line names.
"""
import hashlib
import os
import re
import sys
from pathlib import Path

SOURCE = "Ghost-Hackers/ci-workflows"
MANIFEST = ".github/ci-workflows-copy.txt"
HEADER = re.compile(
    r"^(?:# |<!-- )Copied from (?P<source>\S+)@(?P<commit>[0-9a-f]{40}) \(sha256 (?P<sha>[0-9a-f]{64})\)\. "
    r"Edit it there, not here\.(?: -->)?$"
)
SKIP = {".git", "node_modules", "__pycache__"}


def copy_hash(source, commit, body):
    """The hash in a copy line covers the source and commit it names as well as the file, so editing either fails."""
    return hashlib.sha256(f"{source}@{commit}\n{body}".encode("utf-8")).hexdigest()


def split(text):
    """Return (header match or None, body) for a file's text."""
    first, _, rest = text.partition("\n")
    match = HEADER.match(first)
    return (match, rest) if match else (None, text)


def check_file(root, rel):
    """Return a problem for one copied file, or None."""
    path = root / rel
    if not path.is_file():
        return f"{rel}: listed in {MANIFEST} but missing"
    match, body = split(path.read_text(encoding="utf-8"))
    if not match:
        return f"{rel}: the copy line is missing or damaged"
    if copy_hash(match["source"], match["commit"], body) != match["sha"]:
        return f"{rel}: edited since the copy from {match['source']}@{match['commit'][:7]}"
    return None


def headed_files(root):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or SKIP & set(path.relative_to(root).parts):
            continue
        try:
            first = path.read_text(encoding="utf-8").partition("\n")[0]
        except (UnicodeDecodeError, OSError):
            continue
        if f"Copied from {SOURCE}@" in first:
            yield path.relative_to(root).as_posix()


def check(root, is_copy=False):
    """Return (problems, number of copied files checked)."""
    manifest = root / MANIFEST
    if not manifest.is_file():
        headed = list(headed_files(root))
        if headed or is_copy:
            return [f"{MANIFEST} is missing, so the copied files can't be checked"], 0
        return [], 0
    problems = []
    problem = check_file(root, MANIFEST)
    if problem:
        return [problem], 0
    listed = [line for line in split(manifest.read_text(encoding="utf-8"))[1].splitlines() if line.strip()]
    for rel in listed:
        problem = check_file(root, rel)
        if problem:
            problems.append(problem)
    for rel in headed_files(root):
        if rel != MANIFEST and rel not in listed:
            problems.append(f"{rel}: has a copy line but isn't listed in {MANIFEST}")
    return problems, len(listed)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    # In GitHub Actions any repo other than the source is a copy and must have a manifest.
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    problems, checked = check(root, is_copy=bool(repo) and repo != SOURCE)
    for line in problems:
        print(line)
    print(f"{checked} copied file(s) checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
