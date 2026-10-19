"""Own bounded metadata and version-probe process groups through shutdown."""

from contextlib import contextmanager
import os
import selectors
import signal
import subprocess
import threading
import time


class CaptureError(RuntimeError):
    """An owned process did not complete within its acceptance boundary."""


_owner = threading.local()
_admission = threading.Lock()
_stopping = threading.Event()
_active = {}


@contextmanager
def owned(cancel):
    previous = getattr(_owner, "cancel", None)
    _owner.cancel = cancel
    try:
        yield
    finally:
        _owner.cancel = previous


def cancelled():
    cancel = getattr(_owner, "cancel", None)
    return _stopping.is_set() or (cancel is not None and cancel.is_set())


def capture(arguments, *, env, timeout, limit, label):
    deadline = time.monotonic() + timeout
    with _admission:
        if cancelled():
            raise CaptureError(f"{label} cancelled")
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, env=env, start_new_session=True)
        except OSError as error:
            raise CaptureError(f"Could not start {label} ({type(error).__name__})") from None
        done = _active[process] = threading.Event()
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if cancelled():
                    raise CaptureError(f"{label} cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CaptureError(f"{label} timed out")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > limit:
                        raise CaptureError(f"{label} exceeded its output limit")
        while process.poll() is None:
            if cancelled():
                raise CaptureError(f"{label} cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureError(f"{label} timed out")
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        return bytes(output), process.returncode
    except subprocess.SubprocessError as error:
        raise CaptureError(f"{label} did not complete ({type(error).__name__})") from None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        with _admission:
            _active.pop(process, None)
            done.set()


def shutdown():
    """Keep every metadata/probe supervisor alive until its children are reaped."""
    with _admission:
        _stopping.set()
        pending = list(_active.values())
    deadline = time.monotonic() + 3
    for done in pending:
        if not done.wait(max(0, deadline - time.monotonic())):
            raise CaptureError("Owned metadata or version probe did not stop")


def resume():
    with _admission:
        if not _stopping.is_set():
            return
        if _active:
            raise CaptureError("An owned metadata or version probe is still stopping")
        _stopping.clear()
