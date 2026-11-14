# PR 验证与受限自动合并

Desktop 监测创建报告 PR 后，自动从默认分支触发 `pr-validation.yml`，绑定 PR 编号和完整 head SHA。相同 head 已有验证状态时不重复下载和验证未知接口；手动重试仍可显式 dispatch。人工提交的正常 Package 工作流结束也会触发这条可信验证链。

测试作业只读、无 secrets、无持久 checkout 凭据，并关闭共享 uv 缓存。包测试复用 Linux/macOS × Python 3.11/3.12 四组合；Desktop 验证重新下载报告固定的官方归档，校验版本、校验和、签名和已知接口后运行隔离 smoke。状态与合并操作在独立、仅执行可信主分支脚本的作业完成，只给准确的当前 PR head 写入结果。

自动合并白名单只包含机器人创建的非草稿、同仓库、单个兼容性报告 JSON 更新：文件路径和归档版本一致，报告 compatible、所有检查 passed、native_smoke passed，并且本轮包矩阵和 Desktop 验证全部通过。源代码、适配器、工作流、多个报告、重命名、人工 PR、草稿及未知接口均需审查。取消／跳过必要检查不视为成功。自动化曾开启的 auto-merge 在不再满足范围时撤销，人工自行设置的 auto-merge 不被这条规则冒充或接管。

master 必须保持最新基线，要求 GitHub Actions 提供 `claude-config/package` 和 `claude-config/desktop` 两个检查；单维护者可人工合并复杂 PR，不要求另一个账号审批。自动合并使用 GitHub 受保护的 squash/auto 接口，不使用 admin 绕过，不触发发布或本机升级。仓库 auto-merge 开关关闭时只报告结果，不请求合并。

已验证、白名单内的报告若落后于 master，使用 expected_head_sha 保护的 update-branch 请求同步主线，不直接合并。新 head 必须重新验证；原生事件或下一轮监测负责重新 dispatch。监测会重新检查已成功但基线落后的报告，而未知接口的失败结果不会反复重试。
