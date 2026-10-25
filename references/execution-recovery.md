# 执行日志与中断恢复

0.5.0 修复混合插件安装失败后的恢复，提供追加事件日志、运行检查和普通命令进度。它保留 0.4.0 的 Machine/profile_selection 能力；升级工具不应用配置。

## 日志与运行状态

新的执行使用 schema_version 2 的私有 journal.json 和带序号、摘要链的 events.jsonl。操作意图与备份在目标修改前持久化，完成后追加结果。检查点按递增间隔保存；未变化文件在日志中聚合计数，在报告中仍可检查逐项结果。

读取时核对事件链和检查点，只忽略未完成的最后一条追加。中间损坏、缺失事件或检查点不一致会阻断恢复。旧 v1 日志继续支持读取和回滚；旧 CLI 不支持恢复 v2，不能为处理新日志而降级执行器。

每次执行记录进程 ID、启动标识、主机和原生子进程。running 不是进程存活的证明。新部署会检查相同目标的未完成事务；同一用户的不同 state_dir 使用共同执行锁，并登记尚未处理的目标。

```sh
claude-config runs --format json
claude-config runs --run <run-id> --format json
claude-config rollback --agent claude-code --run <run-id> --format json
claude-config rollback --agent claude-code --run <run-id> --write --expect-plan-id <reviewed-id>
```

runs 只读核对当前内容与 before/after 指纹，不认领文件，也不自动清理。恢复需要明确 run 和本次计划 ID，后续手改仍会阻断。若安装中断在完整观测之前，仅能对明确的逐插件意图、已退出的子进程和完整已知镜像进行恢复；未知部分安装结果保留待核查。

## 原生插件恢复

同一次操作可以包含已有插件和新插件。恢复区分首次安装、升级、启停和未变化镜像；共享 metadata 仍统一协调。已有未变化镜像不卸载、不重装。新镜像的安装器自增内容仅在本次可证明的边界内撤回；未知版本目录和未修改插件的异常变化仍阻断。

SIGTERM/SIGHUP 停止后续步骤并尝试恢复；真实原生 CLI 子进程在异常退出时被终止并等待退出。SIGKILL 或主机重启依赖下次的运行检查。取消调用方等待不等于停止执行。

## 进度与返回协议

进度输出到 stderr，JSON 结果单独输出到 stdout。长阶段有存活提示和当前阶段；提示不表示业务进度。运行 ID 和 journal 路径在开始写入前给出。

返回协议仍为 1，Bootstrap 使用的身份、绑定和 execution 字段不变。文件快照只保存在公开合同的 fingerprints.files/directories，移除重复的内部 file_snapshots/directory_snapshots 副本。原生无变化操作标记 unchanged。plan_stale 错误包含安全的路径和预期/当前指纹，不复制任意原生错误正文。

## 资源运行依赖

requires 可声明 Node 可执行程序依赖及平台约束：

```json
{
  "bindings": {"node": {"kind": "executable", "required": true}},
  "runtime": {
    "kind": "node", "binding": "node", "major": 22,
    "platforms": ["darwin"], "architectures": ["arm64"], "modules_abi": "127"
  }
}
```

Profile 将 node 槽映射到 Machine 的 executables 引用。plan 在隔离环境中用固定探测表达式检查解释器，不执行资源代码、不加载用户 NODE_OPTIONS，也不读取凭据。此检查不改写插件正文；启动器仍需使用相符的解释器，SQLite 加载及真实业务另外验收。

## 验证

普通测试覆盖混合插件、升级失败、逐项中断、事件损坏、旧日志、不同 state_dir 的未完成目标保护和 CLI 返回合同。真实插件测试显式启用并仅使用临时配置根。test_native_fixed_package 支持通过 CLAUDE_CONFIG_FIXED_PACKAGE 指定待验收的固定包，验证五个已有插件加新包、重复检查与完整回滚。

scripts/benchmark_transactions.py 测量 1,000/5,000/10,000 文件，分别记录首次写入和重复无变化执行；不强制清空操作系统缓存。一次 macOS arm64/Python 3.11 的三次中位数为：首次写入约 0.55/2.93/5.53 秒，重复执行约 0.19/0.99/1.98 秒。网络、真实安装器耗时不包含在此数据内。


## 已被后续部署取代的旧失败记录

0.5.1 提供 `reconcile-run --agent <agent> --run <id>`。默认只预览，`--write --expect-plan-id <id>` 才保存独立核对记录。它不修改原始失败日志、不回滚或认领原生内容。

此入口只接受剩余未完成操作全部为同一 Agent 的原生插件操作的历史 failed 事务。当前管理记录必须精确对应较晚一次成功部署写入的结果；每个旧插件必须有有效的当前受管 receipt，当前安装、启停、来源和历史镜像须与记录一致。未知镜像、手工修改、缺少后续成功记录或未恢复文件写入都会阻断。

通过后保存带原日志摘要和后续部署依据的私有 receipt；原日志或事件改变会使该核对失效。`runs` 分别显示原始失败状态及已核对结论，避免把保留后续配置误报为回滚成功。
