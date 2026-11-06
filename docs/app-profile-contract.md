# App Profile declaration API v1

候选接口 `capability: app-profile-declaration-v1`，工具源码基线 0.6.1。只管理当前 Machine 已选择的 Claude Desktop App Profile 的六个可移植声明，不读原生配置，不切换 Profile／供应商，也不执行 Desktop。2026-11-04 新增 desktopHome；调用方必须读取 schema，不能以相同 capability 名称推断字段数量。旧安装没有此 capability 时，调用方应显示接口不可用。

## 命令

```text
claude-config app-profile schema --app claude-desktop --format json
claude-config app-profile inspect --app claude-desktop --machine ABS --format json
claude-config app-profile edit --app claude-desktop --machine ABS --patch ABS --format json
claude-config app-profile edit --app claude-desktop --machine ABS --patch ABS --write --expect-plan-id HASH --format json
```

`schema` 不需要或读取 Machine；`inspect/edit` 必须显式绝对路径。edit 默认预览，只有 `--write --expect-plan-id` 同时出现才保存声明。无 `--profile`、配置切换或原生 apply 参数。

Patch 为严格 JSON：

```json
{"schema_version":1,"set":{"userThemeMode":"dark","autoModeEnabled":false},"unset":["locale"]}
```

`set/unset` 均可省略，但至少一项；禁止同字段同时出现、重复 unset、未知字段、额外键和 null 值。六个可用键来自框架 `app_fields`：userThemeMode、locale、chatTabEnabled、desktopHome、autoModeEnabled、builtinBrowserEnabled。desktopHome 仅接受 standard（统一 Claude）和 off（仅 Code；Code 仍受独立策略约束），不接受 simple/no-vm 旧选项或 Boolean。unset 仅移除 Profile 中的声明，不表达删原生值。未涉及的声明及其他合法 Profile 元数据语义不变；写回 YAML 可能重新排版并丢失注释。

## 稳定输出

四种命令均返回相同 envelope（JSON stdout，无原始错误／秘密）：

```json
{
  "schema_version":1,
  "capability":"app-profile-declaration-v1",
  "operation":"app-profile",
  "action":"inspect",
  "tool":{"name":"claude-config","version":"0.6.1"},
  "app":"claude-desktop",
  "status":"ok",
  "ready":true,
  "preview_only":true,
  "target":{
    "machine_id":"fixture-mini",
    "machine_file":"/private/tmp/fixture/machine.yaml",
    "profile":"profiles/apps/claude-desktop/work.yaml",
    "profile_file":"/private/tmp/fixture/data/profiles/apps/claude-desktop/work.yaml",
    "profile_selection_file":null,
    "source_kind":"embedded"
  },
  "revision":"64-lowercase-hex",
  "settings_schema":{
    "fields":{
      "userThemeMode":{"type":"string","enum":["system","light","dark"],"optional":true},
      "locale":{"type":"string","pattern":"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*","optional":true},
      "chatTabEnabled":{"type":"boolean","optional":true},
      "desktopHome":{"type":"string","enum":["standard","off"],"optional":true},
      "autoModeEnabled":{"type":"boolean","optional":true},
      "builtinBrowserEnabled":{"type":"boolean","optional":true}
    },
    "unset_behavior":"undeclare_only",
    "native_apply_separate":true
  },
  "declarations":{
    "userThemeMode":{"declared":true,"value":"light"},
    "locale":{"declared":false},
    "chatTabEnabled":{"declared":false},
    "desktopHome":{"declared":false},
    "autoModeEnabled":{"declared":false},
    "builtinBrowserEnabled":{"declared":false}
  },
  "proposed_declarations":null,
  "changes":[],
  "plan_id":null,
  "execution":{"started":false,"status":"not_started","native_write":false,"profile_saved":false},
  "recovery":{"available":false,"run_id":null},
  "warnings":[{"code":"native_apply_separate"}],
  "errors":[]
}
```

