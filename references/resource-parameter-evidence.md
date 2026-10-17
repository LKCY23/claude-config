# 资源运行参数：支持范围与验证证据

核对日期：2026-10-18。适用框架：0.2.0。本清单记录现有白名单，不新增 Profile 字段，不改变本机配置，也不把未选用的扩展能力列为必做功能。

## 如何阅读验证状态

| 层级 | 能证明什么 | 不代表什么 |
| --- | --- | --- |
| 框架支持 | 声明通过白名单，能够转换、合并、导出和恢复 | 原生客户端一定读取或执行 |
| 官方依据 | 官方文档或原生配置 Schema 明确包含该字段 | 所有旧版本都支持 |
| 原生解析／发现 | 指定程序在隔离目录接受配置并回读或发现它；错误类型被拒绝 | 模型、服务连接、账号或 GUI 的实际效果 |
| 运行行为 | 对具体资源、事件和输入执行的验收 | 未测试的模型、事件、账户和组合也成立 |

已验证版本是观测记录，不是推测出的最低支持版本。本轮实际程序为 Claude Code **2.1.302**、Codex CLI **0.160.0**、App 后端 **0.163.0-alpha.10.2**。更早的技能、插件和 App 验收保留各自的版本与日期，不改写为本轮结果。

## 两项 Codex MCP 字段的结论

