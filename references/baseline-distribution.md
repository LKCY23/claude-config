# 正式基线的获取、采用与恢复

适用版本：0.2.0。资源发布、配置选择和原生部署是三步操作，每步单独返回报告。日常 `plan/apply` 保持离线，不检查最新版本，也不自动替换固定提交。

## 获取正式发布

安装 Git 和已登录的 GitHub CLI；当前账号须能读取 catalog 仓库和 Profile 已选资源的私有仓库。使用明确的完整提交，不接受 `latest`、分支名或短 SHA。

```sh
claude-config fetch-baseline --agent codex --catalog claudespace-candidates \
  --release baseline-<完整40位SHA>
```

命令从 `catalogs.yaml` 指定仓库读取已发布 GitHub Release，核对 tag 的准确提交、`<catalog别名>.baseline.json` 和 Git 中的登记正文。仅获取此 catalog 被各 Agent Profile 明确选择的资源及已声明依赖；候选目录中的其他资源不会自动安装。跨 catalog 依赖须已选择并准备好其来源。

目录默认在 `<config_dir>/.state/`：

- `sources/OWNER--REPO/SHA/`：干净、固定提交的来源，复用前核对仓库身份及 Git 状态。
- `releases/OWNER--REPO/baseline-SHA/ALIAS.baseline.json`：对应的正式发布记录。
- `reports/ID.json`：本次结果，包括已准备目录、受影响的 Profile 和下一步采用命令。

下载在临时目录完成后才移动到固定位置。失败保留此前完成的来源，并在报告列出；不会采用半成品。现有脏目录、符号链接目的地或不同内容的同名记录会停止操作，不覆盖。凭据沿用 `gh`，不会写入 Profile、Machine 或报告。普通网络设置沿用 Git/gh，不修改系统代理。

## 采用与部署

获取完成后执行报告提供的 `adopt-baseline` 命令。该命令默认预览，检查所有引用此 catalog 的 Profile，包括未绑定到当前机器的 Profile。

```sh
claude-config adopt-baseline --agent codex --catalog claudespace-candidates \
  --commit <完整40位SHA> --checkout /absolute/prepared/source \
  --release-record /absolute/claudespace-candidates.baseline.json
```

审阅后对同一命令增加 `--write --expect-plan-id <预览ID>`。采用只更新 `catalogs.yaml` 和 Machine 中的来源目录，保留资源选择、启用状态和运行参数。随后分别对受影响 Agent 执行 `plan`，审阅正文、安装和原生配置差异，再使用 `apply --expect-plan-id <部署预览ID>`。

`--release-record` 仍支持本地生成记录，便于离线交付；单独采用本地记录不证明它已远端发布。`fetch-baseline` 才包含已发布 Release 和 tag 的核对。结构验证不代表资源运行、账号或原生信任通过。

## 从私有历史目录衔接到正式来源

同仓库、同路径更新只需指定新提交。更换仓库或 catalog 路径必须提供独立、可审阅的精确映射：

```yaml
schema_version: 1
catalogs:
  claudespace-candidates:
    from:
      repo: owner/private-data
      commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      path: catalogs/claudespace-candidates
    to:
      repo: owner/claudespace
      commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      path: domains/candidates
```

获取及采用都增加 `--source-map /absolute/mapping.yaml`。当前声明须等于 `from` 或已完成的 `to`；命令提交须等于 `to.commit`。不存在“允许任意换仓库”开关。报告逐项列出仓库、路径、提交和本机目录变化。映射进入私有配置数据仓库，历史资料不因此公开。

无法公开的历史资源继续固定在私有仓库，通过同样的私有 Release 记录获取。历史快照仅声明已有结构验证，不伪装成支持自动上游更新。保留历史远端 refs、旧来源和恢复日志；发布新的公开基线不需要公开私有历史仓库。

## 恢复

每次采用和部署有独立恢复日志。若已部署新资源，先预览并执行对应部署 `rollback`，再恢复采用操作；如果只采用而未部署，仅恢复采用即可。使用同一 Machine、Agent 和报告中的运行 ID：

```sh
claude-config rollback --agent codex --run <运行ID>
claude-config rollback --agent codex --run <运行ID> --write --expect-plan-id <恢复预览ID>
```

恢复不会回写 GitHub 历史，也不删除准备好的来源。发现操作后的人工作业会报告恢复冲突，保留这些改动。
