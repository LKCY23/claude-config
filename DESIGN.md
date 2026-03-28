# Claude Config - 跨机器配置管理方案

## 背景

Claude Code 的配置分布在多个目录和文件中，目前没有官方的跨机器同步方案。本方案旨在提供一个声明式清单 + Skill 驱动的配置管理工具，支持：

- **双向流转**（Mac ↔ Windows ↔ Linux），非单向迁移
- 跨平台差异处理
- 版本控制（git 追踪配置变更）
- 选择性配置（按平台、按场景）
- **交互式合并**（diff + 逐项解决冲突）
- 敏感信息保护

---

## 配置分类与迁移策略

### 配置类型定义

| 类型 | 定义 | 迁移策略 |
|------|------|----------|
| **通用配置** | 所有机器都适用 | 直接复制/合并 |
| **平台特定配置** | 只能在某个平台使用 | 按平台选择，apply时过滤 |
| **敏感配置** | 不应该直接同步 | 跳过，由外部工具（如 cc switch）处理 |
| **内生配置** | Claude Code 自动生成/管理 | 分类处理 |

### 完整配置清单

| 配置项 | 文件路径 | 类型 | 迁移策略 | 备注 |
|--------|----------|------|----------|------|
| **Settings（用户级）** | `~/.claude/settings.json` | 通用 | 分离处理（见下文） | 全局设置 |
| **Settings（项目级共享）** | `.claude/settings.json` | 通用 | 提交到项目 Git | 团队共享设置 |
| **Settings（项目级本地）** | `.claude/settings.local.json` | 通用 | 合并处理 | 权限配置 |
| **CLAUDE.md（用户级）** | `~/.claude/CLAUDE.md` | 内生 | 直接复制 | 个人偏好指令 |
| **CLAUDE.md（项目级）** | `./CLAUDE.md` | 内生 | 提交到项目 Git | 项目指令 |
| **Rules（用户级）** | `~/.claude/rules/*.md` | 内生 | 直接复制 | 个人全局规则 |
| **Skills（用户级）** | `~/.claude/skills/<skill-name>/SKILL.md` | 通用 | 直接复制 | 用户自定义 skills |
| **Agents（用户级）** | `~/.claude/agents/*.md` | 通用 | 直接复制 | 自定义 subagent |
| **Hooks** | `settings.json` 中的 `hooks` 字段 | 平台特定 | 按平台选择 | bash vs powershell |
| **Statusline Script** | `~/.claude/statusline.sh` / `.ps1` | 平台特定 | 按平台选择 | 自定义状态栏脚本 |
| **Plugins** | `~/.claude/plugins/` | 通用 | 通过 `claude plugin install` | 插件管理 |
| **Auto Memory** | `~/.claude/projects/<project>/memory/` | 内生 | **迁移**（行为偏好） | 已确认 |
| **Teams** | `~/.claude/teams/` | 内生 | **不迁移**（项目特定） | 已确认 |
| **config.json** | `~/.claude/config.json` | 敏感 | 跳过 | API key |
| **~/.claude.json** | `~/.claude.json` | 敏感 | 跳过 | OAuth session |

### 不迁移的配置

| 配置项 | 原因 |
|--------|------|
| `~/.claude/config.json` | API key（由 cc switch 管理） |
| `~/.claude.json` | OAuth session 数据 |
| `~/.claude/session-env/` | 会话环境缓存 |
| `~/.claude/sessions/*.jsonl` | 会话历史 |
| `~/.claude/history.jsonl` | 命令历史 |
| `~/.claude/cache/` | 通用缓存 |
| `~/.claude/telemetry/` | 遥测数据 |
| `~/.claude/teams/` | 项目特定（已确认不迁移） |
| 其他缓存/临时目录 | 无迁移价值 |

---

## 目录结构设计

