# Codex 具名权限规则实现

更新：2026-10-10。新版源码已支持已选范围内的具名权限规则；未变更本机实际权限或网络设置。原生接口以 [OpenAI Docs 权限说明](https://learn.chatgpt.com/docs/permissions)和[配置 Schema](https://learn.chatgpt.com/docs/config-schema.json)核实，并在 Codex CLI 0.160.0、App 后端 0.162.0-alpha.2 的隔离配置中验证。

## Profile 中的可选字段

保留原生字段路径，全部可选，不新增默认值：

| 字段 | 类型和范围 |
| --- | --- |
| default_permissions | 内置策略或具名策略引用 |
| permissions.<name>.description | string |
| permissions.<name>.extends | 具名父策略或 :read-only／:workspace；不能继承 :danger-full-access |
| permissions.<name>.network.enabled | boolean |
| permissions.<name>.network.enable_socks5 | boolean |
| permissions.<name>.network.enable_socks5_udp | boolean |
| permissions.<name>.network.allow_upstream_proxy | boolean |
| permissions.<name>.network.allow_local_binding | boolean |
| permissions.<name>.network.dangerously_allow_non_loopback_proxy | boolean |
| permissions.<name>.network.dangerously_allow_all_unix_sockets | boolean |
| permissions.<name>.network.domains.<pattern> | allow／deny；校验主机、IP 和原生通配格式，拒绝 URL、端口和程序路径 |
| features.network_proxy | boolean；作为域名规则是否启用代理的明确依赖，不自动开启 |

```yaml
# 格式示例，不是本机实际选择
schema_version: 1
agent_type: codex
settings:
  default_permissions: review
  permissions:
    review:
      extends: ':read-only'
      network:
        enabled: false
```

具名策略标识采用严格名称格式，导出和字段策略使用准确 JSON Pointer。例如 `/settings/permissions/review/network/domains/api.example.com` 是一个原生字典键，点号不拆成多层路径。整个 permissions 对象不能作为无边界导出项。

## 本机依赖与合并

字段逐叶复用公共合并；缺省保留，不清空其他策略。允许 Profile 使用明确存在的本机策略，必要的父策略由最终原生结果解析；缺失引用和继承循环阻断。明确 keep_target 时检查最终保留的策略，而不是把未采用的来源选择当成已执行。

文件系统规则、workspace_roots、unix_sockets、proxy_url、socks_url 和证书路径不在可同步白名单内。原生已有的这些内容在同文件合并中保留，不通过依赖解析导入 Profile。

隔离原生检查只复制所需策略的已注册可移植字段和父引用，不复制本机路径、代理地址、socket 或认证。原生 config/read 比对字段，features list 验证策略能够解析；这些检查不执行命令，也不声称验证了真实沙箱或网络连接。

原生 default_permissions 与旧 sandbox_mode／sandbox_workspace_write 的冲突在最终结果检查中阻断，不偷偷删除旧字段。只登记策略不意味着选择它作为默认；工具也不会自动开启 network_proxy。

配置了域名规则但检查的用户配置未启用代理时，报告 warning，说明不能据此认定域名限制已生效。组织管理要求和真实运行效果单独标为未验证，不将配置解析通过说成策略已经执行。

## 验收

11 项普通测试覆盖白名单、类型、域名规则、源外本机字段保留、父依赖和循环、内置策略、字段策略、导出及恢复。2 项原生测试分别通过 CLI 和 App 后端的隔离读取、应用与重复检查。所有目标均为临时目录。
