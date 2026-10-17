#!/usr/bin/env bash
# Install a reviewed local wheel into a uv-managed Python environment.
# Usage: bash install.sh /absolute/claude_config-VERSION-py3-none-any.whl
set -euo pipefail
if [[ $# -ne 1 || "$1" != *.whl || ! -f "$1" ]]; then
    echo 'Usage: bash install.sh /absolute/claude_config-VERSION-py3-none-any.whl' >&2
    exit 2
fi
case "$(uname -s)" in Darwin|Linux) ;; *) echo 'Supported: macOS and Linux. Native Windows is not supported.' >&2; exit 2 ;; esac
if ! command -v uv >/dev/null 2>&1; then
    echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 2
fi
installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
uv tool install --managed-python --python 3.11 --constraints "$installer_dir/constraints.txt" "$1"
command_dir="$(uv tool dir --bin)"
"$command_dir/claude-config" --version
if ! command -v claude-config >/dev/null 2>&1; then
    echo "Add $command_dir to PATH with: uv tool update-shell"
    echo 'Then restart the shell in existing tmux panes.'
fi
echo 'Next: claude-config doctor, or claude-config init --help for a new machine.'
echo 'Software installation did not apply Profiles or change native Agent/App configuration.'
