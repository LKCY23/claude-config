"""Scoped CLI progress output and pause-safe interactive input helpers."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import shutil
import threading
import time

from .terminal_ui import truncate_display

_progress = ContextVar('profile_progress', default=None)
_progress_tty = ContextVar('profile_progress_tty', default=False)


def _is_tty_stream(stream):
    isatty = getattr(stream, 'isatty', None)
    if not callable(isatty):
        return False
    try:
        if not isatty():
            return False
    except (OSError, ValueError):
        return False
    return os.environ.get('TERM', '').lower() not in ('', 'dumb', 'unknown')


def is_progress_tty():
    """Return True when the active progress stream renders single-line inline updates."""
    return bool(_progress_tty.get())


@contextmanager
def progress_output(stream, *, heartbeat_interval=5.0):
    started = time.monotonic()
    stopped = threading.Event()
    latest = [None, started]
    inline_active = [False]
    lock = threading.Lock()
    is_tty = _is_tty_stream(stream)

    def _clear_inline():
        if inline_active[0]:
            stream.write('\r\033[2K')
            stream.flush()
            inline_active[0] = False

    def _fit_line(text):
        cols = shutil.get_terminal_size((100, 24)).columns
        return truncate_display(text, max(24, cols - 1))

    def write(message):
        with lock:
            latest[:] = [message, time.monotonic()]
            if message is None:
                if is_tty:
                    _clear_inline()
                return
            line = f'[{time.monotonic() - started:.1f}s] {message}'
            if is_tty:
                stream.write('\r\033[2K' + _fit_line(line))
                stream.flush()
                inline_active[0] = True
            else:
                print(line, file=stream, flush=True)

    def heartbeat():
        while not stopped.wait(heartbeat_interval):
            with lock:
                if latest[0] and time.monotonic() - latest[1] >= heartbeat_interval:
                    elapsed = time.monotonic() - started
                    if is_tty:
                        line = _fit_line(f'[{elapsed:.1f}s] 仍在处理：{latest[0]}')
                        stream.write('\r\033[2K' + line)
                        stream.flush()
                        inline_active[0] = True
                    else:
                        print(f'[{elapsed:.1f}s] 执行仍在运行；当前阶段：{latest[0]}', file=stream, flush=True)

    token = _progress.set(write)
    tty_token = _progress_tty.set(is_tty)
    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)
        with lock:
            if is_tty:
                _clear_inline()
        _progress_tty.reset(tty_token)
        _progress.reset(token)


def progress(message):
    callback = _progress.get()
    if callback is not None:
        callback(message)


@contextmanager
def pause_progress():
    """Pause background progress/heartbeat cleanly before interactive terminal I/O."""
    progress(None)
    yield


def prompt_input(prompt=''):
    """Read interactive user input with progress heartbeat paused."""
    with pause_progress():
        return input(prompt)
