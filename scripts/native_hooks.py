"""Conservative validation of explicitly selected native command hooks.

This validates event/config structure, not a hook program's runtime JSON output.
Native trust approval is never changed by deployment.
"""
COMMON = {'SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PreToolUse',
          'PostToolUse', 'PermissionRequest', 'Stop', 'SubagentStart',
          'SubagentStop', 'PreCompact', 'PostCompact'}
EVENTS = {'codex': COMMON | {'Interrupt'},
          'claude': COMMON | {'Setup', 'Notification', 'PostToolUseFailure',
                             'StopFailure', 'TaskCompleted', 'TeammateIdle',
                             'WorktreeCreate', 'WorktreeRemove'}}


def validate_hooks(host, hooks):
    if not isinstance(hooks, dict):
        raise ValueError('Native hooks must be an event mapping')
    for event, groups in hooks.items():
        if event not in EVENTS[host] or not isinstance(groups, list):
            raise ValueError('Unsupported native hook event or shape: ' + event)
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError('Hook matcher group must contain a hooks list')
            if 'matcher' in group and not isinstance(group['matcher'], str):
                raise ValueError('Hook matcher must be a string')
            for handler in group['hooks']:
                if not isinstance(handler, dict) or handler.get('type') != 'command' or not isinstance(handler.get('command'), str) or not handler['command'].strip():
                    raise ValueError('Managed hooks currently support native command handlers only')
                if 'timeout' in handler and (isinstance(handler['timeout'], bool) or not isinstance(handler['timeout'], (int, float)) or handler['timeout'] <= 0):
                    raise ValueError('Hook timeout must be positive seconds')
                if host == 'codex' and event in {'SessionEnd', 'Interrupt'} and handler.get('timeout', 1) > 3:
                    raise ValueError('Codex SessionEnd/Interrupt timeout exceeds 3 seconds')
