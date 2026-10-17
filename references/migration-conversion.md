# 旧数据到新版候选：转换器实现

适用范围：尚未迁移机器的显式旧数据副本。本机已完成新版迁移；本文下方的 2026-10-10 盘点／候选是历史证据，不代表当前运行输入或待执行状态。
更新：2026 年 10 月 10 日。单独工具支持当前已声明输入的转换，不由新版 runtime manager 导入，不增加旧字段兼容。

## 命令与结果

```sh
claude-config migration convert \
  --data-dir /absolute/path/to/legacy-data-copy \
  --legacy-machine /absolute/path/to/installed/config.yaml \
  --app-profile codex-app=/absolute/path/to/exported/app-profile.yaml \
  --output-dir /absolute/path/to/new-candidate \
  --state-dir /absolute/path/to/private-reports \
  --format text
```

默认仅预览和保存统一报告；加 --write 才生成本地候选，--expect-plan-id 可绑定已审阅计划。来源、包成员、软链接、框架代码或输出选择变化会改变计划身份；写入前再次核对来源。输出必须不存在，拒绝覆盖或放入输入仓库／来源 catalog，失败的暂存目录自动清理。

候选包括 data/profiles/agents、data/profiles/apps、data/catalogs.yaml、machine.proposed.yaml 和 source 下的严格新登记及固定资源包。source 在独立本地 Git 中提交，包含显式快照的发布文件，即便旧开发 .gitignore 忽略它们；没有 fetch、push 或发布授权。该提交只供审阅和隔离解析，不是假定已在远端发布的基线。

## 转换规则

- 待转换旧数据中的 agents.yaml 配置来源优先；旧 base 的重复模型／推理值不覆盖当前声明。
- 只导出已选注册字段；旧 includeCoAuthoredBy=false 转为 attribution.commit／pr 的空文本，旧并发别名明确映射；不复制账号、供应商、本机状态或未选权限值。
- 保留原插件选择，通过明确来源快照和第三方溯源生成登记。缺少 Codex manifest 时生成不同包装版本并报告差异，原来源和现有安装不变。
- memory 按声明顺序成为一份指令资源。框架技能绑定工具入口，不重复安装；状态栏调用固定 HUD 包及本机程序，报告停止扫描最近缓存的语义变化。
- App Profile 来自旧 Desktop 声明或显式提供的导出，独立保存；排除边界仍由统一白名单验证。
- 外部程序资料、平台说明及认证边界在 review 下记录，不执行安装、收编资源或成为旧格式运行输入。没有明确转换语义的旧资源输入阻断，不能静默丢弃。

报告区分“转换成功”和“切换未就绪”。候选需要审阅／发布来源、确认原生安装身份和管理归属、完整备份及一次切换，deployment_ready 始终 false。不要把冷安装计划当成现有库存的采用授权；[明确插件接管](plugin-adoption.md)已支持保留当前原生名称、核对准确版本和正文后更换固定来源，其他插件保持原状。实际接管未执行，Karpathy 包装差异不能当作无更新接管；待明确版本更新及恢复来源。

## 历史候选与验证：2026-10-10

v2 候选入口（本机历史记录：`deployment/config-migration-candidate-20261010-v2/README.md`）包含四份独立 Profile；保留七个插件选择，登记四个插件包及一份指令包；Codex App 导出 23 个当前存在的可移植叶字段。第一份候选保留供诊断，HUD 发布文件被开发 gitignore 忽略的问题已修正。

四个目标在隔离配置根中通过来源、结构、原生解析或安装源码接口的计划检查。没有 apply、原生安装、模型、Hook 或服务业务；App 生命周期仅在不被 GUI 使用的隔离夹具中放行，实际数据根仍须正常退出 App 后写入。Claude 第三方功能只在已有 3p／appliedId 的隔离夹具条件中验证，不代表迁移部署模式或鉴权。

四端验证摘要（本机历史记录：`deployment/config-migration-candidate-20261010-v2/validation-summary.json`）、现有注册只读盘点（本机历史记录：`deployment/config-migration-candidate-20261010-v2/existing-registration-review.json`）。GUI 效果、HUD 输出、原生身份采用和完整切换仍需后续验收。

后续进展见切换审阅清单（本机历史记录：`deployment/config-cutover-review-20261010/README.md`）：Karpathy 已有准确旧来源及三端实际包更新／回滚证明，HUD 固定 dist 入口通过三组离线输入输出检查。实际 Desktop 配置不具备旧第三方功能开关的写入前提；审阅目录保留原转换 Profile，单独导出只含当前主题／语言的 `official-current.yaml` 并提出该本机绑定。原 v2 候选和本机运行配置未改动，不迁移供应商或账号。新版安装入口、基线发布及最终切换仍未执行。
