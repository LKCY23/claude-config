# App Profile 原生偏好实现

2026-10-17 复验：Codex App 已更新到 `26.1013.31256`，相关打包脚本名称及签名变化。逐项核对后，41 个已登记偏好定义、29 个内置代码主题及其适用模式与此前一致；完整 settings-store 除压缩变量名外一致，仍读取 `config.toml` 的 `[desktop]` 并逐字段写入 `desktop.<key>`。只更新已审查接口签名，保留两个先前审查基线，不增加同步字段或校验步骤。5 项兼容性、32 项 App 配置／恢复及 3 项已安装源码／临时原生文件测试通过，包含新版后端回读、导出和恢复；未修改本机原生配置，未验证 GUI 实际效果。审查记录（本机历史记录：`deployment/codex-app-interface-audit-20261017/audit.json`）

历史复验（2026-10-10）：Codex App 26.1007.21159 已完成安装源码审查；41 个偏好键、已选类型／值域、29 个代码主题及 desktop.<key> 读写接口与既有白名单一致。已登记新版本及源码 SHA256，保留 26.1002.52244 的已审查记录；未知版本／源码仍阻断。三项 App 原生读取、导出与恢复通过，GUI 实际显示和账号／组织覆盖仍未验证。新审查记录（本机历史记录：`deployment/codex-app-interface-audit-20261010/audit.json`）

当前状态（2026-10-17）：已安装 CLI 支持可选 App 偏好及 Codex 主题颜色、内置代码主题标识，本机已完成新版配置与入口迁移。允许范围沿用用户确认的 App 清单；应用快捷键、语音、Desktop 本地 MCP／扩展、供应商、凭据及 Agent 资源不属于本入口。

## 输入和入口

每份文件位于 `profiles/apps/<app>/<name>.yaml`。实际设置全部可选；以下只是结构示例，不是本机已启用值：

```yaml
schema_version: 1
app: codex-app
settings:
  followUpQueueMode: queue
  preventSleepWhileRunning: false
  open-in-target-preferences:
    global: vscode
```

Machine 的 `apps.<app>` 引用文件，并绑定 `application` 与 `data_root`。Codex App 当前已核实使用共享 Codex home，因此还需要 `agents.codex.binding.config_root`；不根据 App 数据目录猜测 Agent 配置位置。App 的管理基线存入 `runtime.state_dir/managed-apps`，与 Agent 分开，但共享原生文件使用同一个文件锁。Agent 字段白名单拒绝 `desktop`，App 字段白名单拒绝模型、权限和七类资源，不会形成双重管理。

```sh
claude-config plan --machine /absolute/path/to/machine.yaml --app codex-app
claude-config apply --dry-run --machine /absolute/path/to/machine.yaml --app codex-app
claude-config export --machine /absolute/path/to/machine.yaml --app codex-app \
  --field /settings/followUpQueueMode --field /settings/open-in-target-preferences/global
claude-config switch --dry-run --machine /absolute/path/to/machine.yaml --app claude-desktop \
  --profile profiles/apps/claude-desktop/other.yaml
```

与 Agent 一样，plan／validate 只读；apply／switch 可用 `--dry-run` 预览；export／rollback 默认预览，`--write` 才执行。`--expect-plan-id` 绑定已审阅的计划；`--agent` 与 `--app` 必须二选一。App 入口拒绝资源导出和 catalog 基线采用。

## 已核实的原生位置

| App | 本批审查版本 | 原生接口 | 核实范围 |
| --- | --- | --- | --- |
| Codex App | 26.1013.31256；保留已审查 26.1007.21159、26.1002.52244 | 共享 `config.toml` 的 `[desktop]`，按叶字段更新 | 设置 schema、读取、原生持久化代码；真实后端隔离 config/read 回读 |
| Claude Desktop | 2.26454.2 | `data_root/config.json` 的 `userThemeMode`、`locale` | 本地主题／界面语言 setter 与启动读取；语言要求有已安装 catalog |
| Claude Desktop 第三方功能 | 同上 | 现有 `configLibrary/_meta.json.appliedId` 指向的 UUID JSON | chatTabEnabled、autoModeEnabled、builtinBrowserEnabled 的类型、第三方作用域和读取来源 |

Codex 当前代码的 `hostStorage` 三分类用于旧资料迁移来源；本批审查的 `settings-store` 对注册偏好的持久化已统一写 `desktop.<key>`。本实现只用已核实的新接口，不读取或修改旧 global-state 偏好来模拟兼容。此前导出和观察文件保留其历史版本含义。

