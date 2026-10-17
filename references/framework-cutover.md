# 多客户端配置切换与恢复

2026-10-17：程序安装交给 Python 包管理器。当前 cutover 只组合数据、Machine 和 Agent/App 原生配置，不复制工具代码。

```yaml
schema_version: 2
machine: /absolute/review/machine.yaml
machine_target: /absolute/local/machine.yaml
data_target: /absolute/config-data
# 可选：明确退役旧数据顶层声明；不会解析它们作为运行配置
migrate_existing: true
# adopt_plugins / replace_plugins 延续现有显式映射格式
```

```sh
claude-config cutover plan --spec /absolute/cutover.yaml --format text
claude-config cutover apply --spec /absolute/cutover.yaml --expect-plan-id "<reviewed-id>" --format text
claude-config cutover rollback --spec /absolute/cutover.yaml --run "<run-id>" --format text
claude-config cutover rollback --spec /absolute/cutover.yaml --run "<run-id>" --write --expect-plan-id "<recovery-id>" --format text
```

绑定客户端子计划共享输入快照；同一个 Codex config.toml 的 Agent 与 App 字段先合成为一次写入，保留未选择字段和格式。仅最终完整计划执行统一验证。
状态默认 <data_target>/.state，显式 runtime.state_dir 保留。私有日志/备份不进入配置同步。
资源/原生写入在前，基线其次，Machine 绑定最后。未知目标数据冲突阻断；不替用户解决账号、供应商、项目与历史数据。
执行仍使用一把 POSIX 状态锁。正常退出绑定 App；生命周期变化阻断写入和不安全恢复。
恢复按逆序执行，只恢复仍等于本次结果的文件，首次冲突停止；保留依赖与现场。不是文件系统级原子事务。
Claude 原生备份轮换仍在原生调用前保存已存在的历史备份正文；旧日志只有指纹时不能重建丢失正文。

v1 含 framework_source/tool_dir/entries 的历史切换声明不作为新版输入。需要恢复历史整次框架切换时，使用当时封存的 release 和原 spec；不要修改历史 release。
新的软件升级使用 uv；Skill 入口及旧脚本安装退役使用 init。见 [打包规范](cli-packaging-and-distribution.md)。
