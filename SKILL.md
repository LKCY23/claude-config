---
name: claude-config
description: Manage Claude Code configuration across machines. Use for applying, tracking, exporting, diffing, merging, and validating config manifests.
argument-hint: <init|apply|track|diff|merge|export|validate|status|add-skill|check-updates|update-skill|push-skill|add-tool|sync-upstream> [--platform <mac|windows|linux>] [--config-dir <path>]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion
---

# claude-config

跨机器管理 Claude Code 配置的 skill。支持双向流转（Mac ↔ Windows ↔ Linux）。

## 全局参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--platform <name>` | 指定平台 (mac/windows/linux) | 自动检测 |
| `--config-dir <path>` | 配置仓库路径 | 见下方解析链 |

### config_dir 解析链

每个命令首先确定配置目录，按以下优先级：

```
1. --config-dir 参数                （命令行覆盖，最高优先级）
2. ~/.claude-config-tool/config.yaml 的 config_dir 字段
3. ~/claude-config-data              （硬编码默认值，始终有效）
```

实现逻辑（每个命令开头执行）：

```bash
# 解析 config_dir
if [ -n "$ARG_CONFIG_DIR" ]; then
  CONFIG_DIR="$ARG_CONFIG_DIR"
elif [ -f "$HOME/.claude-config-tool/config.yaml" ]; then
  CONFIG_DIR=$(grep "^config_dir:" "$HOME/.claude-config-tool/config.yaml" | sed 's/^config_dir: *//' | sed "s|^~|$HOME|")
  CONFIG_DIR=${CONFIG_DIR:-"$HOME/claude-config-data"}
else
  CONFIG_DIR="$HOME/claude-config-data"
fi
```

> `config.yaml` 由 install.sh 自动创建。`update-self`（git pull）不会覆盖它（已加入 .gitignore）。用户可手动编辑修改路径。

## 命令概览

| 命令 | 功能 | 说明 |
|------|------|------|
| `init` | 初始化配置仓库 | 状态驱动，检测本地/远程后引导 |
| `apply` | 安装配置到当前机器 | 纯本地，三阶段执行，不做 git 操作 |
| `sync` | 同步配置仓库 | 多设备协同，处理分叉/超前/落后 |
| `remote` | 管理远程关联 | add / remove / status |
| `update-self` | 更新框架自身 | 升级 ~/.claude-config-tool/ |
| `diff` | 生成差异报告 | 对比本地与清单 |
| `merge` | 交互式合并 | 逐项解决冲突 |
| `track` | 检测新配置 | 发现未追踪项 |
| `export` | 导出配置到清单 | 双向流转支持 |
| `validate` | 验证清单完整性 | 检查文件存在性 |
| `status` | 当前同步状态 | 含本地/远程同步状态 |
| `add-skill` | 添加 skill | 从本地或仓库添加 |
| `check-updates` | 检查更新 | 检查上游更新 |
| `update-skill` | 更新 skill | 拉取最新版本 |
| `push-skill` | 推送修改 | 推送到远程仓库 |
| `add-tool` | 添加第三方工具 | 用 subtree 管理 |
| `sync-upstream` | 同步上游更新 | 拉取 subtree 更新 |

---

## 平台检测

执行任何命令前，先检测当前平台和执行环境：

```bash
# macOS
uname -s | grep -q "Darwin" && PLATFORM="mac"

# Windows 检测（优先 PowerShell）
# 检测 PowerShell 是否可用
if command -v pwsh >/dev/null 2>&1 || command -v powershell >/dev/null 2>&1; then
  PLATFORM="windows"
  EXEC_ENGINE="powershell"
fi

# Git Bash 检测（仅在 PowerShell 不可用时作为备选）
uname -s | grep -qE "MINGW|MSYS|CYGWIN" && PLATFORM="windows"
if [ "$CLAUDE_CODE_GIT_BASH_PATH" ]; then
  EXEC_ENGINE="git-bash"
fi

# Linux（原生，非 WSL）
uname -s | grep -q "Linux" && [ ! "$WSL_DISTRO_NAME" ] && PLATFORM="linux"

# WSL 检测和警告
if [ "$WSL_DISTRO_NAME" ]; then
  echo "⚠ Running in WSL environment"
  echo "  For Windows native config, use PowerShell outside WSL"
  # 询问是否继续
fi
```

**检测优先级**：
1. 检测 macOS → macOS bash 方案
2. 检测 Windows Git Bash（`MINGW|MSYS|CYGWIN` 或 `bash` 可用）→ Windows Git Bash 方案（首选）
3. 检测 Windows PowerShell（`pwsh` 或 `powershell`，Git Bash 不可用时）→ Windows PowerShell 方案（备选）
4. 检测原生 Linux → Linux bash 方案
5. 检测 WSL → 提示用户选择（WSL 用 Linux 方案，或切到 Windows 终端）

---

## init 命令

初始化配置仓库。**状态驱动**：检测本地目录、git、远程关联，告诉用户当前状态并提供对应的操作选项。

### 用法

```
/claude-config init [--config-dir <path>] [--local] [--git]
```

### 状态检测逻辑

```
detect_state(config_dir):
  if not exists(config_dir)           → A: 空空
  if not is_git_repo(config_dir)      → B: 本地目录（非 git）
  if not has_remote(config_dir)       → C: 本地 git（无远程）
                                      → F: 已关联远程
```

### 各状态的处理

**状态 A（什么都没有）**：

