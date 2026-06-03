# Claude Config - 跨机器配置管理方案

## 背景

Claude Code 的配置分布在多个目录和文件中，目前没有官方的跨机器同步方案。本方案旨在提供一个声明式清单 + Skill 驱动的配置管理工具，支持：

- **双向流转**（Mac ↔ Windows ↔ Linux），非单向迁移
- 跨平台差异处理
- 版本控制（git 追踪配置变更）
- 选择性配置（按平台、按场景）
- **多设备协同**（sync 命令，分叉检测 + 交互合并）
- **状态驱动初始化**（检测本地/远程状态，按需引导）
- **交互式合并**（diff + 逐项解决冲突）
- 敏感信息保护

---

## 架构总览

### 四层架构

claude-config 的完整架构分为四层，加上 apply 执行时的三个 Phase：

```
┌──────────────────────────────────────────────────────────────┐
│                   Layer 0: Bootstrap（一次性入口）             │
│                                                              │
│  install.sh / install.ps1                                    │
│  ├─ git clone claude-config → ~/.claude-config-tool/        │
│  └─ cp SKILL.md → ~/.claude/skills/claude-config/           │
│                                                              │
│  职责: 把框架装上，让它能用。不创建配置目录，不初始化数据。      │
│  执行: 每台新机器执行一次，之后再也不用。                       │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                   Layer 1: Framework（工具层）                 │
│                                                              │
│  ~/.claude-config-tool/                                      │
│  ├─ SKILL.md            ← 核心：定义所有命令                   │
│  ├─ DESIGN.md           ← 本文档                             │
│  ├─ install.sh/.ps1     ← 新机器入口                          │
│  └─ templates/          ← init 时复制给用户的模板              │
│                                                              │
│  职责: 定义"怎么做"（apply/sync/init/diff/merge...）          │
│  更新: /claude-config update-self                            │
│  不包含: 任何用户数据、配置内容、skill 源码                     │
└──────────────────────────────────────────────────────────────┘
                              │
                              │  读取/写入
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                   Layer 2: Data（数据层）                      │
│                                                              │
│  ~/claude-config-data/  (用户私有 git repo)                   │
│  ├─ manifest.yaml       ← 声明"装什么"                        │
│  ├─ plugins.yaml        ← 声明"从哪装"                        │
│  └─ assets/             ← settings, memory, hooks, ...       │
│                                                              │
│  职责: 描述"装什么"（配置的声明式描述）                         │
│  管理: git（可选远程），支持纯本地模式                          │
│  不包含: 安装逻辑、平台检测、合并策略的实现                     │
└──────────────────────────────────────────────────────────────┘
                              │
                              │  引用
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                Layer 3: Marketplace（来源层）                  │
│                                                              │
│  <你的 marketplace> + 三方 marketplaces                        │
│  ├─ marketplace.json     ← plugin 目录 + 版本锁定             │
│  └─ skills/*/            ← plugin 源码 (Mode 1: submodule,   │
│                             Mode 2: external ref + sha)      │
│                                                              │
│  职责: 提供 plugin 的源码和版本元数据                           │
│  不包含: 用户的 plugin 选择、安装顺序                           │
└──────────────────────────────────────────────────────────────┘
```

### Apply 的三个 Phase（在 Layer 2 数据驱动下执行）

apply 是纯本地操作——读取本地 manifest.yaml，安装到当前机器。git 同步是 sync 命令的职责。

```
=== Phase 1: Marketplace & Plugins（全自动）===
1. 读取 plugins.yaml
2. 注册未注册的 marketplace
3. 安装所有 plugin（claude plugin install <pkg>@<marketplace>）
4. 版本由 marketplace catalog 锁定，apply 不管版本

=== Phase 2: Static Config（全自动）===
5. 安装 bootstrap skill（claude-config SKILL.md）
6. 复制 memory 文件（assets/memory/ → ~/.claude/memory/）
7. 合并 settings（base.json + permissions，按 merge_strategy）
8. 安装 hooks（statusline 等，注意 plugin 依赖顺序）
9. 检查 env vars（expected + fixed）

=== Phase 3: External Tools（分级处理）===
10. 读取 manifest.yaml external 段
11. 按 strategy 分级：
    - auto:   verify → 缺失则自动安装
    - prompt: verify → 缺失则 AskUserQuestion
    - manual: verify → 缺失则打印文档链接
12. 输出安装报告
```

