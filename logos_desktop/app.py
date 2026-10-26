
import abc
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import threading
import time
from typing import NoReturn, Optional

from logos_desktop import constants
from logos_desktop.constants import (
    PROMPT_OPTION_DIRECTORY,
    PROMPT_OPTION_FILE,
    PROMPT_OPTION_NEW_FILE
)


class UserExitedFromAsk(Exception):
    """Exception thrown when the user hit cancel in an ask dialog"""


class App(abc.ABC):
    # FIXME: consider weighting install steps. Different steps take different lengths
    installer_step_count: int = 0
    """Total steps in the installer, only set the installation process has started."""
    installer_step: int = 1
    """Step the installer is on. Starts at 0"""

    _threads: list[threading.Thread]
    """List of threads

    Non-daemon threads will be joined before shutdown
    """
    _pending_exit: Optional[tuple[str, bool]] = None
    """(reason, intended) handed from a worker thread to the main-thread loop.

    Set when exit() is called off the main thread; read by the CLI/TUI polling
    loops so they can re-invoke exit() on the main thread.
    """
    _last_status: Optional[str] = None
    """The last status we had"""
    def __init__(self, config, **kwargs) -> None:
        # This lazy load is required otherwise these would be circular imports
        from logos_desktop.config import Config
        from logos_desktop.logos import LogosManager

        self._closing = threading.Event()
        self._closed = False
        self._thread_lock = threading.RLock()
        self._hook_lock = threading.RLock()
        self.config_updated_hooks = []
        self._config_updated_event = threading.Event()
        self._threads = []
        self._pending_exit: Optional[tuple[str, bool]] = None
        self._profile_lock = threading.Lock()
        self._instance_session = getattr(config, '_instance_session', None)
        self.conf = Config(config, self)
        if self._instance_session is not None:
            from logos_desktop import instance, maintenance

            if instance.Session.for_config(self.conf).key != self._instance_session.key:
                raise maintenance.MaintenanceError(
                    "The selected installation changed during startup. Restart the controller to select it safely")
        self.logos = LogosManager(app=self)
        if self._instance_session is not None:
            self._instance_session._application = self

        def _config_updated_hook_runner():
            while not self._closing.is_set():
                self._config_updated_event.wait()
                self._config_updated_event.clear()
                with self._hook_lock:
                    hooks = tuple(self.config_updated_hooks)
                for hook in hooks:
                    if self._closing.is_set():
                        return
                    try:
                        self.dispatch_config(hook)
                    except Exception:
                        logging.exception("Failed to run config update hook")
        _config_updated_hook_runner.__name__ = "Config Update Hook"
        self.start_thread(_config_updated_hook_runner, daemon_bool=True)

    @property
    def conf(self):
        return self._conf

    @conf.setter
    def conf(self, configuration):
        from logos_desktop.config import Config

        # Each frontend-owned Config enters controller admission before its
        # existing writer takes the configuration lock. Keep candidate identity.
        if (getattr(self, '_instance_session', None) is not None and isinstance(configuration, Config)
                and getattr(configuration, '_controller_owner', None) is not self):
            write = configuration._change

            def change(*, legacy_changes=None, **changes):
                return self._configuration_change(configuration, write, changes, legacy_changes)

            configuration._change = change
            configuration._controller_owner = self
        self._conf = configuration

    def _configuration_change(self, configuration, write, changes, legacy_changes):
        """Reserve a newly configured prefix before the configuration writer lock."""
        from logos_desktop import config_store, instance
        from logos_desktop.config import EphemeralConfiguration, PersistentConfiguration

        session = getattr(self, '_instance_session', None)
        selection_fields = {'install_dir', 'INSTALLDIR', 'WINEPREFIX'}
        if (session is None or configuration is not self.conf
                or not selection_fields.intersection(changes.keys() | (legacy_changes or {}).keys())):
            return write(legacy_changes=legacy_changes, **changes)

        path = configuration.config_file_path
        document = config_store.apply_changes(configuration._raw._saved, legacy_changes or {}, path)
        document = config_store.apply_changes(document, changes, path)
        old = configuration._overrides
        overrides = EphemeralConfiguration.project(
            document, path, destination=old.config_destination, environment=old.environment,
            cli_overrides=old.cli_overrides, runtime_selection=old.runtime_selection)
        for name in ('assume_yes', 'quiet', 'agreed_to_faithlife_terms', 'app_run_as_root_permitted', 'wine_args'):
            setattr(overrides, name, getattr(old, name))
        candidate = type(configuration)(overrides, self)
        raw = PersistentConfiguration.from_document(document, old.environment)
        raw._file_path = path
        raw._file_bytes = configuration._raw._file_bytes
        candidate.rebind(raw)
        if instance.Session.for_config(candidate).key == session.key:
            return write(legacy_changes=legacy_changes, **changes)

        with self.profile_selection(candidate):
            result = write(legacy_changes=legacy_changes, **changes)
            candidate.rebind(configuration._raw)
        return result

    @contextmanager
    def profile_selection(self, candidate):
        """Reserve both profiles until publication succeeds or the body unwinds."""
        from logos_desktop import instance, maintenance
        from logos_desktop.logos import LogosManager

        if not self._profile_lock.acquire(blocking=False):
            raise maintenance.MaintenanceBusy("A profile change is already underway")
        destination = None
        old_manager = self.logos
        old_session = self._instance_session
        monitoring = old_manager._monitor_thread is not None and old_manager._monitor_thread.is_alive()
        try:
            if self._closing.is_set():
                raise maintenance.MaintenanceBusy("Controller is closing")
            destination = instance.Session.for_config(candidate)
            if old_session is not None and destination.key == old_session.key:
                destination = old_session
            else:
                destination.acquire()
                if old_session is not None and old_session.listening:
                    destination.listen(activation_source=old_session)

            if not old_manager.close():
                old_manager._closed.clear()
                old_manager._watcher_stopping = False
                if monitoring and not self._closing.is_set():
                    old_manager.start_monitoring()
                raise maintenance.MaintenanceBusy("The selected installation is still settling")
            try:
                yield candidate
                if instance.Session.for_config(candidate).key != destination.key:
                    raise maintenance.MaintenanceError(
                        "The selected installation changed during profile selection. "
                        "The previous profile remains selected. Published files and later edits were preserved")
            except BaseException:
                # close() detaches observers, it never stops the product.
                old_manager._closed.clear()
                old_manager._watcher_stopping = False
                if monitoring and not self._closing.is_set():
                    old_manager.start_monitoring()
                raise

            published = destination
            self._instance_session = published
            destination = None
            try:
                self.conf = candidate
                published._application = self
                candidate._overrides._instance_session = published
                if monitoring:
                    self.logos = LogosManager(app=self)
                    if not self._closing.is_set():
                        self.logos.start_monitoring()
                else:
                    old_manager._context = None
                    old_manager._expected = ()
                    old_manager._snapshot = []
                    old_manager._closed.clear()
                    old_manager._watcher_stopping = False
            finally:
                # Publication has succeeded. Even a monitoring startup failure
                # must leave the selected destination owned until shutdown.
                if old_session is not None and old_session is not published:
                    old_session.close()
            self._config_updated_event.set()
        finally:
            if destination is not None and destination is not old_session:
                destination.close()
            self._profile_lock.release()

    def dispatch_config(self, callback):
        callback()

    def subscribe_config(self, callback):
        """Return a removable subscription owned by this application."""
        def guarded():
            with self._hook_lock:
                active = guarded in self.config_updated_hooks and not self._closing.is_set()
            if active:
                callback()

        with self._hook_lock:
            if self._closing.is_set():
                raise RuntimeError("Application is closing")
            self.config_updated_hooks.append(guarded)

        def unsubscribe():
            with self._hook_lock:
                if guarded in self.config_updated_hooks:
                    self.config_updated_hooks.remove(guarded)
        return unsubscribe

    def request_close(self, reason="Controller closed", intended=True):
        if self._pending_exit is None or not intended:
            self._pending_exit = (reason, intended)
        self._closing.set()
        self._config_updated_event.set()

    def close(self, timeout=5):
        """Detach the application after its workers reach a safe boundary.

        False means a worker still owns work. Frontends keep their event loop
        alive and retry instead of abandoning an active mutation.
        """
        if self._closed:
            return True
        self.request_close()
        if self._profile_lock.locked():
            return False
        deadline = time.monotonic() + timeout
        if not self.logos.close(timeout=max(0, deadline - time.monotonic())):
            return False
        with self._hook_lock:
            self.config_updated_hooks.clear()
        with self._thread_lock:
            workers = tuple(self._threads)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))
        with self._thread_lock:
            if any(worker.is_alive() for worker in self._threads):
                return False
        self._closed = True
        if self._instance_session is not None:
            self._instance_session.close()
        return True

    def checkpoint(self):
        """Frontends can cancel preparation between completed installation phases."""
        if self._closing.is_set():
            raise UserExitedFromAsk

    def observe_manager(self, manager, message="", percent=None):
        if message:
            self.status(message, percent)

    def ask(self, question: str, options: list[str]) -> str:
        """Return a canonical choice or validated path, or unwind cancellation."""
        if not options:
            raise ValueError("No choices are available for this question")

        def validate_result(answer: str, options: list[str]) -> Optional[str]:
            simple_options = [option for option in options if option not in constants.PROMPT_OPTION_SIGILS]
            if answer in simple_options:
                return answer
            for option in simple_options:
                if option.casefold() == answer.casefold():
                    return option

            if not answer.strip():
                return None
            path = Path(answer).expanduser()
            if PROMPT_OPTION_FILE in options and path.is_file():
                return str(path)
            if PROMPT_OPTION_DIRECTORY in options and path.is_dir():
                return str(path)
            if (
                PROMPT_OPTION_NEW_FILE in options
                and not path.is_dir()
                and path.parent.is_dir()
            ):
                return str(path)
            return None

        if self.conf._overrides.assume_yes:
            for option in options:
                if option not in constants.PROMPT_OPTION_SIGILS:
                    return option

        passed_options: list[str] | str = options
        if len(options) == 1 and options[0] in constants.PROMPT_OPTION_SIGILS:
            passed_options = options[0]
        elif self._exit_option is not None:
            passed_options = options + [self._exit_option]

        prompt = question
        while True:
            answer = self._ask(prompt, passed_options)
            if answer is None or (self._exit_option and answer.casefold() == self._exit_option.casefold()):
                raise UserExitedFromAsk
            result = validate_result(answer, options)
            if result is not None:
                return result
            prompt = f"That response is not valid, please try again.\n{question}"

    def approve_or_exit(self, question: str, context: Optional[str] = None) -> bool:
        """Asks the user a question, if they refuse, shutdown"""
        if not self.approve(question, context):
            self.exit(f"User refused the prompt: {question}")
        return True

    def approve(self, question: str, context: Optional[str] = None) -> bool:
        """Asks the user a y/n question"""
        question = (f"{context}\n\n" if context else "") + question
        options = ["Yes", "No"]
        return self.ask(question, options) == "Yes"

    def info(self, message: str, context: Optional[str] = None) -> None:
        """Shares information with the user"""
        message = (f"{context}\n" if context else "") + message
        self._info(message)

    def _exit(self, reason: str, intended: bool = False) -> None:
        """Cleanup any lingering objects, per-implementation specific"""

    def _schedule_exit_on_main_thread(self, reason: str, intended: bool) -> None:
        """Wake/redirect the main thread so it runs exit().

        Called on a worker thread when exit() was invoked off the main thread.
        Frontends with their own main loop override this with the appropriate
        thread-safe mechanism, such as a queued Qt signal or a terminal event.
        The default interrupts the main thread, which most blocking main loops
        surface as a KeyboardInterrupt.
        """
        import _thread
        _thread.interrupt_main()

    def exit(self, reason: str, intended: bool = False) -> NoReturn:
        """Request frontend shutdown and unwind the current operation."""
        self.request_close(reason, intended)
        if threading.current_thread() is not threading.main_thread():
            self._schedule_exit_on_main_thread(reason, intended)
            raise SystemExit
        self._exit(reason, intended)
        if not self.close():
            raise RuntimeError("Application workers are still finishing")
        raise SystemExit(0 if intended else 1)

    _exit_option: Optional[str] = "Exit"

    @abc.abstractmethod
    def _ask(self, question: str, options: list[str] | str) -> Optional[str]:
        """Implementation for asking a question per-front end

        Options may include ability to prompt for an additional value.
        Such as asking for one of strings or a directory.
        If the user selects choose a new directory, the
        implementations MUST handle the follow up prompt before returning

        Options may be a single value,
        Implementations MUST handle this single option being a follow up prompt
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def _info(self, message: str) -> None:
        """Implementation for displaying information to the user per-front end
        """
        raise NotImplementedError()

    def is_installed(self) -> bool:
        """Returns whether the install was successful by
        checking if the installed exe exists and is executable"""
        if self.conf.logos_exe is not None:
            return os.access(self.conf.logos_exe, os.X_OK)
        return False

    def status(self, message: str, percent: Optional[int | float] = None):
        """A status update
        
        Args:
            message: str - message to send to user
            percent: Optional[int] - percent of the way through the current install step
                (if installing)
        """
        # Check to see if we want to suppress all output
        if self.conf._overrides.quiet:
            return

        if isinstance(percent, float):
            percent = round(percent * 100)
        # If we're installing
        if self.installer_step_count != 0:
            if percent is None:
                self._status(message, None)
                self._last_status = message
                return
            current_step_percent = percent or 0
            # We're further than the start of our current step, percent more
            installer_percent = round((self.installer_step * 100 + current_step_percent) / self.installer_step_count)
            logging.debug(f"Install {installer_percent}%: {message}")
            self._status(message, percent=installer_percent)
        else:
            # Otherwise just print status using the progress given
            logging.debug(f"{message}: {percent}%")
            self._status(message, percent)
        self._last_status = message

    # FIXME: This implementation is overridden in every implementation
    # Perhaps this should just raise NotImplementedError to avoid confusion?
    @abc.abstractmethod
    def _status(self, message: str, percent: Optional[int] = None):
        """Implementation for updating status per-front end
        
        Args:
            message: str - message to send to user
            percent: Optional[int] - percent complete of the current overall operation
                if None that signifies we can't track the progress.
                Feel free to implement a spinner
        """

        def get_progressbar(pct, w=10, suffix=''):
            suffix = f" {int(pct):>3}%"
            # end = '\r'

            ci = '\u23b8'
            cf = '\u23b9'
            d = '█'
            u = ' '
            d_ct = min(int(w*pct/100), w)
            u_ct = min(int(w - d_ct - 1), w - 1)

            bar = ''
            if d_ct == 0:
                bar += ci + u*(u_ct - 1) + cf
            elif d_ct < w:
                bar += str(d*d_ct) + str(u*u_ct) + cf
            else:
                bar += str(d*d_ct)
            bar += suffix #+ end
            return bar

        # De-dup
        if message != self._last_status:
            bar = ''
            if percent is not None:
                bar = f"{get_progressbar(percent)}"
            if message.endswith("\r"):
                print(f"{bar}{message}", end="\r")
            else:
                print(f"{message}")
                if bar:
                    print(bar)

    def pop_up(self, title: str, message: str):
        """An intrusive message requiring user attention
        
        Args:
            title: str - title of the alert
            message: str - message to send to user
        """
        # Check to see if we want to suppress all output
        if self.conf._overrides.quiet:
            return

        self._pop_up(title=title,message=message)

    # FIXME: This implementation is overridden in every implementation
    # Perhaps this should just raise NotImplementedError to avoid confusion?
    @abc.abstractmethod
    def _pop_up(self, title: str, message: str):
        """Implementation for opening a pop up per-front end
        
        Args:
            title: str - title of the alert
            message: str - message to send to user
        """
        # Fallback to just sending a status if pop up isn't implemented.
        self._status(message=message)

    @property
    def superuser_command(self) -> str:
        """Command when root privileges are needed.

        Raises:
            SuperuserCommandNotFound

        May be sudo or pkexec for example"""
        from logos_desktop.system import get_superuser_command
        return get_superuser_command()

    def start_thread(self, task, *args, daemon_bool: bool = False, **kwargs):
        """Own a worker until its final cleanup, including failure."""
        # Some lightweight frontend adapters supply their own configuration.
        lock = self.__dict__.setdefault("_thread_lock", threading.RLock())

        def run():
            try:
                task(*args, **kwargs)
            finally:
                with lock:
                    self._threads.remove(thread)

        thread = threading.Thread(
            name=f"{constants.APP_NAME} {getattr(task, '__name__', 'worker')}",
            target=run,
            daemon=daemon_bool,
        )
        with lock:
            closing = getattr(self, "_closing", None)
            if closing is not None and closing.is_set() and threading.current_thread() not in self._threads:
                raise RuntimeError("Application is closing")
            self._threads.append(thread)
            try:
                thread.start()
            except BaseException:
                self._threads.remove(thread)
                raise
        return thread