```
No config directory found at ~/claude-config-data.

What would you like to do?
  (a) Create a local-only config directory (no git)          → --local → B
  (b) Create a local git repo (no remote yet)                → --git   → C
  (c) I'll clone an existing config repo myself              → 提示用户 git clone → F

如果用户选 (a):
  mkdir -p ~/claude-config-data/assets/{skills,memory,settings,hooks}
  cp $TOOL_DIR/templates/manifest.template.yaml ~/claude-config-data/manifest.yaml
  cp $TOOL_DIR/templates/plugins.template.yaml ~/claude-config-data/plugins.yaml
  ✓ Created local config directory

如果用户选 (b):
  mkdir -p ~/claude-config-data
  cd ~/claude-config-data && git init
  mkdir -p assets/{skills,memory,settings,hooks}
  cp $TOOL_DIR/templates/manifest.template.yaml manifest.yaml
  cp $TOOL_DIR/templates/plugins.template.yaml plugins.yaml
  ✓ Created local git repo

如果用户选 (c):
  Ok. Run: git clone <your-repo-url> ~/claude-config-data
  Then: /claude-config sync --apply
```

**状态 B（本地目录，非 git）**：

```
Found ~/claude-config-data, but it's not a git repo.

You can:
  (a) Keep it as local-only (no changes needed)
  (b) Initialize git version control          → init --git → C

如果用户选 (b):
  cd ~/claude-config-data && git init
  ✓ Initialized git repo
  To add a remote later: /claude-config remote add <url>
```

**状态 C（本地 git，无远程）**：

```
Config directory is git-managed but has no remote.

You can:
  (a) Keep it local-only (no changes needed)
  (b) Add a remote: /claude-config remote add <url>

Current state: local git, no remote → apply and track work normally.
```

**状态 F（已关联远程）**：

```
Config directory is ready.

  Local:  ~/claude-config-data
  Remote: github:username/repo
  Branch: main

Next step: /claude-config sync --apply
```

---

## apply 命令

根据 manifest.yaml 和 plugins.yaml 安装配置到当前机器。

### 参数

| 参数 | 说明 |
|------|------|
| `--platform <name>` | 指定平台，默认自动检测 |
| `--config-dir <path>` | 配置仓库路径，默认 `~/claude-config-data` |

### 执行流程

apply 是**纯本地操作**，读取本地 manifest.yaml 安装配置。git 同步（fetch/pull/push）由 `/claude-config sync` 命令负责。

### 前置检查

执行 apply 前：

```
if [ -f "$CONFIG_DIR/pending-merge.yaml" ]; then
  通过 AskUserQuestion:
    "⚠ 检测到未完成的 merge（pending-merge.yaml）。
     继续 apply 可能会让这些决策过时。建议先完成 merge。"
    (a) 先去完成 merge（取消 apply）  ← 推荐
    (b) 继续执行 apply（pending 决策可能变 stale）
fi
```

### 执行流程

apply 分三个阶段执行：

```
=== Phase 1: Marketplace & Plugins（全自动）===
1. 检测当前平台（如未指定 --platform）
2. 读取 $CONFIG_DIR/plugins.yaml
3. 注册 marketplaces：
   - 检查每个 marketplace 是否已注册
   - 未注册的执行：`claude plugin marketplace add <repo>`
4. 安装 plugins（按 depends_on 排序）：
   - `claude plugin install <package>@<marketplace>`
   - 注意：claude plugin install 不支持 --version，版本由 marketplace catalog 的 submodule commit 或 sha 锁定
5. 等待 plugins 安装完成

=== Phase 2: Static Config（全自动）===
6. 安装 bootstrap skill（仅 claude-config）：
   - 从 assets/skills/claude-config/ 复制 SKILL.md 到 ~/.claude/skills/
   - 其他所有 skill 已在 Phase 1 通过 marketplace 安装
7. 复制 memory 文件（assets/memory/ → ~/.claude/memory/）
8. 合并 settings（base.json + permissions，按 merge_strategy）
9. 安装 hooks（如 statusline.sh/.ps1，依赖 plugins 的 hook 已在 Phase 1 处理）
10. 检查 env vars（expected + fixed）
11. 验证 settings.json 格式正确

=== Phase 3: External Tools（分级处理）===
12. 读取 manifest.yaml external 段
13. 对每个 external tool，按 strategy 分级：
    - auto:   verify → 缺失则自动执行 install 命令（先告知）
    - prompt: verify → 缺失则 AskUserQuestion 询问是否安装
    - manual: verify → 缺失则打印 setup 文档链接和所需环境变量
14. 输出安装报告
```

**重要**：apply 不再执行 git fetch/pull。如果配置仓库有远程更新，用户应先运行 `/claude-config sync` 或在日常使用 `/claude-config sync --apply`。

### 路径验证原则

**核心原则**：
- 用户明确指定了 `--config-dir`，说明有特定意图，**不要覆盖或擅自修改**
- 遇到路径不存在、manifest.yaml 缺失等问题时，通过 **AskUserQuestion** 与用户交互
- 不要假设用户想要什么，直接问

**交互示例**（使用 AskUserQuestion）：

路径不存在时：
```
Config directory not found: /path/user/specified

What would you like to do?
  (a) Create new config directory at this path
  (b) Use default path instead (~\claude-config-data)
  (c) Cancel and specify a different path
```

没有 manifest.yaml 时：
```
No manifest.yaml found in: /path/user/specified

This may not be a valid claude-config directory.
What would you like to do?
  (a) Initialize manifest.yaml here
  (b) This is wrong path, let me specify another
  (c) Use default path instead
```

### 平台选择原则

**关键决策**：根据平台选择执行引擎：

| 平台 | 执行引擎 | 原因 |
|------|----------|------|
| macOS | bash | 原生支持 |
| Linux | bash | 原生支持 |
| Windows | Git Bash（首选） | 所有 bash 命令原生可用，无需翻译；Git for Windows 自带 |
| Windows | PowerShell（备选） | Git Bash 不可用时的 fallback，agent 需翻译 bash → PowerShell |

**Windows 执行策略**：

