
# References:
#   - https://tkdocs.com/
#   - https://github.com/thw26/LogosLinuxInstaller/blob/master/LogosLinuxInstaller.sh

import copy
import logging
from pathlib import Path
from queue import Queue

import shutil
import subprocess
from threading import Event
import threading
import time
from tkinter import PhotoImage, messagebox
from tkinter import Tk, TclError
from tkinter import Toplevel as TkToplevel
from tkinter import filedialog as fd
from tkinter.ttk import Style
from typing import Callable, Optional

from ou_dedetai.app import App, UserExitedFromAsk
from ou_dedetai.constants import (
    PROMPT_OPTION_DIRECTORY,
    PROMPT_OPTION_FILE,
    PROMPT_OPTION_NEW_FILE,
)
from ou_dedetai.config import EphemeralConfiguration
import ou_dedetai.logos

from . import backup
from . import constants
from . import control
from . import gui
from . import installer
from . import processes
from . import releases
from . import system
from . import utils
from . import updates
from . import wine
import ou_dedetai

class GuiApp(App):
    """Implements the App interface for all windows"""

    _exit_option: Optional[str] = None

    def __init__(self, root: "Root", gui: gui.StatusGui, ephemeral_config: EphemeralConfiguration, **kwargs): 
        super().__init__(ephemeral_config)
        self.root = root
        self._status_gui = gui


    def _ask(self, question: str, options: list[str] | str) -> Optional[str]:
        # This cannot be run from the main thread as the dialog will never appear
        # since the tinker mainloop hasn't started and we block on a response

        if isinstance(options, list):
            answer_q: Queue[Optional[str]] = Queue()
            answer_event = Event()
            ChoicePopUp(question, options, answer_q, answer_event)

            answer_event.wait()
            answer: Optional[str] = answer_q.get()
        elif isinstance(options, str):
            answer = options

        if answer == PROMPT_OPTION_DIRECTORY:
            answer = fd.askdirectory(
                parent=self.root,
                title=question,
                initialdir=Path().home(),
            )
        elif answer == PROMPT_OPTION_FILE:
            answer = fd.askopenfilename(
                parent=self.root,
                title=question,
                initialdir=Path().home(),
            )
        elif answer == PROMPT_OPTION_NEW_FILE:
            answer = fd.asksaveasfilename(
                parent=self.root,
                title=question,
                initialdir=Path().home(),
            )
        return answer or None

    def _info(self, message):
        """Display information to the user"""
        InfoPopUp(message)

    def _pop_up(self, title, message):
        messagebox.showinfo(
            title=title,
            message=message
        )

    def _status(self, message, percent = None):
        message = message.strip()
        if percent is not None:
            self._status_gui.progress.stop()
            self._status_gui.progress.state(['disabled'])
            self._status_gui.progress.config(mode='determinate')
            self._status_gui.progressvar.set(percent)
        else:
            self._status_gui.progress.state(['!disabled'])
            self._status_gui.progressvar.set(0)
            self._status_gui.progress.config(mode='indeterminate')
            self._status_gui.progress.start()
        self._status_gui.statusvar.set(message)

    def clear_status(self):
        self._status('', 0)

    def approve(self, question: str, context: str | None = None) -> bool:
        if context is None:
            context = ""
        message = f"{question}\n\n{context}"
        return messagebox.askquestion(question, message.strip()) == 'yes'

    def _schedule_exit_on_main_thread(self, reason: str, intended: bool) -> None:
        # root.after is the supported way to run work on the mainloop (main)
        # thread from another thread. The callback re-invokes exit() there, so
        # _exit() (and root.destroy()) run on the main thread.
        self.root.after(0, lambda: self.exit(reason, intended))

    def _exit(self, reason: str, intended: bool = False):
        # Create a little dialog before we die so the user can see why this happened
        if not intended:
            gui.show_error(reason, detail=constants.SUPPORT_MESSAGE, fatal=False)
        self.root.destroy()
    
    @property
    def superuser_command(self) -> str:
        """Command when root privileges are needed.
        
        Raises:
            SuperuserCommandNotFound - if no command is found

        pkexec if found"""
        if shutil.which('pkexec'):
            return "pkexec"
        else:
            raise system.SuperuserCommandNotFound("No superuser command found. Please install pkexec.")
    
    def populate_defaults(self) -> None:
        """Populate choices only after an installation workflow was selected."""

        # For the GUI, use defaults until user says otherwise.
        if self.conf._raw.faithlife_product is None:
            self.conf.faithlife_product = constants.FAITHLIFE_PRODUCTS[0]
        if self.conf._raw.faithlife_product_version is None:
            self.conf.faithlife_product_version = constants.FAITHLIFE_PRODUCT_VERSIONS[0] 

        # Set the install_dir to default, no option in the GUI to change it
        if self.conf._raw.install_dir is None:
            self.conf.install_dir = self.conf.install_dir_default

        if self.conf._raw.faithlife_product_release is None:
            releases = self.conf.faithlife_product_releases
            if not releases:
                raise ou_dedetai.maintenance.MaintenanceError("No product release is available for installation")
            self.conf.faithlife_product_release = releases[0]

        if self.conf._raw.wine_binary is None:
            wine_choices = utils.get_wine_options(self)
            if len(wine_choices) > 0:
                self.conf.wine_binary = wine_choices[0]