### 配置解析（config_dir）

每个命令执行前，按以下优先级确定配置数据目录：

```
--config-dir 参数            ← 命令行覆盖（最高优先级）
~/.claude-config-tool/config.yaml → config_dir 字段
~/claude-config-data          ← 硬编码默认值（始终生效）
```

`config.yaml` 是框架的机器特定配置文件：
- 由 `install.sh` 从 `templates/config.template.yaml` 创建（仅当不存在时）
- 在 `.gitignore` 中 → `update-self`（git pull）不会覆盖
- 用户可手动编辑修改路径
- 属于 Layer 1（Framework），不属于 Layer 2（Data），因此不与其他机器同步

---

## 配置目录状态机

### 核心原则

不按"新用户/老用户"分类。只检测事实：**本地目录是否存在？是否 git 仓库？是否关联远程？**

框架告诉用户"你现在在状态 X，你可以做 Y"，不做预设。

### 六种状态

```
                    无远程仓库        有远程仓库
              ┌─────────────────┬─────────────────┐
 无本地目录    │  A: 什么都没有    │  D: 别人分享给你   │
              │  → init          │  → git clone      │
              ├─────────────────┼─────────────────┤
 有本地目录    │  B: 纯本地模式    │  E: 完整协同模式   │
 (非 git)     │  → 本地管理      │  → remote add     │
              ├─────────────────┼─────────────────┤
 有本地目录    │  C: 本地 git     │  F: 已关联远程     │
 (是 git)     │  → 随时可加远程  │  → apply / sync   │
              └─────────────────┴─────────────────┘
```

### 状态检测逻辑

```
detect_state(config_dir):
  if not exists(config_dir)           → A
  if not is_git_repo(config_dir)      → B
  if not has_remote(config_dir)       → C
                                      → F
```

### 各状态的 skill 命令入口

| 当前状态 | 用户意图 | 命令 |
|---------|---------|------|
| A (空空) | 只想本地用 | `init --local` → B |
| A (空空) | 要 git 但先不推 | `init` → C |
| A (空空) | 已有远程仓库 | 直接 `git clone <url>` → F |
| B (本地目录, 非 git) | 想加版本管理 | `init --git` → C |
| C (本地 git) | 想关联远程 | `remote add <url>` → F |
| F (已关联) | 想断掉远程 | `remote remove` → C |
| F (已关联) | 应用配置 | `apply` (纯本地) |
| F (已关联) | 同步远程 | `sync` |

### init 命令行为（状态驱动）

```
/claude-config init [--config-dir <path>] [--local] [--git]

1. 检测当前状态 → 告诉用户

   状态 A (空空):
     "没有检测到配置目录。你可以：
      (a) 创建纯本地配置目录 (--local)
      (b) 创建本地 git 仓库，暂不关联远程 (--git)
      (c) 先创建 GitHub 仓库再 git clone（会提示你 URL）"

   状态 B (本地目录, 非 git):
     "检测到 ~/claude-config-data，但没有 git 版本管理。你可以：
      (a) 保持纯本地模式（无需操作）
      (b) 为此目录初始化 git (--git)"

   状态 C (本地 git, 无远程):
     "配置目录已有 git 管理但未关联远程仓库。你可以：
      (a) 保持本地模式（无需操作）
      (b) 关联远程仓库: /claude-config remote add <url>"

   状态 F (已关联):
     "配置目录已就绪。
      远程: github:username/repo
      下一步: /claude-config apply"

2. 根据用户选择执行：
   - --local: mkdir + 复制模板（不 git init）
   - --git:   mkdir + git init + 复制模板
   - 其他:    不做假设，提示用户
```

---

## Sync 命令（多设备协同）

### 为什么需要 sync

apply 的 git pull 只能处理"远程有新东西"这一种情况。多设备场景下：

