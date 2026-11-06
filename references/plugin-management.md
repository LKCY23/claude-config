# 统一插件管理入口（0.8.0 开发版）

`plugin` 将版本选择、Profile 使用选项和原生安装纳入同一份计划、同一个恢复日志。当前支持 Claude Code 用户级受管插件；使用同一配置目录的 Desktop Code 会受影响。没有更新 CLI、切换账号、运行模型、自动重启 App 或绕过原生信任的副作用。

## 命令

```sh
claude-config plugin list --agent claude-code
claude-config plugin status sanctuary-translate --agent claude-code
claude-config plugin install sanctuary-translate@claudespace-personal --agent claude-code
claude-config plugin upgrade sanctuary-translate@claudespace-personal --agent claude-code --version 0.6.0-dev.14 --enable
claude-config plugin enable sanctuary-translate --agent claude-code
claude-config plugin disable sanctuary-translate --agent claude-code
```

这些命令只在此开发版本中提供；原来的 0.7.0 日常安装没有自动升级。

| 操作 | 版本 | 启用状态 |
| --- | --- | --- |
| install | 新安装取 catalog 固定版本，或显式 `--version` / `--commit`；已选锁定版本优先 | 新安装默认启用；`--disabled` 安装但不启用 |
| upgrade | 只升级已受管安装；缺省取 catalog 当前明确固定的版本，不猜 latest 或分支 | 保留 Profile 明确声明的状态；未声明则保留现状。`--enable` 同时启用 |
| enable | 不下载、不升级、不隐式安装；安装必须与已选择的锁定版本一致 | Profile 和原生状态一起启用 |
| disable | 不下载、不卸载、不删除缓存或业务数据 | Profile 和原生状态一起禁用 |

已安装其他版本时 `install` 报告应使用 `upgrade`；启用未安装的插件会报告应先安装。禁止隐式降级及同一原生版本号复用不同内容。短名优先匹配 Profile 中的唯一插件；未选择的新插件从已准备的 catalog 查找，歧义必须写完整 `name@catalog`。新 catalog 缓存尚未准备时请写完整引用；工具只在声明的私有 sources 缓存位置自动准备，不覆盖自定义 checkout。

带必需运行绑定的新安装可以显式传入：

```sh
claude-config plugin install sanctuary-translate@claudespace-personal --agent claude-code \
  --binding node=executables.sanctuary-node
```

既有插件的绑定不由此入口改写。依赖必须已经明确选择在 Profile 中；工具准备它们的固定来源，沿用已有依赖扩展报告，不偷偷添加未知依赖。

## 一个交互命令，或两个脚本步骤

在交互终端，操作命令展示完整预览并询问 `[y/N]`，确认后执行同一份已审阅计划。确认后重新检查配置、来源、原生状态及指纹。`--format json` 或非交互调用默认只预览，不读取标准输入批准。

```sh
claude-config plugin upgrade sanctuary-translate@claudespace-personal --agent claude-code \
  --commit 5cf9bf124e8d10dbd525d0938151ca1e549e7ef9 --enable --dry-run --format json

claude-config plugin upgrade sanctuary-translate@claudespace-personal --agent claude-code \
  --commit 5cf9bf124e8d10dbd525d0938151ca1e549e7ef9 --enable \
  --write --expect-plan-id '<刚审阅的 plan_id>' --format json
```

`--write` 必须携带计划 ID，`--dry-run` 与 `--write` 互斥。预览可以下载到私有来源缓存并保存报告，但不会写 Profile、插件锁定文件、原生安装或启用状态。查询 `list/status` 连报告都不落盘。

`--version` 必须是精确 SemVer，解析该登记仓库中唯一已发布的对应 GitHub Release（版本号或 `v` 前缀 tag），解析 annotated tag 后锁定完整提交，并核对实际包内版本。不存在唯一发布记录时停止。`--commit` 必须是完整 40 位提交，只允许同一登记仓库及包路径，报告明确它不代表正式 Release。来源不完整、身份改变或脏 checkout 都不会被自动接纳。

## Profile 插件锁定文件

`profiles/agents/claude-code/official.yaml` 对应 `official.plugins.lock.yaml`。锁定文件保存 Profile 身份及按资源引用索引的 catalog 出处、源仓库/完整提交/包路径、manifest 版本、包内容指纹和解析方式。不保存本机绝对路径、凭据或运行配置。启用开关仍只保存在 Profile 的 `resources.plugins.<ref>.options.enabled`。

普通 `plan/apply` 自动使用这个锁定文件。没有锁定文件时兼容现有 catalog 行为；改变 catalog 不会覆盖已锁定插件的提交。catalog 仓库/目录、插件来源仓库/路径改变需独立迁移，不能借改 lock 绕过原有身份检查。

首次创建锁定版本时将 Profile 的 `minimum_tool_version` 至少设为 `0.8.0`，防止旧工具忽略锁定文件装回旧版。Profile YAML 语义无变化时保留原字节；发生声明变化时沿用现有导出器的 YAML 序列化方式。

## 执行、恢复与边界

复用 `DeploymentScope`、`ProfileManager`、`CatalogResolver`、`PluginOperation`、`FilePlan`。新入口先在内存形成候选 Profile/锁定记录，再生成单插件部署及依赖扩展计划。原生操作和管理基线在前，Profile/锁定记录在后；所有步骤共同锁定、校验、写日志，失败逆序补偿。它不是文件系统级原子事务。

启用/禁用利用已有管理记录验证本机安装，无需 Git 源码在线；原生文件或版本漂移仍阻断。无锁定文件且 catalog 已改变时，启用前须先用安装/升级核实版本。禁用不因 catalog 源码离线而受阻。

插件占用会在预检报告；工具不强杀、不自动退出用户会话。按提示退出相关会话后重新预览，不能沿用过期计划。未知来源、其他管理器安装及内容漂移沿用现有接管/冲突处理工作流，本命令不会自动取得所有权。

沿用已有恢复入口，不增加第二套恢复协议：

```sh
claude-config runs --run '<run-id>' --format json
claude-config rollback --agent claude-code --run '<run-id>'
claude-config rollback --agent claude-code --run '<run-id>' --write --expect-plan-id '<恢复计划ID>'
```

Profile、锁定文件或原生数据在执行后被人修改，回滚会阻断而不会覆盖后来的修改。进程异常退出后沿用 `runs` / `rollback` / `reconcile-run`，未完成事务阻止下一次写入。

报告保留既有格式，增加 `plugin_management` 或查询的 `plugins`：声明/锁定版本、安装版本、期望/实际启用状态、来源准备、部署范围及会话未验证。文件安装成功不等于已打开的会话加载了新插件。`list/status` 还列出已受管但不再选择的保留安装；不据缓存自动收编未知插件。

## 验证

`tests/test_plugin_management.py` 使用临时 Git 仓库、临时 Profile 和 `FakeInstaller`，不访问日常配置、不调用付费模型。覆盖安装、升级、锁定重放、启停、交互确认、版本来源、依赖扩展、过期计划、失败恢复及后续修改保护。既有原生插件执行器的测试继续覆盖真实副作用协议与中断恢复；日常安装不属于此轮验证范围。