class Root(Tk):
    def destroy(self):
        closed = getattr(self, "_operation_closed", None)
        if closed is not None:
            closed.set()
        processes.shutdown()
        super().destroy()

    def __init__(self, *args, **kwargs):
        processes.resume()
        super().__init__(**kwargs)
        self.classname = kwargs.get('classname')
        # Set the theme.
        set_style(self)

        # Make root widget's outer border expand with window.
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # Set panel icon.
        self.icon = constants.APP_IMAGE_DIR / 'icon.png'
        self.pi = PhotoImage(file=f'{self.icon}', master=self)
        self.iconphoto(False, self.pi)


class Toplevel(TkToplevel):
    def __init__(self, *args, **kwargs):
        super().__init__(**kwargs)
        set_style(self)

        # Make root widget's outer border expand with window.
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)


class ChoicePopUp:
    """Creates a pop-up with a choice"""
    def __init__(
        self,
        question: str,
        options: list[str],
        answer_q: Queue[Optional[str]],
        answer_event: Event,
        **kwargs
    ):
        self.root = Toplevel()
        # Set root parameters.
        self.gui = gui.ChoiceGui(self.root, question, options)
        self.root.title("Question")
        # Set root widget event bindings.
        self.root.bind(
            "<Return>",
            self.on_confirm_choice
        )
        self.root.bind(
            "<Escape>",
            self.on_cancel_released
        )
        self.gui.cancel_button.config(command=self.on_cancel_released)
        self.gui.okay_button.config(command=self.on_confirm_choice)
        self.answer_q = answer_q
        self.answer_event = answer_event
        self._completed = False
        self.root.protocol("WM_DELETE_WINDOW", self.on_cancel_released)
        self.root.bind("<Destroy>", self._on_destroy, add="+")

    def on_confirm_choice(self, evt=None):
        if self.gui.answer_dropdown.get() == gui.ChoiceGui._default_prompt:
            return
        answer = self.gui.answer_dropdown.get()
        self._complete(answer)

    def on_cancel_released(self, evt=None):
        self._complete(None)

    def _on_destroy(self, event):
        if event.widget == self.root:
            self._complete(None, destroy=False)

    def _complete(self, answer, *, destroy=True):
        if self._completed:
            return
        self._completed = True
        self.answer_q.put(answer)
        self.answer_event.set()
        if destroy:
            self.root.destroy()


class InfoPopUp:
    """Creates a pop-up with info shared to the user"""
    def __init__(self, message: str, **kwargs):
        self.root = Toplevel()
        # Set root parameters.
        self.gui = gui.InfoGui(self.root, message)
        self.root.title("Info")
        # Set root widget event bindings.
        self.root.bind(
            "<Return>",
            self.on_okay_released
        )
        self.root.bind(
            "<Escape>",
            self.on_okay_released
        )
        self.gui.okay_button.config(command=self.on_okay_released)

    def on_okay_released(self, evt=None):
        self.root.destroy()