```
claude-config/                     # 独立 git 仓库
├── SKILL.md                       # 核心执行指令
├── manifest.yaml                  # 配置清单
├── assets/
│   ├── skills/                    # 用户自定义 skills
│   │   ├── github/
│   │   │   └── SKILL.md
│   │   ├── research-brainstorm/
│   │   │   └── SKILL.md
│   │   ├── literature-review/
│   │   │   └── SKILL.md
│   │   └── read-paper/
│   │       └── SKILL.md
│   ├── rules/                     # 用户自定义 rules
│   ├── agents/                    # 用户自定义 agents
│   ├── settings/
│   │   ├── base.json              # 通用基础设置
│   │   ├── permissions-universal.json  # 通用权限
│   │   ├── permissions-mac.json        # Mac 特定权限
│   │   └── permissions-windows.json    # Windows 特定权限
│   ├── hooks/
│   │   ├── mac/
│   │   │   └── statusline.sh
│   │   └── windows/
│   │       └── statusline.ps1
│   ├── claude-md/
│   │   └── CLAUDE.md              # 用户级 CLAUDE.md
│   └── memory/                    # 行为偏好 memory
│       ├── claude-code-teammate-workflow.md
│       ├── collaboration-preference-ask-user-question.md
│       └── ask-user-question-strictly-preferred.md
├── plugins.yaml                   # 插件清单
└── scripts/
    └── validate-manifest.sh
```

---

## manifest.yaml 格式

```yaml
version: 1
metadata:
  name: my-claude-config
  description: 个人 Claude Code 配置清单
  last_updated: 2026-03-28
  last_sync_platform: mac         # 记录最后同步的平台

# 平台定义
platforms:
  mac: [darwin, macos]
  windows: [windows, win32, win64]
  linux: [linux, ubuntu, debian]

# Skills 配置
skills:
  github:
    source: assets/skills/github
    platforms: [all]
    description: GitHub workflow skill

  research-brainstorm:
    source: assets/skills/research-brainstorm
    platforms: [all]

  literature-review:
    source: assets/skills/literature-review
    platforms: [all]

  read-paper:
    source: assets/skills/read-paper
    platforms: [all]

# Rules 配置
rules: {}

# Agents 配置
agents: {}

# Settings 配置（分离处理）
settings:
  base:
    source: assets/settings/base.json
    merge_strategy: replace
    platforms: [all]
    # base.json 内容：
    # {
    #   "model": "opus[1m]",
    #   "effortLevel": "high",
    #   "hasCompletedOnboarding": true
    # }
    # 注意：env 由 cc switch 管理，不在此存储

# Permissions（平台分类）
permissions:
  universal:
    source: assets/settings/permissions-universal.json
    merge_strategy:
      path: permissions.allow
      mode: merge_unique            # 去重合并
    platforms: [all]
    # 内容示例：
    # {
    #   "permissions": {
    #     "allow": [
    #       "Bash(git:*)",
    #       "Bash(gh:*)",
    #       "Bash(python3:*)",
    #       "WebSearch",
    #       "WebFetch(domain:github.com)",
    #       "WebFetch(domain:arxiv.org)"
    #     ]
    #   }
    # }

  mac:
    source: assets/settings/permissions-mac.json
    merge_strategy:
      path: permissions.allow
      mode: merge_unique
    platforms: [mac]
    # Mac 特定权限，如 bash 相关

  windows:
    source: assets/settings/permissions-windows.json
    merge_strategy:
      path: permissions.allow
      mode: merge_unique
    platforms: [windows]
    # Windows 特定权限，如 powershell 相关

# Hooks 配置（平台特定）
hooks:
  statusline:
    mac:
      source: assets/hooks/mac/statusline.sh
      target: ~/.claude/statusline.sh
    windows:
      source: assets/hooks/windows/statusline.ps1
      target: ~/.claude/statusline.ps1

  # Settings.json 中的 hooks 配置也需要平台化
  settings_hooks:
    mac:
      # 如果有 settings.json 中的 hooks 字段
    windows:

# CLAUDE.md（用户级指令）
claude_md:
  source: assets/claude-md/CLAUDE.md
  target: ~/.claude/CLAUDE.md
  platforms: [all]

# Memory（行为偏好，已确认迁移）
memory:
  items:
    claude-code-teammate-workflow:
      source: assets/memory/claude-code-teammate-workflow.md
      platforms: [all]
    collaboration-preference-ask-user-question:
      source: assets/memory/collaboration-preference-ask-user-question.md
      platforms: [all]
    ask-user-question-strictly-preferred:
      source: assets/memory/ask-user-question-strictly-preferred.md
      platforms: [all]

# 环境变量（不管理敏感值，只检查存在性）
env:
  expected:
    - ANTHROPIC_AUTH_TOKEN         # 由 cc switch 管理，仅检查存在
    - ANTHROPIC_BASE_URL           # 由 cc switch 管理
  fixed:
    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "1"
    ENABLE_TOOL_SEARCH: "true"

# 排除列表
exclude:
  - ~/.claude/config.json
  - ~/.claude.json
  - ~/.claude/teams/               # 项目特定，不迁移
  - ~/.claude/session-env/
  - ~/.claude/sessions/
  - ~/.claude/history.jsonl
  - ~/.claude/cache/
  - ~/.claude/telemetry/
```

