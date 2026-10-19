import math
import os
import queue
import select
import shutil
import sys
import threading
from typing import Optional, Tuple

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
        self.running: bool = True
        self.choice_q: queue.Queue[str] = queue.Queue()
        self.input_q: queue.Queue[Tuple[str, list[str]] | None] = queue.Queue()
        self.input_event = threading.Event()
        self.choice_event = threading.Event()
        self.start_thread(self.user_input_processor, daemon_bool=True)

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

    @maintenance.uses_installation
    def winetricks(self):
        process = wine.run_wine_process(
            self,
            self.conf.winetricks_binary,
            sys.stdout,
            sys.stderr,
            sys.stdin,
            exe_args=(self.conf._overrides.wine_args or [])
        )
        if process:
            process.wait()
            sys.exit(process.returncode)

    @maintenance.uses_installation
    def wine(self):
        process = wine.run_wine_process(
            self,
            self.conf.wine64_binary,
            sys.stdout,
            sys.stderr,
            sys.stdin,
            exe_args=(self.conf._overrides.wine_args or [])
        )
        if process:
            process.wait()
            sys.exit(process.returncode)

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

    def _ask(self, question: str, options: list[str] | str) -> str:
        """Passes the user input to the user_input_processor thread
        
        The user_input_processor is running on the thread that the user's stdin/stdout
        is attached to. This function is being called from another thread so we need to
        pass the information between threads using a queue/event
        """
        if isinstance(options, str):
            options = [options]
        self.input_q.put((question, options))
        self.input_event.set()
        self.choice_event.wait()
        self.choice_event.clear()
        output: str = self.choice_q.get()
        # NOTE: this response is validated in App's .ask
        return output

    def _info(self, message: str) -> None:
        """Display information to the user"""
        self.input_q.put((message, ['Continue?']))
        self.input_event.set()
        self.choice_event.wait()
        self.choice_event.clear()

    @classmethod
    def print(_cls, message: str):
        """Prints a message to the console.
        
        Use this instead of python's print.

        Handles implementation detail of drawing nicely
        even if there was progress bar displayed
        """
        print(
            (
                # "\033[2K" is the ANSI code for clearing the current line
                b"\033[2K\r"
                + message.encode("utf-8")
                + b"\n\r"
            ).decode(),
            end=""
        )

    def _schedule_exit_on_main_thread(self, reason: str, intended: bool) -> None:
        # Same handling - exit happens to be thread safe today.
        self._exit(reason=reason, intended=intended)

    def exit(self, reason: str, intended: bool = False):
        # Signal CLI.user_input_processor to stop.
        self.input_q.put(None)
        self.input_event.set()
        # Signal CLI itself to stop.
        self.running = False
        # We always want this to return regardless of level
        self.print(f"Closing {constants.APP_NAME} due to: {reason}")
        return super().exit(reason, intended)

    def request_close(self, reason="Controller closed", intended=True):
        super().request_close(reason, intended)
        self.running = False
        self.input_q.put(None)
        self.input_event.set()
        self.choice_q.put(None)
        self.choice_event.set()
    
    def _status(self, message: str, percent: Optional[int] = None):
        """Implementation for updating status pre-front end
        
        This implementation adds a progres bar.
        
        It should be noted this progress bar won't display nicely if
        other sources call to python's print. Use Cli.print() instead.
        """
        # Enforce percent is not over 100
        if percent is not None and percent > 100:
            percent = 100
        progress_str = ""
        # We don't want to display 100% as it doesn't print nicely.
        if percent is not None:
            # -2 is for the brackets, -1 is for the >
            progress_bar_length = os.get_terminal_size().columns - 2 -1
            # Subtract one for the >
            chars_of_progress = math.floor(float(progress_bar_length) * percent / 100) #noqa: E501
            chars_remaining = progress_bar_length - chars_of_progress
            progress_str = "[" + "-" * chars_of_progress + ">" + " " * chars_remaining + "]" #noqa: E501

        if message != self._last_status:
            self.print(message)
        # Write my progress then carriage return
        # (so we're back at the beginning for the next log line)
        if progress_str:
            print(progress_str, end="\r")

    def _pop_up(self, title, message):
        self._status(message=message)

    @property
    def superuser_command(self) -> str:
        if shutil.which('sudo'):
            return "sudo"
        else:
            raise SuperuserCommandNotFound("sudo command not found. Please install.")

    def user_input_processor(self, evt=None) -> None:
        while self.running:
            prompt = None
            question: Optional[str] = None
            options = None
            choice: Optional[str] = None
            # Wait for next input queue item.
            self.input_event.wait()
            self.input_event.clear()
            prompt = self.input_q.get()
            if prompt is None:
                return
            if prompt is not None and isinstance(prompt, tuple):
                question = prompt[0]
                options = prompt[1]
            if question is not None and options is not None:
                # Convert options list to string.
                default = options[0]
                optstr = f"{options[0]} [default], " + ', '.join(options[1:])
                print(f"{question}: {optstr}: ", end="", flush=True)
                while self.running:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        choice = sys.stdin.readline()
                        if not choice:
                            self.choice_q.put(None)
                            self.choice_event.set()
                            return
                        choice = choice.rstrip("\n")
                        break
                if not self.running:
                    return
                if len(choice) == 0:
                    choice = default
            if choice is not None and choice == self._exit_option:
                self.running = False
            if choice is not None:
                self.choice_q.put(choice)
                self.choice_event.set()
