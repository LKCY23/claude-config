# 既有插件的明确版本更新与恢复

更新：2026-10-17。明确版本更新与恢复已接入已安装 CLI；本机已完成新版迁移。下方 Karpathy 证据记录 10 月 10 日的隔离验收，不是当前待执行清单。

准确接管只适用于相同版本／正文。遇到已有安装与新包不同，使用单独的 `--replace-plugin REF=FILE` 一次输入，不能借普通合并或接管绕过差异。

```sh
claude-config plan --machine /absolute/path/to/machine.yaml \
  --agent codex --replace-plugin guidelines@team=/absolute/path/to/replacement.json \
  --format text
```

它仍是完整 Profile 计划，正式 apply／switch 带相同输入及 `--expect-plan-id`。同一资源不能同时接管和更新；已由新版管理的插件使用正常固定基线更新，不再次使用这个首次更新入口。

## 本机恢复声明

```json
{
  "schema_version": 1,
  "selector": "guidelines@existing-market",
  "version": "1.0.0",
  "image": "<准确旧包的64位SHA256>",
  "source": "/absolute/path/to/verified-old-source"
}
```

恢复 source 是准备好的原始来源包，不是原生安装缓存；必须为普通目录，禁止软链接及 Git 数据库。完整文件／执行位摘要、原生 name／version 与声明对应，本机旧安装的版本、文件和所有文件模式也必须匹配。source、声明及目标库存全部加入计划指纹和变更检查。新包版本必须不同，不允许相同版本替换不同正文。

旧包从已声明的上游准确提交重建。个别既有安装含源 submodule 的 `.git` 指针文件时，只在私有本机恢复包中保留该普通元数据文件；不复制 Git 数据库、不上传它，也不从原生缓存复制正文。先以实际安装器验证这个旧包可准确重建，再进入切换计划。

旧包按内容保存到本机准备来源目录，加入该插件 history；新安装仍用原生安装器执行。旧版本的 Claude-style manifest 可用于已核实的 Codex 历史恢复，新选择的正式包仍遵循严格的 `.codex-plugin` entry，不增加旧 Profile 字段兼容。

## 执行与回滚

保留准确原生 selector；同一 marketplace 的全部已安装插件及作用域按[接管规范](plugin-adoption.md)检查，不能改动未选安装的来源。升级后比对新版本、完整文件和启用状态，随后才写入管理基线。新旧来源及版本变化完整进入报告。

失败或独立回滚用固定旧来源通过原生安装器重建，再恢复本次注册、库存和使用状态。Claude 已处于目标启用状态时不重复执行 enable／disable，因为原生程序会把 already_in_goal_state 返回为失败。恢复冲突附 `recovery_error` 的安全原因与错误类型，保留后续改动；不打印原生 stderr 或配置值。

有未接管的其他历史镜像时首次更新阻断，不能偷偷清理它们。新包、历史恢复来源和回滚准备不等于插件业务、账号模型或 GUI 会话已经验证。

## 历史验收：2026-10-10 Karpathy

本机恢复来源证明（本机历史记录：`deployment/config-cutover-review-20261010/recovery-source-proof.json`）核对原提交 `2c606141936f1eeef17fa3043a72095b4765b9c2`、准确文件及本机旧安装。两端从 1.0.0 更新到候选包装版本 1.0.0+claudespace.migration.1；技能正文不变。

实际包的三端隔离更新／回滚（本机历史记录：`deployment/config-cutover-review-20261010/actual-package-isolation.json`）通过 Claude CLI、Codex CLI 和 Codex App 后端，准确恢复旧文件及元数据。六项通用原生验收另覆盖更新后恢复与最终步骤失败恢复；不执行模型或技能业务。完整待切换范围见 切换审阅清单（本机历史记录：`deployment/config-cutover-review-20261010/README.md`）。
