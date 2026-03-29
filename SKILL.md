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
| `--config-dir <path>` | 配置仓库路径 | `~/claude-config-data` |

## 命令概览

| 命令 | 功能 | 说明 |
|------|------|------|
| `init` | 初始化配置仓库 | 首次使用时执行 |
| `apply` | 安装配置到当前机器 | 直接安装，无交互 |
| `diff` | 生成差异报告 | 对比本地与清单 |
| `merge` | 交互式合并 | 逐项解决冲突 |
| `track` | 检测新配置 | 发现未追踪项 |
| `export` | 导出配置到清单 | 双向流转支持 |
| `validate` | 验证清单完整性 | 检查文件存在性 |
| `status` | 当前同步状态 | 概览 |
| `add-skill` | 添加 skill | 从本地或仓库添加 |
| `check-updates` | 检查更新 | 检查上游更新 |
| `update-skill` | 更新 skill | 拉取最新版本 |
| `push-skill` | 推送修改 | 推送到远程仓库 |
| `add-tool` | 添加第三方工具 | 用 subtree 管理 |
| `sync-upstream` | 同步上游更新 | 拉取 subtree 更新 |

---

## 平台检测

执行任何命令前，先检测当前平台：

```bash
# macOS
uname -s | grep -q "Darwin" && PLATFORM="mac"

# Windows (Git Bash / WSL)
uname -s | grep -qE "MINGW|MSYS|CYGWIN" && PLATFORM="windows"
# 或检测 PowerShell
command -v powershell >/dev/null 2>&1 && PLATFORM="windows"

# Linux
uname -s | grep -q "Linux" && PLATFORM="linux"
```

---

## init 命令

初始化新的配置仓库。首次使用时执行。

### 用法

```
/claude-config init [--config-dir <path>]
```

### 执行流程

```
1. 检查是否已有配置仓库（默认 ~/claude-config-data）
2. 如果没有，询问用户：
   a. 是否已在 GitHub 创建私有仓库？
      - 是：询问仓库地址，clone
      - 否：引导用户去 GitHub 创建
3. 初始化配置目录结构：
   - 复制模板文件
   - 创建 assets/ 目录结构
   - 复制 scripts/
4. 询问用户的 GitHub 用户名，更新 manifest.yaml 的 config_repo
5. 提示用户提交初始配置
```

### 输出示例

```
=== Claude Config Init ===

Checking for existing config repo at ~/claude-config-data... Not found.

Do you already have a private config repo on GitHub? (y/n)
> n

Please create a private repo on GitHub first:
  1. Go to https://github.com/new
  2. Name it something like "my-claude-config"
  3. Keep it private
  4. Don't initialize with README

Press Enter when done, or type the repo URL now.
> https://github.com/username/my-claude-config

Cloning...
✓ Cloned to ~/claude-config-data

Initializing config structure...
✓ Created manifest.yaml
✓ Created plugins.yaml
✓ Created assets/skills/
✓ Created assets/memory/
✓ Created assets/settings/
✓ Created assets/hooks/mac/
✓ Created assets/hooks/windows/
✓ Created scripts/

What's your GitHub username?
> username

Updated manifest.yaml:
  config_repo:
    local: ~/claude-config-data
    remote: github:username/my-claude-config

Next steps:
  1. cd ~/claude-config-data
  2. git add -A && git commit -m "Initial config"
  3. git push
  4. /claude-config apply
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

```
1. 确定 CONFIG_DIR（--config-dir 参数优先，默认 ~/claude-config-data）
2. 验证路径状态：
   - 如果路径或 manifest.yaml 有问题，不要擅自修正
   - 使用 AskUserQuestion 让用户决定如何处理（详见下方"路径验证原则"）
3. 检测当前平台（如未指定 --platform）
4. 读取 $CONFIG_DIR/manifest.yaml 和 $CONFIG_DIR/plugins.yaml
5. 按平台过滤配置项（platforms 字段）
6. 检查 env.expected 变量是否存在，缺失则警告
7. 安装 plugins（执行 claude plugin install）
8. 等待 plugins 安装完成
9. 安装依赖 plugins 的 hooks（depends_on）
10. 复制 $CONFIG_DIR/assets/ 下的 skills/rules/agents/memory 文件
11. 合并 settings（按 merge_strategy）
12. 验证 settings.json 格式正确
13. 输出安装报告
```

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

### 具体操作

**Skills 安装**：

对于每个 skill，读取 manifest.yaml 中的 source 路径，然后复制：

```bash
# 示例：安装 deep-research skill
# source: assets/skills/deep-research
src_path="$CONFIG_DIR/assets/skills/deep-research"
tgt_path="$HOME/.claude/skills/deep-research"
mkdir -p "$tgt_path"
cp "$src_path/SKILL.md" "$tgt_path/SKILL.md"
```

**批量安装所有 skills**：

```bash
CONFIG_DIR="$HOME/claude-config-data"
SKILLS_DIR="$HOME/.claude/skills"

for skill_dir in "$CONFIG_DIR/assets/skills"/*; do
    if [ -d "$skill_dir" ]; then
        skill_name=$(basename "$skill_dir")
        echo "Installing skill: $skill_name"
        mkdir -p "$SKILLS_DIR/$skill_name"
        cp "$skill_dir/SKILL.md" "$SKILLS_DIR/$skill_name/SKILL.md"
    fi
done
```

**重要**：使用 `$HOME` 而不是 `~`，确保路径正确展开。

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
```bash
# 先注册 marketplace（如需要）
claude plugin marketplace add <marketplace-name> <github-repo>

# 安装 plugin
claude plugin install <package>@<marketplace> --version <version>
```

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

### 输出报告

```
=== Apply Report ===
Platform: mac
Time: 2026-03-28 10:30:00

✓ Installed 4 skills
✓ Installed 3 memory items
✓ Merged settings (base + permissions)
✓ Installed 2 plugins
✓ Installed statusline hook

⚠ Missing env: ANTHROPIC_AUTH_TOKEN (configure cc switch)

Configuration complete!
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

显示当前同步状态概览。

### 执行流程

```
1. 确定 CONFIG_DIR（从 manifest.yaml metadata.config_repo.local 或默认 ~/claude-config-data）
2. 检测当前平台
3. 读取 manifest.yaml 和 plugins.yaml
4. 对比本地 ~/.claude/ 状态
5. 输出状态报告
```

### 输出

```
=== Claude Config Status ===
Platform: mac
Config Repo: ~/claude-config-data
Remote: github:LKCY23/claude-config-data
Last sync: 2026-03-28 from mac

--- Tracked ---
Skills: 4 (github, research-brainstorm, literature-review, read-paper)
Plugins: 2 (superpowers@5.0.5, claude-hud@0.0.10)
Memory: 3 items
Permissions: 18 universal, 1 mac-specific

--- Local State ---
Installed skills: 4 (all tracked)
Installed plugins: 2 (versions match)
Memory files: 3 (all tracked)

--- Pending ---
No pending changes

Run /claude-config diff for detailed comparison.
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
/claude-config add-skill github:LKCY23/my-skill --type self

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
Repository: LKCY23/my-skill
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
    source: github:<owner>/<repo>    # 必需
    package: <package-name>          # 必需
    version: "<version>"             # 可选，不指定则最新
    platforms: [all]

marketplaces:
  <name>:
    source: github
    repo: <owner>/<repo>
```

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

默认使用 Git Bash 执行 .sh 脚本。如需纯 PowerShell，需手动创建 .ps1 版本。

### Merge 保存

使用 `(s) Save for later` 会创建 `pending-merge.yaml`，下次 merge 时自动加载。