Claude 功能开关要求已有第三方部署且 appliedId／文件有效。2026-10-17 的 Bootstrap 候选审查确认：已检查 Desktop 构建从 `claude_desktop_config.json` 读取 `deploymentMode`；旧 0.1.3 读取 `config.json` 的问题已在本版 0.1.4 源码修正。推送源码不会替换已安装版本，使用第三方开关前须确认实际运行的是修正后的 CLI，不能通过伪造另一份模式字段绕过检查。官方本机继续使用主题／语言 Profile；三个第三方开关在实际目标端需使用已核对修正的版本再验收。工具不选择模式、不创建供应商资料、不修改 appliedId 或认证。语言指本地界面偏好，不代表账号侧网页语言。

2026-10-12 修正：默认能力检查要求 App bundle 身份及已审查配置接口源码一致，并捕获 Info.plist／app.asar／所选语言 catalog 的快照。构建版本号仅记录为证据；版本号改变但接口正文一致时允许继续，并报告实际版本和对应审查基线。应用身份、接口正文或语言包未知产生 `capability_unknown`。本机 26.1007.30912 的两份相关配置接口与 26.1007.21159 准确相同。源码审查与原生解析不等于 GUI 生效、账号能力或组织策略验证。

## 合并、写入和恢复

普通字段复用公共 MergePlan 的策略优先级、完整差异明细、手改检测和指纹化冲突解决。未声明保留、撤掉声明不重置，切换报告残留；导出只更新显式选择且本机存在的字段。本机缺失不删除 Profile 声明。false 和空分支前缀是有效值。Profile 只有元数据时不创建原生文件或管理基线。

`open-in-target-preferences` 只管理可移植 `global` 选择；本机 `perPath` 始终保留，不能导出其项目路径。内置目标采用逻辑标识，不把本机可执行程序、图标路径或自定义命令放入该字段。目标程序实际是否可用仍需 GUI 或目标机器环境验收。

最终写入前，全部来源、目标、解析、作用域和能力检查通过才开始。TOML 保留未修改字段及注释；JSON 只合并选定叶字段，语义无变化保留原字节。共享文件其余 Agent 配置、凭据、应用运行状态和未知偏好保留。

有实际原生写入时，计划要求用户正常退出对应 App；执行时每个写入校验再次检查，不启动、终止或重启 GUI。只读导出及原生无变化的操作允许 App 运行。框架文件锁不由 App 进程参与；进程检查和输入指纹用于发现变化，不声称拥有 App 的事务锁。

恢复共用私有备份／journal，只在当前文件等于记录结果时恢复；有后续 Agent 或手工写入时整文件恢复阻断，保留这些变化。App／Agent 使用不同恢复作用域，App 恢复不能运行 Agent 插件安装。原生偏好恢复同样要求 App 退出，Profile-only 导出恢复不要求退出。跨文件操作有失败恢复记录，不声称原子事务。

## 已支持范围与明确排除项

Codex Chrome 主题的 accent、accentSource、contrast、ink、surface、opaqueWindows 和 semanticColors 已接入逐叶合并；颜色为六位 hex，contrast 为 0–100 整数。未声明的字体和其他原生项保留，不整块复制主题对象或补原生默认。导出和字段策略选择明确叶子，例如 `/settings/appearanceLightChromeTheme/semanticColors/skill`。

内置代码主题按已审查版本的 29 个标识与 light／dark 适用模式检查；不接受本机主题文件路径或未知自定义标识。41 项 Codex 偏好及 5 项 Claude 偏好已登记。新增主题测试覆盖合并、导出、漂移和恢复，真实 App 后端 config/read 已验证存储结果；GUI 实际显示仍未验证。

2026-10-10 用户明确排除字体选择、字体文件／可用性检查、恢复默认字体和自定义通知声音文件。这些内容不属于同步、迁移或实现待办，不保留 pending 字段或依赖接口。Claude chatFont、Codex 主题 fonts／face 引用的声明、导出和字段策略均拒绝；本机原生值保留。字号、字体平滑及既有内置声音选择不涉及这些文件依赖，仍按现有白名单处理。App command bindings 尚未接入。

