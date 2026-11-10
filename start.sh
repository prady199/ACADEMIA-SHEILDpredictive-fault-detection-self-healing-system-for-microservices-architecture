#!/usr/bin/env bash
# Sets up on first run, then starts Academia Shield.
set -e
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null || PY=python
if [ ! -x .venv/bin/python ]; then
  echo "First run: installing packages. This takes a minute or two."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
