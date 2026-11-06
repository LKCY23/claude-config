# S9：Claude Desktop 4.29.0 偏好接口适配

2026-10-28，Asia/Singapore。源码在 MacBook 编辑，全部验证在 Mac mini（macOS 27.2）。本轮未发布 wheel、升级已安装 CLI、应用日常偏好或启动 Claude。

## 改动

- 登记经过逐项源码审计的 4.29.0 偏好模块，保留 3.6556.2 和更早接口。版本字符串不能绕过未知源码拒绝；兼容由 bundle 身份和模块完整摘要确认。
- 五字段类型和存储方式保持不变。新版本的语言目录改为 `app.asar/resources/i18n/`；新分支读取已审计的 packed JSON，旧接口继续检查原资源目录。
- `zh-CN` 不在新版语言文件中，不能因存在 `zh-Hans` 而静默改写。未提供、无效、unpacked 或 link 类型的目标语言条目继续阻断。
- 所选语言内容包含在完整 ASAR 的计划输入摘要内；预览后源包改变仍会使计划失效。

源码证据与范围见[4.29.0 审计](../references/claude-desktop-4.29.0-audit.md)。没有复制厂商源码正文到仓库，没有扩大可管理字段、改变供应商或削弱生命周期／事务检查。

## 实际验证

| 层次 | 结果 | 证据 |
| --- | --- | --- |
| 定向回归 | 53 项通过：新目录、旧接口、未知源码拒绝、偏好规划／恢复 | [输出](../verification/s9-20261028/config-429-focused-final.txt) |
| 真实安装包＋合成数据 | 2 项通过：从固定摘要源码提取小例程，在内存依赖和新临时 native 文件上验证主题、语言、3P 开关、appliedId／hybrid 优先关系；不启动 Electron，不调用密钥封装 | [输出](../verification/s9-20261028/config-429-source-smoke.txt) |
| 真实所选 Machine 只读计划 | `ready=true`、`changes=0`、`execution_started=false`；installed/reviewed version 均为 4.29.0，无 blocker | [脱敏摘要](../verification/s9-20261028/config-429-real-plan-summary.json) |

第一次定向检查时，候选摘要清单恰逢最后一处 ASAR link/unpacked 拒绝加固更新，完整快照校验停止；不完整同步的测试缺少新模块。保留[失败记录](../verification/s9-20261028/config-429-focused-tests.txt)，重新核对并完整同步后 53 项通过，没有为迁就测试放宽合同。

真实 Machine 的完整计划报告只保留在 Mini 私有临时目录，仓库仅归档脱敏结果。以上证明配置接口与所选当前状态可识别，不代替后续用户确认写入、GUI 效果、账号请求或 Reach 同会话桥接验收。Bootstrap 的恢复页使用独立公开 rollback 合同；它的证据另见 Bootstrap S9。
