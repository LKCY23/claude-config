"""Scoped CLI progress output."""
from contextlib import contextmanager
from contextvars import ContextVar
import time


_progress = ContextVar('profile_progress', default=None)


@contextmanager
def progress_output(stream):
    started = time.monotonic()
    def write(message):
        print(f'[{time.monotonic() - started:.1f}s] {message}', file=stream, flush=True)
    token = _progress.set(write)
    try:
        yield
    finally:
        _progress.reset(token)


def progress(message):
    callback = _progress.get()
    if callback is not None:
        callback(message)