用户已明确排除工作树目录 `git-worktree-root` 和自定义文件处理器 `custom_file_handlers`：两者不属于本系统的同步、迁移或 Machine 绑定管理范围，不列为待实现项，不建立接入口。Profile 声明、导出选择和字段策略均拒绝它们；已有本机原生值保留，其他偏好写入不得改动这两项。普通内置编辑器／浏览器选择和已选 Git 行为偏好仍按原范围管理。

旧数据转换时只记录它们被排除及原因，不向新 Profile／Machine 复制值；历史导出快照保留为证据。运行时不为它们保留兼容别名。

实际使用只部署 Machine 选定 Profile 中声明的字段，不补默认值或变更账号。未支持的 App bindings 和 GUI 效果仍需分别处理；已明确排除的功能不列入后续开发。

历史记录（2026-10-10）：当时本机主题只读导出到本地 App Profile（本机历史记录：`deployment/app-preference-adapter-check-20261010/data/profiles/apps/codex-app/current.yaml`），并生成完整报告（本机历史记录：`deployment/app-preference-adapter-check-20261010/state/reports/edccfb63c8304d7c8cc95be8f7bb1da4.json`）。这是单独的本地导出，未推送，也未修改原生偏好或当前配置仓库。

## 字段注册表

以 `src/claude_config/core/app_fields.py` 为实际白名单。下面记录可同步字段和类型；所有行均可省略。

| App | Profile settings 键 | 类型／值域 |
| --- | --- | --- |
| Codex | `appearanceTheme` | system / light / dark |
| Codex | `appearanceLightChromeTheme`、`appearanceDarkChromeTheme` | 对象，按已登记颜色／contrast／窗口及 semanticColors 叶子合并；fonts／face 明确排除 |
| Codex | `appearanceLightCodeThemeId`、`appearanceDarkCodeThemeId` | 已审查版本的内置逻辑标识，校验 light／dark 模式 |
| Codex | `appearanceDiffMarkerStyle` | color / symbols |
| Codex | `sansFontSize` | number，11–16 |
| Codex | `codeFontSize` | number，8–24 |
| Codex | `useFontSmoothing` | boolean |
| Codex | `usePointerCursors` | boolean |
| Codex | `reduced-motion-preference` | system / on / off |
| Codex | `followUpQueueMode` | queue / steer / interrupt |
| Codex | `composerEnterBehavior` | enter / cmdIfMultiline / cmdAlways |
| Codex | `composerPlainTextMode` | boolean |
| Codex | `confirm-before-close` | never / keyboardOnly / always |
| Codex | `conversationDetailMode` | STEPS_PROSE / STEPS_COMMANDS / STEPS_EXECUTION |
| Codex | `show-context-window-usage` | boolean |
| Codex | `show-educational-tips` | boolean |
| Codex | `default-workspace-full-view` | boolean |
| Codex | `sidebar-show-projects-in-recents` | boolean |
| Codex | `localeOverride` | 已支持的界面 locale |
| Codex | `open-in-target-preferences` | 对象，仅 global 内置逻辑标识 |
| Codex | `open-link-in-target-preference` | in-app-browser / external-browser |
| Codex | `open-local-url-in-target-preference` | in-app-browser / external-browser |
| Codex | `git-branch-prefix` | string |
| Codex | `git-always-force-push` | boolean |
| Codex | `git-create-pull-request-as-draft` | boolean |
| Codex | `git-pull-request-merge-method` | merge / squash |
| Codex | `git-pr-poll-interval-seconds` | integer，15–300 |
| Codex | `git-show-sidebar-pr-icons` | boolean |
| Codex | `worktree-auto-cleanup-enabled` | boolean |
| Codex | `worktree-keep-count` | integer，1–无原生上限 |
| Codex | `git-review-mode` | full / last-turn-only |
| Codex | `reviewDelivery` | inline / detached |
| Codex | `notifications-turn-mode` | off / unfocused / always |
| Codex | `notifications-permissions-enabled` | boolean |
| Codex | `notifications-questions-enabled` | boolean |
| Codex | `notifications-dot-enabled` | boolean |
| Codex | `notifications-sound` | default / classic / none / Basso / Glass / Ping / Pop / Purr / Tink |
| Codex | `preventSleepWhileRunning` | boolean |
| Codex | `keepRemoteControlAwakeWhilePluggedIn` | boolean |
| Claude | `userThemeMode` | system / light / dark |
| Claude | `locale` | 已支持的界面 locale |
| Claude | `chatTabEnabled` | boolean |
| Claude | `autoModeEnabled` | boolean |
| Claude | `builtinBrowserEnabled` | boolean |