```
桌面机                       GitHub                      笔记本
├─ 加了一个 skill            │                           ├─ 改了一个 setting
├─ git push                  │                           ├─ git push → ✗ 冲突
                             │
如果笔记本 git push -f → 桌面机的 skill 丢失
如果笔记本 git pull → merge conflict，用户不知道怎么解决
```

sync 封装了完整的协同逻辑。

### 四象限模型

```
/claude-config sync [--apply]

1. 检测状态（需要处于状态 F，否则提示先 remote add）
2. git fetch
3. 比较本地和远程：

┌──────────┬───────────┬──────────────────────────────────┐
│ 本地超前  │ 远程超前   │ 处理                              │
├──────────┼───────────┼──────────────────────────────────┤
│ ✗        │ ✗         │ 已同步，无事可做                    │
│ ✓        │ ✗         │ 提示用户 push（显示未推送的 commit） │
│ ✗        │ ✓         │ 自动 pull（fast-forward）          │
│ ✓        │ ✓         │ 分叉 → 交互式解决                   │
└──────────┴───────────┴──────────────────────────────────┘

4. 如果 --apply：sync 成功后自动执行 apply
```

### 分叉处理（最关键）

```
本地和远程分叉了（diverged）：

Detected diverged history:

  Local commits (not on remote):
    abc1234 添加 research-brainstorm skill
    def5678 更新 permissions

  Remote commits (not local):
    ghi9012 修改 model 设置
    jkl3456 添加 memory 项

  策略:
  (a) remote-first — 以远程为准，本地 commit 变成本地 stash
  (b) local-first  — 以本地为准，远程 commit 被覆盖（force push）
  (c) interactive  — 逐 commit 检查，手动 merge

  > c

  [1/4] abc1234 "添加 research-brainstorm skill"
    (a) keep (保留这个改动)
    (b) drop (放弃)
    > a

  [2/4] def5678 "更新 permissions"
    (a) keep
    (b) drop
    > a

  ...

  合并方案：git pull --rebase
  或者创建 merge commit

  执行后：
  ✓ 合并完成
  下一步: /claude-config apply
```

### sync 与 apply 的职责分离

| | apply | sync |
|---|---|---|
| 网络操作 | 无（纯本地） | 有（git fetch/pull/push） |
| 操作对象 | 安装到 ~/.claude/ | 同步 ~/claude-config-data/ |
| 日常使用 | 离线改完就装 | 有网时同步远程 |
| 组合用法 | 单独用 | `sync --apply`（日常 90% 场景） |

---

## Remote 命令

管理配置目录的远程仓库关联（状态 C ↔ F 转换）。

```
/claude-config remote status
  → 显示当前 remote 配置
     Remote: github:username/my-claude-config
     URL: https://github.com/username/my-claude-config
     Branch: master

/claude-config remote add <url>
  → C → F
     git remote add origin <url>
     git push -u origin master
     ✓ 已关联远程仓库

/claude-config remote remove
  → F → C
     确认后: git remote remove origin
     ⚠ 仅断开关联，不删除目录和 git 历史
```

---

## Update-Self 命令

框架自身更新。不需要用户记住路径和步骤。

注意：`config.yaml` 在 `.gitignore` 中（untracked），`git pull` 不会覆盖用户的本地配置。

```
/claude-config update-self

1. cd ~/.claude-config-tool
2. git fetch origin
3. 比较版本：
   ├─ 已最新 → 无事
   └─ 有更新 → 显示 changelog，询问是否更新
4. 更新:
   git pull --ff-only
   cp SKILL.md ~/.claude/skills/claude-config/
5. ✓ 框架已更新到 <version>
   → 如有 breaking change，提示查看 DESIGN.md
```

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
~/.claude-config-tool/       # Layer 1: Framework（公开仓库）
├── SKILL.md                  # 核心执行指令
├── DESIGN.md                 # 设计文档（本文件）
├── README.md
├── install.sh                # Unix/Linux/Git Bash 安装脚本
├── install.ps1               # PowerShell 安装脚本
├── config.yaml               # 机器特定配置（untracked, install.sh 创建）
├── .gitignore                # 排除 config.yaml 等本地文件
└── templates/                # init 时复制给用户的模板
    ├── config.template.yaml
    ├── manifest.template.yaml
    └── plugins.template.yaml

