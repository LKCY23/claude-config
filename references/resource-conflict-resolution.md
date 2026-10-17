# 资源冲突检查与处理

2026-10-17。已安装 CLI 已接入已受管资源的准确指纹审阅、来源恢复、本次保留，以及来源更新工作流。本机已完成新版迁移。未知资源归属、路径越界、类型错误、身份歧义及未知插件版本／安装镜像仍阻断，不能用决策绕过。

## B／L／S 的证据

- B：上次成功部署的管理记录及私有正文／注册快照。
- L：当前已受管文件、标记区块、原生服务／处理器及启用状态。
- S：固定来源、Profile 使用选项、具名本机绑定和当前策略。

完整部署会在 `state_dir/resource-baselines/<SHA256>.bin` 保存按内容去重的基线；资源状态中的 native.review 只引用这些私有文件。记录后按摘要验证，快照损坏或丢失不能冒充三方比较。普通配置导出、移除选择和本次保留不会重建基线来自动接纳当前修改。

每个冲突输出 resource_conflicts：conflict_id、逻辑路径、资源 ref、B／L 指纹、来源 commit／rendered_fingerprint、具体文件与变更行区间、声明来源文件差异、结构问题及 eligible_actions。普通 JSON／text 不打印正文、原生连接值或凭据；完整差异材料通过显式私有评审入口获取。

未解决阶段的 incoming 标注 `declaration_before_optional_usage_preservation`：它描述声明文件和模板，不将缺省使用选项误报为删除。明确决策后重新规划，公共合并计算最终原生结果并逐项解释 Profile 策略、缺省保留与实际覆盖。脚本、指令和代理正文没有自动文本拼接合并。

冲突 ID 绑定资源协议、Agent／运行绑定、完整原管理记录摘要、当前受管内容、来源提交、使用选项、依赖、具名绑定及程序指纹、CLI／App 后端指纹和合并策略。本机、来源、绑定、策略或基线变化都会使旧选择失效。执行器另外锁定并复核所有输入与目录；审阅后再次变动返回 plan_stale。

## 一次性决策入口

`--resolutions` 接受一个 JSON 对象，可同时包含既有 `/settings/...` 字段决策和资源决策；资源路径精确到 `/resources/<group>/<ref>`。

```json
{
  "/resources/agents/review@team": {
    "conflict_id": "<从本次报告复制的64位摘要>",
    "action": "adopt_source"
  }
}
```

| action | 处理 | 管理基线与来源 |
| --- | --- | --- |
| adopt_source | 恢复明确受管范围到本次拟部署结果；先按 B 保留源未声明的使用项，再应用现有字段策略 | 完整检查和写入验证后记录新基线；本机旧内容进入私有恢复日志 |
| keep_local | 跳过该资源的正文、注册、启用和相关版本写入 | 原管理记录完整保留，Profile 与 catalog 不改；下次无本次决策仍报告偏离 |
| update_source | 继续阻断部署，返回具体来源更新步骤 | 先导出可移植 options 到 Profile，或审查资源正文、适配与发布 claudespace 基线，然后重新规划 |

这些选择不进入 Profile 的长期 merge_policy；全局 source_wins 不清除外部漂移。没有当前冲突、资源未选择、scope 错误或旧摘要的决策均拒绝。再次显式传入同一审阅文件表示再次选择，本次保留不是永久忽略。

```sh
claude-config plan --machine /absolute/path/to/machine.yaml --agent claude-code
claude-config plan --machine /absolute/path/to/machine.yaml --agent claude-code \
  --resolutions /absolute/path/to/resolutions.json
claude-config apply --machine /absolute/path/to/machine.yaml --agent claude-code \
  --resolutions /absolute/path/to/resolutions.json --expect-plan-id "<已审阅的新计划ID>"
```

先按决策重做预览；FilePlan 的身份包含资源冲突和处理状态，不能把未解决计划的 ID 用于执行另一个结果。保留结果标记 `synchronization=local_changes_retained` 和 kept_resources，不能报告“全部已同步”。保留的是本次原生内容，不采用这些字节或凭据作为新的来源。

## 各类适配与不可绕过的边界

已登记独立 Skills／Agents／Rules 的受管文件、完整脚本包、全局指令标记区块、MCP 已知名字、具有已知 matcher／位置的 command Hook 组、Codex 技能 enabled 及原生插件 enabled 已接入。Source 恢复使用私有 B 控制值，不从未经接纳的 L 推导默认或省略项；原生插件启用恢复继续走原生操作和既有副作用验证，不直接复制安装缓存。

完整正文被手改、模式变更或已知文件被删除可进入审阅；未知附加文件、未知同名资源、重复处理器／选择器、绑定重定位和身份不明确仍阻断。Hook 的 matcher／位置发生无法归属的变化不能当成“旧组删除”去追加第二份处理器。插件版本、安装镜像或历史来源改变仍需独立的来源／版本核对，三种决策不能直接收编或强制覆盖它们。

本次保留会检查直接、传递依赖和引用脚本的命令绑定。如果消费者需要被跳过的新资源镜像或基线，整份部署计划阻断；不能部署消费者后宣称它拿到了新依赖。

失败恢复及独立 rollback 只能恢复与本次结果相符的受管变化。采用来源后的恢复可以还原审阅过的 L；之后的手改仍保护。基线文件、恢复日志和评审材料都是私有本机资料，不作为 Profile 或 claudespace 的同步内容。

## 私有评审与更新来源

```sh
claude-config review-conflicts --machine /absolute/path/to/machine.yaml --agent claude-code
claude-config review-conflicts --machine /absolute/path/to/machine.yaml --agent claude-code --write
```

默认只预览，--write 只生成 `state_dir/resource-reviews/<conflict_id>` 材料：B／L 文件、声明的 incoming 包、原生注册或标记区块的三方 JSON、review.json。state_dir 私有根权限 0700、资料文件 0600；报告返回目录与摘要，正文不进入 stdout。这个操作的 ready 只表示私有材料可生成，另保留 deployment_ready／deployment_blockers，绝不表示允许部署。它不改原生文件、Profile 或管理基线，可独立回滚自己的材料。

使用选项走已有显式资源导出：

```sh
claude-config export --machine /absolute/path/to/machine.yaml --agent claude-code \
  --resource review@team --write
```

完整固定正文和绑定匹配后才可导出可移植 options。导出只改 Profile，不接管管理基线；改变来源后原 conflict_id 作废，需要新预览及明确采用。正文、连接或本机命令无法反向匹配时不能通过这个入口迁入 Profile。

正文更新是人工／Agent 的来源维护工作流：读取私有差异，按原始包格式及逻辑绑定整理改动，在候选来源中保留第三方出处，验证并发布基线，再采用、重新规划。不能直接将包含本机路径、鉴权或原生解析结果的评审包提交为资源。此入口没有实现候选构建／PR／自动发布，相关流水线仍按 claudespace 更新专项继续推进。
