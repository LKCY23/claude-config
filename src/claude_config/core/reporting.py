"""One public report for JSON persistence, terminal presentation and exit status.

This layer never replans or executes a configuration operation. The underlying
planner remains the authority for decisions, fingerprints and recovery evidence.
"""
from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .merge import redact, same
from .transaction import atomic_write, ignore_local_state


LABELS = {'claude-code': 'Claude Code', 'codex': 'Codex',
          'codex-app': 'Codex App', 'claude-desktop': 'Claude Desktop',
          'model': '默认模型', 'effortLevel': '推理强度',
          'model_reasoning_effort': '推理强度', 'language': '输出语言',
          'merge': '配置合并', 'file_preflight': '文件与快照检查',
          'runtime_version': '程序版本', 'fixed_resource_source': '固定资源来源',
          'effective_session': '会话效果', 'resource_runtime': '资源实际运行',
          'gui_effect': '应用界面生效', 'app_lifecycle': '应用运行状态',
          'capability_unknown': '当前接口能力未确认', 'plan_stale': '审阅后的计划已变化'}
LABELS.update(settings='配置', resources='资源', plugins='插件', skills='技能',
              agents='子代理', rules='规则', instructions='全局指令', mcp_servers='MCP', hooks='Hooks')
ACTION_LABELS = {'add': '新增', 'overwrite': '覆盖', 'replace': '整体替换',
                 'object_merge': '按子项合并', 'array_replace': '整体替换列表', 'replace_array': '整体替换列表',
                 'append_unique': '追加并去重', 'preserve': '保留本机值',
                 'keep_target': '保留本机值', 'skip': '本次跳过',
                 'delete': '删除', 'unchanged': '无变化', 'conflict': '尚未解决',
                 'residual': '保留旧 Profile 留下的内容'}
ACTION_LABELS.update(adopt_existing='接管现有插件', native_install='安装插件', native_update='更新插件')
ACTION_LABELS['replace_existing'] = '更新既有插件版本'
ACTION_LABELS.update(coalesce_snapshot='合并相同的恢复副本', combine_client_fields='合成客户端配置写入', framework_install='安装新版框架',
                     retire_legacy_code='退役旧代码', framework_install_receipt='记录安装文件')
POLICY_LABELS = {'source_wins': '采用同步配置', 'keep_target': '保留本机值',
                 'fail_if_different': '存在差异时阻断', 'replace': '整体替换',
                 'recursive': '逐项合并', 'absence': '未声明的内容保留',
                 'type_check': '类型检查', 'known-resource-replacement': '按资源标识更新'}
POLICY_LABELS['explicit-migration-adapter'] = '按明确的转换规则生成候选'
ORIGIN_LABELS = {'program_default': '程序默认策略', 'run_field': '本次命令的字段策略',
                 'profile_field': 'Profile 的字段策略', 'run_default': '本次命令的默认策略',
                 'profile_default': 'Profile 的默认策略', 'machine_default': 'Machine 的默认策略',
                 'resource_adapter': '资源处理规则', 'structural': '结构处理规则', 'preflight': '检查规则', 'profile_switch': 'Profile 切换规则'}
ORIGIN_LABELS['migration_converter'] = '旧数据转换器'
LABELS.update(profiles='Profile', exclusions='排除项', attribution='Git 署名')
LABELS.update(clients='客户端', framework='框架文件', shared_files='共享恢复副本', data='配置数据',
              app_preference_interface='应用配置接口', native_installation='插件安装', options='运行选项', enabled='启用状态')
LABELS.update(catalogs='资源目录', catalog_roots='本机来源目录', repo='来源仓库', commit='固定提交',
              path='仓库内路径', release_record='发布记录', released_baseline='发布基线检查',
              catalog_source_map='来源迁移映射', affected_sources='受影响资源来源')
POLICY_LABELS.update({'exact-framework-release': '按已核对的框架版本更新',
                      'identical-immutable-snapshot': '合并相同的恢复副本', 'exact-profile-data': '使用已校验的 Profile',
                      'one-time-format-migration': '迁移到新版格式'})
ORIGIN_LABELS.update(bootstrap_adapter='框架安装规则', cutover_adapter='迁移规则')
REQUIRED_RESULT_STATUSES = frozenset(('recovery_conflict', 'started', 'failed'))
OWNER_LABELS = {'framework-install': '框架文件', 'resource-baseline': '资源恢复副本',
                'managed-baseline': 'Agent 同步记录', 'app-managed-baseline': 'App 同步记录',
                'profile-data-install': 'Profile 与来源目录', 'retire-legacy-declaration': '旧配置声明',
                'native-plugin-source': '插件来源清单', 'agent-settings': 'Agent 配置',
                'app-preferences': 'App 偏好', 'native-plugins': '原生插件操作'}
STATUS_LABELS = {'passed': '通过', 'blocked': '不通过', 'failed': '失败',
                 'not_verified': '未验证', 'skipped': '未检查', 'warning': '警告',
                 'applied': '已修改', 'unchanged': '无变化', 'planned': '待执行',
                 'rolled_back': '已恢复', 'recovery_conflict': '未恢复'}
