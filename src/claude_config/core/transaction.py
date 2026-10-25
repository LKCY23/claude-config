"""Locked, fingerprint-checked file execution with guarded scoped recovery.

Not a multi-file atomic transaction: a private journal records each write.
Native CLI operations are separately journaled steps in the same checked plan.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import signal
import threading
import time

from .journal import JournalWriter, unfinished, registered_pending, register_run

from .merge import fingerprint
from .execution_context import progress


class PlanStale(RuntimeError):
    def __init__(self, message, *, path=None, expected=None, actual=None, phase='recheck'):
        super().__init__(message)
        self.detail = {'phase': phase, 'path': str(path) if path is not None else None,
                       'expected': expected, 'actual': actual}


class UnfinishedRun(RuntimeError):
    def __init__(self, runs):
        super().__init__('Unfinished execution requires inspection or recovery')
        self.runs = runs


@contextmanager
def interruption_handlers():
    previous = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, frame):
            raise KeyboardInterrupt('Execution interrupted')
        for name in ('SIGTERM', 'SIGHUP'):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, interrupted)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_fingerprint(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing a symlinked target/source: ' + str(path))
    if not path.exists():
        return {'exists': False}
    if not path.is_file():
        raise ValueError('Expected a regular file: ' + str(path))
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'exists': True, 'sha256': digest.hexdigest(),
            'mode': path.stat().st_mode & 0o777}


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Private directory cannot be a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def directory_fingerprint(root):
    root = Path(root)
    if root.is_symlink() or root.exists() and not root.is_dir():
        raise ValueError('Resource directory identity changed')
    entries = {}
    if root.exists():
        for path in root.rglob('*'):
            relative = path.relative_to(root)
            if any(part in ('__pycache__', '.DS_Store', '.in_use') for part in relative.parts):
                continue
            if path.is_symlink():
                entries[relative.as_posix()] = {'kind': 'symlink', 'target': os.readlink(path)}
            elif path.is_file():
                entries[relative.as_posix()] = file_fingerprint(path)
    return {'exists': root.exists(), 'resolved': str(root.resolve()), 'entries': entries}


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing to replace a symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.profile-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def ignore_local_state(root):
    # Local infrastructure survives rollback alongside its journals/backups.
    path = Path(root) / '.gitignore'
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError('State ignore file must be a regular file')
    content = path.read_bytes() if path.exists() else b''
    if content.splitlines()[-1:] != [b'*']:
        content += (b'' if not content or content.endswith(b'\n') else b'\n') + b'*\n'
        atomic_write(path, content)


@contextmanager
def lock(path):
    # First executor is macOS/Linux. Fail closed rather than pretend Windows locks.
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError('This file executor requires POSIX locks') from exc
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        last = time.monotonic()
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - last >= 5:
                    progress('仍在等待配置写入锁；尚未写入')
                    last = time.monotonic()
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class FileChange:
    path: Path
    content: bytes
    owner: str
    validator: object
    expected: object = None
    mode: object = None
    delete: bool = False


class FilePlan:
    def __init__(self, changes, *, sources=(), scopes=(), report=None, directories=(), native_operations=(), native_index=None, steps=None, recovery_guard=None, guards=(), step_guard=None, prepare=True):
        self.changes = tuple(replace(change, path=Path(change.path).absolute()) for change in changes)
        self.native_operations = tuple(native_operations)
        self.recovery_guard = recovery_guard
        self.guards = tuple(guards)
        self.step_guard = step_guard
        self.prepared = prepare
        self.steps = list(self.changes) if steps is None else [replace(step, path=Path(step.path).absolute()) if isinstance(step, FileChange) else step for step in steps]
        if steps is None:
            index = len(self.steps) if native_index is None else native_index
            self.steps[index:index] = self.native_operations
        self.paths = {}
        self.scopes = tuple(scopes)
        watched = set(directories) | {root for operation in self.native_operations for root in operation.directories}
        self.directories = {Path(root).absolute(): directory_fingerprint(root) if prepare else None for root in sorted(watched)}
        self.report = report or {'checks': [], 'decisions': [], 'blockers': []}
        self.report.setdefault('results', [])
        self.report.setdefault('recovery', None)
        self.blockers = self.report['blockers']
        seen = set()
        progress_label = '检查计划文件' if prepare else '收集文件变更'
        progress(f'{progress_label}：共 {len(self.changes)} 项')
        for index, change in enumerate(self.changes, 1):
            original = Path(change.path).absolute()
            # Keep the actual checked path; resolving must not hide a leaf symlink.
            resolved = original.resolve()
            if resolved in seen:
                raise ValueError('Multiple owners write the same file; combine field plans first')
            seen.add(resolved)
            expected = change.expected
            if expected is None:
                expected = file_fingerprint(original)
            elif isinstance(expected, dict):
                expected = dict(expected)
            self.paths[original] = {'file': expected, 'resolved': str(resolved)}
            if change.expected is not None and change.expected != self.paths[original]['file']:
                raise PlanStale('plan_stale during planning: ' + str(original))
            if not isinstance(change.content, bytes):
                raise ValueError('FileChange content must be bytes')
            if change.mode is not None and (type(change.mode) is not int or not 0 <= change.mode <= 0o777):
                raise ValueError('Invalid planned file mode')
            if change.delete and change.content:
                raise ValueError('Delete operations do not carry replacement content')
            if not callable(change.validator):
                raise ValueError('Every target requires a final-result validator')
            if index % 50 == 0:
                progress(f'{progress_label}：{index}/{len(self.changes)}')
        for source in sources:
            path = Path(source).absolute()
            if path not in self.paths:
                self.paths[path] = {'file': dict(sources[source]) if isinstance(sources, dict) else file_fingerprint(path),
                                    'resolved': str(path.resolve())}
            if isinstance(sources, dict) and sources[source] != self.paths[Path(source).absolute()]['file']:
                raise PlanStale('plan_stale source during planning: ' + str(source))
        for operation in self.native_operations:
            for path in operation.paths:
                path = Path(path).absolute()
                if path not in self.paths:
                    self._snapshot(path)
        self.aliases = {}
        for path, snapshot in self.paths.items():
            self.aliases.setdefault(snapshot['resolved'], []).append(path)
        if prepare:
            self._validate_all()
            for operation in self.native_operations:
                operation.preflight()
            self._run_guards()
            self._recheck()
            self.report['checks'].append({'name': 'file_preflight', 'status': 'passed'})
        self.report['file_snapshots'] = {str(path): info for path, info in self.paths.items()}
        self.report['directory_snapshots'] = {str(path): {'exists': value['exists'], 'fingerprint': fingerprint(value)} if value is not None else {'not_checked': True}
                                               for path, value in self.directories.items()}
        identity = {'merge_plan': self.report.get('plan_id'),
                                            'files': self.report['file_snapshots'],
                                            'scopes': self.scopes,
                                            'directories': {str(path): value for path, value in self.directories.items()},
                                            'outputs': [hashlib.sha256(c.content).hexdigest() for c in self.changes],
                                            'modes': [c.mode for c in self.changes], 'deletes': [c.delete for c in self.changes]}
        if self.native_operations:
            identity['native_operations'] = [op.definition for op in self.native_operations]
            identity['order'] = [str(step.path) if isinstance(step, FileChange) else step.identifier for step in self.steps]
        self.report['plan_id'] = fingerprint(identity)
        self.report['results'] = [
            {'path': str(change.path), 'owner': change.owner,
             'action': 'delete' if change.delete else 'write',
             'status': 'unchanged' if self._unchanged(change, self.paths[change.path]['file']) else 'planned'}
            for change in self.changes]
        self.report['results'] += [{'path': str(op.root), 'owner': 'native-plugins', 'action': 'native_plugin', 'status': 'unchanged' if self._native_unchanged(op) else 'planned'} for op in self.native_operations]
        progress(f'{progress_label}完成：{len(self.changes)} 项')

    def _validate_all(self):
        for change in self.changes:
            change.validator(change.content)

    @staticmethod
    def _native_unchanged(operation):
        if hasattr(operation, 'original'):
            return False
        try:
            return getattr(operation, 'unchanged', lambda: False)()
        except (ValueError, OSError):
            # Invalid native state is already explained by planner checks. A
            # display optimization must not replace that report with an error.
            return False

    def _run_guards(self):
        for guard in self.guards:
            guard()

    def _snapshot(self, path):
        self.paths[path] = {'file': file_fingerprint(path), 'resolved': str(path.resolve())}

    @staticmethod
    def _unchanged(change, before):
        if change.delete:
            return not before['exists']
        return before.get('sha256') == hashlib.sha256(change.content).hexdigest() and (change.mode is None or before.get('mode') == change.mode)

    def _recheck(self):
        for path, snapshot in self.paths.items():
            try:
                current = {'file': file_fingerprint(path), 'resolved': str(path.resolve())}
            except (ValueError, OSError) as exc:
                raise PlanStale('Input can no longer be checked', path=path, expected=snapshot) from exc
            if snapshot != current:
                raise PlanStale('Input changed', path=path, expected=snapshot, actual=current)
        for root, expected in self.directories.items():
            try:
                current = directory_fingerprint(root)
            except (ValueError, OSError) as exc:
                raise PlanStale('Resource directory cannot be checked', path=root, expected=fingerprint(expected)) from exc
            if current != expected:
                raise PlanStale('Resource directory changed', path=root, expected=fingerprint(expected), actual=fingerprint(current))

    def _recheck_target(self, path):
        path = Path(path).absolute()
        expected = self.paths[path]
        current = None
        try:
            current = {'file': file_fingerprint(path), 'resolved': str(path.resolve())}
            aliases = [(alias, self.paths[alias]) for alias in self.aliases[expected['resolved']]]
            if current != expected or any(str(alias.resolve()) != snapshot['resolved'] or snapshot != current
                                          for alias, snapshot in aliases):
                raise PlanStale('Target changed', path=path, expected=expected, actual=current)
        except (ValueError, OSError) as exc:
            raise PlanStale('Target changed', path=path, expected=expected, actual=current) from exc

    def apply(self, state_dir, *, writer=atomic_write):
        if not self.prepared:
            raise ValueError('A declaration-only child plan cannot execute')
        if self.blockers:
            raise ValueError('Preflight blockers prevent all writes')
        root = private_dir(state_dir).resolve()
        ignore_local_state(root)
        locks = private_dir(root / 'locks')
        # A user-wide lock also covers two state directories bound to the same
        # native target, without one descriptor per file.
        global_root = private_dir(Path('/tmp').resolve() / ('claude-config-execution-' + str(os.getuid())))
        progress('等待配置写入锁')
        with interruption_handlers(), lock(global_root / 'execution.lock'), lock(locks / 'execution.lock'):
            self._recheck()
            if self.report.get('operation') not in ('rollback', 'reconcile-run') and self.scopes:
                pending = unfinished(root, self.scopes) + registered_pending(global_root, self.scopes)
                if pending:
                    raise UnfinishedRun(pending)
            progress('重新检查配置和来源；尚未写入')
            self._run_guards()
            self._validate_all()
            run_dir = private_dir(root / 'runs' / uuid.uuid4().hex)
            journal = {'schema_version': 2, 'kind': 'profile-file-run',
                       'plan_id': self.report['plan_id'], 'created_at': datetime.now(timezone.utc).isoformat(),
                       'status': 'running', 'scopes': list(self.scopes), 'operations': []}
            events = JournalWriter(run_dir / 'journal.json', journal, atomic_write)
            self.report['recovery'] = str(run_dir / 'journal.json')
            self.report['results'] = journal['operations']
            progress('运行 ' + run_dir.name + '；恢复记录：' + self.report['recovery'])
            try:
                register_run(global_root, self.scopes, run_dir / 'journal.json', atomic_write)
                for index, change in enumerate(self.steps):
                    if index % 25 == 0 or not isinstance(change, FileChange):
                        progress(f'执行并校验：{index + 1}/{len(self.steps)}')
                    if not isinstance(change, FileChange):
                        self._recheck()
                        self._run_guards()
                        if self.step_guard is not None:
                            self.step_guard(change)
                        change.preflight()
                        if getattr(change, 'unchanged', lambda: False)() and not hasattr(change, 'original'):
                            journal['operations'].append({'step': index, 'path': str(change.root), 'owner': 'native-plugins',
                                                          'action': 'native_plugin', 'status': 'unchanged'})
                            continue
                        row = change.capture(run_dir, index)
                        row['step'] = index
                        journal['operations'].append(row)
                        events.operation(row)
                        change.checkpoint = events.operation
                        try:
                            change.perform(row, run_dir)
                        finally:
                            change.observe(row)
                            events.operation(row)
                        change.verify(row)
                        if self.step_guard is not None:
                            self.step_guard(change)
                        row['status'] = 'applied'
                        affected = {str(p.resolve()) for p in change.paths}
                        for path in list(self.paths):
                            if self.paths[path]['resolved'] in affected:
                                self._snapshot(path)
                        for directory in change.directories:
                            self.directories[Path(directory).absolute()] = directory_fingerprint(directory)
                        self._recheck()
                        self._run_guards()
                        events.operation(row)
                        continue
                    self._recheck_target(change.path)
                    before = self.paths[change.path]['file']
                    unchanged = self._unchanged(change, before)
                    row = {'step': index, 'path': str(change.path), 'owner': change.owner, 'before': before,
                           'action': 'delete' if change.delete else 'write',
                           'after_sha256': None if change.delete else hashlib.sha256(change.content).hexdigest(),
                           'status': 'unchanged' if unchanged else 'planned'}
                    journal['operations'].append(row)
                    if unchanged:
                        continue
                    if self.step_guard is not None:
                        self.step_guard(change)
                    if before['exists']:
                        backup = run_dir / (str(index) + '.before')
                        atomic_write(backup, change.path.read_bytes())
                        row['backup'] = str(backup)
                    row['status'] = 'started'
                    mode = change.mode if change.mode is not None else before.get('mode', 0o600)
                    row['expected_after'] = {'exists': False} if change.delete else {'exists': True, 'sha256': row['after_sha256'], 'mode': mode}
                    events.operation(row)
                    if change.delete:
                        change.path.unlink()
                        sync_directory(change.path.parent)
                    else:
                        writer(change.path, change.content, mode)
                    current = file_fingerprint(change.path)
                    if current != row['expected_after']:
                        raise RuntimeError('Written content did not verify: ' + str(change.path))
                    change.validator(b'' if change.delete else change.path.read_bytes())
                    row['after'] = current
                    row['status'] = 'applied'
                    resolved = str(change.path.resolve())
                    for path in self.aliases[resolved]:
                        if str(path.resolve()) != resolved:
                            raise PlanStale('File alias changed during execution', path=path)
                        self.paths[path] = {'file': current, 'resolved': resolved}
                    for directory, snapshot in self.directories.items():
                        if change.path.is_relative_to(directory):
                            relative = change.path.relative_to(directory).as_posix()
                            if change.delete:
                                snapshot['entries'].pop(relative, None)
                            else:
                                snapshot['entries'][relative] = current
                                snapshot['exists'] = True
                    events.operation(row)
                    if self.step_guard is not None:
                        self.step_guard(change)
                self._recheck()
                self._run_guards()
                # Checkpoint failure is an execution failure, and must still
                # enter recovery rather than claim a successful installation.
                events.status('applied')
                events.checkpoint()
                register_run(global_root, self.scopes, run_dir / 'journal.json', atomic_write, remove=True)
            except (Exception, KeyboardInterrupt) as exc:
                progress('执行未完成，正在恢复本次已写入的内容')
                journal['error_type'] = type(exc).__name__
                recovery_stopped = False
                for row in reversed(journal['operations']):
                    if row['status'] not in ('started', 'applied'):
                        continue
                    if recovery_stopped:
                        row['status'] = 'recovery_conflict'
                    else:
                        try:
                            if self.recovery_guard is not None:
                                self.recovery_guard()
                            if row.get('kind') == 'native-plugin':
                                operation = next(op for op in self.native_operations if op.identifier == row['operation_id'])
                                operation.restore(row)
                            else:
                                self._restore(row)
                        except Exception:
                            row['status'] = 'recovery_conflict'
                    recovery_stopped = row['status'] == 'recovery_conflict'
                    try:
                        events.operation(row)
                    except OSError:
                        pass  # Preserve live evidence and attempt final checkpoint.
                journal['status'] = 'failed'
                try:
                    events.status('failed', error_type=type(exc).__name__)
                    events.checkpoint()
                    if not any(row['status'] in ('started', 'applied', 'recovery_conflict') for row in journal['operations']):
                        register_run(global_root, self.scopes, run_dir / 'journal.json', atomic_write, remove=True)
                except OSError:
                    pass
                self.report['results'] = journal['operations']
                raise
            progress(f'执行完成：{len(self.steps)} 项；正在生成报告')
            return self.report

    @staticmethod
    def _restore(row):
        path = Path(row['path'])
        try:
            current = file_fingerprint(path)
            if row.get('after', row.get('expected_after')) == current:
                if row['before']['exists']:
                    content = Path(row['backup']).read_bytes()
                    if hashlib.sha256(content).hexdigest() != row['before']['sha256']:
                        raise ValueError('Recovery backup fingerprint changed')
                    atomic_write(path, content, row['before']['mode'])
                else:
                    path.unlink()
                    sync_directory(path.parent)
                row['status'] = 'rolled_back'
            elif current == row['before']:
                row['status'] = 'failed'  # Write never happened.
            else:
                row['status'] = 'recovery_conflict'
        except (ValueError, OSError):
            row['status'] = 'recovery_conflict'


def check_ownership(claims):
    """Disjoint leaves in a shared file are valid; parents/children overlap."""
    seen = []
    for file, path, owner in claims:
        file, path = str(Path(file).resolve()), tuple(path)
        for old_file, old_path, old_owner in seen:
            if file == old_file and (path[:len(old_path)] == old_path or old_path[:len(path)] == path):
                raise ValueError('Overlapping field ownership: ' + old_owner + ' / ' + owner)
        seen.append((file, path, owner))
