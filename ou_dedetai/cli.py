import os
import select
import shutil
import sys
import threading
from pathlib import Path
from typing import Optional

from ou_dedetai import constants
from ou_dedetai.app import App
from ou_dedetai.config import EphemeralConfiguration
from ou_dedetai.system import SuperuserCommandNotFound
from ou_dedetai.logos import State as LogosRunningState

from . import backup
from . import maintenance
from . import control
from . import installer
from . import wine
from . import utils


class CLI(App):
    def __init__(self, ephemeral_config: EphemeralConfiguration):
        super().__init__(ephemeral_config)
        self._prompt_lock = threading.Lock()
        self._input_failure: str | None = None
        self._stdin = sys.stdin

    def backup(self):
        backup.backup(app=self)

    def create_shortcuts(self):
        installer.create_launcher_shortcuts(self)

    def edit_config(self):
        control.edit_file(self.conf.config_file_path)

    def install_app(self):
        installer.install(self)
        self.exit("Install has finished", intended=True)

    def repair_app(self):
        from . import repair

        repair.repair_installation(self)

    def installation_status(self):
        from . import repair, runtime

        raw = self.conf._raw
        print(f"Configuration: {self.conf.config_file_path}")
        if not raw.install_dir or not raw.faithlife_product:
            print("Installation is not configured")
            return
        context = maintenance.Context.from_app(self, prompt=False)
        failure = repair.inspect(context)
        print(f"Product: {context.product}\nPrefix: {context.prefix}")
        print("Failed upgrade requires --repair" if failure else "No failed-upgrade layout detected")
        if context.needs_recovery() or runtime.pending(context.install):
            print("Pending recovery records are preserved. Use --repair before mutating this installation")

    def update_installed_app(self):
        utils.update_faithlife_product(self)
        self.exit("Update has finished", intended=True)

    def install_dependencies(self):
        utils.install_dependencies(app=self)

    def install_icu(self):
        wine.enforce_icu_data_files(self)

    def remove_index_files(self):
        control.remove_all_index_files(self)

    def uninstall(self):
        control.uninstall(self)

    def remove_library_catalog(self):
        control.remove_library_catalog(self)

    def restore(self):
        backup.restore(app=self)

    def run_indexing(self):
        run = self.logos.index()
        while run.result is None:
            if self._closing.wait(0.2):
                raise maintenance.MaintenanceError("Indexing observation cancelled")
        if run.result.outcome != "succeeded":
            raise maintenance.MaintenanceError(run.result.message)

    def run_installed_app(self):
        self.logos.start()
        # Keep the process running so that our background threads can keep running
        while self.logos.logos_state != LogosRunningState.STOPPED:
            # A worker thread (e.g. the memory watchdog) may have asked us to
            # exit; run it here on the main thread so the process truly stops.
            if self._pending_exit is not None:
                self.exit(*self._pending_exit)
            if self._closing.wait(0.2):
                break

    def stop_installed_app(self):
        self.logos.stop()

    def stop_indexing(self):
        self.logos.stop_indexing()

    @maintenance.uses_installation
    def winetricks(self):
        process = wine.run_wine_process(
            self,
            self.conf.winetricks_binary,
            sys.stdout,
            sys.stderr,
            sys.stdin,
            exe_args=(self.conf._overrides.wine_args or []),
            detach_on_close=False,
        )
        if process:
            self._wait_command(process)
        else:
            raise maintenance.MaintenanceError("Winetricks command could not be started")

    @maintenance.uses_installation
    def wine(self):
        process = wine.run_wine_process(
            self,
            self.conf.wine64_binary,
            sys.stdout,
            sys.stderr,
            sys.stdin,
            exe_args=(self.conf._overrides.wine_args or []),
            detach_on_close=False,
        )
        if process:
            self._wait_command(process)
        else:
            raise maintenance.MaintenanceError("Wine command could not be started")

    def _wait_command(self, process):
        """Keep mutation admission until a detached command has settled."""
        interrupted = False
        while True:
            try:
                process.wait()
                break
            except KeyboardInterrupt:
                interrupted = True
                self.print("Waiting for the active Wine command to finish before closing")
        if interrupted:
            raise KeyboardInterrupt
        raise SystemExit(process.returncode)

    def set_appimage(self):
        utils.set_appimage_symlink(app=self)

    def toggle_app_logging(self):
        self.logos.switch_logging()

    def update_latest_appimage(self):
        utils.update_to_latest_recommended_appimage(self)

    def update_self(self):
        from . import updates

        prepared = utils.update_to_latest_lli_release(self)
        if prepared is not None:
            prepared.restart(updates.resume_arguments(self.conf._overrides.config_path, self.conf._overrides.dialog))

    def get_support(self):
        control.get_support(self)

    _exit_option: str | None = "Exit"

    def _ask(self, question: str, options: list[str] | str) -> Optional[str]:
        with self._prompt_lock:
            if isinstance(options, str):
                default = str(Path.home()) if options == constants.PROMPT_OPTION_DIRECTORY else ""
                return self._read_answer(question, default)
            answer = self._read_answer(question, options[0], options)
            if answer in constants.PROMPT_OPTION_SIGILS:
                default = str(Path.home()) if answer == constants.PROMPT_OPTION_DIRECTORY else ""
                return self._read_answer("Path", default)
            return answer

    def _info(self, message: str) -> None:
        self.print(message)
        if self._stdin.isatty():
            self._ask("Press Return to continue", ["Continue"])

    def _read_answer(self, question: str, default: str, options: list[str] | tuple[str, ...] = ()) -> str | None:
        """Read only this answer so later Wine stdin stays with its subprocess."""
        if self._closing.is_set():
            return None
        if self._input_failure is not None:
            raise maintenance.MaintenanceError(self._input_failure)
        choices = ", ".join(options)
        prompt = f"{question}: {choices} [default: {default}]"
        print(prompt, end=" " if sys.stdout.isatty() else "\n", flush=True)
        answer = bytearray()
        try:
            descriptor = self._stdin.fileno()
            while not self._closing.is_set():
                if not select.select([descriptor], [], [], 0.1)[0]:
                    continue
                chunk = os.read(descriptor, 1)
                if not chunk:
                    self._input_failure = "Required input is unavailable: stdin reached EOF"
                    if not answer:
                        raise maintenance.MaintenanceError(self._input_failure)
                    break
                if chunk == b"\n":
                    break
                answer.extend(chunk)
                if len(answer) > 65536:
                    raise ValueError("Input exceeds the maximum answer length")
            if self._closing.is_set():
                return None
            value = answer.decode(self._stdin.encoding or "utf-8").rstrip("\r")
            return value if value else default
        except (OSError, ValueError) as error:
            self._input_failure = f"Could not read required input: {error}"
            raise maintenance.MaintenanceError(self._input_failure) from error

    @classmethod
    def print(_cls, message: str):
        """Clear terminal progress only when writing to an interactive terminal."""
        prefix = "\033[2K\r" if _terminal_progress() else ""
        print(prefix + message, flush=True)

    def _schedule_exit_on_main_thread(self, reason: str, intended: bool) -> None:
        # Same handling - exit happens to be thread safe today.
        self._exit(reason=reason, intended=intended)

    def exit(self, reason: str, intended: bool = False):
        self.print(f"Closing {constants.APP_NAME} due to: {reason}")
        return super().exit(reason, intended)
    
    def _status(self, message: str, percent: Optional[int] = None):
        """Keep redirected status plain and interactive progress width bounded."""
        if message != self._last_status:
            self.print(message)
        if percent is not None and _terminal_progress():
            width = max(1, min(120, shutil.get_terminal_size(fallback=(80, 24)).columns) - 2)
            filled = width * max(0, min(100, percent)) // 100
            print("[" + "-" * filled + " " * (width - filled) + "]", end="\r", flush=True)

    def _pop_up(self, title, message):
        self._status(message=message)

    @property
    def superuser_command(self) -> str:
        if shutil.which('sudo'):
            return "sudo"
        else:
            raise SuperuserCommandNotFound("sudo command not found. Please install.")


def _terminal_progress():
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ
