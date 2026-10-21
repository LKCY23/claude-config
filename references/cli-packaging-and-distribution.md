# CLI 打包、安装与迁移规范

适用版本：0.3.0。源码提交、CI 构建、Release 附件和本机安装分别记录，不将其中一步当作其他步骤已完成。安装按下列明确版本的 wheel 流程进行。

## 包与命令

标准 setuptools/Python wheel；pyproject.toml 定义 claude-config console entry，Python >=3.11。
源码 src/claude_config，运行模板以 importlib.resources 读取。包不包含 Machine、Profiles、账号、聊天、缓存和状态。
PyYAML/tomlkit 在 pyproject 声明兼容范围；发布配套 constraints.txt 固定本次验证版本。uv tool install 必须传该约束；开发环境锁文件不会自动约束工具安装。
发布的 wheel+约束+安装脚本构成安装附件；每个版本的附件内容固定，不覆盖已发布版本。
CLI --version 与 JSON tool.version 标明工具版本；包构建来源由构建产物及 GitHub 构建记录追溯。
--help/--version 不加载业务配置，也不保存报告。

## 运行环境与路径

推荐 uv tool install --managed-python --python 3.11 -c constraints.txt /absolute/package.whl。
解释器与依赖由 uv 管理，不绑定 Conda；日常直接运行 claude-config，不联网更新。
install.sh 仅包装上述安装和 PATH 检查。uv 未安装时显示官方安装地址并退出，避免再维护解释器/包管理器自举器。
命令目录由 uv tool dir --bin 查询；不假定所有平台都等于 ~/.local/bin。

Machine 查找：显式 --machine > ~/.claude-config-tool/machine.yaml。无工作目录搜索、无自动客户端推测。
Machine.config_dir 定位数据，缺省初始化为 ~/claude-config-data。
状态由 runtime.state_dir 显式覆盖，否则 <config_dir>/.state。保持现有本机状态和日志原位。
CLI 相对路径按调用目录解释；Profile 按数据根目录；包内部按资源接口读取。

## init 与 doctor

init 默认预览；--write 写入，--expect-plan-id 可锁定审阅计划。
--from-machine 导入明确来源，已有不同目标不覆盖。无来源时生成不含客户端选择的空白 Machine。
--skill AGENT=/absolute/SKILL.md 只为已绑定 Agent 写入口；两客户端不得写同一文件，Machine/Skill 不得重叠。
Skill 包含绝对命令入口和明确 Machine 路径，不含 Python/临时 release 路径。通过绝对 CLI 路径运行时优先记录实际调用入口，即使 PATH 不包含该目录；源码模块调用才回退查找 PATH 中已安装命令。
init --retire-script-install 只退役已核对的旧安装清单文件；未知文件保留，已修改的受管文件阻断。
Machine.runtime 中 python/framework_source/framework_commit 仅由 init 迁移移除；正常运行 schema 不接受这些字段。
init 的写入/删除使用现有 FilePlan、锁、私有备份和恢复协议。失败记录执行状态，后续修改不会被恢复覆盖。
init --run ID 使用相同 Machine 与 Skill 目标恢复。历史配置日志保留；旧组合框架切换使用当时保留的发布包恢复，不将旧 spec 当作新格式解释。

doctor 检查工具解释器、Machine、数据目录、Profile 文件、绑定的程序/App、catalog 目录和 executable 依赖存在性。
不运行 MCP/Hook/插件业务，不测试账号、网络或 UI，不授予信任；这些检查不能证明业务可用。
尚未绑定客户端显示警告。缺失依赖使 doctor 不通过。输出遵守报告协议。

## 工具与配置分工

uv 管软件安装/升级/卸载；claude-config 管配置部署/资源运行绑定/恢复。
cutover 使用 v2 spec，组合原生文件和数据/Machine 写入，不再准备 Git 脚本包、复制程序或管理 Skill 安装。
init 管 Skill 命令入口。旧脚本拷贝安装器从运行包移除。
工具升级不会应用 Profile/更新资源/迁移数据格式；格式不兼容明确失败。
降级仅重装旧软件；配置恢复另用 rollback。卸载保留本机数据。
更新前结束正在执行的 claude-config 操作，不承诺包安装本身是多文件原子事务。
不设置后台更新器、每次启动全目录校验或长期兼容旧 schema。

## 发布与验证

本地 uv build 或标准 Python 构建产出 wheel。CI 在 macOS/Linux、Python 3.11/3.12 测试包与模块并保存附件；CI 文件提交后才会执行，不能据此宣称远端测试已通过。
发行渠道为明确版本的 GitHub Release wheel+constraints.txt+install.sh；维护者显式推送与包版本一致的 `vVERSION` tag 后触发。四组测试全部通过后，独立写权限作业发布 Linux/Python 3.11 构建的通用 wheel；其他构建作为 CI 证据保留。构建后端固定版本，时间戳使用 Git 提交时间，允许同一提交重新构建后恢复未完成的发布。
发布器先创建草稿、上传并核对全部附件，再公开 Release；重跑复用相同内容，遇到不同附件、额外附件或已发布缺件会失败，不覆盖已发布版本。资源基线使用同一发布协议，见[基线分发](baseline-distribution.md)。
每个版本验证：任意 cwd/中文路径、退出 Conda、源目录无关、默认/显式 Machine、tmux/静态/JSON、安装重装卸载、init 预览执行恢复、旧清单篡改与文件保留、原生配置不变，以及实际 plan 耗时。
不宣称原生 Windows 支持；Linux 不自动支持 macOS App 适配。实际尚未运行的平台列为未验证。