STATUS_LABELS.update(supported='字段类型可转换', needs_binding='需要程序绑定', needs_semantic_conversion='需要明确字段映射',
                     needs_resource_conversion='需要转换资源声明', excluded='不纳入同步', invalid_type_or_value='类型或值不符合新版',
                     shadowed_legacy_setting='采用当前声明，不使用旧值', present='文件已确认', needs_resource_registration='需要登记资源来源',
                     needs_explicit_source_binding='需要明确来源', external_symlink_needs_binding='仓库外软链接需要明确来源',
                     missing_or_invalid='来源文件缺失或无法读取')
ROUTE_LABELS = {'data_repository_metadata': '保留为数据仓库资料', 'platform_selection': '用于确认平台范围',
                'resource_or_framework_entry': '区分普通技能与框架入口', 'resource_source': '登记到资源目录',
                'app_profile_source': '转换为 App Profile', 'retained_legacy_settings': '核对旧设置与当前声明',
                'retained_legacy_permissions': '按已选权限范围转换', 'resource_and_command_binding': '登记脚本并转换程序绑定',
                'instruction_resources': '整理为指令资源', 'portable_settings_and_local_references': '区分可同步设置和本机环境引用',
                'local_exclusions': '保留排除边界', 'runtime_setup_outside_agent_profiles': '保留运行程序安装资料'}
REASONS_INVENTORY = {
    'Registered portable field type; target runtime capability still needs validation': '字段类型可转换，目标版本和依赖仍需验证',
    'Native command must become a resource reference and a Machine executable binding': '原生命令需要转换成脚本资源引用和本机程序绑定',
    'Native field value does not satisfy the new registered type or enum': '当前声明的类型或取值不符合新版字段规则',
    'Old alias needs an explicit new-field mapping and difference report': '旧字段需要明确映射到新版，并记录语义差异',
    'Resource declaration must become catalog references and usage options': '资源声明需要转换成目录引用和使用选项',
    'Not in the selected portable Agent field scope; preserve existing native value': '不在已选同步范围内，保留本机原值',
    'Not in the selected App preference scope': '不在已选应用配置范围内',
    'Registered App preference; deployment scope and App source audit still need validation': '字段类型可转换，应用部署模式及版本仍需验证',
    'App preference does not satisfy the new registered schema': '应用配置的类型或取值不符合新版规则',
    'Active agents.yaml config source is authoritative; legacy base must not overwrite it during conversion': '以当前 agents.yaml 为准，旧 base 设置不得覆盖当前选择',
}
REASONS = {'Source missing or unselected': '同步配置未声明此项，保留本机内容',
           'Declared leaves merged; other children preserved': '更新已声明的子项，保留其他子项',
           'Object/scalar mismatch': '对象与单值类型不一致',
           'External managed-field drift': '本机字段在上次同步后被修改',
           'Selected value differs': '本机值与同步配置不同',
           'Selected field differs; source wins': '本机值与同步配置不同，按策略采用同步配置',
           'Value added': '本机尚无此项，新增同步配置',
           'Value unchanged': '本机与同步配置一致',
           'Values match': '本机与同步配置一致',
           'Target retained by explicit policy': '按明确策略保留本机值',
           'Values differ under fail_if_different': '本机与来源不同，当前策略要求先处理差异',
           'Value types differ': '本机与来源的值类型不同',
           'Managed field differs from baseline': '本机字段在上次同步后被修改',
           'Previous Profile item is no longer declared; native content retained': '新 Profile 不再声明此项，保留本机已有内容',
           'Owned resource differs from its baseline; choose an eligible action for this exact review': '本机资源在上次同步后被修改，请为本次准确冲突选择处理方式'}
REASONS.update({
    'Exact selected resource body; unrelated resources are retained': '使用已选资源的固定内容，保留其他资源',
    'Identical immutable bytes, modes and input snapshots share one write; both validators run': '恢复副本的内容、权限和原快照相同，合并为一次写入，并执行两边的校验',
    'Compose disjoint Agent settings and App desktop preferences into one native write; preserve other fields': 'Agent 设置和 App 偏好互不重叠，合成一次写入，并保留其他本机字段',
    'Install only the reviewed framework files and explicit bootstrap entries; retain unrelated local state': '安装已核对的框架文件及明确入口，保留其他本机资料和状态',
    'Copy validated Profile declarations; existing different data requires explicit reconciliation': '复制已校验的 Profile；目标有不同内容时阻断，不直接覆盖',
    'Retire legacy runtime declarations; private run backups preserve exact recovery without parsing old fields': '旧声明退出活动位置，原字节保存在私有备份中供恢复，不接回旧字段兼容',
    'Close bound Apps normally before the combined cutover': '请先正常退出绑定的 App，再重新生成整份切换计划',
    'Update the explicit existing plugin version; retain its exact reconstructable prior source for native recovery': '明确更新这个既有插件的版本，并保留能准确重建旧安装的来源供回滚',
    'Existing plugin differs from the explicit replacement recovery pin': '本机插件与指定的旧版本恢复来源不同；请重新核对差异',
    'Keep the existing selector and installed files; rebind its marketplace to the verified fixed source': '保留现有插件名称和文件，将更新来源改为已核对的固定来源',
    'Existing plugin version or files differ; adoption cannot perform an upgrade': '现有插件与候选的版本或文件不同，不能作为无更新接管；需单独处理更新差异',
    'Same-name plugin is already installed; explicitly adopt its exact selector to avoid duplicate installation': '已有同名插件；请明确指定要接管的安装，避免重复安装',
    'Marketplace adoption would change the source of an unselected installed plugin': '同一来源还有未选中的已安装插件；本次不能改动它们的更新来源',
    'Marketplace adoption cannot change a project-scope plugin source': '该来源还用于项目级插件，本次用户级接管不能改动它',
    'Keep unowned historical images unchanged; do not copy them into the fixed source or claim their contents': '保留缓存中的其他旧版本，不把它们复制到来源库，也不接管其内容',
    'Unowned historical images remain; review native cache cleanup before upgrading this plugin': '缓存中仍有未接管的旧版本；更新前需单独核对原生缓存清理，当前保留原样',
    'Values and types are identical': '本机与同步配置的值及类型一致',
    'Selected field is absent in target': '本机尚无此项，新增同步配置',
    'External drift requires a fingerprint-scoped resolution': '本机字段在上次同步后被修改，需要针对本次内容确认处理方式',
    'Plan validates explicit user configuration; account, policy, CLI and session overrides are not session-tested': '已检查本次用户配置；账号、组织策略和实际会话效果未验证',
    'This plan does not run resource business logic or grant native trust': '未运行资源业务，也未授予原生信任',
    'Native files are validated; GUI reload, account and policy overrides are not exercised': '已检查配置文件；界面重新加载、账号和组织策略的实际效果未验证'})