~/claude-config-data/         # Layer 2: Data（用户私有仓库）
├── manifest.yaml             # 配置清单（skills 仅 bootstrap claude-config）
├── plugins.yaml              # 插件清单（引用你自己的 marketplace + 三方 marketplace）
├── scripts/
├── docs/
└── assets/
    ├── skills/               # 仅 claude-config（bootstrap skill）
    ├── memory/               # 行为偏好 memory
    ├── settings/             # settings 和 permissions
    ├── hooks/                # 平台特定 hooks
    └── templates/

<你的 marketplace> + 三方 marketplaces  # Layer 3: Marketplace（来源）
```

marketplace 与 claude-config 的关系：

```
<your-marketplace> (你自己的 marketplace 仓库)
       │
       │  marketplace.json 定义 plugin 目录
       │  Mode 1 (self-hosted): 用户自己的 skill，git submodule 管理版本
       │  Mode 2 (external ref): 三方 skill，sha 锁定版本
       │
       ▼
<config-dir>/plugins.yaml           ← 引用你的 marketplace
       │
       │  /claude-config apply (Phase 1)
       ▼
claude plugin install xxx@<your-marketplace>  ← 安装到本地
```

> 参考实现：[LKCY23/claudespace](https://github.com/LKCY23/claudespace) — 可 fork 作为起点。

---

## Marketplace 机制

### 概念

Marketplace 是 plugin 的发布和版本管理渠道。每个用户应维护自己的 marketplace（例如 fork [LKCY23/claudespace](https://github.com/LKCY23/claudespace) 作为起点），在其中管理自己使用的 skill。

LKCY23/claudespace 是开源参考实现，提供两种 plugin 来源模式：

| | Mode 1: Self-hosted | Mode 2: External reference |
|---|---|---|
| 代码位置 | 在 marketplace 仓库内 (git submodule) | 在外部仓库 |
| 版本锁定 | submodule commit | marketplace.json 中的 sha |
| 更新方式 | `git submodule update --remote` | 手动更新 sha |
| 适用 | 用户自己的 skill | 三方 curated skill |

**为什么用 marketplace 而不是文件副本**：
- 版本精确锁定（commit hash），不会漂移
- shared/ 等跨 skill 依赖由上游仓库结构自然解决
- 更新可见（git diff 能看到版本变化）
- 安装统一：`claude plugin install xxx@<marketplace>`

**设计原则**：
1. **Plugin 通道统一**：所有带版本的 skill/agent/hook/MCP 走 marketplace，不用文件副本
2. **文件通道只管配置**：settings/memory/hooks 这些纯配置走 cp/merge
3. **Plugin 内部不穿透**：不试图管理 plugin 内部的 skill 列表或 shared 目录
4. **External 分级安装**：auto/prompt/manual 三级，不静默安装未审查的软件

---

## manifest.yaml 格式

```yaml
version: 1
metadata:
  name: my-claude-config
  description: 个人 Claude Code 配置清单
  last_updated: 2026-03-28
  last_sync_platform: mac         # 记录最后同步的平台
  config_repo:
    local: ~/claude-config-data
    remote: ""                     # 远程仓库地址（sync/remote 命令管理）

# 平台定义
platforms:
  mac: [darwin, macos]
  windows: [windows, win32, win64]
  linux: [linux, ubuntu, debian]

# Skills 配置
# 注意: 除 claude-config 外，所有 skill 应通过 marketplace 安装
# 通过 plugins.yaml 引用，由 claude plugin install 安装
# skills 段仅保留 bootstrap skill
skills:
  claude-config:
    source: assets/skills/claude-config
    platforms: [all]
    description: 跨机器配置管理工具（bootstrap skill）

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

# Permissions（平台分类）
permissions:
  universal:
    source: assets/settings/permissions-universal.json
    merge_strategy:
      path: permissions.allow
      mode: merge_unique
    platforms: [all]

  mac:
    source: assets/settings/permissions-mac.json
    merge_strategy:
      path: permissions.allow
      mode: merge_unique
    platforms: [mac]

  windows:
    source: assets/settings/permissions-windows.json
    merge_strategy:
      path: permissions.allow
      mode: merge_unique
    platforms: [windows]

