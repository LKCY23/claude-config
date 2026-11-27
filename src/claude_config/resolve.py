"""Guided interactive and single-command resolution for configuration and resource drift."""
from pathlib import Path
import shlex
import sys

from .core.deployment_scope import DeploymentScope
from .core.execution_context import progress, progress_output
from .core.journal import unfinished
from .core.manager import ProfileManager
from .core.reporting import (
    build_report,
    fallback_state_root,
    path_label,
    render_text,
    save_report,
    value_label,
)
from .core.schema import SchemaError, require
from .core.transaction import PlanStale, UnfinishedRun
from .profile import ReportParser, UsageError, output_report, report_format, report_verbose

_SETTINGS_ACTIONS = ('source_wins', 'keep_target', 'export')
_RESOURCE_ACTIONS = ('adopt_source', 'keep_local')


def _parse_choose(items):
    chosen = {}
    for item in items:
        ptr, sep, action = item.partition('=')
        require(bool(sep and ptr.startswith('/') and action),
                '--choose requires POINTER=ACTION (for example /settings/model=source_wins)')
        require(ptr not in chosen, f'Duplicate --choose selection for {ptr}')
        chosen[ptr] = action
    return chosen


def _discover_conflicts(initial_report, target_fp):
    decisions_by_id = {row['id']: row for row in initial_report.get('decisions', [])}
    items = []
    for row in initial_report.get('conflicts', []):
        if row.get('status', 'unresolved') not in ('unresolved', 'source_update_required'):
            continue
        path = row.get('path', [])
        ptr = '/' + '/'.join(path)
        if path[:1] == ['settings']:
            decision_id = (row.get('decision_ids') or [None])[-1]
            decision = decisions_by_id.get(decision_id, {})
            items.append({
                'kind': 'settings',
                'pointer': ptr,
                'path': path,
                'label': path_label(path[1:]),
                'before_display': value_label(decision, 'before', compact=True) if decision else '未知',
                'incoming_display': value_label(decision, 'incoming', compact=True) if decision else '未知',
                'is_drift': decision.get('policy') == 'drift',
                'target_fingerprint': target_fp,
                'actions': _SETTINGS_ACTIONS,
            })
        else:
            eligible = [act for act in row.get('eligible_actions', []) if act in _RESOURCE_ACTIONS]
            items.append({
                'kind': 'resources',
                'pointer': ptr,
                'path': path,
                'ref': row.get('ref') or ptr,
                'conflict_id': row.get('conflict_id'),
                'file_changes_count': len(row.get('file_changes', [])),
                'issues': row.get('issues', []),
                'eligible': eligible,
                'actions': tuple(eligible),
            })
    return items


def _prompt_for_item(index, total, item, profile_name):
    print(f'\n[{index}/{total}] ', end='')
    if item['kind'] == 'settings':
        print(f"设置冲突：{item['pointer']}（{item['label']}）")
        print(f"  本机当前：{item['before_display']}")
        print(f"  配置库值：{item['incoming_display']} ({profile_name})")
        print('  请选择处理方式：')
        print('    1) 采用配置库值 (source_wins)')
        print('    2) 保留本机值 (keep_target，仅本次保留)')
        print('    3) 回写配置库 (export 到 Profile)')
        print('    4) 跳过')
        mapping = {'1': 'source_wins', '2': 'keep_target', '3': 'export'}
        while True:
            ans = input('  请选择 [1-4] (默认 4): ').strip()
            if ans in ('', '4', 'skip'):
                return None
            if ans in mapping:
                return mapping[ans]
            print('  请输入 1、2、3 或 4。')
    print(f"资源冲突：{item['pointer']}（{item['ref']}，涉及 {item['file_changes_count']} 处文件差异）")
    if not item['eligible']:
        print(f"  提示：当前存在阻断项（{', '.join(item['issues']) or '需先更新来源'}），请用 review-conflicts 查看。")
        return None
    options = []
    if 'adopt_source' in item['eligible']:
        options.append(('adopt_source', '采用来源版本 (adopt_source，旧内容保留恢复备份)'))
    if 'keep_local' in item['eligible']:
        options.append(('keep_local', '本次保留本机版本 (keep_local)'))
    print('  请选择处理方式：')
    for pos, (_, label) in enumerate(options, 1):
        print(f'    {pos}) {label}')
    skip_num = len(options) + 1
    print(f'    {skip_num}) 跳过')
    while True:
        ans = input(f'  请选择 [1-{skip_num}] (默认 {skip_num}): ').strip()
        if ans in ('', str(skip_num), 'skip'):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        print(f'  请输入 1 到 {skip_num} 之间的数字。')