def reason_label(value):
    return REASONS.get(value, value or '未提供')


def clean_text(value):
    """Render terminal control bytes visibly, including injected escape codes."""
    return ''.join(char if char in '\n\t' or ord(char) >= 32 and not 127 <= ord(char) <= 159
                   else '\\x%02x' % ord(char) for char in str(value))


def public_value(value):
    # Keep exact paths and values for Agent consumers. Terminal escaping is a
    # renderer responsibility, not a mutation of snapshot identities or keys.
    return redact(deepcopy(value))


def encode_json(report):
    content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    # JSON already escapes ASCII control characters. Also escape C1 controls
    # which some terminal emulators interpret, while retaining readable Chinese.
    return content.translate({code: '\\u%04x' % code for code in range(127, 160)})


def fallback_state_root():
    # Deliberately independent of cwd and agent authentication/configuration.
    return Path.home() / 'Library/Application Support/claude-config' if os.sys.platform == 'darwin' else Path.home() / '.local/state/claude-config'


def _private_path(path):
    """Check all ancestors; do not follow a symlink into arbitrary private data."""
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        # macOS supplies /var and /tmp as system aliases. Other links in the
        # selected storage path remain forbidden, including the state root.
        system_alias = os.sys.platform == 'darwin' and str(parent) in ('/var', '/tmp') and parent.resolve() == Path('/private' + str(parent))
        if parent.is_symlink() and not system_alias:
            raise ValueError('Report storage cannot traverse symlinks')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path.resolve()


def is_difference(row):
    if row.get('action') in ('object_merge', 'conflict', 'unchanged'):
        return False
    # Preserved differing values are a decision, but not a pending write.
    before = (row.get('before_exists', 'before' in row), row.get('before'))
    after = (row.get('after_exists', 'after' in row), row.get('after'))
    return not same(before[0], after[0]) or not same(before[1], after[1])


def _recovery(raw, results, error):
    journal = raw.get('recovery')
    if isinstance(journal, dict):
        journal = journal.get('journal')
    recovered = [row for row in results if row['status'] == 'rolled_back']
    unresolved = [row for row in results if row['status'] in ('recovery_conflict', 'started', 'applied')]
    attempted = recovered or unresolved
    if error and journal and attempted:
        state = 'partial' if unresolved and recovered else 'failed' if unresolved else 'completed'
    else:
        state = 'not_needed'
    return {'status': state, 'journal': journal, 'restored': recovered,
            'remaining': unresolved if error else []}