class InstallerWindow:
    def __init__(self, new_win, root: Root, app: "GuiApp", **kwargs):
        # Set root parameters.
        self.win = new_win
        self.root = root
        self.win.title(f"{constants.APP_NAME} Installer")
        self.win.resizable(False, False)
        self.gui = gui.InstallerGui(self.win, app)
        self.app = app
        self.conf = app.conf
        self.start_thread = app.start_thread

        # Initialize variables.
        self.config_thread = None

        # Set widget callbacks and event bindings.
        self.gui.product_dropdown.bind(
            '<<ComboboxSelected>>',
            self.set_product
        )
        self.gui.version_dropdown.bind(
            '<<ComboboxSelected>>',
            self.set_version
        )
        self.gui.release_dropdown.bind(
            '<<ComboboxSelected>>',
            self.set_release
        )
        self.gui.wine_dropdown.bind(
            '<<ComboboxSelected>>',
            self.set_wine
        )
        self.gui.skipdeps_checkbox.config(command=self.set_skip_dependencies)
        self.gui.cancel_button.config(command=self.on_cancel_released)
        self.gui.okay_button.config(command=self.on_okay_released)

        # Set root widget event bindings.
        self.root.bind(
            "<Return>",
            self.on_okay_released
        )
        self.root.bind(
            "<Escape>",
            self.on_cancel_released
        )

        # Run commands.
        self.app.config_updated_hooks += [self._config_updated_hook]
        # Start out enforcing this
        self._config_updated_hook()

    def _config_updated_hook(self):
        """Update the GUI to reflect changes in the configuration/network""" 

        self.app.populate_defaults()

        # Fill in the UI elements from the config
        self.gui.productvar.set(self.conf.faithlife_product)
        self.gui.versionvar.set(self.conf.faithlife_product_version)

        # Now that we know product and version are set we can download the releases
        self.gui.release_dropdown['values'] = self.conf.faithlife_product_releases
        self.gui.releasevar.set(self.conf.faithlife_product_release)

        self.gui.skipdepsvar.set(not self.conf.skip_install_system_dependencies)

        # In case the product changes
        self.root.icon = Path(self.conf.faithlife_product_icon_path)

        self.gui.wine_dropdown['values'] = utils.get_wine_options(self.app)
        if not self.gui.winevar.get():
            # If no value selected, default to 1st item in list.
            self.gui.winevar.set(self.gui.wine_dropdown['values'][0])

        self.gui.winevar.set(self.conf._raw.wine_binary or '')

        # At this point all variables are populated, we're ready to install!
        self.set_input_widgets_state('enabled', [self.gui.okay_button])

    def _post_dropdown_change(self):
        """Reset progress without changing the selected installation destination."""
        self.installer_step = 0
        self.installer_step_count = 0

    def set_input_widgets_state(self, state, widgets='all'):
        if state == 'enabled':
            state = ['!disabled']
        elif state == 'disabled':
            state = ['disabled']
        all_widgets = [
            self.gui.product_dropdown,
            self.gui.version_dropdown,
            self.gui.release_dropdown,
            self.gui.wine_dropdown,
            self.gui.okay_button,
        ]
        if widgets == 'all':
            widgets = all_widgets
        for w in widgets:
            w.state(state)

    def set_product(self, evt=None):
        self.conf.faithlife_product = self.gui.productvar.get()
        self.gui.product_dropdown.selection_clear()
        self._post_dropdown_change()

    def set_version(self, evt=None):
        self.conf.faithlife_product_version = self.gui.versionvar.get()
        self.gui.version_dropdown.selection_clear()
        self._post_dropdown_change()

    def set_release(self, evt=None):
        self.conf.faithlife_product_release = self.gui.releasevar.get()
        self.gui.release_dropdown.selection_clear()
        self._post_dropdown_change()

    def set_wine(self, evt=None):
        self.conf.wine_binary = self.gui.winevar.get()
        self.gui.wine_dropdown.selection_clear()
        self._post_dropdown_change()

    def set_skip_dependencies(self, evt=None):
        self.conf.skip_install_system_dependencies = self.gui.skipdepsvar.get()  # invert True/False
        logging.debug(f"> {self.conf.skip_install_system_dependencies=}") 

    def on_okay_released(self, evt=None):
        # Update desktop panel icon.
        self.start_install_thread()

    def close(self):
        self.app.config_updated_hooks.remove(self._config_updated_hook)
        # Reset status
        self.app.clear_status()
        self.win.destroy()

    def on_cancel_released(self, evt=None):
        self.app.clear_status()
        self.close()
        return 1

    def start_install_thread(self, evt=None):
        def _install():
            """Function to handle the install"""
            # Close the options window and let the install run
            self.close()
            try:
                installer.install(self.app)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                self.app.status(f"Install failed: {error}", 0)
                self.app.pop_up("Install failed", str(error))
            # Install complete, cleaning up...
            return 0

        # Setup for the install
        self.app.status('Ready to install!', 0)
        self.set_input_widgets_state('disabled')

        self.start_thread(_install)


