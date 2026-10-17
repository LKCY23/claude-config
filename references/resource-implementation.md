# 固定资源与本机绑定：当前实现

更新：2026-10-18。固定来源、静态正文、MCP／command Hook 注册及 Plugins 原生安装、更新、启用和恢复已接入已安装 CLI；本机已完成新版数据与入口迁移。以下支持范围以实际白名单和适配器为准。

现有用户插件的准确名称接管、同名重复安装阻断、启用状态保留及原生来源更换／恢复已接入；详见 [插件接管规范](plugin-adoption.md)。接管要求固定包匹配版本及文件，包装更新不能借此跳过更新审阅；保留未知旧缓存，不将其纳入固定来源。

## 设计职责与当前实现

职责规范见 [架构合同](../DESIGN.md#资源和本机依赖)：claudespace 管理定义、固有配置、支持的入口和约束；Agent Profile 保存资源选择与运行配置；Machine 提供本机依赖及凭据引用；本框架解析和生成原生部署产物。组合到同一原生文件不改变来源归属。自定义代理的提示词、工具清单、权限及内部 MCP／Hook 定义归来源，不扩成 Profile 的任意覆盖字段。

每端 compatibility.entry 是显式默认入口，MCP 可附加 entries 名称到固定包内模板路径映射；Profile options.entry 选择名称，未声明时采用来源默认。默认及所有具名文件都核对固定清单，所选模板实际引用的绑定必须提供。框架记录入口与原生连接变化，指纹区分所选模板；导出保留逻辑选择，不从本机连接猜测。该结构性选择不进入普通字段合并策略，不支持 transport 通配或自动协议转换。

逐字段范围与实际版本证据见 [资源参数证据清单](resource-parameter-evidence.md)。它区分模板字段与 Profile options、Hook 统一控制与逐处理器控制，并记录原生调用控制及尚需进一步证据的字段；整理清单不改变当前源码白名单。

## 来源与协议

`catalog.py` 从配置数据的 catalogs.yaml 读取固定 repo／完整 commit／path，以 Machine.catalog_roots 的 catalog 别名找到已准备的仓库根。它只检查已准备来源，不 fetch、checkout 或跟随 HEAD 最新版本；正式来源准备由独立的 `fetch-baseline` 负责。核对真实 Git 根、origin 身份、准确 HEAD、工作区状态；catalog JSON 和资源文件还逐一比对提交对象，防止 assume-unchanged 等标记隐藏修改。外部来源须预先准备到 state_dir/sources/<owner--repo>/<commit>；submodule 同时核对父 Gitlink、子仓库 HEAD 和身份。

新登记协议的维护源为 claudespace/scripts/resource_catalog.py。框架随源码携带字节一致的受审副本 src/claude_config/core/catalog_schema.py，使框架独立运行且不 import 选定仓库的 Python 代码。修改协议时同步副本并检查一致性；副本不是第二套资源登记。

资源依赖必须已明确选择；缺失和循环阻断，不自动增加安装选择。包内文件软链接仅允许指向包内已跟踪文件，并核对目标提交内容；部署时保存实际文件字节。目录软链接、越界链接、未跟踪目标、未独立声明的嵌套 submodule 和运行缓存不作为资源正文复制。

快照 SHA256 使用规范清单：包内 POSIX 文件路径到 `{sha256, executable}` 的映射，按键排序、JSON 紧凑序列化后计算 SHA256。source_fingerprint 记录原正文，rendered_fingerprint 记录使用选项渲染后的正文，二者分别保存。

## 已接入的正文和控制

| 类型 | 当前接入 | 当前未覆盖或未验证的范围 |
| --- | --- | --- |
| 独立 Skills | 完整目录保留执行位；Claude enabled（skillOverrides on/off）、disable-model-invocation／user-invocable；Codex allow_implicit_invocation 与 skills.config.enabled 分开管理，省略保留；显式资源使用选项导出 | 其他原生控制、原生策略／运行会话的覆盖验收 |
| 全局指令 | 保留区块外文字；包内 Markdown 目标检查；Claude 引用完整固定包入口，检查导入图；Codex 入口链接重定位；引用包纳入导出、漂移与恢复 | 原生实际加载／业务效果未验收；Codex 不承诺 Claude-style 导入，本机／包外引用需明确适配 |
| Rules | Claude Markdown（含 paths 前言结构检查）；Codex .rules 使用原生 execpolicy check 做纯语法校验 | 真实加载／规则行为、扩展选项及跨端完整联调 |
| Agents | Claude Markdown／Codex TOML 独立定义；有限 model／Codex model_reasoning_effort 运行调参接入公共策略、缺失保留和逐项报告，源与渲染指纹分开 | 选中定义内部 MCP／Hook／路径依赖尚未适配；原生模型可用性及真实派发尚未验收；不增加 Profile 工具／权限定义覆盖 |
| 状态栏 Hook 脚本 | status-line 模式完整包，由 typed command 生成命令，不重复注册 | 输出协议、运行效果 |
| 生命周期 Hooks | 原生事件与 matcher 分组、command 处理器、完整脚本包；未知归属／分组修改／重复歧义阻断；注册、普通设置和恢复共用计划；均一处理器选项导出 | 其他处理器及事件、运行效果和原生信任 |
| MCP | Claude 用户 .claude.json／Codex config.toml 的原生服务；来源具名模板选择、具名依赖、环境凭据引用、使用选项、归属和恢复；匹配所选模板后的选项导出 | 连接、认证；自由连接参数覆盖、OAuth／动态 header helper 等扩展配置 |
| Plugins | 固定完整原生包、本地 marketplace、用户安装／更新、独立启用状态、库存与正文核对、副作用日志、失败和独立恢复；准确库存匹配后的启用状态导出 | 插件内部绑定渲染、自动身份重命名和组件控制；真实业务加载和运行仍未自动验收 |

Claude Skills 部署到 config_root/skills/<原生 name>。Codex USER Skills 使用原生 `$HOME/.agents/skills` 布局：登记 requires.bindings.install_root 为 path，Profile 引用 Machine 的路径绑定，路径必须以 .agents/skills 结尾。该目标目录可不存在，写入时创建；其他普通依赖默认必须存在。原生 name 与 catalog ID 分别保留，不擅自改名。[Codex Skills 原生路径与控制](https://learn.chatgpt.com/docs/build-skills#where-to-save-skills)。

Agents 部署到 config_root/agents/<原生 name>.md／.toml；原生字段、定义与普通 settings.agents 运行策略分别管理。[Codex Agents 文件接口](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents)。定义可解析、文件已写入和真实派发生效分别记录。

## 缓存、所有权与更新

完整渲染包保存到 config_root/.claude-config-resources/<catalog>/<id>/<rendered_fingerprint>，供脚本和依赖使用。缓存按内容固定，旧缓存保留供当前引用或恢复，不隐式垃圾回收。

新资源遇到已有未管理文件或未知附加文件会阻断，即使同名也不自动采用。已有受管资源先比较内容和模式的基线；已知源基线更新可以替换，外部修改仍是 drift。完整 Skill 更新删除已知、未修改的旧文件，并返回 remove_obsolete_resource_file；原生 identity 改名仅退役该资源旧身份的受管文件，不清理用户未知内容。

移除选择停止主动管理，保留已安装内容并返回 residual；状态记住安装归属，重新明确选择时仍检查基线。全局指令只拥有标记区块：区块外文字变化可在重新规划时保留，区块内部修改阻断。

文件模式变更、明确的文件删除、目录清单指纹与恢复已纳入公共执行器；预览后或执行期间出现未知附加文件会阻断。全部正文与原生配置、管理状态、Machine 切换在同一文件计划内先预检再执行。不宣称多文件系统级原子性。

完整包内容按准确选定来源替换，不做任意脚本文本合并。普通 settings 和已登记的资源使用选项复用公共合并策略；策略只调整受支持的使用叶子，不绕过正文 pin、归属或修改检测。不接受未知策略路径。

## 独立技能启用与调用控制

Codex 资源 options.control.enabled 写入 config.toml 的 skills.config 路径条目；路径由 Machine 安装根和原生 skill name 生成，指向实际 SKILL.md，不进入可同步 Profile。false 保留完整安装正文，仅声明禁用；没有显式 enabled 且没有既有受管条目时，不写原生默认。allow_implicit_invocation 仍写入技能 agents/openai.yaml.policy，false 限制隐式调用，不等于禁用。[OpenAI Docs：技能启用与调用控制](https://learn.chatgpt.com/docs/build-skills)。

选择器按准确路径管理；首次同路径未知条目、重叠名称选择器、重复、已有条目被改或删除会阻断。受管条目只在原有位置更新，其余 skills.config 条目和顺序保留；skills.include_instructions／max_context_tokens 等普通设置与控制数组合成同一文件结果，不互相覆盖。

省略已管理的启用／调用项保留本机值；移除资源选择保留安装和控制条目并报告 residual。固定资源的原生 name 更新可以迁移自己未修改的路径条目；改变安装根时要求明确重定位，不能遗失旧条目的归属。调用配置正文被手改或删除不会通过省略选项被悄悄采用。Claude 的调用控制也遵守省略保留；enabled 已映射到独立技能的 skillOverrides on/off。

原生 skills/list 回读验证与文件写入验证分开：CLI／App 后端的临时目录验收已确认 enabled true／false，但不把当前会话、管理策略或账号层覆盖误报为已验证。原生文档要求修改 config.toml 后重启客户端；本框架不自动重启。

## 资源选项的策略与报告

已接入的范围：Plugins enabled、Skills 的调用控制及两端 enabled、MCP 已登记使用叶子和工具选项、command Hook 的 timeout／async／statusMessage、Agents 的 model 与 Codex model_reasoning_effort。其他选项和组件控制尚未接入策略注册表，不能声明任意路径。

这些是明确登记的运行选项，不授权改写资源职责。来源仍维护默认值和约束；尤其 Agents 的工具／权限清单与 Hook 的事件、matcher、处理器正文继续随来源版本管理。后续适配以选中资源实际需要的运行选项和依赖为范围，不追求覆盖所有原生字段。

策略路径为 `/resources/<组>/<ref>/options/<原生选项路径>`，例如 `/resources/skills/tiny@team/options/control/enabled`、`/resources/mcp_servers/server@team/options/tools/read/approval_mode`。包含斜线的工具名使用 JSON Pointer 的 ~1；ref、点号和标点作为完整标识保留。

使用公共优先级：本次字段 > Profile 字段 > 本次默认 > Profile 默认 > Machine 默认 > source_wins。支持 source_wins／keep_target／fail_if_different；MCP 工具列表可以显式 replace，保持声明顺序，空列表是明确值。没有资源选项列表登记为无序集合，因此 append_unique 阻断。普通设置导出保留已有资源策略，不据此修改资源；本次导出明确传入资源策略会报不适用。

每项 merge／preserve／overwrite／replace／conflict 返回真实逻辑路径、策略及出处、前后值、目标摘要和解释；Hook 同时记录具体原生事件／分组／处理器定位。同一资源的多个处理器可以分别报告。fail_if_different 阻断整份计划，不能只部署正文。未选择资源或未登记叶子的字段策略在规划时拒绝。

Agents 的策略路径例如 `/resources/agents/review@team/options/model`，以及 Codex 的 `/resources/agents/review@team/options/model_reasoning_effort`。模型与推理强度可各用不同策略；Claude 定义不接受 Codex 推理字段，也不接受数组策略。固定源定义显式提供的值参与 incoming，Profile options 可以覆盖它；若源定义和 Profile 都没有某项，则保留此前受管定义中的值，不补默认。报告区分 catalog 来源和 Profile options 来源。

当前值只能来自已知、未修改的受管 Agent 定义。文件被删除、正文／模型／权限模式被手改、未知同名文件或身份歧义仍阻断，不因 keep_target／source_wins 被收编。新基线正文可更新而模型按 keep_target 保留；原生身份重命名仅退役原有受管文件。反向导出仍核对完整正文后只写 Profile，独立恢复保留后续改动。模型／推理选项校验非空字符串及现有字段规范，账号可用性和实际派发不在此测试范围。

资源正文、服务结构或已受管使用项的外部修改仍单独检测；全局 source_wins 不解除 drift。普通设置的 --resolutions 支持指纹接纳，资源已接入冲突 ID、来源恢复／本次保留及来源更新工作流；详见 [资源冲突处理](resource-conflict-resolution.md)。本次保留不更新管理基线，未知归属不由字段策略收编。

## 命令与反向导出

Profile command binding 仅可指向该端已注册的 command 字段。Machine executables 提供可执行程序；args 为字面参数或已选择资源的包内文件。包内引用必须存在于准确跟踪的资源清单；POSIX 参数逐个 shell quoting，不执行字符串模板、不展开参数里的 `$()` 或变量。

Machine 具名引用按组和完整名称解析（名称含点号也不拆散）。程序、路径依赖检查存在性，程序检查执行权限；凭据只保留 env／native 引用，不转成参数、正文或日志。仅检查 env 引用是否可用，native 认证仍未验证；不是自动认证流程。

导出选中已有 binding 的命令字段时，固定来源和 Machine 绑定重建预期命令。匹配则返回 preserve_binding 并保持逻辑声明，不往 settings 写入绝对命令。无法匹配返回 binding_drift；报告不打印手工命令值，用户需调整逻辑声明后重新规划。

## MCP 原生模板与选项

具名模板由 catalog 的 compatibility.entries 声明；实际参数范围见 [运行参数证据](resource-parameter-evidence.md#mcp-字段与单位)。options.entry 只供框架选模板，不写原生 server；闭合的 native options 与连接模板继续分开校验及合并。不同模板所需槽位可以在 requires 中声明可选，但当前选择实际使用的槽位必须在 Profile／Machine 提供。

每端 compatibility.entry 指向本资源包里的 JSON 模板。模板采用对应端原生 server 字段，只有本机依赖位置使用封闭占位对象；不是新的跨端传输协议，也不是可执行字符串模板：

```json
{
  "name": "example-service",
  "server": {
    "command": {"binding": "program"},
    "args": [{"resource_file": "server.py"}, "literal argument"]
  }
}
```

catalog 的 requires.bindings 声明 program 为 executable；Profile 资源条目 bindings.program 引用 Machine 的 executables.python。包内文件参数须在完整已跟踪清单里，生成指向内容固定包的路径；其他本机参数路径写成 `{binding: 工作目录槽位}`，槽位类型为 path。command 必须使用 executable 绑定，cwd 须使用目录 path 绑定；不把本机绝对路径或隐式 `${VAR}` 直接放入可迁移模板。

远程 server.url 可以是无内嵌凭据／query／fragment 的公开 HTTP(S) URL，或 endpoint 绑定。凭据只引用声明的 secret 槽位：Codex bearer_token_env_var、env_http_headers 和 stdio env_vars 渲染环境变量名称；Claude env／headers 渲染 `${NAME}`，header 可显式加 `prefix: "Bearer "`。不复制环境变量值。Claude 文档明确屏蔽的一组远程 header 凭据变量会阻断，需选择服务自己的变量名；凭据引用可用不等于客户端启动环境和认证成功。[Claude MCP 配置与环境展开](https://code.claude.com/docs/en/mcp#environment-variable-expansion-in-mcpjson)、[Codex MCP 字段](https://learn.chatgpt.com/docs/extend/mcp)。

Claude 首版接入 stdio／http／sse、args／env／headers，以及 Profile options.timeout：原生单位为毫秒，必须至少 1000。Codex 接入 command／args／cwd／env／env_vars 或 URL／环境认证 header；options 支持 startup_timeout_sec、tool_timeout_sec、enabled、required、enabled_tools、disabled_tools、default_tools_approval_mode、tools、supports_parallel_tool_calls、tool_input_schema_max_bytes。选项按原生类型校验，false、空工具列表保留明确语义；不换算成统一跨端 enabled 或 timeout。其他原生字段目前阻断，不能借任意对象穿过白名单。

Claude 用户注册只修改原生用户文件的 mcpServers.<name>，保留机器标识、项目资料和其他服务。默认 ~/.claude 根目录对应 ~/.claude.json；显式 CLAUDE_CONFIG_DIR 对应 config_root/.claude.json。Machine 的 Claude binding 可选 user_config 明确区分这两种原生布局，不能任意指定客户端不读取的文件；转换器和入口核实应填入准确位置。Codex 只修改 config.toml 的 mcp_servers.<name>，和普通设置合成一次文件写入。首次同名条目即使相似也不擅自接管；已受管服务整体指纹改变或缺失时阻断。准确固定来源更新可替换该服务，改名仅删除前一个未修改的受管身份。移除选择保留服务和归属，重选仍检查旧基线。

MCP 使用控制沿用公共增量合并：缺少某个 option 保留受管服务当前值，tools 按工具／叶子合并，工具名单数组完整替换。已登记字段支持策略覆盖，服务连接模板与使用选项分别处理；已知服务漂移可按审阅 ID 恢复来源或本次保留，不能通过全局 source_wins 绕过漂移。恢复来源时未声明项依据私有 B 注册快照，而非手改的 L。

同一配置根已存在受管注册时，改变 user_config 位置返回 registration_binding_changed，不隐式搬走服务或遗失原位置的管理归属；这种重定位须在完整转换／绑定迁移中明确处理。

## 生命周期 Hook 模板与分组归属

生命周期模式的 entry 是原生事件／matcher／handler JSON；首版 command 的 executable、args 为逻辑模板：

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": {
          "executable": {"binding": "shell"},
          "args": [{"resource_file": "check.sh"}]
        }
      }]
    }]
  }
}
```

shell 在 catalog requires 中声明为 executable，Profile 引用 Machine 程序绑定；生成 POSIX shell quoting 的命令，部署阶段不运行处理器。timeout、async、statusMessage 可以在该资源 options 统一覆盖其全部处理器，不支持逐处理器 Profile 控制；Codex 来源模板接受 additionalContextLimit，但该字段不在 Profile options 白名单。非法类型、未接入的事件／处理器、该端会忽略的 matcher 在预检中阻断；Codex SessionEnd／Interrupt timeout 不超过 3 秒。当前事件白名单见 registrations.EVENTS；它不宣称涵盖客户端全部事件。

Claude 注册在 settings.json 的 hooks；Codex 注册在 config_root/hooks.json，保留已存在的 inline hooks 并报告两层同时加载。每个资源拥有准确的 matcher 分组指纹；先按指纹核实所有权，只替换自己的分组，保留其他分组及它们相互间的顺序。当前某个受管分组找不到、被手改或有重复匹配会阻断；不凭 matcher 相同猜测所有权。多个服务／Hook 共享文件时先合并成唯一 FileChange。

已核实归属的同事件／唯一 matcher 分组保持处理器数量时，使用选项按处理器顺序逐字段增量合并，省略 timeout／async 等不会恢复默认，并返回 preserve 决策。分组或处理器布局变化导致省略选项无法定位时返回 hook_options_ambiguous，要求明确使用选项后重新规划；不同事件／matcher 是新的原生分组身份，不隐式继承其他分组的控制。

Codex feature 开关、原生 enabled／exact hash trust 继续由原生流程控制；本计划不设置 trusted_hash、不绕过原生确认，不因注册成功宣称 Hook 可运行。已登记 timeout／async／statusMessage 的反向导出已实现；其他原生策略控制仍不在当前支持范围。[Claude Hooks](https://code.claude.com/docs/en/hooks)、[Codex Hooks 与信任](https://learn.chatgpt.com/docs/hooks)。

## 文件恢复与插件副作用边界

正文、注册、普通设置、管理基线与 Machine 切换共用锁、来源／现状指纹、私有备份与执行日志；独立 rollback 默认预览，见 [运行入口](new-agent-profile.md#独立文件回滚)。恢复完整文件字节及模式，后续修改一律阻断，不清空安装目录。原生注册状态只存服务或分组的身份／摘要，不存凭据值。

两端插件原生操作已经纳入统一执行器。Claude marketplace add／plugin install／plugin update 修改 settings.json、用户 .claude.json、known_marketplaces.json、installed_plugins.json、原生缓存及启动备份；Codex marketplace add／plugin add 修改 config.toml 的 marketplaces／plugins 和缓存。日志记录实际 before／after 指纹、私有元数据备份与历史固定来源，CLI 原始输出不会进入报告。

完整 apply 先准备固定正文和普通设置／MCP／Hook 文件，再运行原生插件步骤，核对准确版本、正文指纹和 enabled，最后写管理基线及 Machine 引用。副作用只允许改变选定插件、其 marketplace 和核实的 Claude 启动元数据；其他字段、库存作用域或备份被修改则保留现场并报告 recovery_conflict，不能用“CLI 写过了”吞掉外部改动。

## Plugins 安装入口、状态与更新

compatibility.entry 必须为包根的 .claude-plugin/plugin.json／.codex-plugin/plugin.json，包含原生 name 与准确 SemVer version。完整包及附属资源一起部署到 `.claude-config-resources/marketplaces/<生成名称>/packages/<原生名称>/<正文摘要>`，此处是固定安装来源，不是原生插件库存。marketplace 元数据只从选定包和已记住的保留条目生成，以受支持的 `./packages/...` 相对路径引用正文；两端原生 installer 负责实际安装。生成名称保留 catalog 别名并附摘要，避免大小写／标点归一后的碰撞；Profile 的 ref 与原生 selector 分开记录。

现有未知安装／注册、版本或正文 drift、手改启用状态、生成入口修改、历史来源变动均在预检阻断。已知源基线明确更新使用原生更新接口：Claude plugin update，Codex plugin add；同一原生版本不得重新发布不同正文，应在发布侧增加原生版本或包装修订版本。原生身份变化不隐式卸载旧插件，首版要求另行明确替换。

options.enabled 显式覆盖；省略时保留受管状态，新安装报告原生默认。移除选择保留安装和归属并返回 residual；同一 marketplace 的其他条目保留。无变化的 apply 不调用安装器。Codex enabled 通过其原生 config.toml 字段单独写入，保留其余字段和注释；Claude 使用原生 enable／disable。

恢复首先核对完整当前库存、缓存和元数据与记录结果，再通过原生接口卸载或重建旧版本。旧正文从已验证历史固定来源读取，不复制缓存来伪造安装。Claude 的原生 .orphaned_at 回收标记按元数据备份恢复；原生卸载留下的、本次新建且正文未改、未在用的退役版本缓存目录才会清理。存在文件型 .in_use、符号链接或非空租约目录时，在卸载前阻断恢复；会话退出留下的空目录不代表占用。不删活跃缓存、不清空插件目录。失败会报告具体阶段和 recovery_conflict；恢复本身的失败也使用同一反向执行协议。

当前原生适配不渲染插件内部的 Machine 依赖，不修改插件组件选项，不复用旧 catalog 格式，不同步认证数据库或授予原生信任。声明这些未支持内容时完整预检阻断。

## 验证与剩余工作

resource_export.py 对明确选择、已声明绑定的资源进行只读反向匹配。插件要求当前 selector、注册位置、准确版本和正文相符；Skills 要求完整目录在调用控制以外与固定包相符；MCP 的连接部分必须仍等于逻辑模板渲染；Hook 要求完整脚本包、绑定命令、分组及可统一表达的控制；指令只检查受管区块，Rules／Agents 检查固定定义，代理可提取模型覆盖。Status-line 使用现有命令绑定导出。只把显式可移植 options 写入 Profile，缺失保留，不复制正文或本机路径。

报告分别说明 preserve_reference、preserve_missing 和 mapping_conflict；原生安装与管理归属未因此改变。导出后对手改控制项的正常 apply 仍可能报告 drift，必须另行明确接纳，不能因“导出一致”推定程序已经取得管理归属。

baseline.py 的 adopt-baseline 只采用本地已准备发布记录与准确 commit，列出影响并修改 pin／来源根；不接管未知安装。其校验层级与入口见 [原生入口](new-agent-profile.md#采用已准备资源基线)。

资源专项单元验收覆盖来源 pin、仓库身份、隐藏修改、依赖缺失／循环、软链接、完整目录、真实模式、未知归属、正文 drift、增量更新、选择移除／重选、绑定逆向映射、目录新增及失败恢复。

主动启用的 test_native_resource_smoke.py 已在临时目录验证：Claude 状态栏部署但脚本不执行、MCP 写入结果与原生 add-json --scope user 一致；Codex USER Skill 发现、原生规则语法，以及 CLI／App 后端的 mcp get 元数据和 hooks/list 发现。脚本标记文件未产生，不调用模型、不连接测试 MCP；原生用户目录、插件库存、登录和网络配置未修改。

test_native_plugin_smoke.py 使用 Claude、Codex CLI 和 App 后端在临时目录验证首次安装、更新、enabled、无变化不调用安装器、失败恢复、独立回滚和多历史版本恢复。原生库存和正文均实际核对；业务插件脚本未执行。

新增资源选项专项测试覆盖选择器归属、重复、手改、缺失文件、策略优先级、数组顺序、空列表、整份阻断、共享 skills 表、恢复及普通导出保留资源策略。原生验收在 CLI／App 后端确认技能禁用仍安装、启用可回读、调用控制与省略保留。

资源反向导出、本地已准备基线采用和旧数据转换已接入，本机迁移已完成。Claudespace 的更新发布、永久 Release、远端附件获取和消费者采用已完成真实联调，跨仓库来源使用精确映射；HUD 也已接入正式更新流程。新选择的资源仍按运行入口、依赖和管理归属逐项检查。Desktop 本地 MCP／桌面扩展继续排除，不增加任意资源定义覆盖。

2026-10-10：具名 MCP 入口 9 项及引用布局 13 项测试通过，覆盖缺失／越界目标、原生导入循环与包装后的深度、文件名转义、入口切换、使用选项保留、未知绑定／入口、结构性选择的策略拒绝、连接漂移、引用包导出、预览过期、指令资源切换和回滚。当前 code-review／code-simplifier／karpathy 包内 Markdown 检查通过；专用代码字符串路径继续使用既有构建校验器。2 项新增原生用例在当前 Codex CLI 和 App 后端回读所选 HTTP 模板并导出；未验证服务连接或原生指令实际加载。详细总体结果及独立 App 审查阻断见 本批验证报告（本机历史记录：`deployment/resource-runtime-and-references-20261010/README.md`）。

## 2026-10-17 Claude 独立技能启用状态

两端 Profile 统一使用 `resources.skills.<ref>.options.control.enabled`（可选布尔值）。Codex 映射到路径型 `skills.config`；Claude Code 映射到 `skillOverrides.<原生 name>` 的 `on` / `off`，安装正文始终保留，enabled 不写入 SKILL.md 前言。未声明时不创建默认值；已经管理的状态在省略字段时保留。

原生状态按资源分别记录归属，未知同名 override、后续修改/删除及身份重定位阻断。来源恢复、保留本机、导出、回滚沿用既有检查；导出不会将 name-only/user-invocable-only 错当成布尔值。独立技能目录如果含插件 manifest、被 Claude 自动暴露为 `<name>@skills-dir`，同一开关还同步该已归属入口的 `enabledPlugins` 状态，避免普通入口关闭而自动入口继续开启。其他正式插件仍由资源 plugins 的启停机制管理。

依据 [Claude 官方技能可见性](https://code.claude.com/docs/en/skills#override-skill-visibility-from-settings)。本机 2.1.278 的隔离 SDK initialize 命令列表验证：全部七项文件已安装，off 的两项不在命令列表，其余五项存在；没有发送用户推理请求或运行技能业务。