def build_report(raw, *, operation, preview_only=True, manager=None, context=None,
                 error=None, execution_started=False, report_id=None):
    """Normalize already-safe planning evidence without changing plan identity."""
    report = public_value(raw or {})
    context = public_value(context or {})
    from claude_config import __version__
    report['tool'] = {'name': 'claude-config', 'version': __version__}
    report.update(schema_version=1, report_id=report_id or uuid.uuid4().hex,
                  created_at=datetime.now(timezone.utc).isoformat(), operation=operation,
                  preview_only=preview_only)
    report.setdefault('plan_id', None)
    decisions = report.setdefault('decisions', [])
    checks = report.setdefault('checks', [])
    blockers = report.setdefault('blockers', [])
    for index, row in enumerate(decisions, 1):
        row['id'] = 'P' + str(index)
    for index, row in enumerate(checks, 1):
        row['id'] = 'K' + str(index)
    changes = []
    changes_by_path = {}
    for row in decisions:
        if is_difference(row):
            key = tuple(row.get('path', []))
            if key not in changes_by_path:
                change = {'id': 'D' + str(len(changes) + 1), 'path': list(key), 'decision_ids': [], 'check_ids': []}
                changes.append(change)
                changes_by_path[key] = change
            changes_by_path[key]['decision_ids'].append(row['id'])
    conflicts = []
    resource_paths = set()
    for row in report.get('resource_conflicts', []):
        row = deepcopy(row)
        row['id'] = 'C' + str(len(conflicts) + 1)
        resource_paths.add(tuple(row.get('path', [])))
        conflicts.append(row)
    for row in decisions:
        if row.get('action') == 'conflict' and not any(tuple(row.get('path', []))[:len(path)] == path for path in resource_paths if path):
            conflicts.append({'id': 'C' + str(len(conflicts) + 1), 'path': row.get('path', []),
                              'status': 'unresolved', 'reason': row.get('reason'), 'decision_ids': [row['id']],
                              'eligible_actions': row.get('alternatives', [])})
    for index, blocker in enumerate(blockers, 1):
        blocker['id'] = 'B' + str(index)
        blocker['check_ids'] = [row['id'] for row in checks if row.get('status') in ('blocked', 'failed')
                                and row.get('path') == blocker.get('path') and row.get('code') == blocker.get('code')]
        blocker['conflict_ids'] = [row['id'] for row in conflicts if row.get('path') == blocker.get('path')]
    paths = report.get('scope')
    if paths is None:
        paths = [row.get('path', []) for row in decisions if row.get('incoming_exists', 'incoming' in row)]
    scope = {'selected': list(dict.fromkeys(path if isinstance(path, str) else '/' + '/'.join(path) for path in paths)),
             'checked': [row['id'] for row in checks],
             'unverified': [row for row in checks if row['status'] in ('not_verified', 'skipped')],
             'note': '只检查本次选定配置与资源；未验证会话、账号或资源业务效果。'}
    results = [{key: row[key] for key in ('path', 'owner', 'action', 'status', 'operation_id', 'kind') if key in row}
               for row in report.get('results', [])]
    machine = manager.machine if manager else {}
    target = {'machine_id': machine.get('machine_id') or context.get('machine_id'), 'machine_file': context.get('machine_file'),
              'agent_type': context.get('agent_type'), 'app': context.get('app'),
              'profile': context.get('profile') or report.get('binding', {}).get('profile') or (manager.entry.get('profile') if manager else None),
              'native_file': str(manager.native_path) if manager else None}
    if context.get('target_label'):
        target['label'] = context['target_label']
        target['control_file'] = target.pop('native_file')
    if context.get('data_root'):
        target['data_root'] = context['data_root']
    if manager is not None and getattr(manager, 'app', None):
        target['app'] = manager.app
        target['application'] = str(manager.application)
        target['data_root'] = str(manager.data_root)
        if manager.app == 'claude-desktop':
            target['deployment_mode'] = getattr(manager, 'deployment_mode', None)
            target['default_third_party_data_root'] = str(Path.home() / 'Library/Application Support/Claude-3p')
    target['profile_file'] = str(manager.root / target['profile']) if manager and target['profile'] else None
    ready = bool(report.get('ready', not blockers)) and not error
    deployment = report.get('deployment_ready', ready if operation in ('plan', 'validate', 'apply', 'switch') else None)
    attempted = any(row['status'] not in ('planned', 'unchanged') for row in results)
    state = 'preview' if preview_only else 'partial' if error and attempted else 'failed' if error else 'completed' if execution_started else 'unchanged'
    if execution_started and not error and all(row['status'] == 'unchanged' for row in results):
        state = 'unchanged'
    report.update(targets=[public_value(target)], scope=scope, changes=changes, conflicts=conflicts,
                  fingerprints={'values': report.get('snapshots', {}), 'files': report.get('file_snapshots', {}),
                                'directories': report.get('directory_snapshots', {})},
                  results=results, readiness={'operation_ready': ready, 'deployment_ready': deployment,
                                              'deployment_blockers': report.get('deployment_blockers', blockers if deployment is False else [])},
                  execution={'status': state, 'started': execution_started, 'results': results},
                  recovery=_recovery(report, results, error), errors=[public_value(error)] if error else [],
                  artifacts={'saved': False, 'json_path': None, 'review_directories': []},
                  ready=ready,
                  status='error' if error and error.get('exit_code', 2) != 1 else 'blocked' if error or not ready else 'ok')
    # A previewed directory is not an artifact that has been generated.
    if execution_started and not error:
        report['artifacts']['review_directories'] = report.get('review_directories', [])
    report['summary'] = {
        'checks': {key: sum(row.get('status') == key for row in checks)
                   for key in ('passed', 'blocked', 'failed', 'warning', 'not_verified', 'skipped')},
        'changes': len(changes), 'conflicts': len(conflicts), 'blockers': len(blockers),
        'unresolved_conflicts': sum(row.get('status', 'unresolved') in ('unresolved', 'source_update_required') for row in conflicts),
        'resolved_conflicts': sum(row.get('status', '').startswith('resolved_') for row in conflicts),
        'applied': sum(row['status'] == 'applied' for row in results),
        'restored': len(report['recovery']['restored']), 'unrecovered': len(report['recovery']['remaining']),
        'kept_resources': len(report.get('kept_resources', []))}
    return report


