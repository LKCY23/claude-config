# 部署一致性、技能交接与显式退役（0.6.0）

## 所有权和观察范围

Claudespace 提供固定正文，Profile 选择资源和可移植选项，Machine 提供本机绑定。Claude 独立技能部署到绑定的 `config_root/skills`；Codex 使用明确绑定的 `.agents/skills`。两个客户端可以采用相同来源，但部署正文及调用控制分别维护。

0.6.0 在原有管理状态和计划报告上增加技能盘点，不新建另一套配置数据库。它区分 Profile 选择及期望启用值、上次部署的来源提交和位置、当前入口及原生启停、缺失／正文／权限差异、其他位置的同名入口，以及 CC Switch 对同一客户端的安装声明。省略启用值不被猜成 true。仅看文件不能证明原生发现或实际调用，盘点的 discovery 状态始终明确为 `not_verified`。

另一个客户端的正规安装不是重复入口。Codex 的 `config_root/skills/<name>` 与明确安装根同时存在时，属于需要显式审阅的旧入口；未选择的系统、插件及独有技能不因此被接管或删除。

## 诊断入口

```sh
claude-config doctor --resources --processes --format text
claude-config doctor --resources --format json --no-save
claude-config doctor --storage --format text
```

`--resources` 利用现有只读计划获取已选技能和已登记 residual 的元数据，不执行技能业务、不修改部署状态。固定来源或原生检查失败不会被“文件存在”掩盖。完整 JSON 包含 `resource_inventories`；普通 Agent plan 包含对应 `resource_inventory`。

默认 doctor 仍保存诊断报告；`--no-save` 不保存报告。`--processes` 仅在 macOS 读取绑定 App 的进程参数，输出 PID、绑定目录和显式 `--user-data-dir`，不收集进程环境或凭据。未声明数据目录、读取失败、另一个合法测试实例都不能被冒充为默认实例已验收，也不会自动关闭或重启 App。

工具身份包含执行中的代码版本、模块位置、Python 和安装来源。本地源码与已安装 distribution 可以不同，报告分别显示，不凭当前工作目录推断正在执行哪份代码。

## 外部管理者交接

标准用户配置根自动检查 `~/.cc-switch/cc-switch.db` 的 skills 表。自定义实例默认不套用全局 CC Switch 声明；如果该实例确实由它管理，Machine 可显式提供 `bindings.paths.cc-switch-data`。

只读取技能名称、目录和该客户端的启用列，不读取供应商配置、密钥或请求历史。已选受管技能同时被 CC Switch 声明启用时，plan 返回 `external_skill_manager_conflict`。已停用但仍被 Profile 选择的技能也需要单一写入者。已存在但不能读取的数据库或未知 skills schema 返回 `external_manager_unreadable`，不当作“无冲突”。

交接顺序：核对本地与固定来源 → 保存必要恢复证据 → 原管理者退出准确客户端／资源范围 → 审阅旧入口 → 重新 plan → 按新 plan ID apply → 原生发现验收。不是从目录库存自动补 Profile 选择。

预览、执行开始和已有事务保护边界会重新检查相关声明。其他应用的请求日志变化不会使计划失效。此机制提供协作式管理约束，不宣称能禁止其他同用户程序任意写文件。

## 四种不同动作

| 动作 | 结果 |
| --- | --- |
| `enabled: false` | 保留完整正文，关闭该客户端的使用 |
| 从 Profile 移除选择 | 停止主动更新；保留受管 residual 和原生状态 |
| 来源更新／恢复 | 使用现有固定来源、三方审阅和 `adopt_source` / `keep_local` / `update_source` |
| `retire-skills` | 删除准确、未修改且已不被选择的独立技能文件和对应原生控制 |

```sh
claude-config retire-skills --agent claude-code --resource example@team --format json
claude-config retire-skills --agent claude-code --resource example@team \
  --write --expect-plan-id '<审阅所得ID>'
```

退役需要先从 Profile 移除选择；不能绕过本地修改、未知文件、未交接外部管理、仍被使用的原生身份或其他资源共享文件。写入必须提供审阅后的 plan ID，继续使用公共事务、作用域锁和独立 rollback。只删除受管文件，可能留下空目录；不会清空客户端目录，也不会顺带应用其他待处理设置。

首版仅支持独立 Skills。不支持插件、带自动 `skills-dir` 插件控制的技能或未知来源；这些继续使用原生插件生命周期。回滚保留之后发生的外部修改。

## Profile 与工具兼容

Agent/App Profile 可选声明 `minimum_tool_version: '0.6.0'`。不满足时，在写入前报告所需与当前版本。该元数据自 0.6.0 起支持；更早工具不能读取含该字段的 Profile，须先升级工具。版本要求不表示客户端登录、原生业务或所有运行选项已经验收。

## 备份与保留

事务只备份本次将改变的文件／原生操作声明范围；技能退役不会把邻近的应用缓存、沙箱磁盘或整个 HOME 收进备份。数据库管理器之外的手工操作也应使用一致性数据库副本，不默认复制整个客户端数据目录。

`doctor --storage` 对管理状态、当前资源基线、恢复运行、报告及固定来源作逻辑体积汇总。它跳过链接，不读取正文，不自动删除。

- 当前 managed state 和其引用的资源基线是后续差异审阅与恢复的必要输入。
- 未完成运行及用户尚需回滚的证据保留到明确处理完成。
- 已完成报告和缓存可以按用户选择的期限、位置显式整理，不能按文件夹名称盲删。
- 固定来源与其他私有工作目录先检查实际引用和归属，再决定保留。

## 验证

`test_resource_inventory.py` 覆盖管理冲突、客户端范围、未知数据库 schema、预览后的外部变动、旧入口、缺失／权限／正文区别及无副作用诊断。
`test_skill_retirement.py` 覆盖已选资源拒绝退役、未知文件与手改阻断、范围内删除、原生控制保留和受保护回滚。

这些测试不能代替真实客户端发现验证；部署验收另记原生版本、实际读到的技能及启用状态，未执行业务的部分明确标注。
