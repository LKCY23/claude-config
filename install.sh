#!/usr/bin/env bash
# claude-config — One-line installer (macOS + Linux + Windows Git Bash)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/main/install.sh | bash
#   curl -fsSL ... | bash -s -- --config-dir ~/my-config
set -euo pipefail

# Default values
CONFIG_DIR="${HOME}/claude-config-data"
TOOL_REPO="https://github.com/LKCY23/claude-config.git"
TOOL_DIR="${HOME}/.claude-config-tool"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config-dir) CONFIG_DIR="$2"; shift 2 ;;
        --tool-dir) TOOL_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

echo ""
echo "  ═══════════════════════════════════════════"
echo "  claude-config Installer"
echo "  ═══════════════════════════════════════════"
echo ""

# ════════════════════════════════════════════
# Platform detection
# ════════════════════════════════════════════
detect_platform() {
    case "$(uname -s)" in
        Darwin*)    echo "mac" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        Linux*)     echo "linux" ;;
        *)          echo "unknown" ;;
    esac
}

PLATFORM=$(detect_platform)
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
# Initialize config repository
# ════════════════════════════════════════════
echo ""
echo "  === Setting up config directory ==="

if [[ ! -d "$CONFIG_DIR" ]]; then
    echo "  Creating ${CONFIG_DIR}..."
    mkdir -p "$CONFIG_DIR"
    cd "$CONFIG_DIR"
    git init

    # Copy templates
    cp "$TOOL_DIR/templates/manifest.template.yaml" "$CONFIG_DIR/manifest.yaml"
    cp "$TOOL_DIR/templates/plugins.template.yaml" "$CONFIG_DIR/plugins.yaml"

    # Create directory structure
    mkdir -p "$CONFIG_DIR/assets/"{skills,memory,settings,hooks/mac,hooks/windows,claude-md}

    echo "  ✓ Config directory initialized"
    echo ""
    echo "  ⚠ Please edit $CONFIG_DIR/manifest.yaml to add your skills/plugins"
else
    echo "  ✓ Config directory exists: $CONFIG_DIR"
fi

# ════════════════════════════════════════════
# Install skill
# ════════════════════════════════════════════
echo ""
echo "  === Installing claude-config skill ==="

SKILL_DIR="${HOME}/.claude/skills/claude-config"
mkdir -p "$SKILL_DIR"
cp "$TOOL_DIR/SKILL.md" "$SKILL_DIR/"

echo "  ✓ Skill installed to ~/.claude/skills/claude-config/"

# ════════════════════════════════════════════
# Done
# ════════════════════════════════════════════
echo ""
echo "  ═══════════════════════════════════════════"
echo "  ✓ Installation complete!"
echo "  ═══════════════════════════════════════════"
echo ""
echo "  Quick start:"
echo "    1. Add your skills/plugins to: $CONFIG_DIR/assets/"
echo "    2. Edit: $CONFIG_DIR/manifest.yaml"
echo "    3. Run: claude"
echo "    4. Use: /claude-config status"
echo "    5. Apply: /claude-config apply --config-dir $CONFIG_DIR"
echo ""
echo "  Update tool:    cd $TOOL_DIR && git pull"
echo "  Reinstall skill: cp $TOOL_DIR/SKILL.md ~/.claude/skills/claude-config/"
echo ""