1. **首选 Git Bash**：检测 `bash` 是否可用（通常位于 `C:\Program Files\Git\bin\bash.exe`）
   - 如果可用 → 所有命令用 Git Bash 执行，代码块无需翻译
2. **备选 PowerShell**：Git Bash 不可用时
   - agent 将 bash 命令翻译为 PowerShell 等价命令
   - `cp` → `Copy-Item`, `mkdir -p` → `New-Item -Force`, `~` → `$env:USERPROFILE`

**Git Bash 检测**：
```bash
# Windows 上检测 Git Bash 是否可用
if command -v bash >/dev/null 2>&1 || [ -f "C:/Program Files/Git/bin/bash.exe" ]; then
  EXEC_ENGINE="git-bash"
elif command -v powershell >/dev/null 2>&1; then
  EXEC_ENGINE="powershell"
fi
```

### 具体操作

**Skills 安装**：

重要原则：只有 `claude-config` 以文件形式安装（bootstrap skill）。其他所有 skill 通过 marketplace 安装，在 Phase 1 由 `claude plugin install` 完成。

仅安装 claude-config bootstrap skill：

```bash
# Bootstrap skill（claude-config 自身，必须先于 plugin 系统可用）
# source: assets/skills/claude-config
src_path="$CONFIG_DIR/assets/skills/claude-config"
tgt_path="$HOME/.claude/skills/claude-config"
mkdir -p "$tgt_path"
cp "$src_path/SKILL.md" "$tgt_path/SKILL.md"
```

注意：不再遍历 assets/skills/ 目录批量安装。manifest.yaml 的 skills 段只包含 claude-config，其余 skill 均通过 plugins.yaml → claude plugin install 安装。

**Memory 安装**：
```bash
# Memory 需要安装到项目目录
# 先获取或创建项目 memory 目录
PROJECT_MEMORY_DIR=~/.claude/projects/<project-hash>/memory

# 对于每个 memory 项
cp <source>/<file>.md $PROJECT_MEMORY_DIR/<file>.md

# 更新 MEMORY.md 索引（如需要）
```

**Settings 合并**：

- `replace`：直接写入字段
- `merge_unique`：读取本地 settings.local.json，合并后去重

```bash
# replace 策略
# 使用 jq 或 python 合并 JSON

# merge_unique 示例（permissions.allow）
local_rules=$(cat ~/.claude/settings.local.json | jq -r '.permissions.allow[]')
manifest_rules=$(cat <source> | jq -r '.permissions.allow[]')
# 合并、去重、写入
```

**Plugins 安装**：

根据平台选择脚本：

**macOS/Linux (bash)**：
```bash
# 读取 plugins.yaml
# 先注册 marketplace（如需要）
for marketplace in $(yq '.marketplaces | keys[]' plugins.yaml); do
  repo=$(yq ".marketplaces.$marketplace.repo" plugins.yaml)
  # 检查是否已注册
  if ! claude plugin marketplace list | grep -q "$marketplace"; then
    echo "Registering marketplace: $marketplace"
    claude plugin marketplace add "$marketplace" "github:$repo"
  fi
done

# 安装 plugins
for plugin in $(yq '.plugins | keys[]' plugins.yaml); do
  marketplace=$(yq ".plugins.$plugin.marketplace" plugins.yaml)
  package=$(yq ".plugins.$plugin.package" plugins.yaml)
  version=$(yq ".plugins.$plugin.version" plugins.yaml)

  if [ -n "$version" ] && [ "$version" != "null" ]; then
    echo "Installing: $package@$marketplace version $version"
    claude plugin install "$package@$marketplace" --version "$version"
  else
    echo "Installing: $package@$marketplace (latest)"
    claude plugin install "$package@$marketplace"
  fi
done
```

**Windows (PowerShell)**：
```powershell
# 读取 plugins.yaml（需要 yq 或手动解析）
# 先注册 marketplace（如需要）
$marketplaces = claude plugin marketplace list
# 对于每个 marketplace in plugins.yaml，检查并注册
# 例如 claude-scientific-writer：
if (-not ($marketplaces -match "claude-scientific-writer")) {
    Write-Host "Registering marketplace: claude-scientific-writer"
    claude plugin marketplace add claude-scientific-writer github:K-Dense-AI/claude-scientific-writer
}

# 安装 plugin（不指定 version 则安装最新）
claude plugin install claude-scientific-writer@claude-scientific-writer
```

**重要**：
- `plugins.yaml` 中如果没有指定 `version`，则安装最新版本
- marketplace 必须先注册才能安装 plugin

**Hooks 安装**：
```bash
# 复制脚本
cp <source> <target>

# 更新 settings.json 的 statusLine 字段
# 注意：必须包含 type 和 command 两个字段
```

**statusLine 格式**（写入 settings.json）：
```json
{
  "statusLine": {
    "type": "command",
    "command": "pwsh ~/.claude/statusline.ps1"
  }
}
```

不同平台：
- macOS/Linux: `"command": "bash ~/.claude/statusline.sh"`
- Windows PowerShell: `"command": "pwsh ~/.claude/statusline.ps1"`
- Windows Git Bash: `"command": "bash ~/.claude/statusline.sh"`

**环境变量检查**：
```bash
# 检查 expected 变量
for var in ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL; do
  if [ -z "${!var}" ]; then
    echo "⚠ Missing env: $var (managed by cc switch)"
  fi
done

# 写入 fixed 变量到 settings.json env 字段
```

---

**External tools 安装**：

Phase 3 处理 manifest.yaml 的 external 段。每个 tool 按 `strategy` 字段分级：

