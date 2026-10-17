# Profile 实现进度

更新：2026-10-17。独立 Agent/App Profiles、七类资源的基础部署、字段与选项合并、导出、恢复、资源冲突审阅、统一报告及 tmux 查看已接入 `claude-config` CLI。本机已完成新版配置和入口迁移，当前源码为 0.1.4。源码与已安装 wheel 独立；安装状态以 `claude-config --version` 为准，推送源码不会升级本机工具。

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
| `core/baseline.py` | 采用已准备 checkout 与基线记录，列出影响，修改选定 pin／来源根 | 不下载远端附件、不部署资源；之后另跑 plan/apply |

表中 `core/` 路径均相对 `src/claude_config/`。具体合同由[架构](../DESIGN.md)、[命令](new-agent-profile.md)、[资源](resource-implementation.md)、[App](app-profile-implementation.md)及[输出协议](report-contract.md)维护。

## 已完成与验证范围

- 本机运行入口为 wheel 安装的 `claude-config`，默认 Machine 为 `~/.claude-config-tool/machine.yaml`，状态在 `<config_dir>/.state`。独立 Profile 已替代旧单文件声明。
- 模块测试使用临时目录；原生烟雾测试须主动启用。隔离写入、原生回读和无变化验证不等于模型业务、服务登录或 GUI 效果验证。
- 2026-10-17 已核对 Codex App 26.1013.31256 配置接口；现有已登记偏好与主题类型保持一致，未知接口仍阻断。
- Claude 独立技能的开关同步覆盖 `skillOverrides`，有自动 `@skills-dir` 插件入口时同步其启停，正文保留。未知入口或手改不强行接管。
- Claudespace 的候选 PR、必需检查、审查合并与基线发布已在 main 实跑，5 份新版目录、61 项资源通过结构检查；消费者安装仍由明确选择控制。

10 月 9–10 日的计数、迁移前状态和隔离记录见[历史验收档案](archive/profile-validation-20261009-10.md)。这些记录不能代替当前版本的检查，也不表示本机仍未迁移。

## 剩余工作与排除项

1. 远端基线附件获取、长期归档与消费者采用联调尚未完成；已有 `adopt-baseline` 接受本地已准备来源。
2. 框架 0.1.4 已包含 Bootstrap 所需的只读目标信息及 Claude 第三方部署模式读取修正；Bootstrap 2.0.0 启动器仍是单独候选，未替换已发布或本机安装的启动器。相应边界，见 [App 偏好边界](app-profile-implementation.md)。
3. 新选择资源仍需核对具体运行入口、本机依赖、管理归属及实际业务。Agents 内嵌 MCP／Hook 按实际资源需求适配，不承诺所有组合可用。
4. App bindings 的部署适配及部分资源模板参数尚未接入，不得写成已支持选项。精确范围见对应适配器和资源文档。

字体选择／文件／检查／清除、自定义通知声音文件、工作树目录、自定义文件处理器、应用快捷键、语音入口及 Desktop 本地 MCP／扩展明确排除，不列后续开发。供应商、认证、项目和会话保持本机管理。

## 开发验证入口

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

先使用 [README](../README.md) 的开发环境安装依赖。正常测试不写真实 Machine 或配置；原生测试的显式环境变量与边界见 [Agent 命令](new-agent-profile.md#验证)。