def save_report(report, state_root):
    """Save once; persistence errors must not erase successful business results."""
    try:
        root = Path(state_root).absolute()
        root = _private_path(root)
        ignore_local_state(root)
        directory = _private_path(root / 'reports')
        identifier = report['report_id']
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise ValueError('Invalid report identity')
        path = directory / (identifier + '.json')
        report['artifacts'].update(saved=True, json_path=str(path))
        atomic_write(path, (encode_json(report) + '\n').encode(), 0o600)
    except (OSError, ValueError, TypeError) as exc:
        report['artifacts'].update(saved=False, json_path=None, error_type=type(exc).__name__)
        report['errors'].append({'code': 'report_save_failed', 'phase': 'report', 'error_type': type(exc).__name__,
                                 'reason': '完整 JSON 未保存；检查报告目录权限和可用空间。', 'exit_code': 2})
        report['status'] = 'error'
    return report


def exit_code(report):
    errors = report.get('errors', [])
    if any(row.get('exit_code') == 130 for row in errors):
        return 130
    if any(row.get('exit_code', 2) == 2 for row in errors):
        return 2
    return 1 if report['status'] == 'blocked' else 0


def path_label(path):
    return ' / '.join(LABELS.get(part, part) for part in path) or '整体配置'


def value_label(row, key, *, compact=False):
    if not row.get(key + '_exists', key in row):
        return '未声明' if key == 'incoming' else '不存在'
    value = row.get(key)
    if compact and isinstance(value, dict):
        path = row.get('path', [])
        if (file_group(path) is not None or path[:1] == ['data']) and 'sha256' in value and set(value) <= {'exists', 'sha256', 'mode'}:
            return '文件（' + (f"权限 {value['mode']:04o}；" if 'mode' in value else '') + '校验值见明细）'
        if path[-1:] == ['native_installation'] and 'version' in value and set(value) <= {'version', 'image', 'enabled'}:
            return '版本 ' + str(value['version']) + ('；' + ('启用' if value['enabled'] else '停用') if 'enabled' in value else '')
    return json.dumps(value, ensure_ascii=False)


def origin_label(origin, *, compact=False):
    kind = {'profile': 'Profile', 'native-config': '原生配置', 'native-resource': '本机资源',
            'catalog-resource': '资源来源', 'released-baseline': '发布基线', 'catalog-selection': '基线选择',
            'resource-usage': '资源运行选项', 'native-resource-options': '本机资源选项'}.get(origin.get('kind'), origin.get('kind', '未指定'))
    location = origin.get('file') or origin.get('ref') or origin.get('agent_type')
    return kind + ('：' + location if location else '') + ('；提交 ' + origin['commit'] if origin.get('commit') and not compact else '')


def decision_lines(row, *, compact=False):
    if compact:
        lines = [f"当前：{value_label(row, 'before', compact=True)} → 结果：{value_label(row, 'after', compact=True)}"]
        if (row.get('incoming_exists', 'incoming' in row), row.get('incoming')) != (row.get('after_exists', 'after' in row), row.get('after')):
            lines.append('来源：' + value_label(row, 'incoming', compact=True))
        lines += ['处理：' + ACTION_LABELS.get(row['action'], row['action']) + '；策略：' +
                  POLICY_LABELS.get(row.get('policy'), row.get('policy', '未指定')) + '；' +
                  ORIGIN_LABELS.get(row.get('policy_origin'), row.get('policy_origin', '未指定')),
                  '原因：' + reason_label(row.get('reason'))]
        for field, label in (('source', '来源位置'), ('target', '目标位置')):
            if row.get(field):
                lines.append(label + '：' + origin_label(row[field], compact=True))
        if row.get('alternatives'):
            lines.append('可选策略：' + '、'.join(POLICY_LABELS.get(item, item) for item in row['alternatives']))
        return lines
    return [f"当前：{value_label(row, 'before')}", f"来源：{value_label(row, 'incoming')}",
            f"结果：{value_label(row, 'after')}",
            '处理：' + ACTION_LABELS.get(row['action'], row['action']),
            '策略：' + POLICY_LABELS.get(row.get('policy'), row.get('policy', '未指定')) + '；' + ORIGIN_LABELS.get(row.get('policy_origin'), row.get('policy_origin', '未指定')),
            '原因：' + reason_label(row.get('reason')),
            '来源位置：' + origin_label(row.get('source', {})),
            '目标位置：' + origin_label(row.get('target', {})),
            '可选策略：' + '、'.join(POLICY_LABELS.get(item, item) for item in row.get('alternatives', []))]


