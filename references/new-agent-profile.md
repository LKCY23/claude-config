# 新版 Agent Profile 原生入口

2026-10-17：已安装 CLI 入口 `claude-config` 已接入普通 Agent 设置及已核实的 [App 偏好](app-profile-implementation.md)。它只读取新 Machine 和独立 Profile，不读取旧 agents.yaml／manifest、不写已安装框架。本机已完成 Profile 迁移；命令和默认路径见 [CLI 打包规范](cli-packaging-and-distribution.md)。

2026-10-10 职责说明：资源定义及支持的入口由 claudespace 管理，Profile 保存资源选择和已登记运行配置，Machine 提供本机程序、端点与凭据引用。统一依据见 [资源专项第 2 节](resource-implementation.md)。导出不把角色提示词、工具／权限定义转成 Profile 覆盖；MCP 已支持 options.entry 选择来源 entries 中的具名模板，包内引用检查与指令布局已接入。具体格式及限制见 [参数清单](resource-implementation.md)。

## 配置与运行入口

Machine 必须明确 agent 的 Profile 选择和本机 executable、config_root。仓库选择文件通过 `profile_selection` 引用，`switch` 更新该文件；本机路径仍留在 Machine。参见 [机器绑定规范](machine-profile-bindings.md)。管理基线、报告和恢复记录默认保存在 `<config_dir>/.state/`；`runtime.state_dir` 可省略，显式指定时才覆盖默认值。`.state/` 为本机资料，配置仓库必须忽略它，不参与 Git 同步。Profile 的实际配置仍全部可选。

```yaml
# 结构示例：绝对路径须按实际机器填写，不是本机已启用配置
schema_version: 1
machine_id: example
config_dir: /absolute/path/to/config-data
catalog_roots: {}
agents:
  codex:
    profile: profiles/agents/codex/work.yaml
    binding:
      executable: /absolute/path/to/codex
      config_root: /absolute/path/to/codex-home
apps: {}
bindings: {}
# runtime.state_dir omitted: /absolute/path/to/config-data/.state
```

```yaml
# profiles/agents/codex/work.yaml
schema_version: 1
agent_type: codex
settings:
  model_reasoning_effort: high
  memories:
    use_memories: false
```

```sh
claude-config plan --machine /absolute/path/to/machine.yaml --agent codex
claude-config validate --machine /absolute/path/to/machine.yaml --agent codex
claude-config apply --dry-run --machine /absolute/path/to/machine.yaml --agent codex
```

三者复用完整结构、字段、版本、原生解析、漂移、结果及指纹预检。报告 operation 记录用户请求，preview_only 标明是否只是预览；保存报告不会部署配置。

## 返回与查看报告

返回结构遵循[配置检查与操作返回协议](report-contract.md)。默认 `--format auto`：输入与输出均连接可用终端时显示展开界面（包括 tmux），否则打印静态人类报告；`--format text` 强制静态输出，`--format json` 的 stdout 只包含一个完整 JSON 文档。

```sh
claude-config plan --machine /absolute/path/to/machine.yaml --agent codex --format text
claude-config plan --machine /absolute/path/to/machine.yaml --agent codex --format json
```

交互界面使用方向键选择／滚动、空格或 Enter 展开、PgUp／PgDn 翻页、q 退出；支持 tmux pane 调整大小。窗口太小、TERM 不可用或 curses 初始化失败时回退静态输出。退出后保留摘要和完整 JSON 地址，查看器不执行修改；查看器内 Ctrl+C 只退出查看，不改变已完成操作的结果。

成功、阻断和失败报告默认保存在 `<config_dir>/.state/reports/<report_id>.json`，显式 `runtime.state_dir` 覆盖时写入该目录，目录 0700、文件 0600。Machine 无法加载或命令参数错误时，使用固定私有目录：macOS 为 `~/Library/Application Support/claude-config/reports`，其他 POSIX 系统为 `~/.local/state/claude-config/reports`。不会在当前项目目录随意生成报告。保存失败时地址为 null，明确说明实际业务结果，退出码为 2。

report_id 每次运行独立；plan_id 仍表示准确可执行计划身份。人类摘要、交互详情和 JSON 使用同一报告；完整 JSON 保存脱敏后的公共明细，不包含私有评审正文和认证值。

退出码：0 表示请求完成（包括可处理差异与明确保留），1 表示检查或冲突阻断，2 表示命令／环境／执行／报告保存错误。中断执行可返回 130，并记录修改与恢复；`--help` 沿用标准帮助行为。review-conflicts 的操作成功与 deployment_ready 分开，不能将资料生成当成允许部署。

正式 apply 去掉 --dry-run。已审阅某份预览时，可传 --expect-plan-id 指定该预览的身份；重新规划结果不一致即 plan_stale，不写入。执行器随后加锁并再次复核全部输入，阻止规划到实际写入之间的变化。

