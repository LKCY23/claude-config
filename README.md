# claude-config

Claude Code 配置管理工具 - 跨机器配置同步解决方案。

## 功能

- **双向流转** - Mac ↔ Windows ↔ Linux 配置同步
- **平台差异处理** - 自动处理平台特定配置
- **交互式合并** - diff + merge 解决冲突
- **敏感信息保护** - 不追踪 API keys、OAuth sessions

## 快速开始

### 1. 创建私有配置仓库

```bash
# 克隆本仓库
git clone https://github.com/LKCY23/claude-config.git

# 创建你的私有配置仓库
mkdir ~/claude-config-data
cd ~/claude-config-data
git init

# 复制模板
cp ../claude-config/templates/*.template.yaml .
mv manifest.template.yaml manifest.yaml
mv plugins.template.yaml plugins.yaml

# 创建目录结构
mkdir -p assets/{skills,memory,settings,hooks/mac,hooks/windows}
```

### 2. 安装为 Skill

```bash
mkdir -p ~/.claude/skills/claude-config
cp claude-config/SKILL.md ~/.claude/skills/claude-config/
```

### 3. 使用

```
/claude-config status
/claude-config diff
/claude-config apply --config-dir ~/claude-config-data
```

## 命令

| 命令 | 功能 |
|------|------|
| `status` | 查看同步状态 |
| `diff` | 对比本地与配置差异 |
| `merge` | 交互式合并 |
| `apply` | 安装配置 |
| `track` | 发现未追踪配置 |
| `export` | 导出配置到清单 |
| `validate` | 验证配置完整性 |

## 目录结构

```
你的私有配置仓库/
├── manifest.yaml          # 配置清单
├── plugins.yaml           # 插件清单
└── assets/
    ├── skills/            # 自定义 skills
    ├── memory/            # 行为偏好 memory
    ├── settings/          # settings 和 permissions
    └── hooks/             # 平台特定 hooks
```

## 详细文档

见 [DESIGN.md](DESIGN.md)