def conflict_lines(row, report):
    lines = ['原因：' + reason_label(row.get('reason') or '本机文件在上次同步后发生变化，需要确认处理方式。')]
    if row.get('ref'):
        lines.append('资源：' + row['ref'])
    if row.get('conflict_id'):
        lines.append('准确冲突 ID：' + row['conflict_id'])
    for identifier in row.get('decision_ids', []):
        lines += decision_lines(next(item for item in report['decisions'] if item['id'] == identifier))
    actions = row.get('eligible_actions', [])
    action_labels = {'adopt_source': '采用来源版本（本机旧内容保留恢复备份）',
                     'keep_local': '本次保留（下次仍提示本机偏离）',
                     'update_source': '先更新资源来源，验证后重新检查'}
    if actions:
        lines.append('可选处理：' + '；'.join(action_labels.get(item, POLICY_LABELS.get(item, item)) for item in actions))
    for key in ('file_changes', 'declared_source_changes', 'issues'):
        if row.get(key):
            lines.append('文件与变更范围：' + key + '：' + json.dumps(row[key], ensure_ascii=False))
    if row.get('status', 'unresolved') != 'unresolved':
        lines.append('本次选择：' + {'resolved_adopt_source': '采用来源版本', 'resolved_keep_local': '本次保留', 'source_update_required': '先更新来源'}.get(row['status'], row['status']))
    return lines


def outcome_lines(report):
    execution = report['execution']
    if report['operation'] == 'fetch-baseline':
        if report['errors']:
            lines = ['基线获取未完成；未修改 Profile、Machine 或原生配置。已准备的固定来源保留。']
        else:
            prepared = report['prepared']
            lines = ['固定基线已获取并校验；尚未采用或部署。',
                     '来源：' + prepared['source']['repo'] + '@' + prepared['source']['commit'],
                     '准备目录：' + prepared['checkout'], '发布记录：' + prepared['release_record'],
                     '下一步预览采用：' + prepared['adopt_command']]
    elif execution['started'] and report['errors'] and any(row.get('phase') != 'report' for row in report['errors']):
        recovery = report['recovery']
        text = '执行失败'
        if recovery['status'] == 'completed':
            text += '，已恢复本次修改'
        elif recovery['remaining']:
            text += f"，仍有 {len(recovery['remaining'])} 项未恢复"
        else:
            text += '，未完成配置修改'
        lines = [text + '。']
    elif execution['started']:
        if report['operation'] == 'review-conflicts':
            lines = ['评审资料已生成；未修改原生配置、Profile 或部署基线。' if execution['status'] != 'unchanged' else '评审资料无变化；未修改原生配置、Profile 或部署基线。']
        elif report['operation'] == 'adopt-baseline':
            lines = ['资源基线选择已更新；尚未部署原生配置。' if execution['status'] != 'unchanged' else '资源基线选择无变化；尚未部署原生配置。']
        elif report['operation'] == 'export':
            lines = ['Profile 导出更新完成；未修改原生配置。' if execution['status'] != 'unchanged' else 'Profile 无变化；未修改原生配置。']
        else:
            lines = ['执行完成，未发生配置变化。' if execution['status'] == 'unchanged' else f"执行完成，修改 {report['summary']['applied']} 个文件或原生操作。"]
    elif report['status'] == 'blocked':
        lines = ['操作已阻断，未修改配置。']
    elif report['errors']:
        lines = ['检查或操作未完成，未开始修改配置。']
    else:
        lines = ['仅预览，未修改配置。']
    if report['readiness']['deployment_ready'] is False and report['operation'] == 'review-conflicts':
        lines.append('评审资料操作与部署分别检查；部署仍被阻断。')
    if report.get('kept_resources'):
        lines.append('本次保留：' + '、'.join(report['kept_resources']) + '；仍偏离同步配置。')
    if report['recovery']['journal']:
        lines.append('恢复日志：' + report['recovery']['journal'])
    if execution['started']:
        grouped = Counter()
        for row in execution['results']:
            if row['status'] in REQUIRED_RESULT_STATUSES or row in report['recovery']['remaining']:
                label = '未恢复' if row in report['recovery']['remaining'] else STATUS_LABELS.get(row['status'], row['status'])
                lines.append(label + '：' + row.get('path', row.get('operation_id', '原生操作')))
            else:
                owner = row.get('owner', '文件或原生操作')
                label = '资源 ' + owner.partition(':')[2] if owner.startswith('resource:') else OWNER_LABELS.get(owner, owner)
                grouped[label, row['status']] += 1
        for (label, status), count in grouped.items():
            lines.append(f"{label}：{STATUS_LABELS.get(status, status)} {count} 项")
        if grouped:
            lines.append('逐文件结果见可展开明细或完整 JSON。')
    for path in report['artifacts']['review_directories']:
        lines.append('私有评审资料：' + path)
    lines.append('完整 JSON：' + report['artifacts']['json_path'] if report['artifacts']['saved'] else '完整 JSON 未保存。')
    return lines


def file_group(path):
    """Only mechanical file decisions are grouped; setting/usage decisions stay visible."""
    offset = 2 if path[:1] == ['clients'] else 0
    tail = path[offset:]
    if tail and tail[0] in ('framework', 'shared_files'):
        return tuple(path[:offset + 1])
    if len(tail) == 4 and tail[0] == 'resources' and Path(tail[-1]).is_absolute():
        return tuple(path[:-1])
    return None