# Hooks 配置（平台特定）
hooks:
  statusline:
    mac:
      source: assets/hooks/mac/statusline.sh
      target: ~/.claude/statusline.sh
    windows:
      source: assets/hooks/windows/statusline.ps1
      target: ~/.claude/statusline.ps1

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

# 环境变量（不管理敏感值，只检查存在性）
env:
  expected:
    - ANTHROPIC_AUTH_TOKEN
    - ANTHROPIC_BASE_URL
  fixed: {}

# 排除列表
exclude:
  - ~/.claude/config.json
  - ~/.claude.json
  - ~/.claude/teams/
  - ~/.claude/session-env/
  - ~/.claude/sessions/
  - ~/.claude/history.jsonl
  - ~/.claude/cache/
  - ~/.claude/telemetry/

# External Tools（外部工具与运行时依赖）
# 每项需指定 strategy: auto / prompt / manual
external: {}
```

---

## plugins.yaml 格式

```yaml
plugins:
  # --- 基础设施 ---
  superpowers:
    marketplace: claude-plugins-official
    package: superpowers
    platforms: [all]

  claude-hud:
    marketplace: claude-hud
    package: claude-hud
    platforms: [all]

  # --- 通过自己的 marketplace 管理的 skill ---
  deep-research:
    marketplace: <your-marketplace>
    platforms: [all]

  academic-paper:
    marketplace: <your-marketplace>
    platforms: [all]

marketplaces:
  claude-plugins-official:
    repo: anthropics/claude-plugins-official
  claude-hud:
    repo: jarrodwatts/claude-hud
  <your-marketplace>:
    repo: <your-username>/<your-marketplace-repo>
```

**字段说明**：
- `marketplace`: marketplace 名称（需先在 marketplaces 段注册）
- `package`: 插件包名（默认与 marketplace 下注册名相同时可省略）
- `platforms`: 支持的平台
- 注：`version` 字段已移除。`claude plugin install` 不支持 `--version`，版本由 marketplace catalog 锁定

### MCP Servers 追踪

```yaml
mcp_servers:
  bailian:
    source: bailian
    package: WebSearch
    description: 阿里百炼 Web Search MCP
    url: https://bailian.console.aliyun.com/
    platforms: [all]
```

---

## SKILL.md 命令设计

### 命令列表

| 命令 | 功能 | 说明 |
|------|------|------|
| `init` | 初始化配置仓库 | **状态驱动**，检测本地/远程状态后引导 |
| `apply` | 安装配置到当前机器 | **纯本地**，三阶段执行，不做 git 操作 |
| `sync` | 同步配置仓库 | **新增**，多设备协同，四象限处理 |
| `remote` | 管理远程关联 | **新增**，add/remove/status |
| `update-self` | 更新框架自身 | **新增**，git pull + 重新安装 bootstrap skill |
| `status` | 查看同步状态 | 含本地/远程同步状态 |
| `diff` | 生成差异报告 | 对比本地与清单 |
| `merge` | 交互式合并 | 逐项解决冲突 |
| `track` | 检测新配置 | 发现未追踪项 |
| `export` | 导出配置到清单 | 双向流转支持 |
| `validate` | 验证清单完整性 | 检查文件存在性 |
| `add-skill` | 添加 skill | 从本地或仓库添加 |
| `check-updates` | 检查更新 | 检查上游更新 |
| `update-skill` | 更新 skill | 拉取最新版本 |
| `push-skill` | 推送修改 | 推送到远程仓库 |
| `add-tool` | 添加第三方工具 | 用 subtree 管理 |
| `sync-upstream` | 同步上游更新 | 拉取 subtree 更新 |

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

[NEW-LOCAL] 3 rules with Mac-specific paths
[NEW-LOCAL] 2 universal rules not in manifest
[PLATFORM-MAC] 2 Mac-only rules in manifest
[PLATFORM-WINDOWS] 2 Windows-only rules in manifest → Skip for mac

--- Skills ---
[MATCH] github, research-brainstorm
[NEW-LOCAL] literature-review (not in manifest)
[MISSING] read-paper (in manifest, not installed)

--- Plugins ---
[MATCH] superpowers@5.0.5
[VERSION-DIFF] claude-hud: manifest=0.0.10, local=0.0.9

--- Memory ---
[MATCH] claude-code-teammate-workflow
[NEW-LOCAL] new-memory-item.md

=== Summary ===
Settings: 3 | Permissions: 7 | Skills: 2 | Plugins: 1 | Memory: 1
Run /claude-config merge to resolve interactively.
```