---

## plugins.yaml 格式

```yaml
plugins:
  superpowers:
    marketplace: claude-plugins-official
    source: github:anthropics/claude-plugins-official
    package: superpowers
    version: "5.0.5"
    platforms: [all]

  claude-hud:
    marketplace: claude-hud
    source: github:jarrodwatts/claude-hud
    package: claude-hud
    version: "0.0.10"
    platforms: [all]

marketplaces:
  claude-plugins-official:
    source: github
    repo: anthropics/claude-plugins-official
  claude-hud:
    source: github
    repo: jarrodwatts/claude-hud
```

---

## SKILL.md 命令设计

### 命令列表

| 命令 | 功能 | 说明 |
|------|------|------|
| `/claude-config apply` | 安装配置 | 直接安装，无交互 |
| `/claude-config diff` | 生成差异报告 | 对比本地与清单 |
| `/claude-config merge` | **交互式合并** | 逐项解决冲突 |
| `/claude-config track` | 检测新配置 | 发现未追踪项 |
| `/claude-config export` | 导出配置到清单 | 双向流转支持 |
| `/claude-config validate` | 验证清单 | 完整性检查 |
| `/claude-config status` | 当前状态 | 同步状态概览 |

---

## 合并管理流程（核心功能）

### diff 输出格式

```
=== Claude Config Diff Report ===
Platform: mac
Generated: 2026-03-28
Last sync: 2026-03-25 from mac

--- Settings ---
[DIFF] model: manifest="opus[1m]", local="sonnet"
  → Options: (a) use manifest, (b) keep local, (c) add as platform override

[NEW-LOCAL] customTheme: "dark" (not in manifest)
  → Options: (a) keep local only, (b) add to manifest universal, (c) add as mac-only

[MISSING] effortLevel: manifest has "high", local missing
  → Options: (a) add to local, (b) remove from manifest

--- Permissions ---
[COMMON] 15 rules match both sides

[NEW-LOCAL] 3 rules with Mac-specific paths:
  - "Bash(python -m json.tool /Users/liyao/paper-read-skills/...)"
  → Options: (a) keep local only, (b) keep local + add placeholder to manifest

[NEW-LOCAL] 2 universal rules not in manifest:
  - "Bash(python3:*)"
  - "WebFetch(domain:sakana.ai)"
  → Options: (a) keep local only, (b) add to manifest universal

[PLATFORM-MAC] 2 Mac-only rules in manifest:
  - "Bash(bash:*)"
  → Not in local. Options: (a) add to local, (b) skip

[PLATFORM-WINDOWS] 2 Windows-only rules in manifest:
  - "Bash(powershell:*)"
  → Skip for mac platform

--- Skills ---
[MATCH] github, research-brainstorm

[NEW-LOCAL] literature-review (not in manifest)
  → Options: (a) keep local only, (b) add to manifest

[MISSING] read-paper (in manifest, not installed)
  → Options: (a) install, (b) remove from manifest

--- Plugins ---
[MATCH] superpowers@5.0.5

[VERSION-DIFF] claude-hud: manifest=0.0.10, local=0.0.9
  → Options: (a) upgrade to manifest version, (b) keep local, (c) update manifest to local version

--- Memory ---
[MATCH] claude-code-teammate-workflow

[NEW-LOCAL] new-memory-item.md
  → Options: (a) keep local only, (b) add to manifest

=== Summary ===
Settings: 3 items need decision
Permissions: 7 items need decision
Skills: 2 items need decision
Plugins: 1 item need decision
Memory: 1 item need decision

Run /claude-config merge to resolve interactively.
```