## 导出和切换

```sh
claude-config export --machine /absolute/path/to/machine.yaml --agent codex \
  --field /settings/model --field /settings/model_reasoning_effort
claude-config switch --dry-run --machine /absolute/path/to/machine.yaml --agent codex \
  --profile profiles/agents/codex/other.yaml
```

export 默认只预览，--write 才写 Profile。必须显式选择已登记原子字段，本机缺失字段保留 Profile 原值；其他原生配置、项目和凭据不导出。可用 --profile 指定同类新文件，导出不会修改 Machine 绑定。

资源使用选项以 --resource 显式选择已有 Profile 中的 ref，不扫描库存来增加选择；可以同时选普通 --field：

```sh
claude-config export --machine /absolute/path/to/machine.yaml --agent codex \
  --resource tiny@team --field /settings/model
```

固定来源、完整正文与本机绑定能反向匹配后，只更新原生显式存在的可移植 options。没有匹配项则保留声明；版本／正文不同、绑定变化、重复选择器或 Hook 控制不能统一表示时阻断。来源、bindings 和其他 Profile 内容保留。导出不安装、不写原生配置或管理基线；它不能自动接纳手改或未知归属，后续 apply 的 drift 检查仍独立成立。Status-line 经 /settings/statusLine/command 的既有绑定导出。

## 采用已准备资源基线

```sh
claude-config adopt-baseline --machine /absolute/path/to/machine.yaml --agent codex \
  --catalog team --commit "<full-commit-sha>" --checkout /absolute/path/to/prepared-checkout \
  --release-record /absolute/path/to/baseline.json
```

默认预览，--write 执行；可用 --expect-plan-id 绑定审阅的计划。准确 checkout 和发布记录必须先准备好，工具不拉取、移动旧仓库或跟随 latest。计划核对 repo／commit／catalog 路径及 registry 摘要，扫描所有 Agent Profile 的影响、依赖和兼容声明；所选正文及子模块／外部依赖经现有固定来源解析器验证。统一更新 catalogs.yaml 的 pin 和 Machine.catalog_roots 的目录指针，其他 Profile 的值与资源选择保留，不部署原生资源。之后另跑 plan／apply。

本地发布记录由 claudespace/scripts/baseline_record.py 对准确提交生成；记录表示 registry 结构和提交内容对应关系，不能把这解释为签名验证或资源业务运行已通过。0.2.0 提供 `fetch-baseline` 从正式 Release 获取记录及已选固定来源，返回采用命令；跨仓库／路径迁移须通过 `--source-map` 提交准确映射。完整流程见[基线获取、采用与恢复](baseline-distribution.md)。来源、记录、数据文件或 Profile 清单在预览后变化会使计划过期。采用失败与独立 rollback 都使用公共文件执行器，不回写远端历史。

switch 正式执行时先更新选定设置和管理基线，再完成 Machine 引用；未声明的旧值保留并报告 residual。任一步失败恢复本次已写入的内容；检测到后续改动则保留改动并报告 recovery_conflict。

--policy 设置本次默认策略，--field-policy /settings/路径=keep_target 等设置本次字段策略。默认 source_wins 不绕过已知外部漂移；--resolutions 接受按 JSON Pointer 组织、包含当前目标 fingerprint 和 source_wins／keep_target 的显式处理。指纹是报告 snapshots.target 的值，代表完整当前目标投影，包含 settings 包装。路径以 JSON Pointer 表达，模型名中的斜线使用 ~1，点号保留在原键中。

已登记资源使用叶子同样支持 --field-policy，例如 `/resources/skills/tiny@team/options/control/enabled=keep_target` 或 `/resources/agents/review@team/options/model=keep_target`。Skills 调用／启用、插件 enabled、MCP、command Hook 和 Agents 模型／Codex 推理选项共用策略优先级和逐项报告。它不修改固定来源、不接纳未知库存。--resolutions 已支持已受管资源的准确冲突 ID 和 adopt_source／keep_local／update_source；具体 scope、私有三方评审入口及来源更新边界见 [资源冲突处理](resource-conflict-resolution.md)。

## 原生边界

工具安装由 uv 管理；多客户端配置写入使用[统一切换入口](framework-cutover.md)的 v2 spec：明确迁移归属、共享文件一次合成、同一备份／恢复日志。cutover 不安装工具代码；旧 agents.yaml 不参与日常运行。本机已完成新版配置及 CLI 迁移。

现有插件可通过明确的 `--adopt-plugin REF=SELECTOR` 保留原名称并更换固定来源；准确版本／正文匹配后才能接管，不兼容升级或未知作用域。它是完整计划的一次输入，不是 Profile 字段。命令、同来源覆盖与回滚边界见 [插件接管](plugin-adoption.md)。