### merge 交互流程

```
/claude-config merge

Generating diff...
Found 14 items requiring decisions.

=== Session 1: Settings ===

[1/14] model: manifest="opus[1m]", local="sonnet"
  (a) Use manifest value (opus[1m])
  (b) Keep local value (sonnet)
  (c) Add local value as mac override
  (d) Update manifest to local value
  > a

[2/14] customTheme: "dark" exists locally, not in manifest
  (a) Keep local only
  (b) Add to manifest as universal
  (c) Add to manifest as mac-only
  > c

=== Session 2: Permissions ===

[3/14] Permission rules with Mac-specific paths (3 rules)
  (a) Keep local only
  (b) Keep local + template (${HOME})
  (m) Review each individually
  > m

  [3.1/14] "Bash(python -m json.tool /Users/liyao/paper-read-skills/...)"
    (a) Keep local only
    (b) Template: replace /Users/liyao with ${HOME}
    > a

... (逐项解决)

=== Decision Summary ===
Settings: 2 resolved
Permissions: 7 resolved
Skills: 2 resolved
Plugins: 1 resolved
Memory: 1 resolved

Apply all changes now?
(y) Yes  (n) No  (d) Dry run  (s) Save for later
> y

Executing...
✓ Done.
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
  (a) Update manifest to "sonnet"
  (b) Keep manifest, revert local on next sync
  (c) Add as platform override

--- New Permissions ---
[NEW] 5 permission rules in settings.local.json
  Analysis: 3 universal, 2 mac-specific
  (a) Add universal ones to manifest
  (b) Add all with platform tags
  (c) Skip all

--- New Memory ---
[NEW] my-workflow-preference.md
  (a) Add to manifest
  (b) Skip
```

---

## 工作流示意

### 新机器安装

```
1. install.sh（一次性）
   → git clone claude-config → ~/.claude-config-tool/
   → cp SKILL.md → ~/.claude/skills/claude-config/

2. 获取配置数据（二选一）：
   a) 已有远程仓库: git clone <url> ~/claude-config-data
   b) 从零开始:   /claude-config init

3. /claude-config sync --apply
   → 如果 clone 的仓库：sync 拉最新（如有）
   → 安装所有 plugin + settings + memory + hooks + external
```

### 日常同步（最常用）

```
/claude-config sync --apply

→ git fetch → 比较本地/远程
  ├─ 同步:     无事，直接 apply
  ├─ 远程超前: 自动 pull，然后 apply
  ├─ 本地超前: 提示 push（不影响 apply）
  └─ 分叉:     交互 merge，然后 apply
```

### 离线修改

```
离线时:
  修改 manifest.yaml
  /claude-config apply        # 纯本地，不需要网络

有网后:
  /claude-config sync         # 检测到本地超前 → 提示 push
```

### 设备间流转

```
Mac:
  1. 加新 skill
  2. /claude-config track → 发现新配置 → 添加到 manifest
  3. git commit + git push
  （或 /claude-config sync → 提示 push）

Windows:
  1. /claude-config sync --apply
     → 拉取 Mac 的改动
     → 自动 apply（Phase 1/2/3）
     → 平台特定项自动过滤
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

```
原始: "Bash(python -m json.tool /Users/liyao/paper-read-skills/...)"
模板: "Bash(python -m json.tool ${HOME}/paper-read-skills/...)"

apply 时:
  Mac: ${HOME} = /Users/liyao
  Windows: ${HOME} = C:\Users\liyao