def check_lines(checks):
    groups = {}
    for row in checks:
        key = (row.get('name'), row['status'], row.get('reason')) if row['status'] in ('not_verified', 'skipped') else (row['id'],)
        groups.setdefault(key, []).append(row)
    lines = []
    for rows in groups.values():
        row = rows[0]
        identifiers = '、'.join(item['id'] for item in rows)
        subjects = list(dict.fromkeys(' / '.join(filter(None, (LABELS.get(item.get('client'), item.get('client')), item.get('ref'),
                                                              path_label(item['path']) if item.get('path') else None))) for item in rows))
        lines.append(identifiers + '  ' + LABELS.get(row.get('name'), row.get('name', '检查')) + '：' +
                     STATUS_LABELS.get(row['status'], row['status']) + (f'（{len(rows)} 项）' if len(rows) > 1 else '') +
                     ('；' + reason_label(row['reason']) if row.get('reason') else ''))
        if any(subjects):
            lines.append('  涉及：' + '；'.join(subject for subject in subjects if subject))
    return lines


def report_sections(report, *, compact=False):
    """Shared expanded content for static and interactive renderers."""
    by_id = {row['id']: row for row in report['decisions']}
    sections = []
    inventory = report.get('migration_inventory')
    if inventory:
        sections.append(('迁移输入分类', [row['section'] + ' → ' + ROUTE_LABELS.get(row['route'], row['route']) for row in inventory['manifest_sections']], False))
        sections.append(('需要转换的插件选择', [row['agent_type'] + ' / ' + row['ref'] for row in inventory['plugins']], False))
        sections.append(('配置字段盘点', [LABELS.get(row.get('agent_type', row.get('app', '')), '') + ' / ' + '/'.join(row['path']) + '：' + STATUS_LABELS.get(row['status'], row['status']) + '；' + REASONS_INVENTORY.get(row['reason'], row['reason'])
                        for row in [*inventory['agent_settings'], *inventory['app_settings']]], False))
        sections.append(('旧资源与文件', [str(row['reference']) + '：' + STATUS_LABELS.get(row['status'], row['status']) for row in inventory['assets']], False))
    file_groups = {}
    for row in report['changes']:
        decision = by_id[row['decision_ids'][0]]
        group = file_group(row['path'])
        if row['path'][:1] == ['shared_files'] and decision.get('action') != 'coalesce_snapshot':
            group = None
        if group is None or len(row['decision_ids']) != 1:
            body = []
            for identifier in row['decision_ids']:
                if len(row['decision_ids']) > 1:
                    body.append('处理记录：' + identifier)
                body.extend(decision_lines(by_id[identifier], compact=compact))
            sections.append((row['id'] + '  ' + path_label(row['path']), body, False))
            continue
        key = (group, *(decision.get(field) for field in ('action', 'policy', 'policy_origin', 'reason')))
        file_groups.setdefault(key, []).append((row, decision))
    for (group, action, policy, origin, reason), rows in file_groups.items():
        body = [f"{ACTION_LABELS.get(action, action)} {len(rows)} 项；{POLICY_LABELS.get(policy, policy)}；{ORIGIN_LABELS.get(origin, origin)}。",
                '原因：' + reason_label(reason), '完整路径、编号和值见可展开文件明细或 JSON。']
        body += ['文件与变更范围：' + row['id'] + '  ' + path_label(row['path']) + '\n' + '\n'.join(decision_lines(decision))
                 for row, decision in rows]
        sections.append(('文件变更：' + path_label(list(group)), body, False))
    for row in report['conflicts']:
        sections.append((row['id'] + '  ' + path_label(row.get('path', [])), conflict_lines(row, report), True))
    for blocker in report['blockers']:
        if not blocker['conflict_ids']:
            sections.append((blocker['id'] + '  检查未通过：' + path_label(blocker.get('path', [])),
                             ['原因：' + reason_label(blocker.get('reason') or LABELS.get(blocker.get('code'), blocker.get('code', '必要检查未通过')))], True))
    for error in report['errors']:
        sections.append(('错误：' + error['code'], ['阶段：' + error.get('phase', '未知'), '原因：' + error['reason'] +
                         ('\n检查详情：' + error['detail'] if error.get('detail') else '')], True))
    selected = {identifier for row in report['changes'] for identifier in row['decision_ids']}
    selected |= {identifier for row in report['conflicts'] for identifier in row.get('decision_ids', [])}
    other = [row for row in report['decisions'] if row['id'] not in selected]
    if other:
        sections.append(('其他处理决策（合并、保留和无变化）',
                         [row['id'] + '  ' + path_label(row.get('path', [])) + '\n    ' + '\n    '.join(decision_lines(row)) for row in other], False))
    if report['checks']:
        sections.append(('逐项检查记录', [row['id'] + '  ' + LABELS.get(row.get('name'), row.get('name', '检查')) +
                         '：' + STATUS_LABELS.get(row['status'], row['status']) +
                         ('；' + reason_label(row['reason']) if row.get('reason') else '') for row in report['checks']], False))
    return sections


def scope_labels(report):
    labels = []
    for value in report['scope']['selected']:
        path = value.lstrip('/').split('/')
        offset = 2 if path[:1] == ['clients'] else 0
        tail = path[offset:]
        length = 3 if tail[:1] == ['resources'] else 1
        label = path_label(path[:offset + length])
        if label not in labels:
            labels.append(label)
    # Single-Agent managers may declare only settings paths in scope.selected;
    # resource checks still identify the selected resources actually inspected.
    for check in report['checks']:
        ref = check.get('ref')
        if check.get('name') == 'resource_runtime' and ref and not any(label.split(' / ')[-1] == ref for label in labels):
            labels.append('资源 / ' + ref)
    return labels


