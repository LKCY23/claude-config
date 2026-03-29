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
    # git outputs progress info to stderr, PowerShell treats it as error
    # Redirect all output to null and check exit code
    $null = git pull --ff-only 2>&1 | Out-Null
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
# Initialize config repository
# ════════════════════════════════════════════
Write-Host ""
Write-Host "  === Setting up config directory ==="

if (-not (Test-Path $ConfigDir)) {
    Write-Host "  Creating $ConfigDir..."
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    Push-Location $ConfigDir
    git init

    # Copy templates
    Copy-Item "$ToolDir\templates\manifest.template.yaml" "$ConfigDir\manifest.yaml"
    Copy-Item "$ToolDir\templates\plugins.template.yaml" "$ConfigDir\plugins.yaml"

    # Create directory structure
    $subdirs = @(
        "assets\skills",
        "assets\memory",
        "assets\settings",
        "assets\hooks\mac",
        "assets\hooks\windows",
        "assets\claude-md",
        "scripts"
    )
    foreach ($subdir in $subdirs) {
        New-Item -ItemType Directory -Force -Path "$ConfigDir\$subdir" | Out-Null
    }

    Write-Host "  ✓ Config directory initialized" -ForegroundColor Green
    Write-Host ""
    Write-Host "  ⚠ Please edit $ConfigDir\manifest.yaml to add your skills/plugins" -ForegroundColor Yellow
} else {
    Write-Host "  ✓ Config directory exists: $ConfigDir" -ForegroundColor Green
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
Write-Host "  Quick start:"
Write-Host "    1. Add your skills/plugins to: $ConfigDir\assets\"
Write-Host "    2. Edit: $ConfigDir\manifest.yaml"
Write-Host "    3. Run: claude"
Write-Host "    4. Use: /claude-config status"
Write-Host "    5. Apply: /claude-config apply --platform windows"
Write-Host ""
Write-Host "  Update tool:    cd $ToolDir; git pull"
Write-Host "  Reinstall skill: cp $ToolDir\SKILL.md ~/.claude/skills/claude-config\"
Write-Host ""