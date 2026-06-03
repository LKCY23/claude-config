#!/usr/bin/env pwsh
# claude-config — PowerShell Installer (Windows)
#
# Usage:
#   iwr -useb https://raw.githubusercontent.com/LKCY23/claude-config/master/install.ps1 | iex
#   iwr -useb ... | iex -Args '-ConfigDir', 'C:\my-config'
param(
    [string]$ConfigDir = "$env:USERPROFILE\claude-config-data",
    [string]$ToolDir = "$env:USERPROFILE\.claude-config-tool"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  ═══════════════════════════════════════════"
Write-Host "  claude-config Installer (PowerShell)"
Write-Host "  ═══════════════════════════════════════════"
Write-Host ""

Write-Host "  Platform: windows"
Write-Host "  Config dir: $ConfigDir"
Write-Host "  Tool dir: $ToolDir"
Write-Host ""

# ════════════════════════════════════════════
# Check dependencies
# ════════════════════════════════════════════
$missing = @()

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    $missing += "git"
}

if (-not (Get-Command python -ErrorAction SilentlyContinue) -and
    -not (Get-Command python3 -ErrorAction SilentlyContinue)) {
    $missing += "python"
}

if ($missing.Count -gt 0) {
    Write-Host "  ✗ Missing dependencies: $missing" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Install Git for Windows from https://git-scm.com/download/win"
    Write-Host "  Install Python from https://www.python.org/downloads/windows/"
    exit 1
}

Write-Host "  ✓ Dependencies OK" -ForegroundColor Green

# ════════════════════════════════════════════
# Clone tool repository
# ════════════════════════════════════════════
Write-Host ""
Write-Host "  === Cloning tool repository ==="

$ToolRepo = "https://github.com/LKCY23/claude-config.git"

if (Test-Path $ToolDir) {
    Write-Host "  Updating existing installation..."
    Push-Location $ToolDir
    # git outputs "From https://..." to stderr, PowerShell treats as error
    # Temporarily suppress all errors
    $prevErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    git pull --ff-only 2>&1 | Out-Null
    $ErrorActionPreference = $prevErrorAction
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✓ Updated successfully" -ForegroundColor Green
    } else {
        Write-Host "  ⚠ Could not update, using existing version" -ForegroundColor Yellow
    }
    Pop-Location
} else {
    Write-Host "  Cloning to $ToolDir..."
    git clone --depth 1 $ToolRepo $ToolDir
}

# ════════════════════════════════════════════
# Create framework config (if not exists)
# ════════════════════════════════════════════
Write-Host ""
Write-Host "  === Setting up framework config ==="

$ConfigFile = "$ToolDir\config.yaml"
if (Test-Path $ConfigFile) {
    Write-Host "  ✓ Config exists: $ConfigFile" -ForegroundColor Green
} else {
    Copy-Item "$ToolDir\templates\config.template.yaml" $ConfigFile
    Write-Host "  ✓ Created default config: $ConfigFile" -ForegroundColor Green
}

# ════════════════════════════════════════════
# Install skill
# ════════════════════════════════════════════
Write-Host ""
Write-Host "  === Installing claude-config skill ==="

$SkillDir = "$env:USERPROFILE\.claude\skills\claude-config"
New-Item -ItemType Directory -Force -Path $SkillDir | Out-Null
Copy-Item "$ToolDir\SKILL.md" $SkillDir

Write-Host "  ✓ Skill installed to ~/.claude/skills/claude-config/" -ForegroundColor Green

# ════════════════════════════════════════════
# Done
# ════════════════════════════════════════════
Write-Host ""
Write-Host "  ═══════════════════════════════════════════"
Write-Host "  ✓ Installation complete!" -ForegroundColor Green
Write-Host "  ═══════════════════════════════════════════"
Write-Host ""
Write-Host "  Next steps:"
Write-Host ""
Write-Host "  If you already have a config repo:"
Write-Host "    git clone <your-repo-url> $env:USERPROFILE\claude-config-data"
Write-Host "    -> Then run /claude-config sync --apply in Claude Code"
Write-Host ""
Write-Host "  If you're starting fresh:"
Write-Host "    -> Run /claude-config init in Claude Code"
Write-Host ""
Write-Host "  Update framework: /claude-config update-self"
Write-Host ""