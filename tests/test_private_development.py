import importlib
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from ou_dedetai import constants, control, network, tui_app, utils
from ou_dedetai.config import Config
from ou_dedetai.app import App, UserExitedFromAsk

try:
    importlib.import_module("tkinter")
except ImportError as error:
    GUI_IMPORT_ERROR = str(error)
    gui_app = None
else:
    from ou_dedetai import gui_app

    GUI_IMPORT_ERROR = ""


class TestPrivateUpdates(unittest.TestCase):
    def test_discovery_ignores_cold_and_warm_upstream_caches(self):
        for warm in (False, True):
            for channel in ("stable", "beta"):
                with self.subTest(warm=warm, channel=channel):
                    requests = object.__new__(network.NetworkRequests)
                    requests._cache = network.CachedRequests()
                    if warm:
                        for repository in ("FaithLife-Community/OuDedetai", "FaithLife-Community/test-builds"):
                            requests._cache.repository_latest_version[repository] = "99.0"
                            requests._cache.repository_latest_url[repository] = f"https://github.com/{repository}/old"

                    with patch.object(requests, "_repo_latest_version") as lookup:
                        self.assertIsNone(requests.app_latest_version(channel))
                        lookup.assert_not_called()

    def test_unavailable_release_reaches_both_config_projections_and_comparison(self):
        config = object.__new__(Config)
        config._raw = SimpleNamespace(app_release_channel="stable")
        config._network = object.__new__(network.NetworkRequests)

        self.assertIsNone(config.app_latest_version)
        self.assertIsNone(config.app_latest_version_url)
        self.assertIsNone(utils.compare_logos_linux_installer_version(SimpleNamespace(conf=config)))

    def test_available_release_projections_are_preserved(self):
        config = object.__new__(Config)
        config._raw = SimpleNamespace(app_release_channel="stable")
        config._network = Mock()
        config._network.app_latest_version.return_value = network.SoftwareReleaseInfo(
            version="4.0.1",
            download_url="https://github.com/sdwoodard/logos-desktop/releases/download/test/controller",
        )

        self.assertEqual(config.app_latest_version, "4.0.1")
        self.assertEqual(
            config.app_latest_version_url,
            "https://github.com/sdwoodard/logos-desktop/releases/download/test/controller",
        )

    def test_both_update_entry_points_refuse_before_config_or_file_work(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            cached = Path(directory) / constants.BINARY_NAME
            launcher.write_bytes(b"installed launcher sentinel")
            cached.write_bytes(b"cached executable sentinel")

            for update in (utils.update_to_latest_lli_release, network.update_lli_binary):
                with self.subTest(update=update.__name__):
                    app = SimpleNamespace(status=Mock())
                    with (
                        patch.object(network.sys, "argv", [str(launcher)]),
                        patch.object(network.os.path, "realpath") as resolve,
                        patch.object(utils, "compare_logos_linux_installer_version") as compare,
                        patch.object(utils, "get_lli_release_version") as probe,
                        patch.object(network, "logos_reuse_download") as download,
                        patch.object(network.shutil, "copy") as copy,
                        patch.object(network.shutil, "move") as move,
                        patch.object(utils, "restart_lli") as restart,
                    ):
                        update(app)

                        for operation in (resolve, compare, probe, download, copy, move, restart):
                            operation.assert_not_called()

                    app.status.assert_called_once_with(constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
                    self.assertEqual(launcher.read_bytes(), b"installed launcher sentinel")
                    self.assertEqual(cached.read_bytes(), b"cached executable sentinel")
                    self.assertEqual(set(Path(directory).iterdir()), {launcher, cached})

    def test_binary_tui_does_not_offer_or_check_controller_update(self):
        app = SimpleNamespace(is_installed=Mock(return_value=False))
        with (
            patch.object(constants, "RUNMODE", "binary"),
            patch.object(utils, "compare_logos_linux_installer_version") as compare,
        ):
            options = tui_app.TUI.set_tui_menu_options(app)

        compare.assert_not_called()
        self.assertNotIn(f"Update {constants.APP_NAME}", options)
        self.assertIn("Install", options)


class TestPrivateSupport(unittest.TestCase):
    def test_support_choices_keep_archive_local_or_open_only_private_project(self):
        for choice in ("Keep archive local", "Open private project issue", "Show links"):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path = root / "fixture-config.json"
                config_path.write_text("{}\n")
                archive_path = root / "support.zip"
                app = Mock()
                app.conf = SimpleNamespace(
                    config_file_path=str(config_path),
                    app_log_path=str(root / "absent-app.log"),
                    app_wine_log_path=str(root / "absent-wine.log"),
                    app_wine_log_previous_path=str(root / "absent-previous.log"),
                    _faithlife_crash_log=None,
                    _raw=SimpleNamespace(wine_binary=None, _as_dict=lambda: {}),
                    _overrides=SimpleNamespace(),
                    _network=SimpleNamespace(_cache=SimpleNamespace(_as_dict=lambda: {})),
                )
                def answer(question, options):
                    if "Where do you" in question:
                        return str(archive_path)
                    with ZipFile(archive_path) as completed:
                        self.assertIsNone(completed.testzip())
                        self.assertIn("context.json", completed.namelist())
                    return choice

                def status(message, percent=None):
                    if message.startswith("Wrote support"):
                        with ZipFile(archive_path) as completed:
                            self.assertIsNone(completed.testzip())

                app.ask.side_effect = answer
                app.status.side_effect = status

                with (
                    patch.object(control.os.path, "expanduser", return_value=str(root)),
                    patch.object(control.subprocess, "check_output", return_value="synthetic command output"),
                    patch.object(control.system, "get_dialog", return_value="cli"),
                    patch.object(control.webbrowser, "open") as browser,
                ):
                    self.assertEqual(control.get_support(app), str(archive_path))
                self.assertEqual(stat.S_IMODE(archive_path.stat().st_mode), 0o600)

                question, options = app.ask.call_args.args
                self.assertEqual(options, ["Keep archive local", "Open private project issue", "Show links"])
                self.assertNotIn("Upload", question)
                if choice == "Open private project issue":
                    browser.assert_called_once_with("https://github.com/sdwoodard/logos-desktop/issues/new")
                else:
                    browser.assert_not_called()

                if choice == "Show links":
                    links = app.info.call_args.args[0]
                    self.assertIn("https://github.com/sdwoodard/logos-desktop/issues", links)
                    self.assertNotIn("FaithLife-Community", links)
                    self.assertNotIn("t.me", links)
                    self.assertNotIn("matrix.to", links)

                with ZipFile(archive_path) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertIn("context.json", archive.namelist())

    def test_failed_archive_close_or_publication_preserves_previous_output(self):
        class CloseFailure(ZipFile):
            def close(self):
                writing = self.fp is not None and self.mode == "w"
                super().close()
                if writing:
                    raise OSError("injected ZIP finalization failure")

        for failure in ("close", "replace"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "support.zip"
                archive.write_bytes(b"previous archive sentinel")
                absent = str(root / "absent")
                app = Mock()
                app.conf = SimpleNamespace(
                    config_file_path=absent, app_log_path=absent, app_wine_log_path=absent,
                    app_wine_log_previous_path=absent, _faithlife_crash_log=None,
                    _raw=SimpleNamespace(wine_binary=None, _as_dict=lambda: {}),
                    _overrides=SimpleNamespace(),
                    _network=SimpleNamespace(_cache=SimpleNamespace(_as_dict=lambda: {})),
                )
                app.ask.return_value = str(archive)
                boundary = (
                    patch.object(control, "ZipFile", CloseFailure) if failure == "close"
                    else patch.object(control.os, "replace", side_effect=OSError("injected replace failure"))
                )
                with (
                    boundary,
                    patch.object(control.subprocess, "check_output", return_value="fixture"),
                    patch.object(control.system, "get_dialog", return_value="cli"),
                    patch.object(control.webbrowser, "open") as browser,
                    self.assertRaises(OSError),
                ):
                    control.get_support(app)
                self.assertEqual(archive.read_bytes(), b"previous archive sentinel")
                self.assertEqual(app.ask.call_count, 1)
                self.assertFalse(any(call.args[0].startswith("Wrote support") for call in app.status.call_args_list))
                browser.assert_not_called()


@unittest.skipIf(gui_app is None, f"Native Tk import unavailable: {GUI_IMPORT_ERROR}")
class TestPrivateGuiUpdates(unittest.TestCase):
    def test_post_uninstall_empty_selection_still_offers_install_controls(self):
        window = object.__new__(gui_app.ControlWindow)
        window.conf = SimpleNamespace(_raw=SimpleNamespace(faithlife_product=None))
        window.gui = Mock()
        window.is_installed = Mock(return_value=False)
        window.update_app_button()
        window.gui.app_button.state.assert_called_with(["!disabled"])
        window.gui.app_install_advanced.state.assert_called_with(["!disabled"])

    def test_native_save_cancellation_and_prompt_retry_cancel(self):
        window = object.__new__(gui_app.ControlWindow)
        window.root = Mock()
        window.conf = SimpleNamespace(_overrides=SimpleNamespace(assume_yes=False))
        with patch.object(gui_app.fd, "asksaveasfilename", return_value=""):
            self.assertIsNone(gui_app.GuiApp._ask(window, "Output", constants.PROMPT_OPTION_NEW_FILE))
        window._ask = Mock(side_effect=["invalid", None])
        with self.assertRaises(UserExitedFromAsk):
            App.ask(window, "Choose", ["valid"])
        self.assertEqual(window._ask.call_count, 2)

    def test_binary_window_initialization_does_not_start_release_discovery(self):
        def initialize_fixture(window, *args, **kwargs):
            window.conf = SimpleNamespace(_raw=SimpleNamespace(faithlife_product=None))

        with (
            patch.object(constants, "RUNMODE", "binary"),
            patch.object(gui_app.GuiApp, "__init__", initialize_fixture),
            patch.object(gui_app.ControlWindow, "start_thread") as start_thread,
            patch.object(gui_app.ControlWindow, "clear_status"),
            patch.object(gui_app.ControlWindow, "_config_update_hook"),
            patch.object(gui_app.ControlWindow, "config_updated_hooks", []),
            patch.object(gui_app.gui, "ToolTip"),
            patch.object(utils, "compare_logos_linux_installer_version") as compare,
        ):
            widgets = Mock()
            gui_app.ControlWindow(Mock(), widgets, Mock())

        compare.assert_not_called()
        callbacks = [call.args[0].__name__ for call in start_thread.call_args_list]
        self.assertEqual(callbacks, ["_monitor_faithlife_product_pids"])
        label_text = widgets.update_lli_label.config.call_args.kwargs["text"]
        self.assertIn(constants.APP_SELF_UPDATE_DISABLED_MESSAGE, label_text)
        widgets.update_lli_button.state.assert_called_with(["disabled"])

    def test_unavailable_binary_release_keeps_label_and_refresh_defined(self):
        window = object.__new__(gui_app.ControlWindow)
        window.conf = SimpleNamespace(app_latest_version=None)
        window.gui = Mock()

        with (
            patch.object(constants, "APP_SELF_UPDATES_ENABLED", True),
            patch.object(constants, "RUNMODE", "binary"),
            patch.object(gui_app.ControlWindow, "clear_status"),
            patch.object(gui_app.gui, "ToolTip") as tooltip,
        ):
            window.update_controller_version()

        self.assertIn("latest: unavailable", window.gui.update_lli_label.config.call_args.kwargs["text"])
        window.gui.update_lli_button.state.assert_called_once_with(["disabled"])
        self.assertIn("No controller release information", tooltip.call_args.args[1])
