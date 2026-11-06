# Claude Desktop 4.29.0 偏好接口审计

日期：2026-10-28。静态源码审计及适配已完成，主任务已在 Mac mini 完成下述定向验证。所有运行与真实应用包提取仅限 Mac mini；MacBook 只审阅已保存源码和编辑候选。

本次新增当前审计记录，保留 3.6556.2、2.42304.2、2.42304.0、2.26454.2 的精确签名。字段范围不变，仍为五项偏好；没有供应商、凭据、会话或部署模式写入。

## 来源与固定身份

- 应用 bundle ID：com.anthropic.claudefordesktop；CFBundleShortVersionString/CFBundleVersion：4.29.0。
- 原生偏好模块：.vite/build/index.chunk-C7NkbHFq.js。
- SHA-256：92e8f69fb04f151845edc56f4246a0d3a2d2f91b311cfdb26e4f64edc43819e0。
- 源码由主任务从 Mini 应用包只读提取，审阅时复核该模块哈希。原始模块仅保存在私有临时审计目录，不纳入本候选或公开仓库。

兼容门禁继续匹配 bundle 身份与精确源码路径/哈希。版本标签只是审计来源；未知源码或被修改的已知版本仍拒绝。此记录不证明运行时控制、应用整体安全、GUI 生效或账号权限。

## 五字段核对

| 字段 | 4.29.0 的源码路径与行为 | 适配结论 |
| --- | --- | --- |
| userThemeMode | Do 为用户 config.json store；p9a 启动读取只接受 system/light/dark。WindowControl.setThemeMode 更新 nativeTheme.themeSource，值变化时才 Do.set。pQe 包装现有 store 读取。 | 名称、存储与枚举保持；未声明仍不写原生值。 |
| locale | twt 从 Do.get(locale) 读取，缺失/不在目录时按系统语言选择；ewt 校验名称并经 ZCt 检查 catalog，缺失时解析为 en-US，实际加载失败不写。 | 仍保存 config.json 的 locale，但 catalog 位置有真实变化，见下节。 |
| chatTabEnabled | fNe.enabled 的 flatKey 为 chatTabEnabled，optional Boolean，3p scope；映射 chatSurface.enabled。vNe/pNe 对 chat 要求显式 true；fl 可先采用 surface grants。 | 继续使用本地已应用 UUID JSON；不承诺政策覆盖后的最终 Chat 可见性。 |
| autoModeEnabled | workspace.autoModeEnabled 的 flatKey 不变，optional Boolean，3p scope。aFe 在缺省时 Code=true/Cowork=false；false 禁用该 reader 两项，true 允许两项。 | 存储与类型保持；其他 runtime/权限门禁未由此验收。 |
| builtinBrowserEnabled | oIe.enabled 的 flatKey 不变，optional Boolean，3p scope，default false；nIe 要求显式 true。 | 存储保持；政策、网络、self-hosted 限制仍可能决定实际能力。 |

Pu 在 macOS 从 userData 派生第三方目录（已有 -3p 后缀则保留；显式 CLAUDE_USER_DATA_DIR 时采用该 userData）；Fu 指向 claude_desktop_config.json，rd 读取 deploymentMode，且只承认 3p/1p。

Lu/Ru/zu 指向 configLibrary、UUID.json 与 _meta.json；DJe 读 metadata，OJe 使用合法 appliedId 读取对应 UUID JSON。jJe 优先采用可识别的 hybridPointer bootstrap，再考虑本地 UUID。现有适配遇任何 hybridPointer 阻断 feature apply/export 的规则继续保留，不写 dormant UUID 配置。lV 的编辑逻辑验证扁平字段后交由 aV 写 Ru；新增密钥封装流程不改变这三个非敏感 Boolean 的位置，适配不会调用封装或密钥 API，其他原字段由现有合并器保留。

## 必要的 catalog 位置修正

4.29.0 的 GCt 返回相对当前 .vite/build 模块的 ../../resources/i18n；QCt 从该目录读取 JSON。这是 app.asar 内的 resources/i18n，而非旧 Contents/Resources/<locale>.json。

主任务对 Mini ASAR header 的只读核对确认 43 个 JSON 全为 packed、非 link、有 offset。en-US.json 为 70,015 字节；fr-FR.json 为 91,122 字节；zh-Hans.json 为 49,217 字节；zh-Hant.json 为 51,153 字节。zh-CN.json 不存在；旧 Resources 根目录的 en-US/fr-FR/zh-CN 也不存在。

候选仅在上述新源码签名匹配后，从已读取并纳入 plan 指纹的 app.asar 中核对所选 catalog：必须是可读取的 packed 项、大小有界且 JSON 对象有效。旧源码签名继续采用旧路径。没有从“新路径缺失”回退到旧目录的逻辑，也没有 zh-CN→zh-Hans 隐式映射；用户要求缺失语言时继续阻断。

## 定向验证

- tests/test_claude_desktop_429_audit.py：全合成 ASAR，覆盖新旧审计登记、packed catalog、错误位置、缺失语言、中文不隐式改写、无效 catalog、未知源码/身份拒绝与按匹配签名选择路径。
- tests/claude_desktop_429_probe.cjs：只从精确哈希的原模块提取已审阅的小函数，在内存 store/path/catalog fixture 中验证主题、语言 fallback、3p/UUID 读取、feature 缺省值和 hybrid 优先级；不 require 原模块，不启动 Electron，不读取用户配置，不调用密钥函数。
- 既有 opt-in test_native_app_smoke.py：对 4.29.0 选择该源码 probe，仍只操作全新的临时 Profile/native fixture，检查 apply/export/rollback 后原字节恢复与未管理字段保留。GUI 生命周期放行只针对测试自建目录。
- 主任务在 Mini 使用显式候选框架路径执行相关回归，并另行对所选 Machine 做只读 plan。只读 plan 不等于真实 apply 或 GUI 验收。

## Mac mini 验收记录

主任务于 2026-10-28 报告，最终完整同步的候选通过以下验证：

- 53 项定向回归通过。
- 2 项 opt-in source smoke 通过：读取 Mac mini 的真实 4.29.0 应用包并校验模块指纹，只在内存中执行选定小函数；native apply/export/rollback 使用新建的合成 Profile 和数据目录。
- 所选 Mac mini Machine 的真实只读 plan 返回 ready=true、changes=0、无 blocker；app_preference_interface 为 passed，version 与 reviewed_version 均为 4.29.0，execution_started=false。

第一次传输与候选最后一次哈希更新交错，曾出现不完整同步、测试依赖模块缺失。该失败记录保留；完整重同步后的结果才计入本次通过结论。它不是应用兼容性失败，也不被删除或冒充通过。

上述验收没有启动 Desktop GUI、安装候选框架或改写日常应用与真实配置。真实 plan 的零变更不等于真实 apply 或 GUI 生效验证；函数夹具也不替代系统语言选择、完整 schema 引擎、加密存储、政策优先级或 Reach 同会话控制验收。测试和 plan 的原始结果由主任务另存本轮证据。