```
for each tool in $CONFIG_DIR/manifest.yaml external:

  strategy = tool.strategy  # "auto" | "prompt" | "manual"
  verify_cmd = tool.setup.verify.command
  install_cmd = tool.install

  if strategy == "auto":
    1. 运行 verify_cmd
    2. 如果成功: 跳过
    3. 如果失败:
       a. 打印 "Installing <name> (required by <boundary.manages>)..."
       b. 执行 install_cmd
       c. 再次 verify
       d. 成功: ✓; 失败: ⚠ 警告

  if strategy == "prompt":
    1. 运行 verify_cmd
    2. 如果成功: 跳过
    3. 如果失败:
       a. 通过 AskUserQuestion 询问用户:
          "<name> not found. Description: <description>. Install now?"
          选项: (a) Yes, install  (b) Skip for now
       b. 如选 Yes: 执行 install_cmd → verify
       c. 如选 Skip: 记录为跳过

  if strategy == "manual":
    1. 运行 verify_cmd
    2. 如果成功: 跳过
    3. 如果失败:
       a. 打印 setup 信息:
          - authority.url
          - 所需 env var (env.required)
          - 说明 (description)
       b. 不执行任何安装命令
```

**strategy 选择原则**：

| strategy | 何时使用 | 示例 |
|----------|----------|------|
| `auto` | 硬依赖：缺失会导致 plugin 不可用 | codex-cli（codex plugin 运行时） |
| `prompt` | 软依赖：有用但非必需，安装有副作用 | agent-browser（npm global install） |
| `manual` | 复杂设置：需要 API key、账号或人工判断 | autoresearchclaw（需要 DASHSCOPE_API_KEY） |

### 输出报告

```
=== Apply Report ===
Platform: linux
Time: 2026-06-03 12:00:00

--- Phase 1: Plugins ---
✓ Registered 5 marketplaces
✓ Installed 13 plugins (4 infra + 9 from your marketplace)

--- Phase 2: Static Config ---
✓ Installed bootstrap skill: claude-config
✓ Installed 3 memory items
✓ Merged settings (base + 18 permissions)
✓ Installed statusline hook
⚠ Missing env: ANTHROPIC_AUTH_TOKEN (configure cc switch)

--- Phase 3: External Tools ---
✓ codex-cli (auto-installed)
✓ agent-browser (already installed)
⚠ humanizer — skipped by user
⚠ aris — 45 skills not linked (run apply again to install)
⚠ autoresearchclaw — manual setup required
     Required env: DASHSCOPE_API_KEY
     See: https://github.com/aiming-lab/AutoResearchClaw

Configuration complete!
```

---

## sync 命令

同步配置仓库的本地和远程状态。处理多设备协同场景。

### 用法

```
/claude-config sync [--apply] [--config-dir <path>]
```

| 参数 | 说明 |
|------|------|
| `--apply` | sync 成功后自动执行 apply |
| `--config-dir <path>` | 配置仓库路径，默认 `~/claude-config-data` |

### 前置条件

配置目录必须处于状态 F（已关联远程）。如果不在状态 F，提示用户先运行 `/claude-config remote add <url>`。

### 前置检查

执行 sync 前：

```
if [ -f "$CONFIG_DIR/pending-merge.yaml" ]; then
  通过 AskUserQuestion:
    "⚠ 检测到未完成的 merge（pending-merge.yaml）。
     继续 sync 可能会让这些决策过时。建议先完成 merge。"
    (a) 先去完成 merge（取消 sync）  ← 推荐
    (b) 继续执行 sync（pending 决策可能变 stale）
fi
```

### 四象限模型

```
1. git fetch origin
2. 比较本地 HEAD vs origin/<branch>：

┌──────────┬───────────┬──────────────────────────────────────┐
│ 本地超前  │ 远程超前   │ 处理                                  │
├──────────┼───────────┼──────────────────────────────────────┤
│ ✗        │ ✗         │ 已同步，无事可做                        │
│ ✓        │ ✗         │ 提示用户 push（显示未推送 commit 列表） │
│ ✗        │ ✓         │ 自动 pull（fast-forward）              │
│ ✓        │ ✓         │ 分叉 → 交互式解决                       │
└──────────┴───────────┴──────────────────────────────────────┘
```

### 分叉处理

```
本地和远程分叉了（diverged）：

Local commits (not on remote):
  abc1234 添加 research-brainstorm skill
  def5678 更新 permissions

Remote commits (not local):
  ghi9012 修改 model 设置
  jkl3456 添加 memory 项

策略:
  (a) remote-first — 以远程为准，rebase 本地到远程之上
  (b) local-first  — 以本地为准，远程 commit 被覆盖（force push）
  (c) interactive  — 逐 commit 检查，手动选择

建议默认 interactive：
  [1/4] abc1234 "添加 research-brainstorm skill"
    (a) keep  (b) drop
  [2/4] def5678 "更新 permissions"
    (a) keep  (b) drop
  [3/4] ghi9012 "修改 model 设置"
    (a) keep  (b) drop
  [4/4] jkl3456 "添加 memory 项"
    (a) keep  (b) drop

最终合并方案：
  - 生成 merge commit 或 rebase
  - 冲突文件需要用户手动编辑
```

### 输出示例

```
=== Sync Report ===

Fetching from github:username/my-claude-config...

  Local:  abc1234 (2 commits ahead)
  Remote: def5678 (1 commit behind)

Remote has 1 new commit:
  def5678 更新 model 设置 — 2 hours ago

Pulling...
✓ Fast-forward merge successful

如果 --apply:
  Running apply...
  (进入 apply 流程)
```

---

## remote 命令

管理配置目录的远程仓库关联（状态 C ↔ F 转换）。

### 用法

```
/claude-config remote status
/claude-config remote add <url>
/claude-config remote remove
```

### remote status

```
显示当前 remote 配置：

  Remote: github:username/my-claude-config
  URL: https://github.com/username/my-claude-config
  Branch: main

或无远程时：
  No remote configured. Use /claude-config remote add <url>
```

### remote add

