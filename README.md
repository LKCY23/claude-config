# claude-config

Claude Code 配置管理工具 - 跨机器配置同步解决方案。

## 功能

- **双向流转** - Mac ↔ Windows ↔ Linux 配置同步
- **多设备协同** - sync 命令，分叉检测 + 交互合并，不怕多台机器各自演进
- **状态驱动初始化** - 检测本地/远程状态，按实际情况引导，不做假设
- **Marketplace 集成** - 通过自己的 marketplace 统一管理 skill，版本精确锁定（参考实现：[claudespace](https://github.com/LKCY23/claudespace)）
- **四层架构** - Bootstrap / Framework / Data / Marketplace，框架和数据彻底解耦
- **三层安装** - Plugin（marketplace）/ Static config（文件）/ External tools（分级安装）
- **平台差异处理** - 自动处理平台特定配置
- **交互式合并** - diff + merge 解决冲突
- **敏感信息保护** - 不追踪 API keys、OAuth sessions

---

## 架构

```
install.sh（装引擎，不造车）
    │
    ▼
~/.claude-config-tool/    Layer 1: Framework（"怎么做"）
    │  读取/写入
    ▼
~/claude-config-data/     Layer 2: Data（"装什么"）
    │  引用
    ▼
marketplaces              Layer 3: Marketplace（"从哪拿"）
```

| 层 | 路径 | 职责 | 更新方式 |
|---|------|------|---------|
| Bootstrap | install.sh | 一次性：把框架装上 | 新机器执行一次 |
| Framework | `~/.claude-config-tool/` | 定义所有命令 | `/claude-config update-self` |
| Framework Config | `~/.claude-config-tool/config.yaml` | 机器特定设置（如配置目录路径） | install.sh 自动创建，不进 git |
| Data | `~/claude-config-data/` | 用户的配置声明 | git（可选远程） |
| Marketplace | 你的 marketplace + 三方 | 提供 plugin 源码 | marketplace catalog |

---

## 安装

### macOS / Linux

```bash
# 一键安装框架
curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/master/install.sh | bash
```

### Windows

**⚠ WSL 警告**：如果你的 Windows 安装了 WSL，在 cmd.exe 中运行 `curl | bash` 会触发 WSL 的 bash，安装到 WSL 路径。请选择以下方式之一：

**方式一：PowerShell 一键安装（推荐）**

```powershell
iwr -useb https://raw.githubusercontent.com/LKCY23/claude-config/master/install.ps1 | iex
```

**方式二：Git Bash**

打开 **Git Bash** 应用（不是 cmd.exe 或 WSL）：

```bash
curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/master/install.sh | bash
```

**方式三：手动安装**

```powershell
# 1. 克隆框架
git clone https://github.com/LKCY23/claude-config.git "$env:USERPROFILE\.claude-config-tool"

# 2. 安装 bootstrap skill
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\skills\claude-config"
Copy-Item "$env:USERPROFILE\.claude-config-tool\SKILL.md" "$env:USERPROFILE\.claude\skills\claude-config\"
```

---

## 安装后 — 两条路径

安装完框架后，根据你有没有配置仓库，走不同的路：

### 路径 A：从零开始

在 Claude Code 中：

```
/claude-config init
```

按提示选择纯本地或 git 模式，框架会创建模板、引导你关联 GitHub。

### 路径 B：已有配置仓库

```bash
git clone https://github.com/<your-username>/my-claude-config.git ~/claude-config-data
```

然后在 Claude Code 中：

```
/claude-config sync --apply
```

---

## 自定义配置目录

默认配置目录是 `~/claude-config-data`。要改为其他路径：

**方式一：编辑 config.yaml（推荐）**

```bash
# 编辑 ~/.claude-config-tool/config.yaml
config_dir: /your/custom/path
```

之后所有命令自动使用此路径。

**方式二：命令行参数**

```
/claude-config apply --config-dir /your/custom/path
```

每次需要手动指定，适合临时使用。

**优先级**：`--config-dir` 参数 > `config.yaml` > `~/claude-config-data`

---

## 日常使用

### 最常用命令

```
/claude-config sync --apply
```

做了什么：检查远程是否有更新 → 有就拉下来 → 安装所有 plugin/settings/memory/hooks/external tools。覆盖 90% 的日常场景。

### 离线修改

```
# 改了 manifest.yaml
/claude-config apply       # 纯本地，不需要网络

# 有网后
/claude-config sync        # 检测到本地超前 → 提示 push
```

### 多设备协同

```
桌面机:  改配置 → git commit → /claude-config sync（push）
笔记本:  /claude-config sync --apply（pull + apply）
```

如果两台机器各自改了配置导致分叉，sync 会检测到并引导你交互式合并。

---

## 命令

| 命令 | 功能 | 说明 |
|------|------|------|
| `init` | 初始化配置仓库 | **状态驱动**，检测本地/远程后引导 |
| `apply` | 安装配置到当前机器 | **纯本地**，三阶段执行 |
| `sync` | 同步配置仓库 | **新增**，多设备协同 |
| `remote` | 管理远程关联 | **新增**，`add`/`remove`/`status` |
| `update-self` | 更新框架自身 | **新增**，升级 claude-config 工具 |
| `status` | 查看同步状态 | 含本地/远程同步状态 |
| `diff` | 对比本地与配置差异 | |
| `merge` | 交互式合并 | 逐项解决冲突 |
| `track` | 发现未追踪配置 | |
| `export` | 导出配置到清单 | |
| `validate` | 验证配置完整性 | |
| `add-skill` | 添加 skill | |
| `check-updates` | 检查上游更新 | |
| `update-skill` | 更新 skill | |
| `push-skill` | 推送修改到远程 | |
| `add-tool` | 添加第三方工具 | subtree 管理 |
| `sync-upstream` | 同步上游更新 | |

---

## 平台差异

| 功能 | macOS | Windows | Linux |
|------|-------|---------|-------|
| 安装脚本 | ✓ curl \| bash | ✓ PowerShell 一键 / Git Bash | ✓ curl \| bash |
| Hooks | bash 脚本 | PowerShell 或 Git Bash | bash 脚本 |
| Statusline | statusline.sh | statusline.ps1 或 Git Bash | statusline.sh |
| 路径格式 | `~/` | `$env:USERPROFILE\` 或 `~` (Git Bash) | `~/` |

---

## 目录结构

```
~/.claude-config-tool/       # 框架（公开仓库）
├── SKILL.md
├── DESIGN.md
├── README.md
├── config.yaml              # 机器特定配置（install.sh 创建，不进 git）
├── install.sh               # Unix/Linux/Git Bash 安装脚本
├── install.ps1              # PowerShell 安装脚本
└── templates/
    ├── config.template.yaml
    ├── manifest.template.yaml
    └── plugins.template.yaml

~/claude-config-data/        # 你的私有配置（由你管理）
├── manifest.yaml            # 配置清单
├── plugins.yaml             # 插件清单
└── assets/
    ├── skills/              # 仅 claude-config（bootstrap skill）
    ├── memory/              # 行为偏好
    ├── settings/            # settings 和 permissions
    ├── hooks/               # 平台特定 hooks
    └── templates/
```

marketplace 与 claude-config 的关系：

```
<your-marketplace> (你自己的 marketplace 仓库)
       │
       │  marketplace.json 定义 plugin 目录
       │  Mode 1 (self-hosted): 用户自己的 skill
       │  Mode 2 (external ref): 三方 skill，sha 锁版本
       │
       ▼
<config-dir>/plugins.yaml           ← 引用你的 marketplace
       │
       │  /claude-config apply
       ▼
claude plugin install xxx@<your-marketplace>  ← 安装到本地
```

> 参考实现：[LKCY23/claudespace](https://github.com/LKCY23/claudespace) — 可 fork 作为起点。

---

## manifest.yaml 示例

```yaml
version: 1
metadata:
  name: my-claude-config
  config_repo:
    local: ~/claude-config-data
    remote: ""

# 只有 claude-config 自身保留在 skills 段（bootstrap skill）
# 其他所有 skill 通过 plugins.yaml → marketplace 安装
skills:
  claude-config:
    source: assets/skills/claude-config
    platforms: [all]
    description: 跨机器配置管理工具（bootstrap）

settings:
  base:
    source: assets/settings/base.json
    merge_strategy: replace
    platforms: [all]

permissions:
  universal:
    source: assets/settings/permissions-universal.json
    merge_strategy:
      path: permissions.allow
      mode: merge_unique
    platforms: [all]

# 外部工具分级安装
external:
  codex-cli:
    strategy: auto           # 硬依赖，apply 自动装
    install: "npm install -g @openai/codex && codex login"
    setup:
      verify:
        command: codex --help

  my-tool:
    strategy: prompt         # 软依赖，apply 询问用户
    install: "npm install -g my-tool"
    setup:
      verify:
        command: my-tool --help

  complex-service:
    strategy: manual         # 复杂设置，apply 仅提示文档
    setup:
      authority:
        url: https://github.com/xxx/xxx
      env:
        required: [SERVICE_API_KEY]
```

---

## 多机器同步

### 日常同步

```bash
# 在 Claude Code 中（最常用）
/claude-config sync --apply
```

### 仅同步数据（不安装）

```bash
/claude-config sync
```

---

## 详细文档

见 [DESIGN.md](DESIGN.md)