class ControlWindow(GuiApp):
    def __init__(self, root, control_gui: gui.ControlGui, 
                 ephemeral_config: EphemeralConfiguration, *args, **kwargs):
        super().__init__(root, control_gui, ephemeral_config)

        # Set root parameters.
        self.root = root
        self.gui = control_gui
        self.actioncmd: Optional[Callable[[], None]] = None

        ver = constants.LLI_CURRENT_VERSION
        self.gui.update_lli_label.config(text=f"Update {constants.APP_NAME}\ncurrent: v{ver}\nlatest: ...")
        self.gui.update_lli_button.state(['disabled'])
        self._controller_checks = releases.ControllerChecks(
            self.conf._network, updates.eligible, getattr(self.conf, "refresh_release_snapshots", None),
        )
        self._controller_timer = None
        self._controller_closed = False
        self.gui.check_controller_button.configure(command=self.check_controller_releases)
        self.root.bind("<Destroy>", self._close_controller_checks, add="+")
        self._poll_controller_checks()

        if self.conf._raw.faithlife_product:
            control_gui.update_product_labelvar.set(f"Update {self.conf._raw.faithlife_product}")

        # Spawn a thread to ensure our logos state stays up to date
        def _monitor_faithlife_product_pids():
            last_state = copy.copy(self.logos.logos_state)
            while True:
                if self.is_installed():
                    self.logos.monitor()
                    time.sleep(1)
                else:
                    # Will probably be some time before we need to monitor.
                    time.sleep(30)
                # If our state changed, we need to update our button.
                if last_state != self.logos.logos_state:
                    self.update_app_button()
                last_state = copy.copy(self.logos.logos_state)
        self.start_thread(_monitor_faithlife_product_pids)
        self.gui.run_indexing_radio.config(
            command=self.on_action_radio_clicked
        )
        self.gui.remove_library_catalog_radio.config(
            command=self.on_action_radio_clicked
        )
        self.gui.remove_index_files_radio.config(
            command=self.on_action_radio_clicked
        )
        self.gui.uninstall_radio.config(
            command=self.on_action_radio_clicked
        )
        self.gui.install_icu_radio.config(
            command=self.on_action_radio_clicked
        )
        self.gui.actions_button.config(command=self.run_action_cmd)

        self.gui.loggingstatevar.set('Enable')
        self.gui.logging_button.config(
            text=self.gui.loggingstatevar.get(),
            command=self.switch_logging
        )
        self.gui.logging_button.state(['disabled'])
        self.gui.support_button.config(command=self.get_support)

        self.gui.config_button.config(command=self.edit_config)
        self.gui.deps_button.config(command=self.install_deps)
        self.gui.backup_button.config(command=self.run_backup)
        self.gui.restore_button.config(command=self.run_restore)
        self.gui.update_product_button.config(command=self.update_faithlife_product)
        self.gui.update_lli_button.config(
            command=self.update_to_latest_lli_release
        )
        self.gui.latest_appimage_button.config(
            command=self.start_appimage_update
        )
        self.gui.set_appimage_button.config(command=self.set_appimage)

        self._config_update_hook()
        # These can be expanded to change the UI based on config changes.
        self.config_updated_hooks += [self._config_update_hook]
        self.root.after(0, updates.confirm_startup)

    def _close_controller_checks(self, event):
        if event.widget != self.root:
            return
        self._controller_closed = True
        self._controller_checks.close()
        if self._controller_timer is not None:
            self.root.after_cancel(self._controller_timer)
            self._controller_timer = None

    def _poll_controller_checks(self):
        if self._controller_closed:
            return
        self._controller_checks.request(self.conf.app_release_channel)
        current = (self._controller_checks.result, self._controller_checks.eligibility_message,
                   self._controller_checks.revision)
        if current != getattr(self, "_controller_rendered", None):
            self._controller_rendered = current
            self.update_controller_version()
            self.update_product_button()
            self.update_latest_appimage_button()
        self._controller_timer = self.root.after(250, self._poll_controller_checks)

    def check_controller_releases(self):
        self._controller_checks.request(self.conf.app_release_channel, force=True)

    def edit_config(self):
        control.edit_file(self.conf.config_file_path)

    def run_install(self, evt=None):
        """Directly install the product.
        
        Fallback to defaults if we don't know a response"""
        def _install():
            try:
                self.populate_defaults()
                installer.install(self)
            except UserExitedFromAsk:
                # Ensure that the defaults are properly set back up
                self.populate_defaults()
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                self.status(f"Install failed: {error}", 0)
                self.pop_up("Install failed", str(error))
            finally:
                # Enable the run button
                self.gui.app_button.state(['!disabled'])
                self.gui.app_install_advanced.state(['!disabled'])
        # Disable the install buttons
        self.gui.app_button.state(['disabled'])
        self.gui.app_install_advanced.state(['disabled'])
        # Start the install thread.
        self.start_thread(_install)

    def run_installer(self, evt=None):
        classname = constants.BINARY_NAME
        installer_window_top = Toplevel()
        InstallerWindow(installer_window_top, self.root, app=self, class_=classname) 

    def stop_logos(self):
        self.start_thread(self.logos.stop)

    def run_logos(self, evt=None):
        self._run_operation(lambda app: app.logos.start())

    def _run_operation(self, operation):
        if getattr(self, "_operation_running", False):
            self.status("Another operation is still running", 0)
            return
        self._operation_running = True
        closed = getattr(self.root, "_operation_closed", None)
        if closed is None:
            closed = Event()
            self.root._operation_closed = closed
            self.root.bind(
                "<Destroy>", lambda event: closed.set() if event.widget == self.root else None, add="+",
            )
        controls = [self.gui.app_button, self.gui.actions_button, self.gui.backup_button, self.gui.restore_button]
        for name in ("update_lli_button", "update_product_button", "latest_appimage_button"):
            widget = getattr(self.gui, name, None)
            if widget is not None:
                controls.append(widget)
        for widget in controls:
            widget.state(["disabled"])

        def finished():
            self._operation_running = False
            for widget in controls:
                widget.state(["!disabled"])
            self.update_app_button()
            if hasattr(self, "_controller_checks"):
                self.update_controller_version()

        def run():
            try:
                result = operation(self)
                if isinstance(result, updates.PreparedUpdate):
                    if closed.is_set():
                        result.cancel()
                    else:
                        try:
                            self.root.after(0, lambda: self._restart_controller(result))
                        except (TclError, RuntimeError):
                            result.cancel()
            except UserExitedFromAsk:
                if not closed.is_set():
                    self.status("Operation cancelled", 0)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                message = str(error)
                if not closed.is_set():
                    self.status(f"Operation failed: {message}", 0)
                    self.root.after(0, lambda: self.pop_up("Operation failed", message))
            finally:
                if not closed.is_set():
                    try:
                        self.root.after(0, finished)
                    except (TclError, RuntimeError):
                        pass

        self.start_thread(run, daemon_bool=False)

    def run_action_cmd(self, evt=None):
        if self.actioncmd:
            self.actioncmd()

    def on_action_radio_clicked(self, evt=None):
        logging.debug("gui_app.ControlPanel.on_action_radio_clicked START")
        if self.is_installed():
            self.gui.actions_button.state(['!disabled'])
            if self.gui.actionsvar.get() == 'run-indexing':
                self.actioncmd = self.run_indexing
            elif self.gui.actionsvar.get() == 'remove-library-catalog':
                self.actioncmd = self.remove_library_catalog
            elif self.gui.actionsvar.get() == 'remove-index-files':
                self.actioncmd = self.remove_indexes
            elif self.gui.actionsvar.get() == 'install-icu':
                self.actioncmd = self.install_icu
            elif self.gui.actionsvar.get() == 'uninstall':
                self.actioncmd = self.uninstall

    def run_indexing(self):
        self._run_operation(lambda app: app.logos.index())

    def remove_library_catalog(self):
        self._run_operation(control.remove_library_catalog)

    def remove_indexes(self):
        self._run_operation(control.remove_all_index_files)

    def install_icu(self):
        self._run_operation(wine.enforce_icu_data_files)

    def uninstall(self):
        self._run_operation(control.uninstall)

    def run_backup(self, evt=None):
        self._run_operation(backup.backup)

    def run_restore(self, evt=None):
        self._run_operation(backup.restore)

    def install_deps(self, evt=None):
        self._run_operation(utils.install_dependencies)

    def open_file_dialog(self, filetype_name, filetype_extension):
        file_path = fd.askopenfilename(
            title=f"Select {filetype_name}",
            filetypes=[
                (filetype_name, f"*.{filetype_extension}"),
                ("All Files", "*.*")
            ],
        )
        return file_path

    def update_faithlife_product(self, evt=None):
        self._run_operation(utils.update_faithlife_product)

    def update_to_latest_lli_release(self, evt=None):
        self.status(f"Updating to latest {constants.APP_NAME} version…")
        self._run_operation(utils.update_to_latest_lli_release)

    def _restart_controller(self, prepared):
        if getattr(self.root, "_operation_closed", Event()).is_set():
            prepared.cancel()
            return
        stopped = False
        try:
            self._controller_checks.close()
            stopped = True
            arguments = updates.resume_arguments(self.conf._overrides.config_path, "tk")
            prepared.restart(arguments)
        except (RuntimeError, OSError, ValueError) as error:
            self.status(f"Controller update failed: {error}", 0)
            self.pop_up("Controller update failed", str(error))
        finally:
            prepared.cancel()
            if stopped and not self._controller_closed:
                self._controller_checks = releases.ControllerChecks(
                    self.conf._network, updates.eligible, getattr(self.conf, "refresh_release_snapshots", None),
                )

    def set_appimage_symlink(self):
        utils.set_appimage_symlink(self)
        self.update_latest_appimage_button()

    def start_appimage_update(self):
        self._run_operation(utils.update_to_latest_recommended_appimage)

    def update_to_latest_appimage(self, evt=None):
        utils.update_to_latest_recommended_appimage(self)
        self.root.event_generate(evt)

    def set_appimage(self, evt=None):
        # TODO: Separate as advanced feature.
        appimage_filename = self.open_file_dialog("AppImage", "AppImage")
        if not appimage_filename:
            return

        def change_runtime(app):
            utils.set_appimage_symlink(app, source=appimage_filename)

        self._run_operation(change_runtime)

    def switch_logging(self, evt=None):
        desired_state = self.gui.loggingstatevar.get()
        self._run_operation(lambda app: app.logos.switch_logging(action=desired_state.lower()))

    def get_support(self):
        self._run_operation(control.get_support)

    def update_logging_button(self, evt=None):
        state = self.reverse_logging_state_value(self.current_logging_state_value())
        self.gui.loggingstatevar.set(state[:-1].title())
        self.gui.logging_button.state(['!disabled'])

    def update_app_button(self, evt=None):
        if getattr(self, "_operation_running", False):
            return
        self.gui.app_button.state(['!disabled'])
        if self.is_installed():
            if self.logos.logos_state == ou_dedetai.logos.State.RUNNING:
                self.gui.app_buttonvar.set(f"Stop {self.conf.faithlife_product}")
                self.gui.app_button.config(command=self.stop_logos)
            else:
                self.gui.app_buttonvar.set(f"Run {self.conf.faithlife_product}")
                self.gui.app_button.config(command=self.run_logos)
            self.gui.logging_button.state(['!disabled'])
            self.gui.app_install_advanced.grid_forget()
            self.gui.actions_button.state(['!disabled'])
        else:
            self.gui.app_buttonvar.set("Install")
            self.gui.app_button.config(command=self.run_install)
            self.gui.app_install_advanced.config(command=self.run_installer)
            self.gui.show_advanced_install_button()
            self.gui.actions_button.state(['disabled'])
            self.gui.app_button.state(['!disabled'])
            self.gui.app_install_advanced.state(['!disabled'])

    def update_product_button(self, evt=None):
        msg = None
        if not self.is_installed():
            state = 'disabled'
            msg = "This button is disabled. Please install first."
        elif self.conf.is_installed_faithlife_product_release_latest:
            state = 'disabled'
            msg = f"This button is disabled. {self.conf.faithlife_product} is up-to-date."
        elif self.conf.is_installed_faithlife_product_release_latest is None:
            state = 'disabled'
            msg = "Product release information is unavailable or stale. Use Check now to retry."
        else:
            state = '!disabled'
        if msg:
            gui.ToolTip(self.gui.update_product_button, msg)
        self.clear_status()
        self.gui.update_product_button.state([state])

    def update_controller_version(self):
        current = constants.APP_RELEASE_VERSION or "private, unversioned"
        if not constants.APP_SELF_UPDATES_ENABLED:
            latest_text = constants.APP_SELF_UPDATE_DISABLED_MESSAGE
        else:
            lookup = self._controller_checks.result
            state = releases.controller_state(lookup)
            latest = lookup.release.version if lookup.release else "unavailable"
            latest_text = f"latest: {latest} ({state})"

        self.gui.update_lli_label.config(text=f"Update {constants.APP_NAME}\ncurrent: {current}\n{latest_text}")
        self.update_latest_lli_release_button()

    def update_latest_lli_release_button(self, evt=None):
        state = 'disabled'
        msg = None
        if not constants.APP_SELF_UPDATES_ENABLED:
            msg = constants.APP_SELF_UPDATE_DISABLED_MESSAGE
        elif getattr(self, "_operation_running", False):
            msg = "Wait for the current operation to finish"
        else:
            lookup = self._controller_checks.result
            result = releases.controller_state(lookup)
            eligibility = self._controller_checks.eligibility_message
            if eligibility:
                msg = eligibility
            elif result == "available":
                state = '!disabled'
            elif result == "current":
                msg = f"This button is disabled. {constants.APP_NAME} is up-to-date."
            elif result == "development":
                msg = f"This button is disabled. {constants.APP_NAME} is newer than the latest release."
            else:
                msg = lookup.message if result != "unversioned" else "This private build has no release version"

        if msg:
            gui.ToolTip(self.gui.update_lli_button, msg)
        self.clear_status()
        self.gui.update_lli_button.state([state])

    def update_latest_appimage_button(self, evt=None):
        state = None
        msg = None
        if not self.is_installed():
            state = "disabled"
            msg = "Please install first"
        elif self.conf._raw.wine_binary_code not in ["Recommended", "AppImage", None]:
            state = 'disabled'
            msg = "This button is disabled. The configured install was not created using an AppImage."
            self.gui.set_appimage_button.state(['disabled'])
            gui.ToolTip(
                self.gui.set_appimage_button,
                "This button is disabled. The configured install was not created using an AppImage."
            )
        elif self.conf._raw.wine_binary is not None:
            status, detail = getattr(self.conf, "recommended_wine_status", (-1, "Not checked"))
            if status == 0:
                state = '!disabled'
            elif status == 1:
                state = 'disabled'
                msg = "This button is disabled. The AppImage is already set to the latest recommended."
            elif status == 2:
                state = 'disabled'
                msg = "This button is disabled. The AppImage version is newer than the latest recommended."
            else:
                # Failed to check
                state = '!disabled'
                msg = detail
        else:
            # Not enough context to figure out if this should be enabled or not
            state = '!disabled'
        if msg:
            gui.ToolTip(self.gui.latest_appimage_button, msg)
        self.clear_status()
        self.gui.latest_appimage_button.state([state])

    def _config_update_hook(self, evt=None):
        self.update_logging_button()
        self.update_app_button()
        try:
            self.update_latest_lli_release_button()
        except Exception:
            logging.exception("Failed to update release button")
        try:
            self.update_latest_appimage_button()
        except Exception:
            logging.exception("Failed to update appimage button")
        try:
            self.update_product_button()
        except Exception:
            logging.exception("Failed to update product button")
        product = self.conf._raw.faithlife_product
        if product:
            self.gui.update_product_labelvar.set(f"Update {product}")


    def current_logging_state_value(self) -> str:
        if self.conf.faithlife_product_logging:
            return 'ENABLED'
        else:
            return 'DISABLED'

    def reverse_logging_state_value(self, state) ->str:
        if state == 'DISABLED':
            return 'ENABLED'
        else:
            return 'DISABLED'