def _unselected_blocked_report(manager, agent, initial_report, unselected, machine_path):
    first = unselected[0]
    if first['kind'] == 'settings':
        hint_cmd = (
            f"claude-config resolve --agent {shlex.quote(agent)} "
            f"--choose {shlex.quote(first['pointer'])}=source_wins --write"
        )
    elif first['eligible']:
        hint_cmd = (
            f"claude-config resolve --agent {shlex.quote(agent)} "
            f"--choose {shlex.quote(first['pointer'])}={first['eligible'][0]} --write"
        )
    else:
        hint_cmd = f"claude-config review-conflicts --agent {shlex.quote(agent)}"
    raw = {
        **initial_report,
        'blockers': [
            {
                'code': 'unresolved_conflicts',
                'reason': f'仍有 {len(unselected)} 处未选择处理方式的冲突；请交互确认或运行：{hint_cmd}',
            }
        ],
        'ready': False,
    }
    return build_report(
        raw,
        operation='resolve',
        preview_only=True,
        manager=manager,
        context={'machine_file': str(machine_path), 'agent_type': agent},
        error={
            'code': 'unresolved_conflicts',
            'phase': 'selection',
            'reason': f'仍有 {len(unselected)} 处未选择处理方式的冲突；示例命令：{hint_cmd}',
            'exit_code': 1,
        },
    )


def main(argv=None):
    with progress_output(sys.stderr):
        return _entry(argv)


