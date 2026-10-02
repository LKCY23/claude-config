#!/usr/bin/env bash
# claude-config — One-line installer (macOS + Linux + Windows Git Bash)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/master/install.sh | bash
#   curl -fsSL ... | bash -s -- --config-dir ~/my-config
set -euo pipefail

# Default values
CONFIG_DIR="${HOME}/claude-config-data"
TOOL_REPO="https://github.com/LKCY23/claude-config.git"
TOOL_DIR="${HOME}/.claude-config-tool"
LOCAL_SOURCE=""
INSTALL_AGENT="both"
CATALOG_ROOT="${HOME}/claudespace"
DRY_RUN=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config-dir) CONFIG_DIR="$2"; shift 2 ;;
        --tool-dir) TOOL_DIR="$2"; shift 2 ;;
        --local-source) LOCAL_SOURCE="$2"; shift 2 ;;
        --agent) INSTALL_AGENT="$2"; shift 2 ;;
        --catalog-root) CATALOG_ROOT="$2"; shift 2 ;;
        --dry-run) DRY_RUN="plan"; shift ;;
        *) shift ;;
    esac
done

echo ""
echo "  ═══════════════════════════════════════════"
echo "  claude-config Installer"
echo "  ═══════════════════════════════════════════"
echo ""

# ════════════════════════════════════════════
# Platform detection (with WSL detection)
# ════════════════════════════════════════════
detect_platform() {
    case "$(uname -s)" in
        Darwin*)    echo "mac" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        Linux*)     echo "linux" ;;
        *)          echo "unknown" ;;
    esac
}

is_wsl() {
    # Check if running under WSL
    if [[ -f /proc/version ]]; then
        grep -qiE "microsoft|wsl" /proc/version 2>/dev/null && return 0
    fi
    uname -r | grep -qiE "microsoft|wsl" 2>/dev/null && return 0
    return 1
}

PLATFORM=$(detect_platform)

# Reviewed Mac deployment uses a durable local source and the native bootstrap.
# The existing other-platform path is retained, without new Codex assumptions.
if [[ "$PLATFORM" == "mac" && -n "$LOCAL_SOURCE" ]]; then
    case "$INSTALL_AGENT" in claude|codex|both) ;; *) echo "Invalid --agent" >&2; exit 1 ;; esac
    BOOTSTRAP_PYTHON=""
    for candidate_python in python3 python; do
        if command -v "$candidate_python" >/dev/null 2>&1 && "$candidate_python" -c 'import yaml, tomlkit' >/dev/null 2>&1; then
            BOOTSTRAP_PYTHON="$candidate_python"; break
        fi
    done
    if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
        echo "Use a Python environment with ${LOCAL_SOURCE}/requirements.txt installed." >&2
        exit 1
    fi
    exec "$BOOTSTRAP_PYTHON" "$LOCAL_SOURCE/scripts/bootstrap.py" "${DRY_RUN:-apply}" \
        --source "$LOCAL_SOURCE" --tool-dir "$TOOL_DIR" --config-dir "$CONFIG_DIR" \
        --catalog-root "$CATALOG_ROOT" --agent "$INSTALL_AGENT"
fi

# WSL warning for Windows users
if [[ "$PLATFORM" == "linux" ]] && is_wsl; then
    echo ""
    echo "  ═══════════════════════════════════════════"
    echo "  ⚠ WSL Environment Detected!"
    echo "  ═══════════════════════════════════════════"
    echo ""
    echo "  You are running bash under WSL (Windows Subsystem for Linux)."
    echo "  This will install to WSL paths (/root/), NOT Windows paths."
    echo ""
    echo "  If you want to install for Windows:"
    echo "    → Use PowerShell: iwr -useb https://raw.githubusercontent.com/LKCY23/claude-config/master/install.ps1 | iex"
    echo "    → Or use Git Bash (open 'Git Bash' app, not WSL bash)"
    echo ""
    echo "  Continue installing to WSL? (y/n)"
    read -r response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "  Installation cancelled."
        echo "  Run the PowerShell installer instead."
        exit 0
    fi
    echo ""
fi
echo "  Platform: $PLATFORM"
echo "  Config dir: $CONFIG_DIR"
echo "  Tool dir: $TOOL_DIR"
echo ""

# ════════════════════════════════════════════
# Check dependencies
# ════════════════════════════════════════════
check_deps() {
    local missing=""

    if ! command -v git >/dev/null 2>&1; then
        missing="$missing git"
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        missing="$missing python3"
    fi

    if [[ -n "$missing" ]]; then
        echo "  ✗ Missing dependencies:$missing"
        echo ""
        case "$PLATFORM" in
            mac)   echo "  Install with: xcode-select --install && brew install python3" ;;
            linux) echo "  Install with: sudo apt install git python3 (or your package manager)" ;;
            windows) echo "  Install Git for Windows from https://git-scm.com/download/win" ;;
        esac
        exit 1
    fi

    echo "  ✓ Dependencies OK"
}

check_deps

# ════════════════════════════════════════════
# Clone tool repository
# ════════════════════════════════════════════
echo ""
echo "  === Cloning tool repository ==="

if [[ -d "$TOOL_DIR" ]]; then
    echo "  Updating existing installation..."
    cd "$TOOL_DIR" && git pull --ff-only 2>/dev/null || {
        echo "  ⚠ Could not update, using existing version"
    }
else
    echo "  Cloning to ${TOOL_DIR}..."
    git clone --depth 1 "$TOOL_REPO" "$TOOL_DIR"
fi

# ════════════════════════════════════════════
# Create framework config (if not exists)
# ════════════════════════════════════════════
echo ""
echo "  === Setting up framework config ==="

CONFIG_FILE="${TOOL_DIR}/config.yaml"
if [[ -f "$CONFIG_FILE" ]]; then
    echo "  ✓ Config exists: $CONFIG_FILE"
else
    cp "$TOOL_DIR/templates/config.template.yaml" "$CONFIG_FILE"
    echo "  ✓ Created default config: $CONFIG_FILE"
fi

# ════════════════════════════════════════════
# Install skill
# ════════════════════════════════════════════
echo ""
echo "  === Installing claude-config skill ==="

SKILL_DIR="${HOME}/.claude/skills/claude-config"
mkdir -p "$SKILL_DIR"
cp "$TOOL_DIR/SKILL.md" "$SKILL_DIR/"
# Preserve the legacy workflow's references after the entry is made concise.
for resource in references scripts; do
    if [[ -d "$TOOL_DIR/$resource" ]]; then
        cp -R "$TOOL_DIR/$resource" "$SKILL_DIR/"
    fi
done

echo "  ✓ Skill installed to ~/.claude/skills/claude-config/"

# ════════════════════════════════════════════
# Done
# ════════════════════════════════════════════
echo ""
echo "  ═══════════════════════════════════════════"
echo "  ✓ Installation complete!"
echo "  ═══════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo ""
echo "  If you already have a config repo:"
echo "    git clone <your-repo-url> ~/claude-config-data"
echo "    → Then run /claude-config sync --apply in Claude Code"
echo ""
echo "  If you're starting fresh:"
echo "    → Run /claude-config init in Claude Code"
echo ""
echo "  Update framework: /claude-config update-self"
echo ""
