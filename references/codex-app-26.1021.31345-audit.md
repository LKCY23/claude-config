# Codex App 26.1021.31345 偏好接口复验

日期：2026-10-27，Asia/Singapore。工具补丁版本：0.6.1。

## 问题与边界

原审查记录为 26.1013.31256；当前安装的 bundle 仍为 `com.openai.codex`，构建 26.1021.31345（21828），但相关脚本路径和内容指纹已改变，故原工具返回 `capability_unknown`。版本号仅作为证据；放行仍需要应用身份和已审查源码一致。

此次继续支持现有 41 项可移植偏好和 29 个代码主题，不扩展字段，不改 Agent 模型、供应商或认证，不执行 live App 偏好写入。未知接口、外部改动和运行中的 App 写入保护继续生效。

## 核验结果

- 现有注册字段的类型、枚举、数值边界和可移植取值均通过安装包内实际 schema 验证；主题标识及 light/dark 适用模式与工具白名单一致。
- 原生存储仍读取 `config.toml` 的 `[desktop]`，逐键持久化为 `desktop.<key>`，使用 replace 合并策略；不以旧 global-state 作为新的持久化入口。
- 新版 settings-store 增加异步持久化失败的内存恢复：`setAndPersist` 失败后恢复前值与对应配置值。隔离测试覆盖成功写入、失败恢复、非法值拒绝和未初始化拒绝。
- 原生 TOML codec 会补回 schema 可空成员（例如主题字体的 null 值）；测试按这一行为验证读取结果。字体仍不进入可同步白名单，本机值继续保留。
- App 后端真实 `config/read` 回读、Profile 导出和事务回滚通过临时目录验证；模型与未知偏好保持不变。

## 固定证据

| 安装包内路径 | SHA-256 |
| --- | --- |
| `.vite/build/src-CTNXdakG.js` | `8bef484d79c728e944bf1ee5bc9d91e135864363ad449a3c182f0d888a991660` |
| `.vite/build/startup-requirements-CuyNnLmn.js` | `3451b138221aaad11aad1db8ec122a3363189379b9d47d2b0fe7bf553636a46f` |
| `.vite/build/zod-DDAr8jEZ.js` | `e21ef21d2a511d5fa0b276b4f759011baf5b21d99d11e82a7c4515996eb4e0db` |
| `.vite/build/rolldown-runtime-CPUxUITh.js` | `9a875a54cb52a6ee0d1097895ae92aa8c9d999af95f167e90456d6fc0c03f296` |

保留三份旧 Codex App 审查记录。源码正文不提交；测试从明确绑定的安装包提取并核对以上摘要，只执行选定 schema 和存储函数，以内存依赖替代磁盘和后端写入，不加载 Electron 入口。

## 复验命令

```sh
PYTHONPATH=src:tests python3 -m unittest test_app_audit_versions test_profile_apps test_profile_themes
CLAUDE_CONFIG_RUN_NATIVE_SMOKE=1 \
CLAUDE_CONFIG_CODEX_APPLICATION='/absolute/ChatGPT.app' \
CLAUDE_CONFIG_NATIVE_CODEX_APP='/absolute/ChatGPT.app/Contents/Resources/codex-cli/bin/codex' \
PYTHONPATH=src:tests python3 -m unittest \
  test_native_app_smoke.NativeAppSmoke.test_codex_261021_native_schemas_store_and_failed_write_recovery \
  test_native_app_smoke.NativeAppSmoke.test_codex_app_preferences_backend_readback
```

需要 Node.js 执行隔离源码测试。该证据不证明 GUI 已重载、账号权限或组织策略生效。实际配置选择继续以 Profile 为准，软件升级本身不应用偏好。

检查失败现在附带 `stage` 与可取得的 `version`：应用元数据、应用身份、偏好接口、语言 catalog 分别报告原因，不再把所有失败合并为同一句提示。
