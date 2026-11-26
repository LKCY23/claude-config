"""Installed command dispatch; help/version do not load profiles or dependencies."""
import difflib
import json
import re
import sys
from . import __version__

_PROFILE_COMMANDS = (
    'plan', 'validate', 'apply', 'export', 'switch', 'rollback',
    'adopt-baseline', 'review-conflicts', 'reconcile-run', 'retire-skills',
)
_ALL_COMMANDS = (
    'status', 'sync', 'resolve', 'baseline', 'report',
    'init', 'doctor', 'plugin', 'runs', 'app-profile',
    'fetch-baseline', 'cutover', 'migration',
    *_PROFILE_COMMANDS,
)

HELP = """usage: claude-config COMMAND [OPTIONS]

日常工作流（建议优先使用）：
  status     只读全局看板：检查基线缓存、各端同步状态与未解决冲突
  sync       一键同步：对齐基线并更新所有就绪的 Agent 与 App 配置
  resolve    交互式解决冲突（支持 --choose 快速非交互选择）
  baseline   基线管理（baseline status / sync / adopt）
  report     查看最近或指定报告（默认在终端以可展开树打开）
配置操作（明确选择 --agent 或 --app）：
  plan  validate  apply  export  switch  rollback
  adopt-baseline  review-conflicts  reconcile-run  retire-skills
本机设置：
  init       初始化/迁移 Machine 和明确指定的 Skill 入口，默认预览
  doctor     检查安装、本机绑定；--resources 盘点技能，--processes 观察 App 实例
专项操作：
  plugin     插件 install/upgrade/enable/disable/list/status；统一预览及部署
  runs       只读查看执行状态；--run ID 核对当前文件与恢复证据
  app-profile  查看 App 声明 schema/inspect，预览并受控保存五字段声明
  fetch-baseline  获取明确发布的固定来源，不采用或部署配置
  cutover    多客户端配置统一切换与恢复（不安装工具代码）
  migration  旧配置盘点与转换候选

常用示例：
  claude-config status
  claude-config sync
  claude-config resolve --agent codex
  claude-config baseline sync --write
  claude-config report last

--machine 默认 ~/.claude-config-tool/machine.yaml；不搜索当前目录。
--format auto|text|tui|json；默认输出简洁可读文本，交互树形查看请用 --format tui 或 claude-config report。
--verbose 显示完整保留决策与细粒度输出。
claude-config COMMAND --help 查看参数。
--version 显示版本。工具安装/升级/卸载由 uv tool 管理。
"""


def _wants_json(args):
    mode = 'auto'
    for index, item in enumerate(args):
        candidate = (
            item.partition('=')[2]
            if item.startswith('--format=')
            else args[index + 1]
            if item == '--format' and index + 1 < len(args)
            else None
        )
        if candidate in ('auto', 'json', 'text', 'tui'):
            mode = candidate
    return mode == 'json'


def _unknown_command(command, rest):
    safe_label = command if re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,31}', command) else '<command>'
    suggestions = difflib.get_close_matches(command.lower(), _ALL_COMMANDS, n=3, cutoff=0.6)
    hint = f"（你是否想输入：{'、'.join(suggestions)}）" if suggestions else ''
    reason = f'未知子命令：{safe_label}{hint}；请运行 claude-config --help 查看全部命令。'
    if _wants_json(rest):
        err = {
            'schema_version': 1,
            'operation': 'cli',
            'ready': False,
            'error': {'code': 'unknown_command', 'reason': reason, 'exit_code': 2},
        }
        print(json.dumps(err, ensure_ascii=False, indent=2))
    else:
        print(f'错误：{reason}', file=sys.stderr)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv in (['--help'], ['-h']):
        print(HELP)
        return 0
    if argv == ['--version']:
        print('claude-config ' + __version__)
        return 0
    command = argv[0]
    if command == 'plugin':
        from .plugin import main as run
        return run(argv[1:])
    if command == 'app-profile':
        from .app_profile import main as run
        return run(argv[1:])
    if command == 'runs':
        from .runs import main as run
        return run(argv[1:])
    if command == 'report':
        from .report_cmd import main as run
        return run(argv[1:])
    if command == 'status':
        from .status_cmd import main as run
        return with_progress(run, argv[1:])
    if command == 'sync':
        from .sync_cmd import main as run
        return with_progress(run, argv[1:])
    if command == 'baseline':
        from .baseline_cmd import main as run
        return with_progress(run, argv[1:])
    if command == 'resolve':
        from .resolve import main as run
        return run(argv[1:])
    if command in ('init', 'doctor'):
        from .setup import main as run
        return with_progress(run, argv)
    if command == 'cutover':
        from .cutover import main as run
        return run(argv[1:])
    if command == 'migration':
        from .migration import main as run
        return with_progress(run, argv[1:])
    if command == 'fetch-baseline':
        from .fetch_baseline import main as run
        return with_progress(run, argv[1:])
    if command in _PROFILE_COMMANDS:
        from .profile import main as run
        return run(argv)
    return _unknown_command(command, argv[1:])


def with_progress(run, argv):
    if '-h' in argv or '--help' in argv:
        return run(argv)
    from .core.execution_context import progress_output, progress
    with progress_output(sys.stderr):
        progress('检查输入并准备操作')
        return run(argv)
