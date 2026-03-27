#!/usr/bin/env bash
set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
PLUGIN_BASE="$CLAUDE_DIR/plugins/cache/claude-hud/claude-hud"

plugin_dir="$({ ls -d "$PLUGIN_BASE"/*/ 2>/dev/null || true; } | awk -F/ '{ print $(NF-1) "\t" $0 }' | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -1 | cut -f2-)"

if [[ -z "$plugin_dir" ]]; then
  echo "claude-hud plugin not found in $PLUGIN_BASE" >&2
  exit 1
fi

if command -v bun >/dev/null 2>&1; then
  runtime="$(command -v bun)"
  source_file="${plugin_dir}src/index.ts"
elif command -v node >/dev/null 2>&1; then
  runtime="$(command -v node)"
  source_file="${plugin_dir}dist/index.js"
else
  echo "Neither bun nor node was found in PATH" >&2
  exit 1
fi

exec "$runtime" "$source_file"
