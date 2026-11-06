# S7：公开 App Profile 声明接口

2026-10-28，Asia/Singapore。新增接口在源码基线 0.6.1 上开发，尚未发布新发行版或升级 Mac mini 已安装的 0.6.0。能力通过 `app-profile-declaration-v1` 明确识别，不能仅凭旧版本号声称已支持。

## 实现

`claude-config app-profile schema|inspect|edit --app claude-desktop` 为 Bootstrap 提供五字段权威 schema、当前所选声明以及预览／保存。字段为 userThemeMode、locale、chatTabEnabled、autoModeEnabled、builtinBrowserEnabled。

inspect/edit 要求明确绝对 Machine，沿用 MachineProfiles 的显式选择。edit 接受严格 set/unset JSON，默认只预览；`--write --expect-plan-id` 才保存同一计划。unset 仅表示不再声明该字段，不删除原生偏好。命令不调用原生配置读取、应用检查或启动，不切 Profile／供应商，不输出完整 Machine 或秘密。

写入复用 FilePlan 的锁、来源指纹、事务日志、备份和已有 rollback。发生实际写入后报告异常时不宣称保存成功，保留原 run 供核对。路径核验固定首次批准的 canonical Profile，读取前后复核绑定；模拟两次解析间切换链接的测试确认错误正文未读取。通用框架没有因此新增第二套执行器。

完整接口见 [app-profile-contract.md](app-profile-contract.md)。YAML 有实质修改时可能重新排版并失去注释；无变化保留原字节且不制造执行记录。

## Mac mini 验证

- 最终全量 unittest：**716 项发现，656 通过、60 项原生显式 opt-in 测试跳过，0 失败**。没有打开 opt-in 去触碰日常应用。[结果](../verification/s7-20261028/claude-config-tests-20261028-s7-final.txt)
- 新接口 13 项定向检查全部通过，涵盖所选Profile、未声明与false、保存/恢复、原生数据未访问、过期计划、路径切换和写入失败。[结果](../verification/s7-20261028/claude-config-s7-focused-race-final.txt)
- 真实 Mac mini Machine 的 schema/inspect 只读通过，识别 `mac-mini-third-party` 与当前 Desktop Profile，五字段中三项已声明；没有保存或原生应用。[结果](../verification/s7-20261028/claude-config-profile-inspect-20261028-s7.json)
- Bootstrap 跨语言联调在合成数据上通过真实 CLI schema→inspect→edit preview→同plan保存→既有rollback预览/执行；Swift校验真实报告，3项声明恢复原字节，原生canary不变。见 [Bootstrap S7](../../claude-desktop-bootstrap/docs/implementation-20261028-s7.md)。

执行全部在 Mac mini 的 `/private/tmp/claude-config-s7-20261028`。MacBook只编辑源码。后续仍需正式发布／安装与真实设置修改验收；源码交付不等于用户日常工具已升级。