```
/claude-config remote add <url>

状态 C → F:
  git remote add origin <url>
  git push -u origin HEAD

✓ Remote added: github:username/repo
  现在可以用 /claude-config sync 同步了
```

### remote remove

```
/claude-config remote remove

状态 F → C:
  确认后:
    Are you sure? This only removes the remote link, not your files.
    (a) Yes, remove remote  (b) Cancel

  git remote remove origin

  ✓ Remote removed
  ⚠ 配置目录仍在本地，git 历史保留
```

---

## update-self 命令

更新 claude-config 框架自身。

### 用法

```
/claude-config update-self
```

### 执行流程

```
1. cd ~/.claude-config-tool
2. git fetch origin
3. 比较版本：
   ├─ 已最新 → 无事
   └─ 有更新 → 显示 commit log，询问是否更新
4. 如果更新：
   git pull --ff-only
   cp SKILL.md ~/.claude/skills/claude-config/SKILL.md
   ✓ 框架已更新
5. 如有 breaking change，提示查看 CHANGELOG
```

### 输出示例

```
=== Update claude-config ===

Current:  9b4a627 (2026-06-03)
Latest:   c8f1234 (2026-06-05)

New commits:
  c8f1234 feat: add sync command
  a1b4567 fix: Windows PowerShell detection

Update now?
(a) Yes, update
(b) Skip for now

如果选 Yes:
  ✓ Pulled c8f1234
  ✓ Bootstrap skill updated
  Done. Framework updated to c8f1234.
```

---

## diff 命令

对比本地配置与 manifest，生成详细差异报告。

### 执行流程

```
1. 读取 manifest.yaml
2. 扫描本地 ~/.claude/ 目录
3. 对比每个配置项
4. 分类差异：MATCH / DIFF / NEW-LOCAL / MISSING / PLATFORM-SPECIFIC
5. 输出报告
```

### 差异分类

| 标签 | 含义 |
|------|------|
| `[MATCH]` | 两边一致 |
| `[DIFF]` | 内容不同 |
| `[NEW-LOCAL]` | 本地有，清单无 |
| `[MISSING]` | 清单有，本地无 |
| `[PLATFORM-MAC]` | Mac 专属（清单中） |
| `[PLATFORM-WINDOWS]` | Windows 专属（清单中） |
| `[PATH-SPECIFIC]` | 包含项目路径（不迁移） |
| `[GARBAGE]` | 疑似垃圾规则 |

### 输出格式

```
=== Claude Config Diff Report ===
Platform: mac
Generated: 2026-03-28
Last sync: 2026-03-25 from mac

--- Settings ---
[DIFF] model: manifest="opus[1m]", local="sonnet"
  → (a) use manifest, (b) keep local, (c) platform override

[NEW-LOCAL] customTheme: "dark"
  → (a) local only, (b) add universal, (c) add mac-only

--- Permissions ---
[MATCH] 15 universal rules

[NEW-LOCAL] 3 path-specific rules:
  - "Bash(.../Users/liyao/paper-read-skills/...)"
  → (a) local only (recommended), (b) template

[GARBAGE] 2 rules detected:
  - "Bash(printf \"%s\" $?)"
  → (a) local only, (b) cleanup recommended

--- Skills ---
[MATCH] github, research-brainstorm

[NEW-LOCAL] my-new-skill
  → (a) local only, (b) add to manifest

--- Plugins ---
[VERSION-DIFF] claude-hud: manifest=0.0.10, local=0.0.9
  → (a) upgrade, (b) keep local, (c) update manifest

=== Summary ===
Settings: 2 decisions needed
Permissions: 5 decisions needed
Skills: 1 decision needed
Plugins: 1 decision needed

Run /claude-config merge to resolve.
```

---

## merge 命令

交互式合并，逐项解决 diff 发现的冲突。

### 执行流程

```
0. 检查 pending-merge.yaml：
   - 如存在 → AskUserQuestion:
     "检测到上次保存的 N 条未执行决策。"
     (a) 加载继续 → 逐条确认上次的决策
     (b) 放弃 → 删除 pending-merge.yaml，重新 diff
1. 执行 diff，收集待决策项
2. 按类别分组（Settings / Permissions / Skills / Plugins / Memory）
3. 对每组进行交互式决策
4. 收集所有决策
5. 显示决策摘要
6. 确认后执行
```

### 交互模式

使用 AskUserQuestion 工具进行交互：

```
对于每个差异项，提供选项：

Settings [DIFF]:
(a) Use manifest value
(b) Keep local value
(c) Add as platform override
(d) Update manifest to local

Permissions [NEW-LOCAL]:
(a) Keep local only
(b) Add to manifest universal
(c) Add to manifest platform-specific

Permissions [GARBAGE]:
(a) Keep local only
(b) Remove from local too (cleanup)

Permissions [PATH-SPECIFIC]:
(a) Keep local only (recommended)
(b) Template with ${HOME}
(m) Review individually

Skills [NEW-LOCAL]:
(a) Keep local only
(b) Add to manifest universal
(c) Add to manifest platform-specific

Plugins [VERSION-DIFF]:
(a) Upgrade to manifest version
(b) Keep local version
(c) Update manifest to local version
```

### 决策摘要

收集所有决策后显示：

```
=== Decision Summary ===

Settings:
  ✓ model → opus[1m] (from manifest)
  ✓ customTheme → added as mac-only

Permissions:
  ✓ 3 path-specific → keep local only
  ✓ 2 universal → added to manifest
  ✓ 2 garbage → cleanup (remove from local)

Skills:
  ✓ my-new-skill → added to manifest

Plugins:
  ✓ claude-hud → upgrade to 0.0.10

Apply all changes?
(y) Yes, execute
(n) No, cancel
(d) Dry run (show commands)
(s) Save for later (pending-merge.yaml)
```

