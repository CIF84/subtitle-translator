#!/bin/zsh
set -eu
cd "$(dirname "$0")"
exec env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m web.launch stop
