# Profile 实现进度

更新：2026-10-18。独立 Agent/App Profiles、七类资源的基础部署、字段与选项合并、导出、恢复、资源冲突审阅、统一报告及 tmux 查看已接入 `claude-config` CLI。本机已完成新版配置和入口迁移，当前已发布版本为 0.2.0，已核验 Claude Desktop 2.42304.0 的应用偏好接口。源码与已安装 wheel 独立；安装状态以 `claude-config --version` 为准，推送源码不会升级本机工具。

## 当前入口与模块

| 入口／模块 | 已实现 | 边界 |
| --- | --- | --- |
| `src/claude_config/cli.py`、`setup.py` | 独立命令、init、doctor、明确的 Machine 查找和 Skill 入口 | 软件由 uv 安装；doctor 不验证模型、账号或资源业务 |
| `profile.py` 与 `core/manager.py` | Agent/App 的计划、检查、应用、导出、切换、回滚、基线采用和冲突审阅 | 明确选择目标；不从库存猜测配置 |
| `cutover.py` | v2 spec 下多客户端配置及数据／Machine 的组合写入与恢复 | 不安装工具代码；旧 v1 框架 spec 不能直接运行 |
| `migration.py` 与 `core/migration_inventory.py`、`core/migration_convert.py` | 显式旧输入盘点和独立转换候选 | 运行时不回退读取旧声明；转换不部署原生配置 |
| `core/schema.py`、`merge.py`、`fields.py`、`permissions.py` | 独立文件、准确白名单、对象叶子与数组策略、冲突和残留说明 | 只同步选定可移植字段，不复制本机路径、凭据或项目 |
| `core/native.py`、`app_native.py`、`app_manager.py` | Claude JSON、Codex TOML、已核实 App 偏好及隔离原生检查 | GUI、账号和真实会话效果另验收；App 接口变化会阻断 |
| `core/catalog.py`、`catalog_schema.py`、`resources.py` | 固定来源、完整资源正文、依赖、兼容入口和引用布局 | 来源必须已准备，不在普通 apply 中拉取 latest |
| `core/registrations.py`、`native_plugins.py`、`skill_controls.py` | MCP／command Hook、插件原生操作、技能启停及独立恢复 | 不执行业务或授予 Hook 信任，未知归属须明确处理 |
| `core/resource_options.py`、`agent_options.py`、`resource_export.py` | 已登记运行参数、Agents 模型／推理选项、显式 ref 的反向导出 | 不增加任意提示词、工具清单和权限定义覆盖 |
| `core/resource_conflicts.py`、`transaction.py`、`recovery.py` | 私有三方材料、明确处理策略、输入复核、锁、备份及后续改动保护 | 不自动文本合并或强制接管未知版本；不是跨文件原子事务 |
| `core/reporting.py`、`report_viewer.py` | 人类报告、完整 JSON、交互展开及静态输出 | 仅展示已生成报告，不改变业务结果或执行第二次操作 |
| `fetch_baseline.py`、`core/baseline.py` | 获取正式 Release 与已选来源；精确来源映射、影响预览、采用和恢复 | 获取不改变选择，采用不部署；之后另跑 plan/apply |

表中 `core/` 路径均相对 `src/claude_config/`。具体合同由[架构](../DESIGN.md)、[命令](new-agent-profile.md)、[资源](resource-implementation.md)、[App](app-profile-implementation.md)及[输出协议](report-contract.md)维护。

## 已完成与验证范围

- 本机运行入口为 wheel 安装的 `claude-config`，默认 Machine 为 `~/.claude-config-tool/machine.yaml`，状态在 `<config_dir>/.state`。独立 Profile 已替代旧单文件声明。
- 模块测试使用临时目录；原生烟雾测试须主动启用。隔离写入、原生回读和无变化验证不等于模型业务、服务登录或 GUI 效果验证。
- 2026-10-17 已核对 Codex App 26.1013.31256 配置接口；现有已登记偏好与主题类型保持一致，未知接口仍阻断。
- Claude 独立技能的开关同步覆盖 `skillOverrides`，有自动 `@skills-dir` 插件入口时同步其启停，正文保留。未知入口或手改不强行接管。
- Claudespace 的候选 PR、必需检查、审查合并与基线发布已在 main 实跑，6 份新版目录、62 项资源已进入固定来源检查，包括正式 HUD catalog；消费者安装仍由明确选择控制。

10 月 9–10 日的计数、迁移前状态和隔离记录见[历史验收档案](archive/profile-validation-20261009-10.md)。这些记录不能代替当前版本的检查，也不表示本机仍未迁移。

## 2026-10-18 分发与参数证据收尾

0.2.0 已发布 wheel／约束／安装器，公开和私有基线的获取、采用、部署与恢复均已验收。HUD 使用已核对的官方来源，显示配置保留，新版以待审候选推进。原生参数证据现集中记录在[资源参数清单](resource-parameter-evidence.md)：两项 Codex MCP 字段已经找到官方 Schema，并通过 CLI／App 的类型、边界、严格回读和未知字段负对照。16 项主动原生验收通过，不将解析成功写成模型／服务行为通过。

## 剩余工作与排除项

1. 框架自 0.1.4 起已包含 Bootstrap 所需的只读目标信息及 Claude 第三方部署模式读取修正；Bootstrap 2.0 使用[独立源码 Release](https://github.com/LKCY23/claudespace/releases/tag/bootstrap-v2.0.0)，目标机器按明确 Machine 构建 Dock App；升级框架不会替换本机启动器。真实第三方部署的 GUI／账号验收仍单独进行。相应边界，见 [App 偏好边界](app-profile-implementation.md)。
2. 新选择资源仍需核对具体运行入口、本机依赖、管理归属及实际业务。Agents 内嵌 MCP／Hook 按实际资源需求适配，不承诺所有组合可用。
3. App command bindings 和未登记资源模板参数不属于当前支持范围；只有实际需要时才讨论适配，不自动增加开发任务。精确边界见对应适配器和资源文档。

字体选择／文件／检查／清除、自定义通知声音文件、工作树目录、自定义文件处理器、应用快捷键、语音入口及 Desktop 本地 MCP／扩展明确排除，不列后续开发。供应商、认证、项目和会话保持本机管理。

## 开发验证入口

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

先使用 [README](../README.md) 的开发环境安装依赖。正常测试不写真实 Machine 或配置；原生测试的显式环境变量与边界见 [Agent 命令](new-agent-profile.md#验证)。
