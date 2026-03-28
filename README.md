# claude-config

Claude Code 配置管理工具 - 跨机器配置同步解决方案。

## 功能

- **双向流转** - Mac ↔ Windows ↔ Linux 配置同步
- **平台差异处理** - 自动处理平台特定配置
- **交互式合并** - diff + merge 解决冲突
- **敏感信息保护** - 不追踪 API keys、OAuth sessions
- **第三方工具集成** - git subtree 管理，支持本地定制

## 快速开始

### 第一步：安装工具

```bash
# 一键安装
curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/main/install.sh | bash
```

这会：
- 克隆工具框架到 `~/.claude-config-tool`
- 安装 skill 到 `~/.claude/skills/claude-config/`

### 第二步：创建私有配置仓库

在 Claude Code 中执行：

```
/claude-config init
```

这个命令会引导你：
1. 在 GitHub 创建私有仓库（或使用已有的）
2. 在本地初始化配置目录
3. 设置 `config_repo` 信息

或者手动创建：

```bash
# 1. 在 GitHub 上创建一个私有仓库（如 my-claude-config）

# 2. Clone 到本地
git clone https://github.com/<your-username>/my-claude-config.git ~/claude-config-data

# 3. 初始化配置
cd ~/claude-config-data
cp ~/.claude-config-tool/templates/*.template.yaml .
mv manifest.template.yaml manifest.yaml
mv plugins.template.yaml plugins.yaml
mkdir -p assets/{skills,memory,settings,hooks/mac,hooks/windows,scripts}
cp -r ~/.claude-config-tool/scripts/* assets/scripts/ 2>/dev/null || true

# 4. 编辑 manifest.yaml，填写 config_repo 信息

# 5. 提交初始配置
git add -A && git commit -m "Initial config" && git push
```

### 第三步：应用配置

在 Claude Code 中：

```
/claude-config apply
```

---

## 已有配置仓库（新机器）

如果你已经在另一台机器上设置过配置仓库：

```bash
# 1. 安装工具
curl -fsSL https://raw.githubusercontent.com/LKCY23/claude-config/main/install.sh | bash

# 2. Clone 你的配置仓库
git clone https://github.com/<your-username>/my-claude-config.git ~/claude-config-data
```

然后在 Claude Code 中：

```
/claude-config apply
```

---

## 命令

| 命令 | 功能 |
|------|------|
| `init` | 初始化新的配置仓库 |
| `status` | 查看同步状态 |
| `diff` | 对比本地与配置差异 |
| `merge` | 交互式合并 |
| `apply` | 安装配置 |
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
└── install.sh

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
    └── hooks/               # 平台特定 hooks
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

# 或者检查差异后合并
/claude-config diff
/claude-config merge
```

---

## 详细文档

见 [DESIGN.md](DESIGN.md)