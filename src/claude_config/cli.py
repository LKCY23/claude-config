"""Installed command dispatch; help/version do not load profiles or dependencies."""
import sys
from . import __version__

HELP = """usage: claude-config COMMAND [OPTIONS]

配置操作（明确选择 --agent 或 --app）：
  plan  validate  apply  export  switch  rollback
  adopt-baseline  review-conflicts
本机设置：
  init       初始化/迁移 Machine 和明确指定的 Skill 入口，默认预览
  doctor     检查安装、本机绑定及本地依赖，不运行资源业务
专项操作：
  fetch-baseline  获取明确发布的固定来源，不采用或部署配置
  cutover    多客户端配置统一切换与恢复（不安装工具代码）
  migration  旧配置盘点与转换候选

--machine 默认 ~/.claude-config-tool/machine.yaml；不搜索当前目录。
--format auto|text|json；交互终端默认可展开，管道默认静态文字。
claude-config COMMAND --help 查看参数。
--version 显示版本。工具安装/升级/卸载由 uv tool 管理。
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv in (['--help'], ['-h']):
        print(HELP)
        return 0
    if argv == ['--version']:
        print('claude-config ' + __version__)
        return 0
    command = argv[0]
    if command in ('init', 'doctor'):
        from .setup import main as run
        return run(argv)
    if command == 'cutover':
        from .cutover import main as run
        return run(argv[1:])
    if command == 'migration':
        from .migration import main as run
        return run(argv[1:])
    if command == 'fetch-baseline':
        from .fetch_baseline import main as run
        return run(argv[1:])
    from .profile import main as run
    return run(argv)