### 执行变更

```
# Settings 变更
# 使用 jq 或 Edit 更新 settings.json

# Permissions 变更
# 合并到 settings.local.json
# 更新 manifest.yaml

# Skills 变更
# 复制到 assets/skills/
# 更新 manifest.yaml

# Plugins 变更
# 执行 claude plugin install 或更新 plugins.yaml

# Garbage cleanup
# 从 settings.local.json 中移除
```

---

## track 命令

扫描本地配置，发现未在 manifest 中追踪的项目。

### 执行流程

```
1. 读取 manifest.yaml
2. 扫描 ~/.claude/skills/
3. 扫描 ~/.claude/rules/
4. 扫描 ~/.claude/agents/
5. 扫描 ~/.claude/settings.json（非 plugins 字段）
6. 扫描 ~/.claude/settings.local.json（permissions）
7. 扫描 ~/.claude/projects/<project>/memory/
8. 扫描 ~/.claude/plugins/installed_plugins.json
9. 对比 manifest，发现新增项
10. 对新增项进行交互式追踪决策
```

### Skills 分析

检测 SKILL.md 的 shell 依赖：

```bash
# 检测 bash/sh 依赖
if grep -qE "bash|/bin/sh|\.sh|#!/usr/bin/env bash" SKILL.md; then
  echo "⚠ Contains bash dependency → mac/linux only"
fi

# 检测 powershell 依赖
if grep -qE "powershell|\.ps1|#!/usr/bin/env pwsh" SKILL.md; then
  echo "⚠ Contains powershell dependency → windows only"
fi
```

### Permissions 分析

```bash
# 检测路径特定规则
if echo "$rule" | grep -q "/Users/"; then
  echo "⚠ Path-specific (Mac) → local only or template"
fi

# 检测垃圾规则
if echo "$rule" | grep -qE 'printf.*\$?|\\n|\$\?'; then
  echo "⚠ Possible garbage → cleanup recommended"
fi
```

### 输出与交互

```
/claude-config track

--- New Skills ---
[NEW] my-skill at ~/.claude/skills/my-skill/
  Shell analysis: no platform dependency detected
  (a) Add universal, (b) Add mac-only, (c) Skip

--- New Permissions ---
[NEW] 5 rules in settings.local.json
  Analysis: 3 universal, 2 path-specific
  (a) Add universal to manifest
  (b) Review individually
  (c) Skip all

--- Modified ---
[MODIFIED] model: "opus[1m]" → "sonnet"
  (a) Update manifest
  (b) Revert on next sync
  (c) Platform override
```

---

## export 命令

导出当前本地配置到 manifest.yaml（首次创建或全量更新）。

### 执行流程

```
1. 扫描所有本地配置
2. 分类配置项（universal / platform-specific / sensitive / path-specific）
3. 复制文件到 assets/
4. 生成/更新 manifest.yaml
5. 生成/更新 plugins.yaml
6. 跳过敏感配置（添加到 exclude）
```

### 处理逻辑

**从 settings.json 提取**：
- ✓ model, effortLevel, hasCompletedOnboarding, includeCoAuthoredBy → base.json
- ✗ env（敏感）→ 添加到 env.expected
- ✗ statusLine → hooks section
- ✗ enabledPlugins, extraKnownMarketplaces → plugins.yaml

**从 settings.local.json 提取**：
- 分析每条 permission 规则
- universal → permissions-universal.json
- mac-specific → permissions-mac.json
- path-specific → 不导出，仅记录
- garbage → 建议清理

**Skills 导出**：
```bash
for skill in ~/.claude/skills/*/; do
  name=$(basename $skill)
  mkdir -p assets/skills/$name
  cp $skill/SKILL.md assets/skills/$name/
  # 添加到 manifest.yaml skills section
done
```

---

## validate 命令

验证 manifest.yaml 和文件完整性。

### 检查项

```
1. manifest.yaml 格式正确（YAML 语法）
2. plugins.yaml 格式正确（YAML 语法）
3. 所有 source 路径存在
4. platforms 定义有效（mac/windows/linux/all）
5. merge_strategy 有效（replace/merge/merge_unique）
6. plugins.yaml marketplace 已注册
7. 无重复配置项名称
8. depends_on 的 plugin 在 plugins.yaml 中存在
9. 本地 settings.json 格式正确（JSON 语法）
10. statusLine 格式有效（必须有 type 和 command 字段）
```

### 输出

```
=== Validation Report ===

✓ manifest.yaml: valid YAML
✓ plugins.yaml: valid YAML
✓ settings.json: valid JSON
✓ 4/4 skills sources exist
✓ 3/3 memory sources exist
✓ 4/4 settings sources exist
✓ 1/1 hooks sources exist (mac)
⚠ 0/1 hooks sources exist (windows) - need statusline.ps1?

✓ All platforms valid
✓ All merge_strategy valid
✓ No duplicate names
✓ All depends_on plugins exist
✓ statusLine format valid

Status: VALID (1 warning)
```

---

## status 命令

显示当前配置和同步状态概览。

### 执行流程

```
1. 确定 CONFIG_DIR（从 manifest.yaml metadata.config_repo.local 或默认 ~/claude-config-data）
2. 检测配置目录状态（A/B/C/F）
3. 检测当前平台
4. 如果在状态 F：检测本地/远程同步状态（ahead/behind/diverged/clean）
5. 读取 manifest.yaml 和 plugins.yaml
6. 对比本地 ~/.claude/ 状态
7. 输出状态报告
```

### 输出示例

**状态 F（已关联，已同步）**：

