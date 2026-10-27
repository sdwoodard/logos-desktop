import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from logos_desktop import constants, control, network, support, tui_app, updates, utils
from logos_desktop.config import Config


class TestPrivateUpdates(unittest.TestCase):
    def test_discovery_ignores_cold_and_warm_upstream_caches(self):
        for warm in (False, True):
            for channel in ("stable", "beta"):
                with self.subTest(warm=warm, channel=channel):
                    requests = object.__new__(network.NetworkRequests)
                    requests._cache = network.CachedRequests()
                    if warm:
                        for repository in ("other-owner/old-controller", "other-owner/test-builds"):
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
                        patch.object(updates, "eligible") as admit,
                        patch.object(updates, "prepare") as prepare,
                    ):
                        update(app)

                        for operation in (admit, prepare):
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
        for choice in ("Keep archive local", "Open project issue", "Show links"):
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
                        self.assertEqual(completed.namelist(), ["manifest.json", "doctor.json", "summary.txt"])
                    return choice

                def status(message, percent=None):
                    if message.startswith("Wrote support"):
                        with ZipFile(archive_path) as completed:
                            self.assertIsNone(completed.testzip())

                app.ask.side_effect = answer
                app.status.side_effect = status

                with (
                    patch.object(control.os.path, "expanduser", return_value=str(root)),
                    patch.object(control.system, "run_command") as browser,
                ):
                    self.assertEqual(control.get_support(app), str(archive_path))
                self.assertEqual(stat.S_IMODE(archive_path.stat().st_mode), 0o600)

                question, options = app.ask.call_args.args
                self.assertEqual(options, ["Keep archive local", "Open project issue", "Show links"])
                self.assertNotIn("Upload", question)
                if choice == "Open project issue":
                    browser.assert_called_once_with(
                        ["xdg-open", "https://github.com/sdwoodard/logos-desktop/issues/new"],
                        timeout=10, check=False,
                    )
                else:
                    browser.assert_not_called()

                if choice == "Show links":
                    links = app.info.call_args.args[0]
                    self.assertIn("https://github.com/sdwoodard/logos-desktop/issues", links)
                    self.assertNotIn("t.me", links)
                    self.assertNotIn("matrix.to", links)

                with ZipFile(archive_path) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(archive.namelist(), ["manifest.json", "doctor.json", "summary.txt"])
                    preview = app.info.call_args_list[0].args[0]
                    for name in archive.namelist():
                        self.assertIn(archive.read(name).decode(), preview)
                    self.assertNotIn(str(root), preview)

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
                    patch.object(support, "ZipFile", CloseFailure) if failure == "close"
                    else patch.object(support.os, "replace", side_effect=OSError("injected replace failure"))
                )
                with (
                    boundary,
                    patch.object(control.system, "run_command") as browser,
                    self.assertRaises(OSError),
                ):
                    control.get_support(app)
                self.assertEqual(archive.read_bytes(), b"previous archive sentinel")
                self.assertEqual(app.ask.call_count, 1)
                self.assertFalse(any(call.args[0].startswith("Wrote support") for call in app.status.call_args_list))
                browser.assert_not_called()