### merge 交互流程

```
/claude-config merge

Generating diff...
Found 14 items requiring decisions.

=== Session 1: Settings ===

[1/14] model: manifest="opus[1m]", local="sonnet"
  This is a core setting affecting all sessions.
  Choose action:
  (a) Use manifest value (opus[1m])
  (b) Keep local value (sonnet)
  (c) Add local value as mac override (manifest stays universal, mac uses sonnet)
  (d) Update manifest to local value (both sync to sonnet)
  > a
  ✓ Will use manifest value

[2/14] customTheme: "dark" exists locally, not in manifest
  This appears to be a personal preference.
  Choose action:
  (a) Keep local only (don't sync)
  (b) Add to manifest as universal (sync to all machines)
  (c) Add to manifest as mac-only (sync only on mac)
  > c
  ✓ Will add as mac-only setting

=== Session 2: Permissions ===

[3/14] Permission rules with Mac-specific paths (3 rules)
  These contain "/Users/liyao/" paths that won't work on Windows.
  (a) Keep local only (don't sync)
  (b) Keep local + template in manifest (replace path with ${HOME})
  (m) Review each rule individually
  > m

  [3.1/14] "Bash(python -m json.tool /Users/liyao/paper-read-skills/...)"
    Path: /Users/liyao/paper-read-skills (project-specific)
    (a) Keep local only
    (b) Template: replace /Users/liyao with ${HOME}
    > a
    ✓ Keeping local only

  [3.2/14] "Bash(bash /Users/liyao/research-reading-skills/tests/bin/...)"
    Path: /Users/liyao/research-reading-skills (project-specific)
    (a) Keep local only
    (b) Template: replace /Users/liyao with ${HOME}
    > a
    ✓ Keeping local only

[4/14] Universal rules not in manifest (2 rules)
  - "Bash(python3:*)"
  - "WebFetch(domain:sakana.ai)"
  These appear to be universally useful.
  Choose action:
  (a) Keep local only
  (b) Add both to manifest as universal
  (m) Review each rule individually
  > b
  ✓ Will add to manifest

[5/14] Mac-only rules in manifest not in local (2 rules)
  - "Bash(bash:*)"
  These are marked mac-only in manifest.
  Choose action:
  (a) Add to local
  (b) Skip (keep local as is)
  > a
  ✓ Will add to local

=== Session 3: Skills ===

[6/14] "literature-review" exists locally, not in manifest
  SKILL.md preview:
  ---
  name: literature-review
  description: Academic literature review workflow
  ---
  (a) Keep local only (don't sync)
  (b) Add to manifest as universal
  (c) Add to manifest as mac-only
  > b
  ✓ Will add to manifest

[7/14] "read-paper" in manifest, not installed locally
  (a) Install skill
  (b) Remove from manifest
  > a
  ✓ Will install

=== Session 4: Plugins ===

[8/14] claude-hud: manifest=0.0.10, local=0.0.9
  (a) Upgrade to 0.0.10 (use manifest version)
  (b) Keep 0.0.9 (keep local version)
  (c) Update manifest to 0.0.9 (local is newer/correct)
  > a
  ✓ Will upgrade

=== Session 5: Memory ===

[9/14] "new-memory-item.md" exists locally, not in manifest
  Content preview:
  ---
  name: my-custom-workflow
  description: Personal workflow preference for X
  ---
  (a) Keep local only
  (b) Add to manifest
  > b
  ✓ Will add to manifest

=== Decision Summary ===

Settings:
  ✓ model → opus[1m] (from manifest)
  ✓ customTheme → added as mac-only to manifest

Permissions:
  ✓ 3 path-specific rules → keep local only
  ✓ 2 universal rules → added to manifest
  ✓ 2 mac-only rules → added to local

Skills:
  ✓ literature-review → added to manifest
  ✓ read-paper → will install

Plugins:
  ✓ claude-hud → upgrade to 0.0.10

Memory:
  ✓ new-memory-item → added to manifest

Apply all changes now?
(y) Yes, execute all
(n) No, cancel everything
(d) Dry run (show commands without executing)
(s) Save decisions for later (create pending-merge.yaml)
> y

Executing...

[Settings]
  ✓ Updated ~/.claude/settings.json (model=opus[1m])
  ✓ Added customTheme to manifest as mac override

[Permissions]
  ✓ Merged permissions into ~/.claude/settings.local.json
  ✓ Updated manifest with new universal rules

[Skills]
  ✓ Copied literature-review to assets/skills/
  ✓ Updated manifest
  ✓ Installed read-paper skill

[Plugins]
  ✓ Running: claude plugin install claude-hud@claude-hud --version 0.0.10

[Memory]
  ✓ Copied new-memory-item.md to assets/memory/
  ✓ Updated manifest

Sync complete!
Run /claude-config status to see current state.
```