```
=== Claude Config Status ===
Platform: mac
Config Dir: ~/claude-config-data
Remote: github:username/my-claude-config
Sync: clean (up to date)

--- Tracked ---
Skills: 1 (claude-config bootstrap)
Plugins: 13 (4 infra + 9 from marketplace)
Memory: 3 items
Permissions: 18 universal, 1 mac-specific

--- Local State ---
Installed skills: 1 ✓
Installed plugins: 13 (versions match)
Memory files: 3 ✓
Settings: base + merged permissions ✓

Run /claude-config diff for detailed comparison.
```

**状态 F（本地超前）**：

```
Sync: ahead (2 local commits not pushed)
  → Run /claude-config sync to push
```

**状态 F（远程超前）**：

```
Sync: behind (3 remote commits not pulled)
  → Run /claude-config sync to pull
```

**状态 F（分叉）**：

```
Sync: diverged (2 local + 1 remote)
  → Run /claude-config sync to resolve
```

**状态 C（本地 git，无远程）**：

```
Config Dir: ~/claude-config-data
Sync: local only (no remote configured)
  → Run /claude-config remote add <url> to add a remote
```

**状态 B（纯本地）**：

```
Config Dir: ~/claude-config-data
Sync: local only (not a git repo)
  → Run /claude-config init --git to add version control
```

---

## add-skill 命令

从本地路径或 GitHub 仓库添加新的 skill 到配置清单。

### 用法

```
/claude-config add-skill <source> [--type self|third-party] [--ref <branch|tag>]
```

### 参数

| 参数 | 说明 |
|------|------|
| `<source>` | 本地路径或 GitHub 仓库 (github:user/repo) |
| `--type` | skill 类型 (self/third-party)，默认自动检测 |
| `--ref` | 分支或 tag，默认 main |

### 执行流程

```
1. 解析 source，判断是本地路径还是 GitHub 仓库
2. 如果是本地路径：
   - 复制 skill 文件到 assets/skills/<name>/
3. 如果是 GitHub 仓库：
   - 克隆仓库到临时目录
   - 复制 skill 文件到 assets/skills/<name>/
   - 记录 upstream 信息
4. 更新 manifest.yaml，添加 skill 配置
5. 询问是否立即 apply
```

### 示例

```
# 从本地路径添加
/claude-config add-skill /path/to/my-skill

# 从 GitHub 仓库添加
/claude-config add-skill github:xxx/research-mate

# 指定类型为自制 skill
/claude-config add-skill github:username/my-skill --type self

# 指定分支
/claude-config add-skill github:xxx/skill --ref develop
```

---

## check-updates 命令

检查所有有远程上游的 skills 是否有更新。

### 用法

```
/claude-config check-updates
```

### 执行流程

```
1. 读取 manifest.yaml 中所有 skills
2. 对于有 upstream.repo 的 skill：
   - fetch 上游仓库
   - 比较 last_sync 和最新 commit
3. 输出更新报告
```

### 输出示例

```
=== Skill Updates Check ===
[third-party] research-mate
  Local:  2026-03-20
  Remote: 2026-03-28
  → 5 commits behind

[self] my-skill
  Local:  2026-03-25
  Remote: 2026-03-28
  → 2 commits ahead (unpushed changes)

=== Summary ===
1 skill has updates available
1 skill has unpushed changes
Run /claude-config update-skill <name> to update
Run /claude-config push-skill <name> to push changes
```

---

## update-skill 命令

从上游拉取 skill 的最新版本。

### 用法

```
/claude-config update-skill <name>
```

### 参数

| 参数 | 说明 |
|------|------|
| `<name>` | skill 名称（manifest.yaml 中定义的名称） |

### 执行流程

```
1. 读取 skill 的 upstream.repo 和 upstream.ref
2. fetch 上游仓库最新内容
3. 显示 diff（本地版本 vs 上游版本）
4. 确认后复制新文件到 assets/skills/<name>/
5. 更新 manifest.yaml 的 last_sync 时间
6. 询问是否 apply 到本地
```

### 输出示例

```
=== Update skill: research-mate ===
Fetching upstream...

Changes:
  M SKILL.md (3 additions, 1 deletion)
  A templates/new-template.md

Apply update?
(y) Yes, update and apply
(n) No, cancel
(d) View full diff
```

---

## push-skill 命令

推送自制 skill 的修改到远程仓库。

### 用法

```
/claude-config push-skill <name>
```

### 参数

| 参数 | 说明 |
|------|------|
| `<name>` | skill 名称（必须是 type: self 的 skill） |

### 执行流程

```
1. 检查 skill 的 upstream.type 是否为 self
2. 检查 upstream.repo 是否已配置
3. 对比 assets/skills/<name>/ 和远程
4. 显示 diff
5. 确认后 git push
6. 更新 manifest.yaml 的 last_sync
```

### 仅适用于

- `upstream.type` 为 `self` 的 skill
- 已配置 `upstream.repo` 的 skill

### 输出示例

```
=== Push skill: my-skill ===
Repository: username/my-skill
Branch: main

Changes to push:
  M SKILL.md (5 additions, 2 deletions)
  A new-feature.md

Push to remote?
(y) Yes, push
(n) No, cancel

✓ Pushed successfully
Updated last_sync: 2026-03-28
```

---

## add-tool 命令

用 git subtree 添加第三方工具，支持本地定制和上游更新合并。

### 用法

```
/claude-config add-tool <name> <git-url> [--ref <branch>]
```

### 参数

| 参数 | 说明 |
|------|------|
| `<name>` | skill 名称（将创建 assets/skills/<name>/） |
| `<git-url>` | Git 仓库 URL |
| `--ref` | 分支或 tag，默认 main |

### 执行流程

```
1. 确定 CONFIG_DIR（默认 ~/claude-config-data）
2. 检查 assets/skills/<name>/ 是否已存在
3. 执行 git subtree add：
   git subtree add --prefix=assets/skills/<name> <git-url> <ref> --squash
4. 更新 manifest.yaml，添加 skill 配置：
   - source: assets/skills/<name>
   - upstream.type: third-party
   - upstream.repo: <git-url>
   - upstream.ref: <ref>
   - upstream.subtree: true
   - upstream.last_sync: 当前日期
5. 询问是否 apply
```

