#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print 'Create the virtual environment first; see docs/WEB-INTERFACE.md.'
  exit 1
fi
if ! .venv/bin/python -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  print 'Install web dependencies first: .venv/bin/python -m pip install -r requirements-web.txt'
  exit 1
fi
exec env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m web.launch start
