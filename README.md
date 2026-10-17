# claude-config

管理独立 Agent/App Profiles、固定资源及经过检查的本机配置部署。
Claudespace 托管资源正文；Profile 选择配置和资源运行参数；Machine 提供本机绑定。

## 安装

将下列 wheel 路径替换为实际构建版本，文件中的 VERSION 是占位符。支持 macOS 和 Linux；原生 Windows 执行器尚不支持。macOS App 适配不自动适用于 Linux。
先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后从明确版本的发布附件或本地构建取得 wheel、`constraints.txt` 和 `install.sh`：

```sh
bash install.sh "/absolute/path/to/claude_config-VERSION-py3-none-any.whl"
claude-config --version
```

安装器使用 uv 管理的 Python 3.11 和独立工具环境。命令目录未进入 PATH 时，运行 `uv tool update-shell`，然后在已有 tmux 窗格重新加载 shell。
本文对应 0.1.5 源码。0.1.5 纳入 Claude Desktop 2.42304.0 已核验的偏好接口；字段与写入规则不变。可从明确提交构建本地 wheel，或使用对应 CI／Release 的完整安装附件；源码推送和 CI 构建不等于已经发布 PyPI 或 GitHub Release。已安装版本以 `claude-config --version` 为准。

## 日常使用

```sh
claude-config plan --agent codex
claude-config validate --agent claude-code
claude-config plan --app codex-app
claude-config apply --agent codex --expect-plan-id "<reviewed-plan-id>"
claude-config plan --agent codex --format text
claude-config plan --agent codex --format json
```

默认读取 `~/.claude-config-tool/machine.yaml`；`--machine` 可以显式覆盖，不搜索当前工作目录。
数据默认 `~/claude-config-data`，报告和私有恢复日志默认 `<config_dir>/.state/`。
TTY/tmux 默认可展开；管道/重定向输出静态文字。所有配置操作保存完整 JSON。见 [返回协议](references/report-contract.md)。

## 初始化与迁移

```sh
claude-config init --config-dir ~/claude-config-data --format text
claude-config init --config-dir ~/claude-config-data --write --expect-plan-id "<preview-id>"
claude-config doctor --format text
```

空白初始化不猜测客户端、Profile、登录或供应商。可用 `--from-machine /path/to/reviewed-machine.yaml` 导入已准备的绑定。
`--skill codex=/absolute/.agents/skills/claude-config/SKILL.md` 为明确选择的已绑定 Agent 安装入口。
既有脚本安装通过 `init --retire-script-install` 核对旧清单后退役；全部外部 Skill 必须用 `--skill` 明确列出。
`init` 默认预览，`--write` 执行。`init --run <run-id>` 预览初始化恢复，使用同一 Machine、config-dir（如有）和 Skill 目标；加 `--write` 执行。

详见 [CLI 打包、分发与迁移](references/cli-packaging-and-distribution.md)、[命令参考](references/new-agent-profile.md)、[多客户端切换](references/framework-cutover.md)。

## 升级、卸载与恢复

安装新版 wheel：再次运行其配套 `install.sh`。恢复旧工具版本：安装之前的 wheel；不自动恢复 Profile 或原生配置。
`uv tool uninstall claude-config` 只卸载程序环境及命令，保留 Machine、数据、报告和已部署配置。
不要在配置执行过程中升级工具。仅升级工具不要求退出 Claude Desktop 或 Codex App；原生配置写入仍按现有生命周期检查执行。

## 开发

```sh
uv venv --python 3.11
uv pip install -e . -c constraints.txt
uv build
PYTHONPATH=src python -m unittest discover -s tests
```

原生安装冒烟测试须显式启用，使用隔离目录；正常测试不会操作真实账号和配置。
包代码位于 `src/claude_config/`。开发源码与已安装 wheel 相互独立。

## Claude Desktop Bootstrap 接口

0.1.4 为外部启动器提供明确的 App 路径、数据目录和只读部署模式信息，详见[返回协议](references/report-contract.md#app-启动器使用的目标信息014)。
Bootstrap 的代码与 App 构建由 claudespace 维护；它调用本工具的 plan → apply，并在成功后启动 Desktop。
设置合并、检查、冲突、备份与恢复继续由本工具负责，不新增第二套配置引擎。
升级本工具本身不会替换 Bootstrap、切换供应商或把本机改为第三方部署。