---

## track 命令（双向支持）

```
/claude-config track [--platform <name>]

Scanning ~/.claude/ for new configurations...

--- New Skills ---
[NEW] my-windows-tool at ~/.claude/skills/my-windows-tool/
  Detected: Windows-specific (contains PowerShell references)
  (a) Add to manifest as windows-only
  (b) Add to manifest as universal
  (c) Skip

--- New Rules ---
[NEW] auto-commit.md at ~/.claude/rules/
  (a) Add to manifest
  (b) Skip

--- Modified Settings ---
[MODIFIED] model changed: "opus[1m]" → "sonnet"
  Last sync: manifest had "opus[1m]"
  (a) Update manifest to "sonnet"
  (b) Keep manifest, revert local on next sync
  (c) Add as platform override (different per platform)

--- New Permissions ---
[NEW] 5 permission rules in settings.local.json
  Analysis:
  - 3 universal (Bash(python:*), etc.)
  - 2 mac-specific (path contains /Users/liyao)
  (a) Add universal ones to manifest
  (b) Add all to manifest (with mac tag for path-specific)
  (c) Skip all

--- New Memory ---
[NEW] my-workflow-preference.md
  (a) Add to manifest
  (b) Skip (keep local only)
```

---

## 工作流示意（双向）

### Mac → Windows

```
Mac:
1. 配置变更（新 skill、新权限）
2. /claude-config track
   → 发现新配置，添加到 manifest
3. git commit + push

Windows:
1. git pull
2. /claude-config diff
   → 看到 Mac 新增的配置
3. /claude-config merge
   → 选择要同步的项目
4. 完成
```

### Windows → Mac

```
Windows:
1. 在 Windows 上开发了新的 PowerShell skill
2. /claude-config track
   → 发现 my-windows-tool，标记为 windows-only
3. git commit + push

Mac:
1. git pull
2. /claude-config diff
   → 看到新的 windows-only skill（自动跳过）
3. 其他通用配置同步
```

### 冲突解决

```
两边都修改了 model:
Mac: model = "opus[1m]"
Windows: model = "sonnet"

任意一边:
/claude-config merge

[DIFF] model: manifest="opus[1m]", local="sonnet"
(c) Add local value as platform override
  → manifest 保持 universal unspecified
  → mac override: opus[1m]
  → windows override: sonnet

这样两边都能保持各自的偏好，同时 manifest 记录了差异。
```

---

## 合并策略详解

### Settings 合并策略

| 策略 | 适用场景 | 实现 |
|------|----------|------|
| replace | 基础设置（model, effortLevel） | 直接替换字段 |
| merge_unique | 权限列表 | 合并后去重 |
| platform_override | 平台差异设置 | 按 platform 字段分开存储 |

