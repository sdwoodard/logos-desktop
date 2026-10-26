from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from textual import on
from textual.app import App as TextualApp, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (Button, Footer, Input, Label, Log, OptionList, Static)
from textual.widgets.option_list import Option

from ou_dedetai.app import App, UserExitedFromAsk
from ou_dedetai.config import EphemeralConfiguration

from . import control
from . import constants
from . import installer
from . import logos
from . import maintenance
from . import system
from . import utils
from . import wine


class PromptModal(ModalScreen):
    """Complete an owned waiter when a prompt is removed without dismissal."""

    def dismiss(self, result=None):
        self._delivered = True
        return super().dismiss(result)

    def on_unmount(self):
        cancel = getattr(self, "cancel_waiter", None)
        if not getattr(self, "_delivered", False) and cancel is not None:
            cancel()


class MenuModal(PromptModal):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield OptionList(
                *[Option(opt, id=f"opt-{i}") for i, opt in enumerate(self._options)],
                id="menu-list",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        from . import processes

        processes.resume()
        self.query_one("#menu-list", OptionList).focus()

    @on(OptionList.OptionSelected)
    def on_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss("Return to Main Menu")


class InfoModal(PromptModal):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def __init__(self, message: str, title: str = "Information") -> None:
        super().__init__()
        self.message = message
        self.dialog_title = title

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self.dialog_title, id="dialog-title")
            yield Static(self.message, id="dialog-body")
            yield Button("OK", variant="primary", id="ok")

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        self.dismiss()

    def action_cancel(self) -> None:
        cancel = getattr(self, "cancel_waiter", None)
        if cancel is not None:
            cancel()
        self.dismiss()


