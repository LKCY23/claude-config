# Claude Desktop 4.4212.0 / 4.16336.0 偏好接口审计

日期：2026-11-04，Asia/Singapore。MacBook 仅静态审阅已提取代码与编辑候选；所有应用包提取、下载、签名检查、源码函数测试、合成原生文件测试与真实只读 plan 均在 Mac mini 执行。没有启动、安装或替换日常 Claude，没有写真实 Profile/原生设置，也没有安装候选 CLI。

## 精确来源

| 来源 | 模块 | SHA-256 |
| --- | --- | --- |
| Mini 起始安装 4.4212.0 | `.vite/build/index.chunk-m4oMImJ-.js` | `926694919e5ff5d05cb804945637ffbe6ae75bdeac5e461f30e7c3d5fa043ccb` |
| 官方 4.16336.0 / Mini 后续安装 | `.vite/build/index.chunk-Du-Wqi0E.js` | `fcc1026c3634f62ae40581c680389b807c49e85626ff9989838fa0dc931d7f1a` |

官方稳定更新 feed 当次返回 4.16336.0。归档为 388,855,481 字节，SHA-256 `3740c05b4b82456e4bd60ee9faff13c7640f101a7683715b977333b283f0eecd`，完整 URL 在本轮 `verification/desktop-20261104/verified-report.json`。使用既有 download_app/inspect 验证大小、摘要、安全 ZIP 路径、bundle `com.anthropic.claudefordesktop`、版本、严格 codesign 和 Anthropic Team `Q6L2SF6YDW`，解压仅位于 Mini 独立临时目录。

本轮期间日常安装变为 4.16336.0；再次只读查询其 Info.plist 的修改时间为 2026-11-04 20:25:07。未将此次变化归因于候选工具，亦未主动触发日常升级。升级前提取的 4.4212.0 模块保留在 Mini 私有审计目录；该版本后续函数证据针对这个保存模块，不冒充旧完整 App 的当前实测。原始模块没有纳入仓库。

## 已核对的读写与语义

- 主偏好仍来自用户 `config.json`。4.4212 的 Eo/xQe 与 4.16336 的 bo/h1e 读取主题，setThemeMode 与启动代码仅采用 system/light/dark，未变化值不重复写。
- locale 的原生路径均为 `app.asar/resources/i18n`。路径函数分别为 gTt/Lkt；setter TTt/Kkt 先校验 locale 名称与已提供的 catalog，缺失时回退 en-US。框架仍拒绝缺失 catalog，不把 zh-CN 静默映射成 zh-Hans。源码匹配决定路径，不能靠版本号猜测或回退到旧 loose 文件。
- Mac 第三方根仍从 userData 派生 `-3p`，部署选择读取 `claude_desktop_config.json.deploymentMode`，本地功能项读取 `configLibrary/_meta.json.appliedId` 对应 UUID JSON。4.4212 的 ju/Mu/Pu/Fu/Iu/WJe/GJe 与 4.16336 的 Tu/Eu/Ou/ku/Au/xXe/SXe 的路径和读取逻辑已核对。
- 两版 `desktopHome` 均为可选字符串、3p 范围。原生 schema 保留 standard/simple/no-vm/off，公开 UI optionValues 只有 standard/off。本工具仅新增 standard/off 声明：前者给 Chat/文件分析/Cowork 授权，后者不给这些 surface 授权；Code 是另一个独立设置，不在本次新增范围。
- 明确 desktopHome 的 surface grants 优先于旧 chatTabEnabled。旧 Chat 值仍保留；不会删除旧字段或将未声明的 desktopHome 自动填为 standard/off。simple/no-vm 的既有原生值可以在未选择该字段时保留，但不能作为新的可移植声明导入。
- 原生 rollout 时钟在 2026-11-10 20:00 UTC（新加坡 11-11 04:00）跨过默认启用时间；源码 helper 仍考虑 1p 和 awaitingServed，4.4212 另有 retention 不一致的 heldBack 条件，4.16336 删除该条件。故报告只提示未验证的启动默认/策略效果，不凭当前日期声称 GUI 已启用。
- 三个旧 Boolean 的 scope/type 保持；autoMode 缺省 Code=true/Cowork=false、false 同时拒绝两者，内置浏览器要求显式 true。这里只核对这些 reader，不证明账号、工具或网络最终能力。
- hybridPointer 仍优先本地 UUID；原适配的 hybrid 阻断保持。sealed credentials 保持原字段合并，测试没有调用密钥 API、解封或获取真实凭据。

官方来源：[Unified Claude](https://claude.com/docs/third-party/claude-desktop/unified-claude)。文档与源代码核对范围相符，但管理策略、实际运行时启动时刻和 GUI 效果仍需日常验收。

## 实现与验证

- AppProbe 登记两份精确接口签名、保留此前所有版本；新增 desktopHome 在尚未审查该字段的旧接口上以 `preference_field` 阻断。未知源码仍阻断实际配置写入。
- `claude_desktop_home_semantics` 为非阻断报告：recognized 本地计划结果为 overridden；未设置为 default_not_verified；未知原生值为 unrecognized。三者都保留 gui_effect=not_verified，不泄露未知原值。
- `tests/claude_desktop_4x_probe.cjs` 只提取哈希锁定的小函数，在内存 store/path/catalog fixture 中执行；校验主题、locale、surface grants、真实 rollout 时钟及上下文条件、auto/browser reader 和本地 UUID 路径。hybrid 优先和 sealed keep 以静态断言核查，不运行完整策略或密钥系统。
- source smoke 对匹配的审计版本选择 probe，而非相信 Info.plist 标签。真实偏好 apply/export/rollback 只落在全新临时合成 Profile/数据根；包括 standard/off 往返、保留未管理字段与恢复原字节。
- Desktop watch 只选 Claude smoke，防止独立 Codex App 的 opt-in 检查污染单个 Desktop 发行版结论。
- Mini 完整回归：733 项，674 通过，59 跳过（含未主动开启的真实 runtime 项）；最后修改后 85 项定向回归通过。
- 官方隔离 4.16336.0 的既有完整兼容检查返回 compatible/native_smoke=passed；3 个 Claude smoke 测试通过（desktopHome 测试内覆盖 standard/off，因此共 4 次源码 probe）。保存的 4.4212.0 模块 source probe 通过。
- 当前 Mini 明确 Machine 的候选只读 plan：ready=true，changes=0，无 blockers，匹配 version/reviewed_version=4.16336.0，execution.started=false。desktopHome 仍未声明且本地未设置，语义报告 default_not_verified。

早期失败保留为调试事实：新增回归复用 fixture 时人为清掉已管理字段，正确触发外部漂移阻断；之后修正 fixture。SSH 非登录 PATH 初次未找到 Node；显式采用 Mini 已安装 `/opt/homebrew/bin` 后继续。source probe 初版 `desktopHome:` 定位不唯一而拒绝执行，改为两版各自唯一 schema 锚点后通过。没有放松哈希、唯一锚点或原生写入门禁来使测试通过。

证据目录：`verification/desktop-20261104/`。这些结果不替代真实配置应用、GUI、手机、账号调用或 Desktop 同会话远控验收。
