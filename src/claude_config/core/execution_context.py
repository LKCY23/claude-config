"""Scoped CLI progress output."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import threading


_progress = ContextVar('profile_progress', default=None)


@contextmanager
def progress_output(stream):
    started = time.monotonic()
    stopped = threading.Event()
    latest = [None, started]
    def write(message):
        latest[:] = [message, time.monotonic()]
        if message is None:
            return
        print(f'[{time.monotonic() - started:.1f}s] {message}', file=stream, flush=True)
    def heartbeat():
        while not stopped.wait(5):
            if latest[0] and time.monotonic() - latest[1] >= 5:
                print(f'[{time.monotonic() - started:.1f}s] 执行仍在运行；当前阶段：{latest[0]}', file=stream, flush=True)
    token = _progress.set(write)
    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)
        _progress.reset(token)


def progress(message):
    callback = _progress.get()
    if callback is not None:
        callback(message)
