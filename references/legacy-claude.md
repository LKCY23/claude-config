# 已退役的 Claude 单客户端工作流

本页原先描述基于 `manifest.yaml` / `plugins.yaml` 的 skill、脚本和外部工具安装工作流，已经退役。旧正文保留在 Git 历史中，不能作为现行命令参考。

现行入口：

- 安装、默认目录、升级和卸载：[README](../README.md)。
- Agent 与 App Profile：[架构](../DESIGN.md)、[Agent 命令](new-agent-profile.md)、[App 偏好](app-profile-implementation.md)。
- 尚未迁移机器的旧数据：显式运行 [inventory](migration-inventory.md) 和 [convert](migration-conversion.md)，审阅候选后使用 [cutover](framework-cutover.md)。
- 已迁移机器：直接使用 `claude-config plan/validate/apply/export/switch/rollback` 等命令，不重建旧声明，不自动合并旧选择。

旧 slash-command 的 `sync`、`diff`、`status` 和外部工具安装子流程不是当前 CLI 子命令。Git 拉取／提交与配置应用分开；以 `claude-config --help` 和各子命令 `--help` 为准。
