#!/bin/sh
# Show herdr, Kiro, and hook status. Does not print secrets.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
export PYTHONPATH="$ROOT/prototype${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$ROOT/prototype/herdr_kiro_doctor.py"