class InputModal(PromptModal):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def __init__(self, question: str, default: str = "") -> None:
        super().__init__()
        self.question = question
        self.default = default

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self.question, id="dialog-title")
            yield Input(value=self.default, id="input-field")
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#input-field", Input).focus()

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        self.dismiss(self.query_one("#input-field", Input).value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def on_submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class TextualUI(TextualApp[None]):
    THEMES = {
        "logos": Theme(
            name="logos",
            primary="#00AEEF",
            secondary="#666666",
            accent="#FFFFFF",
            background="#101010",
            surface="#181818",
            panel="#202020",
            foreground="#F0F0F0",
            dark=True,
        ),
        "system-dark": Theme(
            name="system-dark",
            primary="#5FA8D3",
            secondary="#7F8C8D",
            accent="#FFFFFF",
            warning="#F2C14E",
            error="#E06C75",
            success="#98C379",
            foreground="#E6E6E6",
            background="#121212",
            surface="#1E1E1E",
            panel="#252525",
            dark=True,
        ),
        "system-light": Theme(
            name="system-light",
            primary="#005A9C",
            secondary="#5F6368",
            accent="#000000",
            warning="#8A5700",
            error="#B3261E",
            success="#137333",
            foreground="#202124",
            background="#FFFFFF",
            surface="#F5F5F5",
            panel="#E8E8E8",
            dark=False,
        )
    }
    CSS = """
    Screen {
        background: $background;
        color: $text;
    }

    #header-area {
        dock: top;
        height: auto;
        background: $surface;
        border-bottom: solid $primary;
    }

    #title { text-style: bold; }

    #main-area { height: 1fr; }

    #menu-title {
        text-style: bold;
        padding: 0 1;
        background: $primary;
        color: $text;
    }

    #main-menu {
        height: 1fr;
        padding: 0 1;
    }

    #dialog {
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #dialog-title {
        text-style: bold;
    }

    #dialog-buttons {
        align: center middle;
    }

    #console-area {
        border: solid $primary;
        background: $surface;
        padding: 0 1;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    title_text: reactive[str] = reactive("")
    subtitle_text: reactive[str] = reactive("")

    def __init__(self, domain: "TUI") -> None:
        super().__init__()
        self.domain = domain
        self.register_theme(self.THEMES["logos"])
        self.register_theme(self.THEMES["system-dark"])
        self.register_theme(self.THEMES["system-light"])
        self.theme = "logos"

    def compose(self) -> ComposeResult:
        with Vertical(id="header-area"):
            yield Static(id="title")
            yield Static(id="subtitle")
        with Vertical(id="main-area"):
            yield Static("Main Menu", id="menu-title")
            yield OptionList(id="main-menu")
        yield Log(id="console-area", max_lines=300, highlight=True)
        yield Footer()
        yield Static(constants.APP_NAME, id="status-bar")

    def on_mount(self) -> None:
        self.title = constants.APP_NAME
        self.sub_title = getattr(constants, "LLI_CURRENT_VERSION", "")
        self.domain.set_title()
        self._populate_main_menu()
        self.query_one("#main-menu", OptionList).focus()
        self.domain.logos.start_monitoring()
        self._monitor_timer = self.set_interval(0.5, self._monitor_tick)
        self._controller_timer = self.set_interval(1, self.domain.poll_controller_releases)
        self.domain.poll_controller_releases()
        self.domain.status("TUI ready")
        from . import updates

        updates.confirm_startup()
        if hasattr(self.domain, "_controller_identity"):
            self.domain.start_thread(self.domain._identify_controller)

    def _monitor_tick(self) -> None:
        try:
            if self.domain.logos:
                self._populate_main_menu()
        except Exception as exc:
            logging.debug("monitor tick failed: %s", exc)

    def _refresh_header(self) -> None:
        try:
            self.query_one("#title", Static).update(self.title_text)
            self.query_one("#subtitle", Static).update(self.subtitle_text)
        except Exception:
            pass

    def watch_title_text(self, value: str) -> None:
        self._refresh_header()

    def watch_subtitle_text(self, value: str) -> None:
        self._refresh_header()

    def _populate_main_menu(self) -> None:
        menu = self.query_one("#main-menu", OptionList)
        options = [str(label) for label in self.domain.set_tui_menu_options()]
        current_options = [str(option.prompt) for option in menu.options]
        if options == current_options:
            return
        current = menu.highlighted
        menu.clear_options()
        for i, label in enumerate(options):
            menu.add_option(Option(label, id=f"main-{i}", disabled=label.startswith("Stopping ")))
        if current is not None and options:
            menu.highlighted = min(current, len(options) - 1)

    @on(OptionList.OptionSelected, "#main-menu")
    def on_main_menu(self, event: OptionList.OptionSelected) -> None:
        self.domain.main_menu_select(str(event.option.prompt))

    def action_refresh(self) -> None:
        self._populate_main_menu()
        self.domain.set_title()

    def action_quit(self) -> None:
        self.domain.is_running = False
        self.domain.cancel_pending_prompts()
        self.domain.request_close()
        self._finish_close()

    def _finish_close(self):
        if self.domain.close(timeout=0):
            self.exit()
        else:
            self.set_timer(0.1, self._finish_close)

    def on_unmount(self):
        if hasattr(self, "_monitor_timer"):
            self._monitor_timer.stop()
        if hasattr(self, "_controller_timer"):
            self._controller_timer.stop()
        if hasattr(self.domain, "_controller_checks"):
            self.domain._controller_checks.close()
        from . import processes

        processes.shutdown()
        self.domain.cancel_pending_prompts()

    def write_status(self, message: str) -> None:
        try:
            self.query_one("#console-area", Log).write_line(message)
        except Exception:
            pass

    def go_to_main_menu(self) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self._populate_main_menu()
        self.domain.set_title()
        try:
            self.query_one("#main-menu", OptionList).focus()
        except Exception:
            pass

    def _system_theme(self) -> str:
        colorfgbg = os.environ.get("COLORFGBG", "")
        try:
            background = int(colorfgbg.rsplit(";", 1)[-1])
        except ValueError:
            return "system-dark"
        return "system-light" if background >= 7 else "system-dark"

    def toggle_color_scheme(self) -> str:
        if self.theme == "logos":
            self.theme = self._system_theme()
        else:
            self.theme = "logos"
        return self.theme


class TUI(App):
    def __init__(self, ephemeral_config: EphemeralConfiguration) -> None:
        self._prompt_lock = threading.Lock()
        self._pending_prompts = {}
        self._closing = threading.Event()
        super().__init__(ephemeral_config)
        self.is_running = True
        self._installer_thread: Optional[threading.Thread] = None
        self._operation_lock = threading.Lock()
        self._operation_active = False
        self._last_console_message: str | None = None
        self.ui = TextualUI(self)
        from . import releases, updates

        self._controller_checks = releases.ControllerChecks(self.conf._network, updates.eligible,
                                                            self.conf.refresh_release_snapshots)
        self._controller_rendered = None
        self._controller_identity = None
        self._controller_result = None
        self._controller_standalone = False
        self.subscribe_config(self._config_update_hook)

    def dispatch_config(self, callback):
        self._ui_call(callback)

    def _schedule_exit_on_main_thread(self, reason, intended):
        self.ui.call_from_thread(self.ui.action_quit)

    def exit(self, reason, intended=False):
        self.request_close(reason, intended)
        if threading.get_ident() == self.ui._thread_id:
            self.ui.action_quit()
        else:
            self._schedule_exit_on_main_thread(reason, intended)
            raise SystemExit

    def poll_controller_releases(self):
        from . import releases

        identity = getattr(self, "_controller_identity", None)
        if identity is not None and not self._controller_standalone:
            result = self._controller_result
            text = f"{identity.owner}: {result.status}: {result.message}" if result else (
                f"unsupported: {identity.message}" if identity.owner == "unsupported" else
                f"{identity.owner}: Controller installation provider has not been checked")
            if text != self._controller_rendered:
                self._controller_rendered = text
                self.ui.query_one("#status-bar", Static).update(text)
                self.ui.action_refresh()
            return
        if not constants.APP_SELF_UPDATES_ENABLED:
            return
        if hasattr(self, "_controller_identity") and not self._controller_standalone:
            return
        self._controller_checks.request(self.conf.app_release_channel)
        result = self._controller_checks.result
        current = result, self._controller_checks.revision
        if current != self._controller_rendered:
            self._controller_rendered = current
            self.ui.query_one("#status-bar", Static).update(f"Controller releases: {releases.controller_state(result)}")
            self.ui.action_refresh()

    def _identify_controller(self):
        from . import controller_install, updates

        try:
            controller_install.confirm_startup()
            if self._closing.is_set():
                return
            identity = controller_install.identify()
            standalone = False
            if identity.owner == "unsupported" and constants.APP_SELF_UPDATES_ENABLED:
                try:
                    updates.eligible()
                except updates.UpdateError:
                    pass
                else:
                    standalone = True
        except Exception:
            logging.exception("Cannot inspect the controller installation during startup")
            return

        self._ui_call(self._controller_identified, identity, standalone)

    def _controller_identified(self, identity, standalone):
        # An explicit check may have supplied a newer identity while startup probed.
        if self._closing.is_set() or self._controller_identity is not None:
            return
        self._controller_identity = identity
        self._controller_standalone = standalone
        self.poll_controller_releases()

    def _check_controller(self, app):
        import shlex
        from . import controller_install

        self._controller_identity = controller_install.identify()
        self.status("Checking the controller installation provider")
        result = controller_install.check(self._controller_identity)
        self._controller_result = result
        text = f"{result.status}: {result.message}"
        if result.command:
            text += "\n" + shlex.join(result.command)
        self.status(text)
        self._ui_call(self.poll_controller_releases)
        self.info(text)

    def _handoff_controller(self, app):
        import shlex
        from . import controller_install

        identity = controller_install.identify()
        restart = ("The terminal supplies the command to restart this profile after AUR installation."
                   if identity.owner == "aur" else
                   "The environment helper launches the new controller after installation.")
        if not self.approve("Open controller update in a separate terminal?",
                            "The terminal owns review and authorization. " + restart
                            + " This TUI closes after handoff. Logos is left running."):
            return
        self.checkpoint()
        result = utils.handoff_controller(self, identity, "tui")
        self._controller_identity = identity
        self._controller_result = result
        if result.status == "handed-off":
            self._ui_call(self.ui.action_quit)
            return
        text = f"{result.status}: {result.message}"
        if result.status == "manual":
            text += "\nClose this controller before running the command in a terminal."
        if result.command:
            text += "\n" + shlex.join(result.command)
        self.status(text)
        self.info(text)

    def _ask(self, question: str, options: list[str] | str) -> Optional[str]:
        screen: PromptModal
        if isinstance(options, str):
            default = str(Path.home()) if options == constants.PROMPT_OPTION_DIRECTORY else ""
            screen = InputModal(question, default=default)
        else:
            screen = MenuModal(question, list(options) + ["Return to Main Menu"])
        answer = self._wait_for_prompt(screen)
        if answer in constants.PROMPT_OPTION_SIGILS:
            default = str(Path.home()) if answer == constants.PROMPT_OPTION_DIRECTORY else ""
            answer = self._wait_for_prompt(InputModal(question, default=default))
        return None if answer == "Return to Main Menu" else answer or None

    def _info(self, message: str) -> None:
        self._wait_for_prompt(InfoModal(message))

    def _wait_for_prompt(self, screen):
        if threading.get_ident() == self.ui._thread_id:
            raise RuntimeError("Blocking prompts must run outside the Textual event thread")
        event = threading.Event()
        result = [None, False]
        with self._prompt_lock:
            if self._closing.is_set():
                raise UserExitedFromAsk
            self._pending_prompts[event] = result

        def done(choice=None, *, accepted=True):
            with self._prompt_lock:
                if self._pending_prompts.pop(event, None) is None:
                    return
                result[:] = [choice, accepted]
            event.set()

        screen.cancel_waiter = lambda: done(accepted=False)
        try:
            self.ui.call_from_thread(self.ui.push_screen, screen, done)
        except RuntimeError:
            done(accepted=False)
        event.wait()
        if not result[1]:
            raise UserExitedFromAsk
        return result[0]

    def cancel_pending_prompts(self):
        with self._prompt_lock:
            self._closing.set()
            waiting = list(self._pending_prompts)
            self._pending_prompts.clear()
        for event in waiting:
            event.set()

    def _ui_call(self, callback, *args) -> None:
        if self._closing.is_set():
            return
        if threading.get_ident() == self.ui._thread_id:
            callback(*args)
        else:
            self.ui.call_from_thread(callback, *args)

    def _status(self, message: str, percent: Optional[int] = None) -> None:
        message = message.strip()
        if self._last_console_message == message:
            return
        self._last_console_message = message
        try:
            self._ui_call(self.ui.write_status, message)
        except Exception:
            pass

    def _pop_up(self, title: str, message: str) -> None:
        try:
            self._ui_call(self.ui.push_screen, InfoModal(message, title=title))
        except Exception:
            pass

    def _exit(self, reason: str, intended: bool = False) -> None:
        msg = f"Exiting {constants.APP_NAME} due to {reason}…"
        if not intended and hasattr(constants, "SUPPORT_MESSAGE"):
            msg += "\n" + constants.SUPPORT_MESSAGE
        self._status(msg)
        try:
            self.ui.exit()
        except Exception:
            pass

    def set_title(self) -> None:
        version = getattr(constants, "LLI_CURRENT_VERSION", "")
        channel = getattr(self.conf, "app_release_channel", "")
        self.ui.title_text = f"Welcome to {constants.APP_NAME} {version} ({channel})"

        product = (getattr(self.conf._raw, "faithlife_product", None) or constants.FAITHLIFE_PRODUCTS[0])
        if self.is_installed():
            rel = getattr(self.conf, "installed_faithlife_product_release", "?")
            ch = getattr(self.conf, "faithlife_product_release_channel", "")
            self.ui.subtitle_text = f"{product} Version: {rel} ({ch})"
        else:
            self.ui.subtitle_text = f"{product} not installed"

    def go_to_main_menu(self) -> None:
        self.ui.go_to_main_menu()

    def _config_update_hook(self) -> None:
        try:
            self._ui_call(self.ui.action_refresh)
        except Exception:
            pass

    def set_tui_menu_options(self) -> list[str]:
        labels: list[str] = []

        identity = getattr(self, "_controller_identity", None)
        managed = identity is not None and identity.owner in ("venv", "aur")
        if managed:
            result = self._controller_result
            labels.append("Check controller updates" if result is None else "Check controller updates again")
            if result is not None and result.status == "available":
                labels.append("Update via AUR" if identity.owner == "aur" else "Update environment")
        elif (constants.APP_SELF_UPDATES_ENABLED and getattr(constants, "RUNMODE", "") == "binary"
              and (not hasattr(self, "_controller_identity") or self._controller_standalone)):
            from . import releases

            checks = self._controller_checks
            if releases.controller_state(checks.result) == "available" and not checks.eligibility_message:
                labels.append(f"Update {constants.APP_NAME}")
            labels.append("Check controller releases now")

        if self.is_installed():
            product = self.conf.faithlife_product
            if self.logos.logos_state in (logos.State.STARTING, logos.State.RUNNING):
                labels.append(f"Stop {product}")
            elif self.logos.logos_state == logos.State.STOPPING:
                labels.append(f"Stopping {product}")
            else:
                labels.append(f"Run {product}")

            if self.logos.indexing_state in (logos.State.STARTING, logos.State.RUNNING):
                labels.append("Stop Indexing")
            elif self.logos.indexing_state == logos.State.STOPPING:
                labels.append("Stopping Indexing")
            else:
                labels.append("Run Indexing")

            if getattr(self.conf, "is_installed_faithlife_product_release_latest", None) is False:
                labels.append(f"Update {product}")
        else:
            labels.extend(["Install", "Advanced Install"])

        labels.extend(["Utilities →", "Wine Config →", "Change Color Scheme", "Get Support", "Exit"])
        return labels

    def _run_operation(self, operation, *, assume_yes=None):
        with self._operation_lock:
            if self._closing.is_set():
                return None
            if self._operation_active:
                self.status("Another operation is still running")
                return None
            self._operation_active = True

        def run():
            original_assume_yes = self.conf._overrides.assume_yes
            failure = None
            try:
                if assume_yes is not None:
                    self.conf._overrides.assume_yes = assume_yes
                result = operation(self)
                from . import updates

                if isinstance(result, updates.PreparedUpdate):
                    if self._closing.is_set():
                        result.cancel()
                    else:
                        try:
                            arguments = updates.resume_arguments(self.conf._overrides.config_path, "tui")
                            self._ui_call(self._restart_controller, result, arguments)
                        finally:
                            result.cancel()
            except UserExitedFromAsk:
                self.status("Operation cancelled")
            except Exception as error:
                logging.exception("TUI operation failed")
                failure = str(error) or type(error).__name__
                self.status(f"Operation failed: {error}")
            finally:
                if assume_yes is not None:
                    self.conf._overrides.assume_yes = original_assume_yes
                with self._operation_lock:
                    self._operation_active = False
                self._ui_call(self._operation_finished, failure)

        try:
            return self.start_thread(run, daemon_bool=False)
        except BaseException:
            with self._operation_lock:
                self._operation_active = False
            raise

    def _operation_finished(self, failure):
        self.ui.go_to_main_menu()
        if failure:
            self.ui.push_screen(InfoModal(failure, title="Operation failed"))

    def _restart_controller(self, prepared, arguments):
        from . import releases, updates

        stopped = False
        try:
            self._controller_checks.close()
            stopped = True
            prepared.restart(arguments)
        finally:
            if stopped and not self._closing.is_set():
                self._controller_checks = releases.ControllerChecks(
                    self.conf._network, updates.eligible, self.conf.refresh_release_snapshots,
                )

    def main_menu_select(self, choice: str) -> None:
        if choice in (None, "Exit"):
            self.ui.action_quit()
            return

        if choice in ("Install", "Advanced Install"):
            self._installer_thread = self._run_operation(installer.install, assume_yes=choice == "Install")
            self.status("Starting installation…")

        elif choice in ("Update via AUR", "Update environment"):
            self._run_operation(self._handoff_controller)

        elif choice in ("Check controller updates", "Check controller updates again"):
            self._run_operation(self._check_controller)

        elif choice.startswith(f"Update {constants.APP_NAME}"):
            self._run_operation(utils.update_to_latest_lli_release)

        elif choice == "Check controller releases now":
            self._controller_checks.request(self.conf.app_release_channel, force=True)

        elif self.conf._raw.faithlife_product and choice == f"Update {self.conf.faithlife_product}":
            self._run_operation(utils.update_faithlife_product)

        elif self.conf._raw.faithlife_product and choice == f"Run {self.conf._raw.faithlife_product}":
            self._run_operation(lambda app: app.logos.start())

        elif self.conf._raw.faithlife_product and choice == f"Stop {self.conf.faithlife_product}":
            self._run_operation(lambda app: app.logos.stop())
            self.ui._populate_main_menu()

        elif choice == "Run Indexing":
            self._run_operation(lambda app: app.logos.index())

        elif choice == "Stop Indexing":
            self._run_operation(lambda app: app.logos.stop_indexing())

        elif choice == "Remove Library Catalog":
            self._run_operation(control.remove_library_catalog)

        elif choice.startswith("Wine Config"):
            self.ui.push_screen(
                MenuModal("Wine Config Menu", self.set_wineconfig_menu_options()),
                callback=self.wineconfig_menu_select)

        elif choice.startswith("Utilities"):
            self.ui.push_screen(
                MenuModal("Utilities Menu", self.set_utilities_menu_options()),
                callback=self.utilities_menu_select)

        elif choice == "Change Color Scheme":
            theme = self.ui.toggle_color_scheme()
            scheme = "Logos Colors" if theme == "logos" else "System Colors"
            self.status(f"Color scheme: {scheme}")

        elif choice == "Get Support":
            self._run_operation(control.get_support)

    def wineconfig_menu_select(self, choice: str | None) -> None:
        if not choice or choice == "Return to Main Menu":
            self.go_to_main_menu()
            return
        if choice == "Set Renderer":
            self.ui.push_screen(
                MenuModal("Choose Renderer", ["gdi", "gl", "dxvk", "vulkan", "Return to Main Menu"]),
                callback=self.renderer_select)
        elif choice == "Set Windows Version for Logos":
            self.ui.push_screen(MenuModal("Windows Version for Logos", constants.WINDOWS_VERSIONS),
                                callback=self.win_ver_logos_select)
        elif choice == "Set Windows Version for Indexer":
            self.ui.push_screen(MenuModal("Windows Version for Indexer", constants.WINDOWS_VERSIONS),
                                callback=self.win_ver_index_select)

    def utilities_menu_select(self, choice: str | None) -> None:
        if not choice or choice == "Return to Main Menu":
            self.go_to_main_menu()
            return
        operations = {
            "Remove Library Catalog": control.remove_library_catalog,
            "Remove All Index Files": control.remove_all_index_files,
            "Uninstall": control.uninstall,
            "Install ICU": wine.enforce_icu_data_files,
            "Update to Latest AppImage": utils.update_to_latest_recommended_appimage,
            "Install Dependencies": utils.install_dependencies,
            "Edit Config": lambda app: control.edit_file(app.conf.config_file_path),
            "Reload Config": lambda app: app.conf.reload(),
            "Change Logos Release Channel": lambda app: app.conf.toggle_faithlife_product_release_channel(),
            f"Change {constants.APP_NAME} Release Channel": lambda app: app.conf.toggle_installer_release_channel(),
            "Check OpenGL": lambda app: app.status(system.check_opengl_version(app)[1]),
        }
        if choice in operations:
            self._run_operation(operations[choice])
            return
        if choice in ("Enable Logging", "Disable Logging"):
            action = "enable" if choice == "Enable Logging" else "disable"
            self._run_operation(lambda app: app.logos.switch_logging(action))
            return
        self.go_to_main_menu()

    def renderer_select(self, choice: str | None) -> None:
        if choice in ("gdi", "gl", "dxvk", "vulkan"):
            @maintenance.uses_installation
            def configure(app):
                wine.set_renderer(app, app.conf.wine64_binary, choice)
                app.status(f"Renderer set to {choice}")
            self._run_operation(configure)

    def win_ver_logos_select(self, choice: str | None) -> None:
        if choice in ("vista", "win7", "win8", "win10", "win11"):
            self._run_operation(lambda app: wine.set_win_version(app, "logos", choice))

    def win_ver_index_select(self, choice: str | None) -> None:
        if choice in ("vista", "win7", "win8", "win10", "win11"):
            self._run_operation(lambda app: wine.set_win_version(app, "indexer", choice))

    def set_wineconfig_menu_options(self) -> list[str]:
        return [
            "Set Renderer",
            "Set Windows Version for Logos",
            "Set Windows Version for Indexer",
            "Return to Main Menu",
        ]

    def set_utilities_menu_options(self) -> list[str]:
        labels: list[str] = []
        if self.is_installed():
            labels.extend(["Remove Library Catalog", "Remove All Index Files", "Install ICU"])
        if (
            self.is_installed()
            or Path(self.conf.config_file_path).exists()
            or (getattr(self.conf._raw, "install_dir", None) and Path(self.conf._raw.install_dir).exists())
        ):
            labels.append("Uninstall")
        labels.extend(["Install Dependencies", "Edit Config", "Reload Config"])
        if self.is_installed():
            labels.extend([
                "Change Logos Release Channel",
                f"Change {constants.APP_NAME} Release Channel",
            ])
        if self.is_installed():
            labels.append("Disable Logging" if self.conf.faithlife_product_logging else "Enable Logging")
        labels.append("Check OpenGL")
        labels.append("Return to Main Menu")
        return labels


def control_panel_app(ephemeral_config: EphemeralConfiguration) -> None:
    tui = TUI(ephemeral_config)
    try:
        tui.ui.run()
    finally:
        tui.cancel_pending_prompts()
        if not tui.close():
            raise RuntimeError("Controller closed with an operation still finishing")
        print()
    if tui._pending_exit is not None and not tui._pending_exit[1]:
        raise SystemExit(1)
