# 开发与候选记录

本文归档阶段性开发候选说明与体验改进记录，保持 `README.md` 聚焦当前版本的使用、安装与维护说明。

## 0.9.0 UX 与日常工作流改进（Phase 1–4）

- **Phase 1（诊断与输出可读性）**：允许列表安全参数报错与拼写提示；细分 SSH 22 端口被代理断开等传输诊断与 `[repo]` 上下文；严格对齐检查同时在 `doctor` 下宽容展示差异明细；基于逻辑实体统计变更、`next_steps_lines` 保留作用域选择器；默认非劫持式文本输出 + `claude-config report [last|<id>|<path>]`。
- **Phase 2（冲突解决与短计划 ID）**：新增引导式 `claude-config resolve`（支持 `--choose POINTER=ACTION` 与导出后自动对齐基线）；`plan_stale` 对比历史报告精准归因；支持 8–64 位短前缀 `--plan-id`。
- **Phase 3（全局编排与基线一键同步）**：`fetch-baseline` 省略 `--agent` 自动探测；新增 `claude-config baseline status|sync|adopt`、只读全局看板 `claude-config status` 以及四阶段统一同步 `claude-config sync`。
- **Phase 4（帮助与文档清理）**：重构根级 `--help` 命令分组与常见示例、`--help` 静默无进度噪音、未知子命令拼写建议，并将候选开发日志从 `README.md` 迁移至此。

## 2026-11-04 候选：Claude Desktop 4.4212.0 / 4.16336.0 审计

> 2026-11-04 候选：新增 Claude Desktop 4.4212.0 / 4.16336.0 审计、`desktopHome` `standard`/`off` 与旧 Chat 覆盖语义；声明 API 现在为六字段。Mini 回归、源码/合成文件验证和真实只读 `plan` 已通过，尚未升级日常 CLI 或应用真实配置。见[实现记录](implementation-20261104-desktop.md)。

## 2026-10-28 S9：Claude Desktop 4.29.0 偏好接口审计

> 2026-10-28 S9：新增 Claude Desktop 4.29.0 偏好接口审计与包内语言目录适配；Mac mini macOS 27.2 上定向回归、真实源码合成偏好验证及真实 Machine 只读 `plan` 通过。未升级已安装工具或写入日常偏好。见[实现记录](implementation-20261028-s9.md)。

## 2026-10-28 S7：App Profile 声明 API

> 2026-10-28 S7：新增未发布的五字段 App Profile 声明 API，已在 Mac mini 完成回归与 Bootstrap 合成保存／恢复联调。见[实现记录](implementation-20261028-s7.md)。本机正式安装尚未升级。