def set_style(tkapp):
    # Set the theme.
    tkapp.style = Style()
    tkapp.style.theme_use('alt')

    # Update color scheme.
    tkapp.style.configure('TCheckbutton', bordercolor=constants.LOGOS_GRAY)
    tkapp.style.configure('TCombobox', bordercolor=constants.LOGOS_GRAY)
    tkapp.style.configure('TCheckbutton', indicatorcolor=constants.LOGOS_GRAY)
    tkapp.style.configure('TRadiobutton', indicatorcolor=constants.LOGOS_GRAY)
    bg_widgets = [
        'TCheckbutton', 'TCombobox', 'TFrame', 'TLabel', 'TRadiobutton'
    ]
    fg_widgets = ['TButton', 'TSeparator']
    for w in bg_widgets:
        tkapp.style.configure(w, background=constants.LOGOS_WHITE)
    for w in fg_widgets:
        tkapp.style.configure(w, background=constants.LOGOS_GRAY)
    tkapp.style.configure(
        'Horizontal.TProgressbar',
        thickness=10, background=constants.LOGOS_BLUE,
        bordercolor=constants.LOGOS_GRAY,
        troughcolor=constants.LOGOS_GRAY,
    )

    # Justify to the left [('Button.label', {'sticky': 'w'})]
    tkapp.style.layout(
        "TButton", [(
            'Button.border', {
                'sticky': 'nswe', 'children': [(
                    'Button.focus', {
                        'sticky': 'nswe', 'children': [(
                            'Button.padding', {
                                'sticky': 'nswe', 'children': [(
                                    'Button.label', {'sticky': 'w'}
                                )]
                            }
                        )]
                    }
                )]
            }
        )]
    )