两项均存在于[官方配置 Schema](https://learn.chatgpt.com/docs/config-schema.json)的 `definitions.RawMcpServerConfig.properties`。普通 MCP 指南和配置参考的文字列表未列出它们，不能据此认定原生不支持。

| 原生路径／Profile option | 类型与含义 | 当前证据与边界 |
| --- | --- | --- |
| `mcp_servers.<name>.supports_parallel_tool_calls`／同名 option | 布尔，可省略；Schema 没有进一步的行为说明 | CLI 和 App 均接受并回读 true，拒绝字符串和数字；false 在解析后的回读中可被省略。未把 true 解释为强制并发，也未验证调度／模型行为 |
| `mcp_servers.<name>.tool_input_schema_max_bytes`／同名 option | 正整数。单个普通 MCP 工具输入 schema 的 UTF-8 字节压缩阈值，官方说明默认 5,000；显式设置也参与 Code Mode 对该工具输入类型的渲染 | 两端均接受并回读正数，拒绝 0、负数和字符串。不是工具输出 token 预算，也不是所有工具 schema 总预算；实际压缩及模型效果未在本轮执行 |

不要将第二项与 `code_mode.tool_input_schema_max_bytes` 混同；两者的作用位置、默认值和下限不同。本轮不会把 Code Mode 的同名字段追加到 Profile。

原生试验同时包含故意拼错的字段：普通 TOML 能读入不代表字段有效，该未知字段不能通过严格 `config/read`。`codex mcp get --json` 的输出会省略这两项，因此它也不能单独用来判断字段是否被识别。`--strict-config` 用于 app-server，不能传给 `codex features`；负例使用普通 `features list` 的类型校验。上述差异都已写入可复现测试。

## 现有七类资源的运行参数

下列路径相对于资源条目的 `options`，全部可选。程序、路径、端点和凭据引用由 `bindings` 提供，不混进这些使用选项。

| 资源 | 已登记字段 | 原生位置／证据 | 限制 |
| --- | --- | --- | --- |
| Plugins | `enabled` | Claude 原生 enable/disable；Codex `plugins`。原生安装、更新、恢复已有独立验收 | 不覆盖插件内部定义、组件配置或 Machine 依赖；不把安装成功当成业务运行成功 |
| Claude Skills | `control.enabled`、`control.disable-model-invocation`、`control.user-invocable` | enabled → `skillOverrides`，已有自动 `@skills-dir` 入口时同步其启停；另两项写技能前言。见[官方技能控制](https://code.claude.com/docs/en/skills)和[已完成的启停验收](resource-implementation.md#2026-10-17-claude-独立技能启用状态) | enabled 与两种调用方式分别管理；本轮不扩充原生 visibility 枚举 |
| Codex Skills | `control.enabled`、`control.allow_implicit_invocation` | `skills.config[].enabled` 与 `agents/openai.yaml.policy`。[官方说明](https://learn.chatgpt.com/docs/build-skills)；两端隔离发现／启停回读通过 | 禁用保留正文；隐式调用关闭不等于完全禁用；真实模型选择技能未执行 |
| MCP | `entry` 及下表所列字段 | entry 是框架模板选择；其余落到原生服务配置。两种传输和两端回读通过 | 不连接服务，不验证 OAuth、凭据或实际工具执行 |
| Hooks | `timeout`、`async`、`statusMessage` | command handler 字段；模板、注册、发现和导出有测试 | options 统一作用于该资源处理器；不支持逐处理器覆盖；不执行脚本或授予原生信任 |
| Instructions | 无额外 options | 固定正文、受管区块、包内引用与导出／恢复 | 未把文件布局检查表述为模型确已加载 |
| Rules | 无额外 options | Claude Markdown；Codex 原生 execpolicy 语法检查 | 不跨端翻译规则，不执行命令验证实际拦截 |
| Agents | Claude `model`；Codex `model`、`model_reasoning_effort` | [Claude](https://code.claude.com/docs/en/sub-agents)、[Codex](https://learn.chatgpt.com/docs/agent-configuration/subagents)定义文件；框架转换、合并和恢复测试 | 自定义代理的工具／权限／提示词归来源；未验证账号模型可用性和真实派发，嵌套 MCP／Hook 仍按选用需求适配 |

### MCP 字段与单位

| 端 | options 字段 | 框架约束／作用 |
| --- | --- | --- |
| 两端 | `entry` | 来源已声明的入口名称；不写入原生 server，不自动转换 transport |
| Claude | `timeout` | 整数，至少 1000，单位毫秒；Claude 2.1.302 的隔离 add-json 接受并保存 1000、拒绝字符串；最低原生版本及数值下限未据此推定 |
| Codex | `startup_timeout_sec`、`tool_timeout_sec` | 正数，单位秒；本轮 1.5／7.5 的隔离回读通过 |
| Codex | `enabled`、`required` | 布尔；本轮使用禁用的模拟服务核对配置，未测试启动失败策略；解析后的 required=false 可被省略 |
| Codex | `enabled_tools`、`disabled_tools` | 字符串数组，允许空列表，整字段替换；实际工具筛选未连接服务验收 |
| Codex | `default_tools_approval_mode`、`tools.<name>.approval_mode` | `auto`／`prompt`／`writes`／`approve`；回读通过，不代表实际审批交互已验证 |
| Codex | `tools.<name>.output_token_limit` | 正整数，工具输出 token 预算；与输入 schema 字节阈值分别管理 |
| Codex | `supports_parallel_tool_calls`、`tool_input_schema_max_bytes` | 见上节的官方 Schema 和严格原生测试 |

文档依据：[Claude MCP](https://code.claude.com/docs/en/mcp)、[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp)、[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。连接模板的 command/args/env/cwd/url/header 与 Machine 绑定另有闭合结构，完整定义见[资源实现](resource-implementation.md#mcp-原生模板与选项)。上表的约束是框架当前选择的范围，不宣称穷尽原生能力。

### Hook 事件及行为边界

框架事件白名单来自 `registrations.EVENTS`：

- Claude：SessionStart、SessionEnd、UserPromptSubmit、PreToolUse、PermissionRequest、PostToolUse、PostToolUseFailure、Notification、Stop、SubagentStart、SubagentStop、PreCompact、PostCompact、TeammateIdle、TaskCompleted。
- Codex：SessionStart、SessionEnd、UserPromptSubmit、PreToolUse、PermissionRequest、PostToolUse、PreCompact、PostCompact、SubagentStart、SubagentStop、Stop、Interrupt。

`timeout` 是秒；Codex SessionEnd／Interrupt 在框架中限制不超过 3 秒。async 不保证每种事件都异步：Codex 的 SessionEnd 始终同步，Claude 异步处理也不能套用同步 timeout 的执行结论。statusMessage 的界面呈现未验收。原生新增事件不会自动进入白名单。[Claude Hooks](https://code.claude.com/docs/en/hooks)、[Codex Hooks](https://learn.chatgpt.com/docs/hooks)。

Codex `additionalContextLimit` 仅作为来源模板中的非负整数支持，未开放为 Profile option。官方说明它决定大块 additionalContext 何时保存到文件；0 表示直接保留完整上下文，不是丢弃上下文。官方未声明最早支持版本，当前配置解析／发现不等于阈值触发行为已经执行。

## 可复现测试与固定结果

- `tests/test_native_mcp_options.py`：CLI/App、HTTP/stdio、已登记 MCP options、两个字段的非法类型与边界、未知字段负对照；Claude timeout 的合法值写入和错误类型拒绝。Codex 模拟服务保持禁用，Claude 只登记 /bin/false，不连接服务。
- `tests/test_native_resource_smoke.py`：已有配置渲染、技能发现与启停、MCP 入口回读、Hook 发现及规则语法；不执行业务。
- 本轮两组共 **16 项通过**。详细的可公开结论见 [固定结果](evidence/mcp-options-20261018.json)。完整本机诊断留在验收目录，不提交账号或用户配置。

在开发环境明确指定原生程序后运行：

```sh
CLAUDE_CONFIG_RUN_NATIVE_SMOKE=1 \
CLAUDE_CONFIG_NATIVE_CODEX=/absolute/codex \
CLAUDE_CONFIG_NATIVE_CODEX_APP=/absolute/app/backend/codex \
CLAUDE_CONFIG_NATIVE_CLAUDE=/absolute/claude \
PYTHONPATH=src:tests python -m unittest test_native_mcp_options test_native_resource_smoke -v
```

这是主动验收入口，不加入日常启动、plan 或 apply 的额外探测。未提供原生程序时测试跳过，不能把跳过算作原生验证成功。
