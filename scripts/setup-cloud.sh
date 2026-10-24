#!/usr/bin/env bash
# Development-only setup for Codex Cloud; safe to invoke from another directory.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -L .venv ]]; then
    echo 'Refusing to install into a symlinked .venv.' >&2
    exit 1
fi
if [[ ! -e .venv ]]; then
    python3 -c 'import sys; assert sys.version_info >= (3, 11), "Select Python 3.11 or later in the cloud environment"'
    python3 -m venv .venv
fi
.venv/bin/python - <<'PY'
import sys
from pathlib import Path
assert sys.version_info >= (3, 11), 'Existing .venv requires Python 3.11+'
assert sys.prefix != sys.base_prefix, 'Expected a virtual environment'
assert Path(sys.prefix).resolve() == (Path.cwd() / '.venv').resolve(), 'Unexpected environment location'
PY

# uv-created virtual environments may not include pip yet.
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -e . -c constraints.txt
.venv/bin/claude-config --version
printf '%s\n' 'Ready. Run tests with: .venv/bin/python -m unittest discover -s tests'