### Permissions 三层结构

```
manifest.yaml:
  permissions:
    universal: [...]    # 所有平台合并
    mac: [...]          # 只 mac 合并
    windows: [...]      # 只 windows 合并

apply 时:
  local = local + universal + ${platform}
  （去重合并）
```

### 路径模板化（可选）

对于包含用户路径的权限规则：

```
原始: "Bash(python -m json.tool /Users/liyao/paper-read-skills/...)"
模板: "Bash(python -m json.tool ${HOME}/paper-read-skills/...)"

apply 时:
  Mac: ${HOME} = /Users/liyao
  Windows: ${HOME} = C:\Users\liyao
```

**注意**：这需要 skill 支持路径替换逻辑，目前可选。

---

## 已确认事项

| 事项 | 决定 |
|------|------|
| Memory 迁移 | **迁移**（行为偏好，非项目特定） |
| Teams 迁移 | **不迁移**（项目特定） |
| 代理/认证 | **不处理**（由 cc switch 管理） |
| Permissions | **分类**：universal + mac + windows |
| 流转方向 | **双向**（Mac ↔ Windows） |
| 合并方式 | **交互式**（diff + merge） |

---

## 当前用户配置现状

### Skills（4个）
- github/SKILL.md
- literature-review/SKILL.md
- read-paper/SKILL.md
- research-brainstorm/SKILL.md

### Plugins（2个）
- superpowers@claude-plugins-official (v5.0.5)
- claude-hud@claude-hud (v0.0.10)

### Settings
- settings.json：model, effortLevel, statusLine, enabledPlugins
- settings.local.json：~40 条 permissions.allow 规则

### Memory（3个）
- claude-code-teammate-workflow.md
- collaboration-preference-ask-user-question.md
- ask-user-question-strictly-preferred.md

### Hooks/Statusline
- statusline.sh（bash 脚本）

---

## 模拟验证发现的问题与修订

以下问题通过实际配置的 diff/merge 模拟发现，需要对设计进行补充。

### 问题 1：`includeCoAuthoredBy` 字段未覆盖

**发现**：settings.json 中有 `includeCoAuthoredBy: false` 字段，设计文档未提及。

**含义**：控制是否在 git commit 中添加 "Co-authored-by: Claude"。

**修订**：添加到 base.json 可选字段：

```json
// assets/settings/base.json
{
  "model": "opus[1m]",
  "effortLevel": "high",
  "hasCompletedOnboarding": true,
  "includeCoAuthoredBy": false    // 新增
}
```

---

### 问题 2：垃圾权限规则识别

**发现**：settings.local.json 中存在疑似调试遗留的规则：
```
"Bash(printf \"%s\" $?)"
"Bash(printf \"---\\\\n\")"
```

**问题**：这些不是有意义的权限，可能是测试时临时授权的残留。

**修订**：merge 流程增加"垃圾规则检测"：

```
[GARBAGE DETECTED] 2 rules appear to be debug artifacts:
  - "Bash(printf \"%s\" $?)"
  - "Bash(printf \"---\\\\n\")"

  These don't match typical permission patterns.
  Choose action:
  (a) Keep local only (don't sync)
  (b) Remove from local too (cleanup recommended) ← 新增选项
  (c) Add to manifest
```

**垃圾规则检测逻辑**：
- 命令片段而非完整命令（如 `printf` 单独出现）
- 包含 `$?`、`\\n` 等 shell 特殊字符
- 非标准工具名称

---

### 问题 3：Skills 的 shell 依赖检测

**发现**：设计假设 skills 可以标记 platforms，但 SKILL.md 内部可能包含 bash 命令。

**问题**：一个 skill 标记为 `[all]` 但内部使用 bash，在 Windows 上可能失败。

**修订**：export/track 时扫描 SKILL.md 内容，检测 shell 依赖：

```
[SKILL] my-tool at ~/.claude/skills/my-tool/
  Shell dependency analysis:
  ⚠ Contains: bash script execution (line 15)
  ⚠ Contains: /bin/sh path reference (line 23)

  Detected platform: mac/linux (bash-dependent)

  Choose action:
  (a) Add to manifest as mac/linux only (recommended)
  (b) Add to manifest as universal (may fail on Windows)
  (c) Add with conversion notes (need Windows equivalent)
```