```

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

## 参考实例：LKCY23 的配置现状

> 以下是 claude-config 作者 (LKCY23) 的实际配置，作为参考示例。你的配置内容会不同。

### Skills（通过 marketplace: LKCY23/claudespace）
- github/SKILL.md (Mode 1: self-hosted)
- literature-review/SKILL.md (Mode 1)
- read-paper/SKILL.md (Mode 1)
- research-brainstorm/SKILL.md (Mode 1)
- deep-research (Mode 2: external ref)
- academic-paper (Mode 2)
- academic-paper-reviewer (Mode 2)
- academic-pipeline (Mode 2)
- karpathy-llm-wiki (Mode 2)

### Plugins（13 个，5 个 marketplace）
- superpowers@claude-plugins-official
- claude-hud@claude-hud
- claude-scientific-writer
- codex@openai-codex
- 9 个来自 LKCY23/claudespace

### Settings
- settings.json：model, effortLevel, statusLine, enabledPlugins
- settings.local.json：~40 条 permissions.allow 规则

### Memory（3 个）
- claude-code-teammate-workflow.md
- collaboration-preference-ask-user-question.md
- ask-user-question-strictly-preferred.md

### Hooks/Statusline
- statusline.sh（bash 脚本）

---

## 模拟验证发现的问题与修订

### 问题 1：`includeCoAuthoredBy` 字段未覆盖

**发现**：settings.json 中有 `includeCoAuthoredBy: false` 字段，设计文档未提及。

**修订**：添加到 base.json 可选字段：

```json
{
  "model": "opus[1m]",
  "effortLevel": "high",
  "hasCompletedOnboarding": true,
  "includeCoAuthoredBy": false
}
```

### 问题 2：垃圾权限规则识别

**发现**：settings.local.json 中存在疑似调试遗留的规则：
```
"Bash(printf \"%s\" $?)"
"Bash(printf \"---\\\\n\")"
```

**修订**：merge 流程增加"垃圾规则检测"，提供"从本地删除"选项。

**垃圾规则检测逻辑**：
- 命令片段而非完整命令（如 `printf` 单独出现）
- 包含 `$?`、`\\n` 等 shell 特殊字符
- 非标准工具名称

### 问题 3：Skills 的 shell 依赖检测

**发现**：export/track 时扫描 SKILL.md 内容，检测 bash 依赖，提示平台兼容性。

**检测关键词**：`bash`, `/bin/bash`, `/bin/sh`, `.sh` 文件引用, shebang, `$?`, `$HOME`, `${VAR}`

### 问题 4：statusline Windows 方案

**默认方案**：Git Bash 执行 .sh（Windows 用户通常有 Git），可选 .ps1：

```yaml
hooks:
  statusline:
    mac:
      source: assets/hooks/mac/statusline.sh
      target: ~/.claude/statusline.sh
      runtime: bash
    windows:
      source: assets/hooks/windows/statusline.sh
      target: ~/.claude/statusline.sh
      runtime: git-bash
```

### 问题 5：hooks 与 plugins 的依赖关系

**修订**：manifest.yaml 增加 `depends_on` 字段，apply 先装 plugin 再装 hook。

### 问题 6：settings.json 中 plugins 字段处理

**明确**：`enabledPlugins` 和 `extraKnownMarketplaces` 由 plugin 安装自动填充，export 时排除，apply 时不写入。

### 问题 7：项目路径权限处理策略

**策略**：项目路径权限**不迁移**，分类为"local-only"。

### 问题 8：`python3` vs `python` 跨平台兼容性

**修订**：使用兼容语法 `Bash(python*:*)` 或分平台配置。

### 问题 9：Memory 文件验证

**结论**：Memory 可以安全迁移，无需特殊处理。✓

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
| 10 | 框架/数据解耦 | install.sh 只装框架，不初始化配置目录 |
| 11 | init 状态驱动 | 检测本地/远程状态，按需引导 |
| 12 | sync 命令 | 四象限模型，多设备协同 |
| 13 | remote 命令 | 管理远程仓库关联 |
| 14 | update-self 命令 | 框架自更新正规化 |
| 15 | apply 纯本地 | git 操作剥离到 sync |
