"""Main-thread delivery and bounded progress for owned application workers."""

from dataclasses import dataclass, field
import logging
import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot


@dataclass(frozen=True)
class Ticket:
    number: int
    generation: int


@dataclass(frozen=True)
class Progress:
    ticket: Ticket
    message: str
    percent: int | None = None


@dataclass(frozen=True)
class Result:
    outcome: str
    message: str
    details: str = ""

    def __post_init__(self):
        if self.outcome not in {"succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError("An operation needs a checked terminal outcome")


@dataclass(frozen=True)
class Completion:
    ticket: Ticket
    result: Result


@dataclass(eq=False)
class Prompt:
    question: str
    options: tuple[str, ...] | str
    done: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def complete(self, answer=None):
        with self._lock:
            if self.done.is_set():
                return False
            self.answer = answer or None
            self.done.set()
            return True


class Bridge(QObject):
    initialized = Signal()
    started = Signal(object)
    completed = Signal(object)
    progress = Signal(object)
    prompt = Signal(object)
    cancel_prompt = Signal(object)
    information = Signal(str, str)
    close_requested = Signal(str, bool)
    _wake = Signal()
    _completion = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._latest = None
        self._callbacks = {}
        self._pending = False
        self._disposed = False
        self._wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)
        self._completion.connect(self._deliver_completion, Qt.ConnectionType.QueuedConnection)

    def post(self, callback=None, progress=None):
        with self._lock:
            if self._disposed:
                return
            if callback is not None:
                self._callbacks[callback] = None
            if progress is not None:
                self._latest = progress
            if self._pending:
                return
            self._pending = True
        self._wake.emit()

    def finish(self, completion):
        self._completion.emit(completion)

    @Slot(object)
    def _deliver_completion(self, completion):
        if not self._disposed:
            self.completed.emit(completion)

    @Slot()
    def _drain(self):
        with self._lock:
            callbacks = tuple(self._callbacks)
            latest = self._latest
            self._callbacks.clear()
            self._latest = None
            self._pending = False
            if self._disposed:
                return
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logging.exception("Qt configuration callback failed")
        if latest is not None:
            self.progress.emit(latest)

    def dispose(self):
        with self._lock:
            self._disposed = True
            self._callbacks.clear()
            self._latest = None
