#!/usr/bin/env bash
# Runs the unit tests and byte-compiles the package.
#
# Some tests fill a real disk and need RR_SMALLFS, a folder on a small filesystem (see tests/smallfs.py).
# CI sets it. On Linux, when it isn't set, this script runs itself again in a private mount namespace
# with a 4 MB tmpfs mounted there. No root needed, and the mount goes away with the namespace.
# Elsewhere (Git Bash on Windows) set RR_SMALLFS yourself, or those tests fail and say how.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=${PY:-python3}

# Root reads past file permissions, so the tests that lock a file or folder couldn't see the
# refusal they check for. Run as a normal user (in a container: add one and use it).
if [ "$(uname -s)" = Linux ] && [ "$(id -u)" -eq 0 ]; then
  echo "check.sh: running as root, so the file permission tests can't work. Run it as a normal user." >&2
  exit 1
fi

if [ -z "${RR_SMALLFS:-}" ] && [ "$(uname -s)" = Linux ]; then
  if ! unshare -rm true 2>/dev/null; then
    echo "check.sh: unshare -rm failed, so no small filesystem for the disk-full tests." >&2
    echo "Mount a tmpfs of about 4 MB yourself and set RR_SMALLFS to a folder on it." >&2
    exit 1
  fi
  small=$(mktemp -d "${TMPDIR:-/tmp}/rr-smallfs.XXXXXX")
  trap 'rmdir "$small"' EXIT
  # The outer namespace maps us to root, which may mount. The inner one maps us back to our own
  # user, so the tests can't read past file permissions the way root could.
  rc=0
  unshare -rm sh -c 'mount -t tmpfs -o size=4m tmpfs "$1" &&
    exec unshare --map-user="$2" --map-group="$3" env RR_SMALLFS="$1" bash "$4"' \
    sh "$small" "$(id -u)" "$(id -g)" "$PWD/tools/check.sh" || rc=$?
  exit $rc
fi

$PY -m unittest discover -s tests
$PY -m compileall -q src tests
echo "check ok"
