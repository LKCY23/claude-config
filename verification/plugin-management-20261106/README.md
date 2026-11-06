# 统一插件管理入口验证 · 2026-11-06

基于 `origin/master` 的 0.7.0（571f437），工作分支 `feat/plugin-management`，开发版本 `0.8.0.dev0`。原源码目录的未提交 Desktop 修改没有复制、覆盖或丢弃。

- 完整隔离回归：780 项，721 通过，59 项按默认开关跳过，0 失败。
- 新增插件管理测试：49 项通过；开发 wheel 加载相同测试也全部通过。
- wheel 中所有 Python 文件已逐字节核对工作副本，摘要见 [result.json](result.json)。
- `git diff --check` 和 Python 编译检查通过。
- 初次沙箱回归中的两个 tmux 测试因本地 socket 限制失败；允许临时 tmux socket 后通过，未修改测试以跳过它们。
- 真实宿主安装冒烟开关保持关闭。没有日常 CLI、Profile、catalog 基线、插件或 App 的部署、启停或重启。日常 `claude-config --version` 仍为 0.7.0。

核心覆盖：新安装和声明选择、默认/显式启用状态、升级不安装缺失插件、精确提交及发布版本、catalog 冷缓存、锁定重放、旧 CLI 拒绝忽略锁定文件、继承原有依赖扩展、歧义/漂移/占用阻断、离线启停、交互确认、确认后过期、下载与原生安装及声明写入失败、统一恢复、后续手改保护、无副作用状态查询。

可重复运行（使用含项目依赖的 Python 3.11+）：

```sh
PYTHONPATH=src:tests python -m unittest test_plugin_management -q
CLAUDE_CONFIG_RUN_NATIVE_SMOKE=0 PYTHONPATH=src:tests python -m unittest discover -s tests -q
uv build --wheel
```

完整测试需要允许本地 tmux 临时 socket。未覆盖真实 GitHub 账号、真实插件仓库发布与日常原生安装端到端；版本解析用受控 API 响应、Git 使用真实临时仓库、原生安装副作用使用已有 FakeInstaller。正式安装或原生验收需另行授权。