def _entry(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = report_format(argv)
    verbose = report_verbose(argv)
    parser = ReportParser(description=__doc__, prog='claude-config resolve')
    parser.add_argument('--agent', required=True, choices=('claude-code', 'codex', 'antigravity'))
    parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    parser.add_argument('--choose', action='append', default=[], metavar='POINTER=ACTION',
                        help='设置冲突: source_wins|keep_target|export；资源冲突: adopt_source|keep_local')
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument('--dry-run', action='store_true')
    execution.add_argument('--write', action='store_true', help='确认并执行冲突处理；在非交互模式下必须显式指定才会写入')
    parser.add_argument('--format', choices=('auto', 'text', 'tui', 'json'), default='auto')
    parser.add_argument('--verbose', action='store_true', help='显示完整保留决策与细粒度输出')

    manager = None
    error = None
    started = False
    preview_only = True
    raw = {'checks': [], 'decisions': [], 'blockers': [], 'results': []}
    args = None
    try:
        args = parser.parse_args(argv)
        chosen = _parse_choose(args.choose)
        machine_path = args.machine.expanduser().absolute()
        if not machine_path.is_file():
            raise SchemaError('未找到 Machine；请运行 claude-config init，或指定 --machine。')
        manager = ProfileManager(machine_path, args.agent)
        progress(f'扫描 {args.agent} 的未解决冲突')
        pending_runs = unfinished(manager.state_root, [str(manager.native_path.resolve())])
        if pending_runs:
            raise UnfinishedRun(pending_runs)
        initial = manager.plan()
        initial_report = build_report(
            initial.report,
            operation='plan',
            preview_only=True,
            manager=manager,
            context={'machine_file': str(machine_path), 'agent_type': args.agent},
        )
        target_fp = (initial.report.get('snapshots') or {}).get('target')
        discovered = _discover_conflicts(initial_report, target_fp)
        by_pointer = {item['pointer']: item for item in discovered}
        for ptr, action in chosen.items():
            require(ptr in by_pointer, f'指定路径当前不存在未解决冲突：{ptr}')
            item = by_pointer[ptr]
            if item['kind'] == 'settings':
                require(action in _SETTINGS_ACTIONS,
                        f'设置冲突 {ptr} 不支持操作 {action}（可选：{", ".join(_SETTINGS_ACTIONS)}）')
            else:
                if not item['eligible']:
                    raise SchemaError(
                        f"资源冲突 {ptr} 包含结构/来源阻断（{', '.join(item['issues']) or '需先更新来源'}），"
                        f"请先运行 claude-config review-conflicts --agent {args.agent} 处理。"
                    )
                require(action in item['eligible'],
                        f"资源冲突 {ptr} 不支持操作 {action}（可选：{', '.join(item['eligible'])}）")

        if not discovered:
            if initial.report.get('ready', False):
                raw = {
                    'checks': [{'name': 'conflict_discovery', 'status': 'passed', 'reason': '当前没有未解决的冲突'}],
                    'decisions': [],
                    'blockers': [],
                    'results': [],
                    'ready': True,
                }
            else:
                raw = initial.report
        else:
            interactive = (sys.stdin.isatty() and sys.stdout.isatty()
                           and args.format != 'json' and not args.dry_run)
            selections = dict(chosen)
            for idx, item in enumerate(discovered, 1):
                ptr = item['pointer']
                if ptr in selections:
                    continue
                if interactive:
                    pick = _prompt_for_item(idx, len(discovered), item, manager.entry['profile'])
                    if pick is not None:
                        selections[ptr] = pick
            unselected = [item for item in discovered if item['pointer'] not in selections]
            if unselected and not selections:
                report = _unselected_blocked_report(manager, args.agent, initial.report, unselected, machine_path)
                save_report(report, manager.state_root)
                return output_report(report, args.format, verbose=args.verbose)

            partial_scope = None
            if unselected:
                selected_settings = [ptr for ptr in selections if by_pointer[ptr]['kind'] == 'settings']
                selected_resources = [
                    by_pointer[ptr]['path'][2] if len(by_pointer[ptr]['path']) >= 3 else by_pointer[ptr]['ref']
                    for ptr in selections
                    if by_pointer[ptr]['kind'] == 'resources'
                ]
                if selected_settings and selected_resources:
                    raise UsageError('部分解决冲突时，单次 --choose 需同属设置字段或同属资源冲突；或一次选择全部未解决冲突。')
                if selected_settings:
                    partial_scope = DeploymentScope(fields=tuple(selected_settings))
                elif selected_resources:
                    partial_scope = DeploymentScope(resources=tuple(selected_resources))

            export_fields = []
            resolutions = {}
            run_policy_fields = {}
            for ptr, action in selections.items():
                item = by_pointer[ptr]
                if item['kind'] == 'settings':
                    if action == 'export':
                        export_fields.append(ptr)
                    elif item['is_drift']:
                        resolutions[ptr] = {'target_fingerprint': item['target_fingerprint'], 'policy': action}
                    else:
                        run_policy_fields[ptr] = action
                else:
                    resolutions[ptr] = {'conflict_id': item['conflict_id'], 'action': action}

            def _scoped_drift_resolutions(mgr, scope_obj, base_fp):
                if scope_obj is None or not any(
                    by_pointer[p]['kind'] == 'settings' and by_pointer[p]['is_drift'] for p in selections
                ):
                    return dict(resolutions), base_fp
                if mgr.plan().report.get('snapshots', {}).get('target') != base_fp:
                    raise PlanStale('Native configuration changed during partial conflict resolution')
                scoped_fp = mgr.plan(selection=scope_obj).report.get('snapshots', {}).get('target')
                scoped_res = {
                    ptr: ({'target_fingerprint': scoped_fp, 'policy': val['policy']}
                          if ptr in by_pointer and by_pointer[ptr]['kind'] == 'settings' and by_pointer[ptr]['is_drift']
                          else val)
                    for ptr, val in resolutions.items()
                }
                return scoped_res, scoped_fp

            def _build_preview():
                if export_fields and not resolutions and not run_policy_fields:
                    return manager.export(export_fields, run_policy={'default': 'source_wins'})
                preview_res, preview_fp = _scoped_drift_resolutions(manager, partial_scope, target_fp)
                preview_rp = dict(run_policy_fields)
                for ptr in export_fields:
                    item = by_pointer[ptr]
                    if item['is_drift']:
                        preview_res[ptr] = {'target_fingerprint': preview_fp, 'policy': 'keep_target'}
                    else:
                        preview_rp[ptr] = 'keep_target'
                rp = {'fields': preview_rp} if preview_rp else None
                plan_obj = manager.plan(run_policy=rp, resolutions=preview_res or None, selection=partial_scope)
                if export_fields:
                    plan_obj.report.setdefault('checks', []).append({
                        'name': 'export_resolution_preview',
                        'status': 'passed',
                        'reason': '预览模式：' + '、'.join(export_fields) + ' 将通过 export 写回 Profile',
                    })
                return plan_obj

            execute = False if args.dry_run else True if args.write else False
            preview_plan = _build_preview()
            if not args.write and not args.dry_run and interactive and preview_plan.report.get('ready', False):
                shown = build_report(
                    preview_plan.report,
                    operation='resolve',
                    preview_only=True,
                    manager=manager,
                    context={'machine_file': str(machine_path), 'agent_type': args.agent},
                )
                print('\n' + render_text(shown, verbose=args.verbose))
                execute = input('\n确认执行以上冲突处理？[y/N] ').strip().lower() in ('y', 'yes')

            if not execute or not preview_plan.report.get('ready', False):
                raw = preview_plan.report
            else:
                preview_only = False
                export_ok = True
                has_export_drift = any(by_pointer[ptr]['is_drift'] for ptr in export_fields)
                if export_fields:
                    progress('导出所选字段回写 Profile')
                    export_plan = manager.export(export_fields, run_policy={'default': 'source_wins'})
                    if not export_plan.report.get('ready', False):
                        export_ok = False
                        raw = export_plan.report
                    else:
                        started = True
                        raw = export_plan.apply(manager.state_root)
                        if resolutions or run_policy_fields or has_export_drift:
                            manager = ProfileManager(machine_path, args.agent)
                if export_ok and (resolutions or run_policy_fields or has_export_drift):
                    progress('对齐基线并应用剩余冲突解决策略')
                    rp = {'fields': run_policy_fields} if run_policy_fields else None
                    scope = (
                        partial_scope
                        if partial_scope is not None
                        else DeploymentScope(fields=tuple(export_fields))
                        if (has_export_drift and not resolutions and not run_policy_fields)
                        else None
                    )
                    scoped_res, ack_target_fp = _scoped_drift_resolutions(manager, scope, target_fp)
                    export_drift_ack = {
                        ptr: {'target_fingerprint': ack_target_fp, 'policy': 'source_wins'}
                        for ptr in export_fields
                        if by_pointer[ptr]['is_drift']
                    }
                    merged_res = {**scoped_res, **export_drift_ack}
                    apply_plan = manager.plan(run_policy=rp, resolutions=merged_res or None, selection=scope)
                    if apply_plan.report.get('ready', False):
                        started = True
                        raw = apply_plan.apply(manager.state_root)
                    else:
                        raw = apply_plan.report
    except UsageError as exc:
        report = build_report(
            {},
            operation='resolve',
            error={'code': 'invalid_arguments', 'phase': 'arguments', 'reason': str(exc), 'exit_code': 2},
        )
        save_report(report, fallback_state_root())
        return output_report(report, mode, verbose=verbose)
    except (Exception, KeyboardInterrupt) as exc:
        started = started and bool(raw.get('recovery'))
        code = (
            'unfinished_run' if isinstance(exc, UnfinishedRun)
            else 'interrupted' if isinstance(exc, KeyboardInterrupt)
            else 'plan_stale' if isinstance(exc, PlanStale)
            else 'invalid_configuration' if isinstance(exc, SchemaError)
            else 'operation_failed'
        )
        reason = (
            str(exc) if isinstance(exc, SchemaError)
            else '目标存在未完成事务；先使用 runs 检查，再预览 rollback 或 reconcile-run。' if code == 'unfinished_run'
            else '操作被中断；请核对本次修改与恢复记录。' if code == 'interrupted'
            else '冲突快照在执行前发生变化；请重新运行 resolve。' if code == 'plan_stale'
            else '冲突解决未完成；请核对出错阶段与恢复日志。'
        )
        error = {
            'code': code,
            'phase': 'execution' if started else 'planning',
            'error_type': type(exc).__name__,
            'reason': reason,
            'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 1 if not started and isinstance(exc, (SchemaError, PlanStale)) else 2,
        }
        if isinstance(exc, UnfinishedRun):
            error['runs'] = exc.runs
        raw.setdefault('blockers', []).append({'code': code, 'reason': reason})

    report = build_report(
        raw,
        operation='resolve',
        preview_only=preview_only,
        manager=manager,
        context={'machine_file': str(args.machine.expanduser().absolute()) if args else None,
                 'agent_type': args.agent if args else None},
        error=error,
        execution_started=started,
    )
    save_report(report, manager.state_root if manager else fallback_state_root())
    return output_report(report, args.format if args else mode, verbose=args.verbose if args else verbose)