**检测关键词**：
- `bash`, `/bin/bash`, `/bin/sh`
- `.sh` 文件引用
- `set -e`, `#!/usr/bin/env bash` 等 shebang
- `$?`, `$HOME`, `${VAR}` 等 bash 变量语法

---

### 问题 4：statusline Windows 方案

**发现**：当前 statusline.sh 使用 bash + bun/node，在 Windows 上需要明确方案。

**选项分析**：

| 方案 | 优点 | 缺点 |
|------|------|------|
| Git Bash 执行 .sh | 最简单，无需重写 | 需要 Git Bash 环境 |
| 创建 .ps1 版本 | 纯 Windows | 需要维护两份代码 |
| 跨平台 JS 脚本 | 单一代码源 | 需要 node/bun 环境 |

**修订**：默认方案为 Git Bash 执行 .sh（Windows 用户通常有 Git），可选 .ps1：

```yaml
hooks:
  statusline:
    mac:
      source: assets/hooks/mac/statusline.sh
      target: ~/.claude/statusline.sh
      runtime: bash
    windows:
      source: assets/hooks/windows/statusline.sh   # 使用同一 .sh
      target: ~/.claude/statusline.sh
      runtime: git-bash                            # 通过 Git Bash 执行
      # 可选替代：
      # source: assets/hooks/windows/statusline.ps1
      # target: ~/.claude/statusline.ps1
      # runtime: powershell
```

**settings.json 处理**：

```
Windows 上的 statusLine 配置：
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline.sh"   // 明确用 bash 执行
  }
}
```

---

### 问题 5：hooks 与 plugins 的依赖关系

**发现**：statusline.sh 调用 claude-hud plugin，两者有依赖关系。

**问题**：如果先复制 statusline.sh 但 plugin 未安装，会失败。

**修订**：manifest.yaml 增加 `depends_on` 字段：

```yaml
hooks:
  statusline:
    depends_on: [claude-hud]        # 新增：依赖的 plugin
    mac:
      source: assets/hooks/mac/statusline.sh
      target: ~/.claude/statusline.sh
```

**apply 执行顺序调整**：

```
1. Install plugins first
2. Wait for plugin installation complete
3. Then copy hooks that depend on plugins
4. Update settings.json
```

---

### 问题 6：settings.json 中 plugins 字段处理

**发现**：`enabledPlugins` 和 `extraKnownMarketplaces` 由 plugin 安装自动填充。

**明确处理方式**：

**export 时**：从 settings.json 提取时，移除这些字段：
```
Settings fields to EXCLUDE from base.json:
- enabledPlugins        → managed by plugins.yaml
- extraKnownMarketplaces → managed by plugins.yaml
- statusLine.command    → managed by hooks section
```

**apply 时**：这些字段由 plugin 安装自动填充，无需手动写入。

**base.json 最终内容**：
```json
{
  "model": "opus[1m]",
  "effortLevel": "high",
  "hasCompletedOnboarding": true,
  "includeCoAuthoredBy": false
}
```
（不含 plugins、statusLine、env）

---

### 问题 7：项目路径权限处理策略

**发现**：大量包含 `/Users/liyao/paper-read-skills` 等项目路径的权限规则。

**分析**：
- 这些路径在不同机器上不存在
- 即使路径存在，项目位置可能不同
- `.worktrees/` 是 git worktree 特有结构

**修订策略**：项目路径权限**不迁移**，分类为"local-only"：

```
[PATH-SPECIFIC] 24 rules contain project paths:
  - paper-read-skills/.worktrees/... (11 rules)
  - research-reading-skills/tests/bin/... (6 rules)
  ...

  Analysis:
  - These are temporary permissions for specific project work
  - Paths won't exist on other machines
  - Projects may be in different locations

  Choose action:
  (a) Keep local only (recommended)
  (b) Template with ${HOME} (requires same project structure on target)
  (m) Review individually
```

