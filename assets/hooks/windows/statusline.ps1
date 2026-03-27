#!/usr/bin/env pwsh
# claude-hud statusline for Windows PowerShell
# Equivalent to statusline.sh for Mac/Linux

$CLAUDE_DIR = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$HOME/.claude" }
$PLUGIN_BASE = "$CLAUDE_DIR/plugins/cache/claude-hud/claude-hud"

# Find the latest version directory
if (Test-Path $PLUGIN_BASE) {
    $versions = Get-ChildItem -Path $PLUGIN_BASE -Directory | Sort-Object { [version]$_.Name } -ErrorAction SilentlyContinue
    if ($versions) {
        $pluginDir = $versions[-1].FullName
    }
}

if (-not $pluginDir) {
    Write-Error "claude-hud plugin not found in $PLUGIN_BASE"
    exit 1
}

# Check for bun or node
if (Get-Command bun -ErrorAction SilentlyContinue) {
    $runtime = "bun"
    $sourceFile = Join-Path $pluginDir "src/index.ts"
} elseif (Get-Command node -ErrorAction SilentlyContinue) {
    $runtime = "node"
    $sourceFile = Join-Path $pluginDir "dist/index.js"
} else {
    Write-Error "Neither bun nor node was found in PATH"
    exit 1
}

# Execute the statusline
& $runtime $sourceFile