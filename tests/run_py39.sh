#!/bin/sh
# Run the hook tests on Python 3.9, the stock macOS interpreter.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PY="${PY39:-}"
if [ -z "$PY" ]; then
  if command -v uv >/dev/null 2>&1; then
    PY=$(uv python find 3.9)
  fi
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "python 3.9 is not installed. Install it with: uv python install 3.9" >&2
  exit 1
fi
cd "$ROOT"
"$PY" -m unittest tests.test_grace tests.test_install tests.test_detection tests.test_doctor tests.test_redaction -q
echo "python39 $($PY -c 'import sys; print("%s.%s" % sys.version_info[:2])') ok"
