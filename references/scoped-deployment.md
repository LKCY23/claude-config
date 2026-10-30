# 按范围同步

`plan`、`validate`、`apply` 共用同一个部署范围。省略选择器时同步当前绑定的完整 Agent/App Profile；不会自动获取新版本、切换 Profile 或同步其他客户端。

```sh
# 全部已声明 Agent 设置
claude-config plan --agent claude-code --scope settings

# 全部已选资源，或只选一种资源组
claude-config plan --agent claude-code --scope resources
claude-config plan --agent claude-code --resource-group plugins

# 指定已选资源
claude-config plan --agent claude-code \
  --resource agent-session-migration@claudespace-personal

# 已声明的设置叶子，App 偏好也可按字段选择
claude-config plan --agent codex --field /settings/model_reasoning_effort
claude-config plan --app claude-desktop --field /settings/userThemeMode

# 使用相同选择器执行已审阅的计划
claude-config apply --agent claude-code \
  --resource agent-session-migration@claudespace-personal \
  --expect-plan-id <plan-id>
```

`--scope`、`--resource-group`、`--resource`、`--field` 四种选择方式互斥；后三种各自允许重复。资源组使用 Profile 中的名字：plugins、skills、mcp_servers、hooks、instructions、rules、agents。App 只接受 settings 范围或字段。局部 apply 要求计划 ID；switch 和 cutover 保持完整配置语义。export/retire-skills 的既有选择器含义不变。

## 范围与依赖

报告的 `deployment_scope` 区分 requested、effective 和 expanded。资源依赖必须已经在 Profile 中声明；部署自动纳入它们及引用这些资源的命令绑定。绑定使用多个资源时一起展开，以生成一致的命令。资源启用控制所需的原生设置字段也列入 effective。范围外的运行参数不被隐式采用。

范围外的管理记录、资源版本、active 状态、settings 基线和 bindings 保留。局部同步不会将其他资源标为 residual。绑定 Profile 与管理记录不同，需要先完成完整切换。

跨 skill 的 Markdown 链接不是升级依赖。局部部署检查范围外实际安装的受管版本；被引用的文件也参与执行前复核。改名或删除文件会破坏范围外 skill 的已记录引用时，报告 scope_expansion_required，要求将消费者及其修正后的引用一起纳入。未知安装归属、目标手改和路径冲突继续阻断。

## 并发与生效

无变化插件保留安装状态、归属及镜像校验，但不创建原生安装操作，也不因安装器可能改写的用户配置或备份目录产生无关的阻断。实际插件变更仍使用完整副作用快照、在用检查和恢复流程。MCP 等确实读写用户配置的操作继续保护该文件。

纯资源正文更新不把仅为盘点读取的原生主配置当作执行依赖。涉及原生字段写入时继续检查整个共享文件，发现并发变化后重新规划，不自动覆盖。固定来源、被更新文件和实际依赖仍严格检查；失败只恢复本次写入且未被后来改动的文件。

新增支持文件先于 SKILL.md 写入，已知旧文件最后删除。逐文件发布和 journal 回滚不保证整个目录原子切换；运行中的任务若已加载旧说明，需要重新调用 skill 或重新读取引用。

`activation` 单独报告资源的采用方式，verified 为 false，表示没有验证当前会话已加载。文件同步成功不代表会话上下文自动替换。此实现不自动发送重载命令、启动或退出客户端；App 偏好的既有生命周期限制保留。
