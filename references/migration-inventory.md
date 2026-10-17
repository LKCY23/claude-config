# 旧配置迁移输入盘点

适用范围：尚未迁移机器的显式旧数据副本。本机已完成新版迁移；本文下方的 2026-10-10 盘点／候选是历史证据，不代表当前运行输入或待执行状态。
更新：2026-10-10。inventory 保持只读盘点，独立 convert 已接入当前声明的转换，生成本地未发布候选及报告。入口与格式见 [转换器实现](migration-conversion.md)。两个入口均不部署原生配置、不接管安装或写入现有数据仓库。

旧格式只在这一独立工具中读取。新版 ProfileManager 和 AppProfileManager 不导入此模块，不增加旧格式兼容。归档 plugins.yaml 和历史四客户端 v2 导出不作为权威输入。

```sh
claude-config migration inventory \
  --data-dir /absolute/path/to/legacy-data-copy \
  --state-dir /absolute/path/to/private-reports \
  --format text
```

支持 `--format auto|text|json`；输出与私有保存复用[返回协议](report-contract.md)。操作完成表示盘点完成，不表示允许部署，deployment_ready 保持 false。

## 盘点边界

- 核对旧格式版本、当前 Agent 类型、插件引用的 catalog 和重复选择。
- 读取配置来源，逐项记录字段名、类型与新版处理类别，不导出字段值。
- 待转换旧数据中的 agents.yaml 的配置来源优先；旧 base 中重复的模型或推理设置标记为不采用旧值，不能在转换时覆盖当前选择。
- 将普通设置、App 偏好、七类资源来源、框架入口、环境引用与独立程序安装资料区分。
- 原生命令要求转换成本机程序绑定；旧别名要求明确语义映射；排除字段保留本机原值。
- 文件缺失、解析失败或未分类声明阻断盘点；仓库外软链接只记录需明确来源，不跟随读取私有内容。
- 不读取原生账号或缓存，不运行安装器、Hook、技能、远端拉取或模型调用。

11 项测试覆盖当前声明、旧 base 优先级、历史文件不加载、未知项和缺失源、软链接边界、插件选择、标准 JSON 保存、人类输出、参数错误及字体排除。排除字段只记录类别，不复制值；本机原值保留。正式转换仍需核对来源 pin、原生身份、完整资源目录与符号链接，以及全部排除和语义变化。

## 历史盘点：2026-10-10

当前输入为 7 个插件选择、14 个 Agent 字段、3 个 App 字段、13 处本地来源。

实际报告：迁移输入盘点 JSON（本机历史记录：`deployment/migration-inventory-20261010/reports/7ebe9df840e3465aa3a967c4c7e9034b.json`）。原配置、原生状态和安装入口未改变。

includeCoAuthoredBy 需要映射为新的署名配置并解释差异；旧 permissions.allow 不在目前选定的 Claude 普通设置范围，不能因转换顺带导入；statusline 的 Mac／Windows 脚本与三个 memory 文件需要资源登记及消费者核对。框架技能还需辨明是 Bootstrap 入口还是普通资源，不能机械重复安装。

当时转换器复用来源清单生成独立文件和资源登记；该批候选覆盖四个 Profile、原有七个插件选择、全局指令及状态栏绑定。原生身份／归属、来源发布及统一切换继续单独确认，不建立新旧分批交接。候选入口（本机历史记录：`deployment/config-migration-candidate-20261010-v2/README.md`）