def start_gui_app(
    ephemeral_config: EphemeralConfiguration,
    recovery: Optional[Callable[[App], None]] = None,
    install_only: bool = False
):
    classname = constants.BINARY_NAME
    root = Root(className=classname)

    # Need to title/resize and create the initial gui
    # BEFORE mainloop is started to get sizing correct other things
    # in the ControlWindow constructor are run after mainloop is running
    # To allow them to ask questions while the mainloop is running
    root.title(f"{constants.APP_NAME} Control Panel")
    root.resizable(False, False)

    def _start_install() -> Callable[[], None]:
        # This needs to be created before root.mainloop is called
        installer_gui = gui.StatusWithLabelGui(root, "Installing FaithLife app")
        def _run():
            app = GuiApp(root, installer_gui, ephemeral_config)
            # This may take a minute to run, as it may need to reach out to the internet
            app.populate_defaults()
            try:
                installer.install(app)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                app.status(f"Install failed: {error}", 0)
                app.pop_up("Install failed", str(error))
                return
            # Wait for a couple seconds so user can understand they're done.
            time.sleep(3)
            root.destroy()
        return _run

    def _start_control_panel() -> Callable[[], None]:
        # This needs to be created before root.mainloop is called
        control_gui = gui.ControlGui(root)
        def _run():
            if recovery:
                recovery_gui = gui.StatusWithLabelGui(root, "Recovering FaithLife app")
                recovery(GuiApp(root, recovery_gui, ephemeral_config))
                recovery_gui.destroy()
            ControlWindow(root, control_gui, ephemeral_config, class_=classname)
        return _run

    if install_only:
        target=_start_install()
    else:
        target=_start_control_panel()

    # Start the control panel on a new thread so it can open dialogs
    # as a part of it's constructor
    threading.Thread(
        name=f"{constants.APP_NAME} GUI main loop",
        target=target,
        daemon=True
    ).start()

    root.mainloop()
