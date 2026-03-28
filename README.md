# claude-config

Claude Code 配置管理工具 - 跨机器配置同步解决方案。

## 功能

- **双向流转** - Mac ↔ Windows ↔ Linux 配置同步
- **平台差异处理** - 自动处理平台特定配置
- **交互式合并** - diff + merge 解决冲突
- **敏感信息保护** - 不追踪 API keys、OAuth sessions
- **第三方工具集成** - git subtree 管理，支持本地定制

---

## 安装

### macOS / Linux

```bash
# 一键安装
curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/master/install.sh | bash

# 克隆你的私有配置仓库
git clone https://github.com/<your-username>/my-claude-config.git ~/claude-config-data
```

### Windows

**⚠ WSL 警告**：如果你的 Windows 安装了 WSL，在 cmd.exe 中运行 `curl | bash` 会触发 WSL 的 bash，安装到 WSL 路径（/root/），而不是 Windows。请选择以下方式之一：

**方式一：PowerShell 一键安装（推荐）**

```powershell
# 一键安装（在 PowerShell 中运行）
iwr -useb https://raw.githubusercontent.com/LKCY23/claude-config/master/install.ps1 | iex

# 克隆你的私有配置仓库
git clone https://github.com/<your-username>/my-claude-config.git "$env:USERPROFILE\claude-config-data"
```

**方式二：Git Bash**

打开 **Git Bash** 应用（不是 cmd.exe 或 WSL）：

```bash
# 一键安装
curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/master/install.sh | bash

# 克隆你的私有配置仓库
git clone https://github.com/<your-username>/my-claude-config.git ~/claude-config-data
```

**方式三：手动安装**

```powershell
# 1. 克隆工具
git clone https://github.com/LKCY23/claude-config.git "$env:USERPROFILE\.claude-config-tool"

# 2. 安装 skill
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\skills\claude-config"
Copy-Item "$env:USERPROFILE\.claude-config-tool\SKILL.md" "$env:USERPROFILE\.claude\skills\claude-config\"

# 3. 克隆私有配置仓库
git clone https://github.com/<your-username>/my-claude-config.git "$env:USERPROFILE\claude-config-data"
```

---

## 快速开始

### 第一次使用（创建配置仓库）

在 Claude Code 中：

```
/claude-config init
```

按提示创建私有配置仓库。

### 已有配置仓库（新机器）

安装完成后，在 Claude Code 中：

```
/claude-config apply
```

Windows 用户指定平台：

```
/claude-config apply --platform windows
```

---

## 平台差异

| 功能 | macOS | Windows | Linux |
|------|-------|---------|-------|
| 安装脚本 | ✓ curl \| bash | ✓ PowerShell 一键 / Git Bash | ✓ curl \| bash |
| Hooks | bash 脚本 | PowerShell 或 Git Bash | bash 脚本 |
| Statusline | statusline.sh | statusline.ps1 或 Git Bash | statusline.sh |
| 路径格式 | `~/` | `$env:USERPROFILE\` 或 `~` (Git Bash) | `~/` |

### Windows 特殊说明

1. **安装环境选择**：
   - **PowerShell**：推荐，一键安装，无 WSL 干扰
   - **Git Bash**：命令与 Mac/Linux 一致，需手动打开 Git Bash 应用
   - **cmd.exe + curl \| bash**：会被 WSL 捕获，不推荐（除非你确实想安装到 WSL）

2. **Hooks**：
   - Mac/Linux 使用 `bash` 执行 `.sh` 脚本
   - Windows 可选择 Git Bash 执行 `.sh` 或 PowerShell 执行 `.ps1`

3. **路径**：
   - Git Bash: `~/.claude/`（与 Mac 一致）
   - PowerShell: `$env:USERPROFILE\.claude\`

---

## 命令

| 命令 | 功能 |
|------|------|
| `init` | 初始化新的配置仓库 |
| `apply` | 安装配置到当前机器 |
| `status` | 查看同步状态 |
| `diff` | 对比本地与配置差异 |
| `merge` | 交互式合并 |
| `track` | 发现未追踪配置 |
| `export` | 导出配置到清单 |
| `validate` | 验证配置完整性 |
| `add-skill` | 添加 skill（本地或远程） |
| `check-updates` | 检查上游更新 |
| `update-skill` | 更新 skill |
| `push-skill` | 推送修改到远程 |
| `add-tool` | 用 subtree 添加第三方工具 |
| `sync-upstream` | 同步 subtree 上游更新 |

---

## 目录结构

```
~/.claude-config-tool/       # 工具框架（公开）
├── SKILL.md
├── templates/
├── install.sh              # Unix/Linux/Git Bash 安装脚本
└── install.ps1             # PowerShell 安装脚本

~/claude-config-data/        # 你的私有配置（私有仓库）
├── manifest.yaml            # 配置清单
├── plugins.yaml             # 插件清单
├── scripts/                 # subtree 管理脚本
│   ├── add-tool.sh
│   └── sync-upstream.sh
└── assets/
    ├── skills/              # 自定义 skills
    ├── memory/              # 行为偏好 memory
    ├── settings/            # settings 和 permissions
    └── hooks/
        ├── mac/             # Mac hooks
        └── windows/        # Windows hooks
```

---

## manifest.yaml 示例

```yaml
version: 1
metadata:
  name: my-claude-config
  config_repo:
    local: ~/claude-config-data
    remote: github:username/my-claude-config

skills:
  my-skill:
    source: assets/skills/my-skill
    platforms: [all]
    upstream:
      type: self

  third-party-skill:
    source: assets/skills/third-party-skill
    platforms: [all]
    upstream:
      type: third-party
      repo: github:xxx/skill
      ref: main
      subtree: true
      last_sync: "2026-03-28"
```

---

## 多机器同步

### 日常同步

```bash
# 拉取最新配置
cd ~/claude-config-data && git pull

# 在 Claude Code 中应用
/claude-config apply
```

Windows (PowerShell):
```powershell
cd "$env:USERPROFILE\claude-config-data"
git pull
# 然后在 Claude Code 中: /claude-config apply --platform windows
```

---

## 详细文档

见 [DESIGN.md](DESIGN.md)