### 示例

```
# 添加第三方 skill
/claude-config add-tool research-mate https://github.com/xxx/research-mate.git

# 指定分支
/claude-config add-tool my-tool https://github.com/user/my-tool.git --ref develop
```

### 输出示例

```
=== Adding tool: research-mate ===
  URL: https://github.com/xxx/research-mate.git
  Branch: main
  Target: assets/skills/research-mate

Running git subtree add...
✓ Subtree added successfully

Updated manifest.yaml with:
  research-mate:
    source: assets/skills/research-mate
    upstream:
      type: third-party
      repo: https://github.com/xxx/research-mate.git
      subtree: true
      last_sync: "2026-03-28"

Apply now? (y/n)
```

---

## sync-upstream 命令

同步 subtree 管理的第三方工具的上游更新。

### 用法

```
/claude-config sync-upstream [name]
```

### 参数

| 参数 | 说明 |
|------|------|
| `[name]` | 可选，指定要同步的 skill 名称。不指定则同步所有 subtree 工具 |

### 执行流程

```
1. 读取 manifest.yaml 中 subtree: true 的 skills
2. 对于每个工具（或指定的工具）：
   a. 读取 upstream.repo 和 upstream.ref
   b. 执行 git subtree pull
   c. 如果成功，更新 last_sync 时间
   d. 如果有冲突，报告并停止
3. 输出同步报告
```

### 示例

```
# 同步所有 subtree 工具
/claude-config sync-upstream

# 同步指定工具
/claude-config sync-upstream research-mate
```

### 输出示例

```
=== Syncing all subtree tools ===

--- Syncing research-mate ---
  URL: https://github.com/xxx/research-mate.git
  Branch: main
  Running git subtree pull...
  ✓ Done. Updated last_sync: 2026-03-28

--- Syncing another-tool ---
  No updates available

=== Summary ===
1 tool updated
Run 'git status' to see changes.
```

### 冲突处理

如果有冲突：
1. 脚本会报告失败
2. 手动解决冲突
3. `git add . && git commit`

---

## 文件格式参考

### manifest.yaml 关键字段

```yaml
skills:
  <name>:
    source: assets/skills/<name>    # 必需
    platforms: [all] | [mac, linux] # 必需
    description: <text>             # 可选

permissions:
  universal | mac | windows:
    source: assets/settings/permissions-*.json
    merge_strategy:
      path: permissions.allow       # JSON 路径
      mode: merge_unique            # 合并模式
    platforms: [all] | [mac] | [windows]

hooks:
  <name>:
    depends_on: [plugin-name]       # 可选
    mac | windows:
      source: assets/hooks/<platform>/<file>
      target: ~/.claude/<file>
      runtime: bash | git-bash | powershell
```

### plugins.yaml 关键字段

```yaml
plugins:
  <name>:
    marketplace: <marketplace-name>  # 必需
    package: <package-name>          # 可选（默认与 name 相同）
    platforms: [all]

marketplaces:
  <name>:
    repo: <owner>/<repo>
```

注：`version` 和 `source` 字段已弃用。`claude plugin install` 不支持 `--version`，版本由 marketplace catalog 的 submodule commit 或 sha 锁定（详见 DESIGN.md 的 Marketplace 机制章节）。

---

## 注意事项

### 路径验证原则

**所有涉及 config-dir 的命令都必须遵循**：
- 用户指定了 `--config-dir` → 这是明确的意图，不要擅自改路径
- 遇到问题（路径不存在、manifest 缺失等）→ 用 AskUserQuestion 交互解决
- 不要假设、不要"智能修正"、不要覆盖用户意图

### settings.json 格式要求

**写入 settings.json 时必须确保**：
- JSON 语法正确（无 BOM，无尾随逗号，引号正确）
- 使用 UTF-8 无 BOM 编码
- 写入后验证 JSON 可解析

**statusLine 格式**：
```json
{
  "statusLine": {
    "type": "command",
    "command": "pwsh ~/.claude/statusline.ps1"
  }
}
```
- 必须同时包含 `type` 和 `command` 两个字段
- `type` 只能是 `"command"`

**验证方法**：
```bash
# 验证 JSON 语法
python3 -c "import json; json.load(open('~/.claude/settings.json'))"

# 或用 jq
jq . ~/.claude/settings.json
```

### 敏感信息

以下永不追踪或迁移：
- ~/.claude/config.json（API key）
- ~/.claude.json（OAuth session）
- env.ANTHROPIC_AUTH_TOKEN、env.ANTHROPIC_BASE_URL（由 cc switch 管理）

### 路径特定权限

包含 `/Users/<name>/` 或项目路径的权限规则不迁移，在新机器上按需授权。

### Windows statusline

**推荐 Git Bash 方案**（与 macOS/Linux 一致，无需维护两份脚本）：

原因：
- Git for Windows 自带 Git Bash，大多数 Windows 开发者已安装
- 使用与 macOS/Linux 相同的 `.sh` 脚本，无需翻译
- Claude Code 在 Windows 终端中可直接调用 `bash`

配置示例：
```json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline.sh"
  }
}
```

**备选 PowerShell 方案**（不使用 Git Bash 时）：
```json
{
  "statusLine": {
    "type": "command",
    "command": "pwsh ~/.claude/statusline.ps1"
  }
}
```

**注意**：如果使用 Git Bash 方案，只需维护 `.sh` 版本。如果同时支持 PowerShell，需额外提供 `.ps1` 版本。

### Merge 保存

使用 `(s) Save for later` 会创建 `pending-merge.yaml`，下次 merge 时自动加载。