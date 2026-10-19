
import abc
import logging
import os
from pathlib import Path
import threading
import time
from typing import NoReturn, Optional

from ou_dedetai import constants
from ou_dedetai.constants import (
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
        from ou_dedetai.config import Config
        from ou_dedetai.logos import LogosManager

        self._closing = threading.Event()
        self._closed = False
        self._thread_lock = threading.RLock()
        self._hook_lock = threading.RLock()
        self.config_updated_hooks = []
        self._config_updated_event = threading.Event()
        self._threads = []
        self._pending_exit: Optional[tuple[str, bool]] = None
        self.conf = Config(config, self)
        self.logos = LogosManager(app=self)

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
        return True

    def ask(self, question: str, options: list[str]) -> str:
        """Asks the user a question with a list of supplied options

        Returns the option the user picked.

        If the internal ask function returns None, the process will exit with 1
        """
        def validate_result(answer: str, options: list[str]) -> Optional[str]:
            special_cases = set(constants.PROMPT_OPTION_SIGILS)
            # These constants have special meaning, don't worry about them to start with
            simple_options = list(set(options) - special_cases)
            # This MUST have the same indexes as above
            simple_options_lower = [opt.lower() for opt in simple_options]

            # Case sensitive check first
            if answer in simple_options:
                return answer
            # Also do a case insensitive match, no reason to fail due to casing
            if answer.lower() in simple_options_lower:
                # Return the correct casing to simplify the parsing of the ask result
                return simple_options[simple_options.index(answer.lower())]
            
            # Now check the special cases
            if PROMPT_OPTION_FILE in options and Path(answer).is_file():
                return answer
            if PROMPT_OPTION_DIRECTORY in options and Path(answer).is_dir():
                return answer
            if (
                PROMPT_OPTION_NEW_FILE in options
                and not Path(answer).is_dir()
                and Path(answer).parent.is_dir()
            ):
                return answer

            # Not valid
            return None

        # Check to see if we're supposed to prompt the user
        if self.conf._overrides.assume_yes:
            # Get the first non-dynamic option
            for option in options:
                if option not in [PROMPT_OPTION_DIRECTORY, PROMPT_OPTION_FILE]:
                    return option

        passed_options: list[str] | str = options
        if len(passed_options) == 1 and passed_options[0] in constants.PROMPT_OPTION_SIGILS: 
            # Set the only option to be the follow up prompt
            passed_options = options[0]
        elif passed_options is not None and self._exit_option is not None:
            passed_options = options + [self._exit_option]

        answer = self._ask(question, passed_options)
        if answer is None or answer == self._exit_option:
            raise UserExitedFromAsk
        while answer is None or validate_result(answer, options) is None:
            invalid_response = "That response is not valid, please try again."
            new_question = f"{invalid_response}\n{question}"
            answer = self._ask(new_question, passed_options)
            if answer is None or answer == self._exit_option:
                raise UserExitedFromAsk

        if answer is not None:
            answer = validate_result(answer, options)
            if answer is None:
                # Huh? coding error, this should have been checked earlier
                logging.critical("An invalid response slipped by, please report this incident to the developers") 
                self.exit("Failed to get a valid value from user")

        return answer

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
        thread-safe mechanism (e.g. tk's root.after, or poking a polled queue).
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
        from ou_dedetai.system import get_superuser_command
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
