#!/usr/bin/env bash
# Runs the unit tests and byte-compiles the package.
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
$PY -m unittest discover -s tests
$PY -m compileall -q src tests
echo "check ok"
