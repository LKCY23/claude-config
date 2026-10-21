# claude-config 架构与配置合同

更新：2026-10-18。本文描述独立 Agent Profile、App Profile 和 Machine 的现行实现；日常入口是安装后的 `claude-config`。当前已发布版本为 0.3.0，已安装工具版本独立，远端发布状态以 [打包说明](references/cli-packaging-and-distribution.md) 为准。

## 职责

| 位置 | 管理内容 |
| --- | --- |
| Claudespace | 七类资源正文、固定来源、版本、宿主适配、可选运行入口及约束 |
| `catalogs.yaml` | 配置数据选用的 catalog 仓库、准确提交及仓库内路径 |
| Agent Profile | `claude-code` 或 `codex` 的可移植 settings、资源选择及已支持运行参数 |
| App Profile | `claude-desktop` 或 `codex-app` 的可选应用偏好；不重复管理 Agent 资源 |
| Machine | 本机 Profile 绑定、原生配置根、程序、来源目录、端点及凭据引用 |
| claude-config | 检查、差异与冲突说明、字段合并、资源部署和恢复 |

每份 Profile 独立保存，不隐式继承或叠加。资源被登记不代表被选择，选择也不代表所有兼容入口、客户端加载或业务效果均已验证。

## 数据与本机路径

```text
~/claude-config-data/
├── catalogs.yaml
├── profiles/
│   ├── agents/claude-code/<profile>.yaml
│   ├── agents/codex/<profile>.yaml
│   ├── apps/claude-desktop/<profile>.yaml
│   └── apps/codex-app/<profile>.yaml
└── .state/                       # 本机报告、基线、恢复资料，Git 忽略

~/.claude-config-tool/machine.yaml # 本机绑定；--machine 可显式指定其他位置
```

Profile 路径相对 `Machine.config_dir` 解析。`Machine.catalog_roots` 指向已准备的准确来源 checkout。状态默认 `<config_dir>/.state`，仅显式 `runtime.state_dir` 覆盖；命令不从当前工作目录猜测 Machine。

Claude Code 与 Codex 是两种 Agent 配置类型。App 对同一个原生配置文件的使用可能与 CLI 共享，不能仅凭四个产品名称就声明四套隔离配置。App 与 Agent 使用不重叠字段，组合操作先合成同一目标文件再写入。

## 原生配置与 Profile

```text
本机 settings.json / config.toml
  → 按明确选择的字段和资源导出
  → Profile 中展开的 settings / resources
  → 通过 Git 同步 Profile
  → 在目标机器按字段合并和部署
```

所有实际配置均可省略。导出只更新选中且本机存在的值；未选择或本机缺失时保留 Profile 原值。部署时未声明字段保留本机原值，移除声明不自动删除原生内容。

普通对象按登记叶子合并，普通数组作为完整字段替换，资源集合按 `ref` 处理。切换 Profile 会报告遗留值，不把未声明旧值悄悄清除。任何合并、覆盖、保留或冲突决策均进入人类报告和原始 JSON；详见 [命令与合并规则](references/new-agent-profile.md) 和 [返回协议](references/report-contract.md)。

## 资源和本机依赖

资源类别包括 Plugins、Skills、MCP、Hooks、Instructions、Rules 和 Agents。正文与固有定义留在 Claudespace；Profile 只选择资源及其已登记运行参数。例如 MCP 可以选择来源提供的具名 stdio/HTTP 入口，程序路径、地址和凭据引用由 Machine 提供。

子代理提示词、工具清单及权限定义不转成任意 Profile 覆盖。插件安装与启用分别记录；技能的启用和调用控制沿用各端原生语义。包内相对引用与导入边界在部署前检查。格式和支持范围见 [资源实现](references/resource-implementation.md)。

## 预览、写入与恢复

```sh
claude-config plan --agent codex
claude-config validate --agent claude-code --format text
claude-config apply --agent codex --expect-plan-id "<reviewed-plan-id>"
claude-config rollback --agent codex --run "<run-id>"
```

写入前核对来源、原生结构、版本能力、字段范围、资源归属和输入是否仍与计划一致；加锁后再次检查。已知本地改动必须有可解释的处理决定，不能用全局覆盖策略消除未知归属。

备份和 journal 记录文件与原生插件操作。失败按记录恢复；后续手工改动或在用资源使不安全恢复停止并保留现场。安装只因本次新建镜像含有安装器自加的内容（例如依赖目录）而偏离固定包时，仍可删除该镜像并撤回前序写入，未接管的历史目录不参与。这不是跨文件或原生 CLI 的原子事务。需要修改 App 原生配置时遵守生命周期检查；只读操作不要求为了查看报告退出 App。

## 安装、迁移和日常更新

- 工具安装、升级、卸载由 uv 管理，正常配置操作不安装工具代码。
- `init` 管理本机 Machine 和明确选择的 Skill 命令入口。
- `migration inventory/convert` 只处理显式提供的旧数据副本，生成可审阅候选；旧声明不是运行时回退来源。
- `cutover` 使用 v2 spec 组合数据、Machine 和客户端配置写入，不再复制框架 release。
- Claudespace 检查上游、生成候选 PR、验证并发布准确提交的 GitHub Release。`fetch-baseline` 获取正式记录及已选固定来源，`adopt-baseline` 明确采用，再由 `plan/apply` 更新本机；跨仓库／路径须有精确来源映射。正式分发、私有历史衔接及本机采用已完成联调，见[基线分发](references/baseline-distribution.md)。

历史 `manifest.yaml`、`plugins.yaml`、`agents.yaml` 及旧脚本入口不参与日常部署。历史恢复使用对应运行保存的记录和当时封存工具，不能把旧 spec 当新格式执行。

## 边界和参考

保留账号、供应商、项目、会话、认证和原生信任。Claude Desktop 的本地 MCP／桌面扩展、字体文件、自定义声音文件、工作树目录及自定义文件处理器不纳入本系统。资源连接成功、模型效果和 GUI 生效需要适用环境的单独验收。

- [安装与 CLI](README.md)、[打包与迁移](references/cli-packaging-and-distribution.md)
- [Agent 操作](references/new-agent-profile.md)、[App 偏好](references/app-profile-implementation.md)
- [资源冲突处理](references/resource-conflict-resolution.md)、[多客户端切换](references/framework-cutover.md)
- [当前实现及验证边界](references/profile-implementation.md)
