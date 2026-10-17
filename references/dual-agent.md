# Agent 与 App Profile 的关系

更新：2026-10-17。旧的单文件双宿主声明已退役；本页说明现行独立 Profile 模型。

- Agent 类型为 `claude-code`、`codex`，文件在 `profiles/agents/<agent>/<profile>.yaml`。
- App 类型为 `claude-desktop`、`codex-app`，文件在 `profiles/apps/<app>/<profile>.yaml`。
- `catalogs.yaml` 固定资源来源；Machine 决定本机采用哪份 Profile，以及程序和原生配置位置。
- App/CLI 实际共用原生目录时，Agent 配置也共享。App 专属字段与 Agent 字段不重叠，组合写入由 `cutover` 处理。

```sh
claude-config plan --agent claude-code
claude-config plan --agent codex
claude-config plan --app claude-desktop
claude-config plan --app codex-app
```

每次日常操作明确选择一种 Agent 或 App，不使用 `--agent both` 或 `--agent claude`。需要多目标组合计划时使用 [cutover](framework-cutover.md)。

原生字段只从 Profile 中展开的 `settings` 导出／合并；不整份复制配置文件、项目、会话或凭据。资源定义在 Claudespace，Profile 保存选择和已支持运行参数，Machine 保存本机依赖引用。

完整规范见 [架构](../DESIGN.md)、[Agent 命令](new-agent-profile.md)、[App 偏好](app-profile-implementation.md)和[资源实现](resource-implementation.md)。旧 `agents.yaml` 仅可作为 [迁移转换器](migration-conversion.md)的显式输入，不与新版配置混合读取。
