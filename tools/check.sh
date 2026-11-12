#!/usr/bin/env bash
# Runs the unit tests and byte-compiles the package.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=${PY:-python3}
$PY -m unittest discover -s tests
$PY -m compileall -q src tests
echo "check ok"
