"""App interaction adapter with cooperative operation and prompt ownership."""

import logging
import threading

from ou_dedetai.app import App, UserExitedFromAsk
from .bridge import Completion, Progress, Prompt, Result, Ticket


class QtApp(App):
    _exit_option = None

    def __init__(self, config, bridge):
        self.bridge = bridge
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.initialization_error = ""
        self.active = None
        self.generation = 0
        self._serial = 0
        self._cancel = threading.Event()
        self._prompts = set()
        self._prompt_lock = threading.Lock()
        self.bridge.completed.connect(self._completed)
        self._bootstrap = threading.Thread(
            target=self._initialize, args=(config,), name="LogosDesktop Qt initialization",
        )
        self._bootstrap.start()

    def _initialize(self, config):
        try:
            super().__init__(config)
            if self.stop_requested.is_set():
                super().request_close()
        except Exception as error:
            logging.exception("Qt application initialization failed")
            self.initialization_error = str(error) or type(error).__name__
        finally:
            self.ready.set()
            self.bridge.initialized.emit()

    def dispatch_config(self, callback):
        self.bridge.post(callback)

    def run_operation(self, operation):
        if (
            not self.ready.is_set() or self.initialization_error
            or self.stop_requested.is_set() or self.active is not None
        ):
            return False
        self._serial += 1
        ticket = Ticket(self._serial, self.generation)
        self.active = ticket
        self._cancel = threading.Event()
        cancel = self._cancel
        self.bridge.started.emit(ticket)

        def run():
            try:
                result = operation(self, cancel)
                if not isinstance(result, Result):
                    raise RuntimeError("Operation did not return a checked result")
                if cancel.is_set() and result.outcome == "succeeded":
                    result = Result("cancelled", "Cancellation completed after work settled")
            except UserExitedFromAsk:
                result = Result("cancelled", "Operation cancelled")
            except SystemExit:
                intended = getattr(self, "_pending_exit", None)
                state = "cancelled" if intended and intended[1] else "failed"
                result = Result(state, intended[0] if intended else "Operation exited without a result")
            except Exception as error:
                logging.exception("Qt operation failed")
                detail = str(error) or type(error).__name__
                result = Result("failed", "The operation could not be completed", detail)
            self.bridge.finish(Completion(ticket, result))

        try:
            self.start_thread(run)
        except Exception as error:
            self.bridge.finish(Completion(ticket, Result("failed", "Cannot start operation", str(error))))
        return True

    def _completed(self, completion):
        if completion.ticket == self.active:
            self.active = None

    def change_context(self):
        if self.active is not None:
            raise RuntimeError("Wait for the current operation before changing installation")
        self.generation += 1

    def cancel_operation(self):
        self._cancel.set()
        self._cancel_prompts()

    def _cancel_prompts(self):
        with self._prompt_lock:
            prompts = tuple(self._prompts)
        for prompt in prompts:
            prompt.complete()
            self.bridge.cancel_prompt.emit(prompt)

    def _ask(self, question, options):
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("Blocking questions must run outside the Qt event thread")
        prompt = Prompt(question, tuple(options) if isinstance(options, list) else options)
        with self._prompt_lock:
            if self.stop_requested.is_set() or self._cancel.is_set():
                raise UserExitedFromAsk
            self._prompts.add(prompt)
        try:
            self.bridge.prompt.emit(prompt)
            while not prompt.done.wait(.05):
                if self.stop_requested.is_set() or self._cancel.is_set():
                    prompt.complete()
                    self.bridge.cancel_prompt.emit(prompt)
            return prompt.answer
        finally:
            with self._prompt_lock:
                self._prompts.discard(prompt)

    def _info(self, message):
        self.bridge.information.emit("Information", message)

    def _pop_up(self, title, message):
        self.bridge.information.emit(title, message)

    def _status(self, message, percent=None):
        ticket = self.active
        if ticket is not None and not self.stop_requested.is_set():
            self.bridge.post(progress=Progress(ticket, message, percent))

    def request_close(self, reason="Controller closed", intended=True):
        self.stop_requested.set()
        self.cancel_operation()
        if self.ready.is_set() and not self.initialization_error:
            super().request_close(reason, intended)

    def _schedule_exit_on_main_thread(self, reason, intended):
        self.bridge.close_requested.emit(reason, intended)

    def exit(self, reason, intended=False):
        self.request_close(reason, intended)
        self._schedule_exit_on_main_thread(reason, intended)
        if threading.current_thread() is not threading.main_thread():
            raise SystemExit(0 if intended else 1)

    def close(self, timeout=0):
        self.request_close()
        if self._bootstrap.is_alive():
            self._bootstrap.join(timeout)
            if self._bootstrap.is_alive():
                return False
        if self.initialization_error:
            return True
        return super().close(timeout)