def overview_lines(report):
    target = report['targets'][0]
    stats = report['summary']
    counts = stats['checks']
    lines = ['claude-config ' + report['operation'],
             '配置检查与操作报告 · ' + ('仅预览' if report['preview_only'] else '执行'),
             '对象：' + LABELS.get(target.get('agent_type') or target.get('app'), target.get('agent_type') or target.get('app') or target.get('label') or '未确定') +
             (' (' + (target.get('agent_type') or target.get('app')) + ')' if target.get('agent_type') or target.get('app') else ''),
             '机器：' + (target.get('machine_id') or '未获取') + ('；Profile：' + target['profile'] if target.get('profile') else '')]
    if target.get('data_root') and not target.get('app'):
        lines[2:] = ['对象：配置数据仓库', '数据目录：' + target['data_root']]
        input_counts = report['migration_inventory']['counts'] if report.get('migration_inventory') else {}
        if input_counts:
            lines.append(f"盘点：{input_counts['plugins']} 个插件选择，{input_counts['agent_settings']} 个 Agent 字段，{input_counts['app_settings']} 个 App 字段，{input_counts['assets']} 处本地来源。")
        conversion = report.get('migration_conversion')
        if conversion:
            lines.append(('已生成' if conversion.get('written') else '计划生成') + f"：{len(conversion['profiles'])} 份 Profile，{len(conversion['resources'])} 项资源；候选目录：" + conversion['output_root'])
            lines.append('这是新版候选，不修改现有配置；来源发布、现有安装归属和统一切换尚未完成。')
            for row in conversion.get('review_required', []):
                lines.append('需要审阅：' + (row.get('ref') or row['code']) + '；' +
                             (str(row['before_version']) + ' → ' + str(row['candidate_version']) if 'candidate_version' in row else row['reason']))
    if target.get('native_file'):
        lines.append(('导出来源：' if report['operation'] == 'export' else '原生配置：') + target['native_file'])
    if target.get('control_file'):
        lines.append('机器配置指针：' + target['control_file'])
    if target.get('profile_file'):
        lines.append('Profile 文件：' + target['profile_file'])
    lines += [f"检查：{counts['passed']} 项通过，{counts['blocked'] + counts['failed']} 项不通过，{counts['warning']} 项警告，{counts['not_verified'] + counts['skipped']} 项未验证或未检查。",
              f"{'计划差异' if report['execution']['started'] else '待更新'}：{stats['changes']} 项；{stats['unresolved_conflicts']} 处未解决冲突，{stats['resolved_conflicts']} 处已处理冲突，{stats['blockers']} 项阻断。"]
    for row in report['decisions']:
        if row.get('path') in (['settings', 'model'], ['settings', 'effortLevel'], ['settings', 'model_reasoning_effort'], ['settings', 'language']):
            lines.append(path_label(row['path'][1:]) + '：' + value_label(row, 'before') + ' → ' + value_label(row, 'after'))
    scope = scope_labels(report)
    if len(scope) > 4:
        lines += ['检查范围：', *('  ' + label for label in scope)]
    else:
        lines.append('检查范围：' + ('、'.join(scope) or '尚未生成选定配置的检查计划'))
    lines.append(report['scope']['note'])
    return lines


def render_text(report, *, brief=False):
    lines = overview_lines(report)
    outcome = outcome_lines(report)
    lines.insert(2, outcome[0])
    for title, body, expanded in report_sections(report, compact=True):
        if brief and not expanded and title != '逐项检查记录':
            continue
        if title == '其他处理决策（合并、保留和无变化）':
            other = [row for row in report['decisions'] if row['action'] in ('preserve', 'residual', 'object_merge')]
            if other:
                lines += ['', title]
                for row in other:
                    if row['action'] == 'preserve' and not row.get('incoming_exists', 'incoming' in row):
                        lines.append(f"  {row['id']}  {path_label(row.get('path', []))}：保留本机值 {value_label(row, 'after', compact=True)}；" +
                                     reason_label(row.get('reason')) + '；' +
                                     ORIGIN_LABELS.get(row.get('policy_origin'), row.get('policy_origin', '未指定')))
                        continue
                    lines.append(f"  {row['id']}  {path_label(row.get('path', []))}：{ACTION_LABELS.get(row['action'], row['action'])}；" +
                                 POLICY_LABELS.get(row.get('policy'), row.get('policy', '未指定')) + '；' + ORIGIN_LABELS.get(row.get('policy_origin'), row.get('policy_origin', '未指定')))
                    if row['action'] != 'object_merge':
                        lines += ['    ' + line for line in decision_lines(row, compact=True)]
            continue
        if title == '逐项检查记录':
            exceptional = [row for row in report['checks'] if row['status'] != 'passed'
                           and (not brief or row['status'] in ('warning', 'blocked', 'failed'))]
            if exceptional:
                lines += ['', '需注意的检查', *check_lines(exceptional)]
            continue
        lines += ['', title, *('  ' + line for line in body if not (title.startswith('文件变更：') and line.startswith('文件与变更范围：')))]
    lines += ['', *outcome[1:]]
    return clean_text('\n'.join(lines))
