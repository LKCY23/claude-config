# 2026-11-04 Desktop 兼容与统一入口

候选完成 4.4212.0 和 4.16336.0 的偏好接口审查，新增 desktopHome 的 standard/off 声明及旧 Chat 存储值的覆盖/未验证报告。保留未声明不写、旧版接口、第三方/本地 UUID 范围和严格实际写入检查。

[审计与验证](../references/claude-desktop-4x-unified-audit.md)记录确切源哈希、Mini 验证、期间日常安装版本变化及测试边界。[声明接口](app-profile-contract.md)现在返回六字段 schema；客户端应按 schema 识别，不凭 capability 名字或工具版本推断。Bootstrap 跳过配置仅启动的条件见[公开报告协议](../references/report-contract.md)，不是 claude-config 对受阻 apply 放行。

Mini 全套 733 项检查（59 跳过）、最终 85 项定向检查、两版本源函数证据与最新官方归档兼容流程均通过；真实 Machine 只读 plan 已无 blocker 且零配置变更。所有执行在 Mini。候选没有安装为日常 CLI，没有应用真实 Profile，也没有启动或替换日常 Desktop。