- Schema 响应中 `target/revision/declarations/proposed_declarations/plan_id` 为 null，仍返回完整 settings_schema。
- `inspect` 只报告所选 Profile 的六字段，不输出 description、Machine 全量内容、原生配置、绑定秘密、供应商或其他 App／Agent 的值。`revision` 是输入身份／内容摘要，不是原生配置版本。
- `edit` 预览返回 `proposed_declarations`，changes 每项为 `{field,action,before:{declared,value?},after:{declared,value?}}`；只列实际声明变化。`plan_id` 为 FilePlan 的 64 位小写十六进制摘要，`execution.status=preview`。
- 保存成功：`preview_only=false`、`execution.started=true`、`status=saved`（在 execution 内）、`profile_saved=true`，顶层 `declarations/revision` 更新为保存后的声明身份，proposed_declarations 同最终声明；recovery 带事务 run_id。无变化时 execution.status=unchanged、profile_saved=false、started=false，不制造备份或执行记录。
- 拒绝／失败：`ready=false`；顶层 status 为 blocked（输入／计划不可用）或 error（执行／I/O失败）；errors 只含固定 code，不能直接展示底层异常。保存失败不声称 profile_saved；如事务已启动，recovery 暴露原run_id供现有 runs／rollback 核对，不新增恢复引擎。
- UI 必须核对 capability/schema/operation/action/app、目标 Machine/Profile 和 plan_id；只有 ready 且精确同预览计划的 write 可以保存。保存声明后清除旧 native plan，由用户另行使用现有 plan/apply/launch。

固定错误码：invalid_arguments、invalid_patch、invalid_configuration、plan_stale、unfinished_run、file_access_failed、operation_failed、interrupted。退出码成功0；输入／stale拒绝1；其他失败2；中断130。预览与 inspect 不保存报告到配置或状态目录。write 仅用既有 FilePlan 事务、备份与恢复账本保存选定声明。

## 实现边界与验证

- 字段类型／枚举／locale pattern 从 `core.app_fields` 同一权威注册表导出，不维护第二套可写字段白名单。
- Machine 和仓库 `machines/<id>.yaml` 选择由 `MachineProfiles` 既有路径解析。读取选中声明前核对它仍在对应 App Profile 目录内，拒绝跨根链接或把 native data/application 作为声明目录。读取固定为首次核准的 canonical 路径，读取前后复查引用；不再通过二次 Profile 解析切换到其他目标。仍复用框架 snapshot_read、Profile validation 和 sources/references 绑定，不改公开 JSON 合同。
- FilePlan 绑定 Machine、选择文件、Profile 与 patch 的快照、实际路径和输出摘要；源码变动、换 Profile 或 patch 后旧 plan ID 无效。write 使用既有锁、原子写、写后校验、备份及失败恢复；runs／rollback继续适用。
- 通用 `build_report` 同时承载原生部署和更多内部元数据。本接口输出独立的窄投影，不把整个 Machine、其他 Profile 元数据或 FilePlan 原始 source/result 直接发布；事务执行机制仍完全复用。
- 本接口未安装到正式工具。schema/inspect/edit 的候选入口由工具路径／capability 发现，不能仅依赖版本号。UI 保存声明后必须丢弃旧原生计划，由用户重新预览并应用。

本轮本机只编辑源码，没有执行 Python CLI、测试或构建。2026-10-28 主任务在 Mac mini 完成验证：完整回归发现 715 项，其中 654 通过、60 跳过，唯一失败是新测试把 MachineProfiles 保留的 selection_path 错当为 canonical 路径。仅修正测试预期后，最终 12 项 App Profile 定向回归全部通过；其他 714 项无需因这个测试断言重复执行。API 的 Profile 路径仍 canonical，selection_path 保持既有语义，未改实现来迁就测试。此结果不表示真实日常配置已经保存或 Desktop 界面已经生效。

随后交叉审查补入首次核准路径与二次解析之间的实际竞态修复，新增实际 symlink 切换并断言未读目标正文的第 13 项测试。该最小修复候选等待主任务在 Mini 复验；先前的 12/12 不冒充新版已经运行。

复验命令（仅在 Mac mini 的明确候选目录）：

```sh
# 使用目标机已有测试解释器，PYTHONPATH 只指向这份候选源码。
PYTHONPATH=/absolute/candidate/src /absolute/test-python -m unittest discover \
  -s /absolute/candidate/tests -p test_app_profile_api.py -v
PYTHONPATH=/absolute/candidate/src /absolute/test-python -m unittest discover \
  -s /absolute/candidate/tests -v
```

新增回归覆盖 schema无需Machine、五字段投影、无native入口调用、仓库选择与不切换、preview/write同ID和准确before/after、未声明语义、noop保留字节、无效／重复patch、缺失确认、Profile／Machine／选择／patch变动、换目标、FilePlan执行前重检、越界／native目录拒绝、失败回滚以及既有rollback可恢复。所有样例是临时合成数据，不能将其通过当成真实日常配置已保存或Desktop界面已生效。