已有安装与正式候选版本不同，可另用 `--replace-plugin REF=FILE` 指定经过核对的旧来源恢复声明。它核对本机旧包并保留固定历史来源，再执行不同版本的原生更新；完整计划和失败／独立恢复规则不变。详见 [明确版本更新](plugin-replacement.md)。

- Claude 写入 config_root/settings.json，按选择保留其他 JSON 字段；Codex 写入 config_root/config.toml，保留未声明内容和 TOML 注释。无变化时保留原文件字节。
- `fields.py` 登记已选普通字段，拒绝普通 settings 中的插件库存、技能路径、MCP、凭据、项目、旧别名及原始命令字符串。[非路径具名权限规则](permissions-implementation.md)已接入；本机文件系统路径、socket 和代理地址仍排除，不能用整个对象绕过白名单。
- Claude 没有已核实的公共 schema 校验命令。--version 和 plugin list 会接受错误设置，所以检查不将它们当成 native schema 验收。当前依据文档、严格字段类型和 2.1.278 起的用户设置接口检查；要求更新版本的字段按 since 阻断。模型账号可用性、角色／自定义风格依赖、终端显示环境和真实会话效果仍需资源与会话验收。
- Codex 在临时 HOME／CODEX_HOME 中启动 stdio app-server，完成 initialize、config/read 的请求／响应，再比对选定字段；另以 features list 检查配置解析和权限引用。临时配置只含选定可移植设置及必要的后端选择，不复制登录、MCP 或 Hooks，不调用模型。匹配原生解析不等于会话生效。
- 绑定 Codex App 时，同一 Agent 配置还检查其明确指定的 backend；缺少 backend 路径阻断。agents.max_depth 在 V2 或无法核实为 V1 时阻断；V2 对 agents.enabled 的覆盖单独警告，不误报其已关闭全部多代理工具。
- 普通设置导出保留未选资源声明；固定来源、静态资源正文和命令绑定已接入，细节见 [资源实现](resource-implementation.md)。已有绑定命令可显式选字段导出：匹配保留逻辑声明，无法匹配返回 binding_drift。
- MCP、command Hook 与 Plugins 安装／更新／启用已接入完整计划；原生操作验证后才更新管理基线。资源反向导出与已核实 App 偏好已连接；后续按选中资源适配运行入口与实际依赖，不增加任意定义覆盖。
- 新管理状态记录普通设置叶子、资源归属及插件历史固定来源；移除声明保留原生内容。journal 同时记录文件与原生插件副作用，独立 rollback 按反向顺序恢复，不覆盖后续手改或在用的版本缓存目录。

## 独立文件回滚

apply／switch 的 recovery 路径为 state_dir/runs/<run-id>/journal.json。使用对应 Machine 和 Agent 绑定预览；只有增加 --write 才执行：

```sh
claude-config rollback --machine /absolute/path/to/machine.yaml --agent codex --run "<run-id>"
claude-config rollback --machine /absolute/path/to/machine.yaml --agent codex --run "<run-id>" \
  --write --expect-plan-id "<reviewed-plan-id>"
```

回滚只接受新格式运行记录，核对原生配置作用域和备份指纹。当前文件须等于该运行的 after，才能恢复 before；已等于 before 则无变化，其他状态返回 recovery_conflict 并阻断整份恢复计划。本次新建文件可删除，已有文件恢复原字节和模式；不按目录清空、不覆盖之后的手改。恢复本身也有锁、再次指纹检查、备份和失败恢复记录。此规则采用完整文件指纹；即使之后只改了原生文件中的其他字段，也需要先明确处理冲突。

包含原生插件步骤时，同样核对元数据、缓存和固定历史来源；通过原生安装器重建原状态，再恢复文件。安装与原生缓存内容变化以日志实际结果为准，无法核实或仍在用时保留现场并报告冲突。详细边界见 [插件原生恢复](resource-implementation.md#plugins-安装入口状态与更新)。

参考：[Claude 字段与版本](https://code.claude.com/docs/en/settings-reference)、[Codex 字段](https://learn.chatgpt.com/docs/config-file/config-reference)、[Codex Schema](https://learn.chatgpt.com/docs/config-schema.json)。

## 验证

普通 tests 使用临时目录与模拟能力检查。`tests/test_native_profile_smoke.py` 为主动启用的真实二进制验收：设置 CLAUDE_CONFIG_RUN_NATIVE_SMOKE=1，并分别通过 CLAUDE_CONFIG_NATIVE_CLAUDE、CLAUDE_CONFIG_NATIVE_CODEX、CLAUDE_CONFIG_NATIVE_CODEX_APP 提供绝对程序路径。所有目标仍是新建临时目录；不将真实 config_root 写入测试。

2026-10-09 已在隔离目录使用本机 Claude 2.1.278、Codex CLI 0.160.0 和 App 后端 0.162.0-alpha.2 完成规划、写入和重复无变化验证。未登录模型服务、未验证真实会话效果、未切换本机运行配置。