**建议**：在新机器上工作时，按需授权新的项目路径权限。

---

### 问题 8：`python3` vs `python` 跨平台兼容性

**发现**：`Bash(python3:*)` 在 Windows 上可能不工作（通常是 `python`）。

**分析**：
- Mac/Linux：`python3` 是标准
- Windows：`python` 是标准（`python3` 可能不存在）

**修订**：permissions-universal.json 使用兼容语法：

```json
{
  "permissions": {
    "allow": [
      "Bash(python:*)",        // 改为 python（两边都有）
      "Bash(python3:*)",       // 保留（Mac 需要）
      // 或者合并为：
      "Bash(python*:*)"        // 匹配 python 和 python3
    ]
  }
}
```

**Windows 特定补充**：

```json
// permissions-windows.json
{
  "permissions": {
    "allow": [
      "Bash(powershell:*)",
      "Bash(cmd:*)",
      "Bash(python:*)"          // 确保 Windows 有 python 权限
    ]
  }
}
```

---

### 问题 9：Memory 文件验证

**验证结果**：当前 3 个 memory 文件均不包含路径引用：

| 文件 | 内容类型 | 路径引用 |
|------|----------|----------|
| claude-code-teammate-workflow.md | 工作流偏好 | 无 |
| collaboration-preference-ask-user-question.md | 协作偏好 | 无 |
| ask-user-question-strictly-preferred.md | 交互偏好 | 无 |

**结论**：Memory 可以安全迁移，无需特殊处理。

---

## 设计修订汇总

| # | 问题 | 修订内容 |
|---|------|----------|
| 1 | `includeCoAuthoredBy` | 添加到 base.json |
| 2 | 垃圾权限规则 | merge 增加"建议清理"选项 |
| 3 | Skills shell 依赖 | export 时扫描检测，提示平台兼容性 |
| 4 | statusline Windows | 默认 Git Bash 执行 .sh，可选 .ps1 |
| 5 | hooks 依赖 plugins | manifest 增加 `depends_on`，调整 apply 顺序 |
| 6 | settings plugins 字段 | export 时排除，apply 时自动填充 |
| 7 | 项目路径权限 | 分类为 local-only，不迁移 |
| 8 | python3/python | 使用兼容语法或分平台配置 |
| 9 | Memory 验证 | 确认无路径引用，可安全迁移 ✓ |

---

## 工具同步与更新

### Skill 类型定义

| 类型 | 上游仓库 | 可读 | 可写 | 典型场景 |
|------|---------|------|------|---------|
| `self` | 配置仓库本身 | ✓ | ✓ | skill 和配置放一起 |
| `third-party` | 社区仓库 | ✓ | ✗ | 使用他人开发的 skill |

### manifest.yaml upstream 字段格式

```yaml
skills:
  # 自制 skill（存在本仓库）
  my-skill:
    source: assets/skills/my-skill
    platforms: [all]
    upstream:
      type: self                    # 本仓库内

  # 自制 skill（有独立仓库）
  my-shared-skill:
    source: assets/skills/my-shared-skill
    platforms: [all]
    upstream:
      type: self
      repo: github:user/my-shared-skill
      ref: main
      last_sync: "2026-03-28"

  # 第三方 skill
  third-party-skill:
    source: assets/skills/third-party-skill
    platforms: [all]
    upstream:
      type: third-party
      repo: github:xxx/repo
      ref: main
      last_sync: "2026-03-28"
```

### 新增命令

| 命令 | 功能 |
|------|------|
| `add-skill` | 从本地或仓库添加 skill |
| `check-updates` | 检查有远程上游的 skills 更新 |
| `update-skill` | 从上游拉取最新版本 |
| `push-skill` | 推送自制 skill 到远程仓库 |

### 工作流示意

```
[上游仓库] ──pull──> [配置仓库 assets/skills/] ──apply──> [~/.claude/skills/]
     │                     │                              │
     │                     │<──export/track────────────────┘
     │                     │
     └──push（仅自制）──────┘
```