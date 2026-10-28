import json
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from types import SimpleNamespace

try:
    import PySide6  # noqa: F401
except ModuleNotFoundError:
    QT_AVAILABLE = False
    QtCase = unittest.TestCase
else:
    QT_AVAILABLE = True
    from logos_desktop import maintenance
    from logos_desktop.qt.services import Services
    from logos_desktop.qt.window import InstallDraft
    from tests.qt_fixtures import QtCase


@unittest.skipUnless(QT_AVAILABLE, "Optional Qt frontend is not installed")
class TestProductionServices(QtCase):
    def test_managed_controller_checks_are_explicit_cached_and_off_the_gui_thread(self):
        from logos_desktop import constants, controller_install

        ui_thread = threading.get_ident()
        threads = []

        def identify():
            threads.append(threading.get_ident())
            return controller_install.Identity("venv", "1.0.0", "1.0.0")

        with (patch.object(constants, "APP_SELF_UPDATES_ENABLED", False),
              patch.object(controller_install, "identify", side_effect=identify) as admission,
              patch.object(controller_install, "check", return_value=controller_install.Result(
                  "available", "Private wheel provider has version B")) as check):
            window = self.production_window()
            check.assert_not_called()
            self.assertEqual(admission.call_count, 1)
            self.assertNotIn(ui_thread, threads)
            for _ in range(3):
                window._render()
            self.assertEqual(admission.call_count, 1)
            self.assertFalse(window.home.updates.buttons["controller_update"].isHidden())
            self.assertFalse(window.home.updates.buttons["controller_update"].isEnabled())
            window.home.updates.buttons["controller_check"].click()
            self.pump(lambda: window.app.active is None)
            check.assert_called_once()
            self.assertTrue(window.home.updates.buttons["controller_update"].isEnabled())
            self.assertEqual(window.snapshot.controller_status, "available")
            self.assertIn("Private wheel provider", window.home.updates.controller.detail.text())

    def test_public_flag_does_not_admit_an_unsupported_source_controller(self):
        from logos_desktop import constants, controller_install, updates

        identity = controller_install.Identity("unsupported", "1.0.0", "", message="Source checkout")
        with (patch.object(constants, "APP_SELF_UPDATES_ENABLED", True),
              patch.object(controller_install, "identify", return_value=identity),
              patch.object(updates, "eligible", side_effect=updates.UpdateError("Source is not standalone")),
              patch.object(controller_install, "check") as check):
            window = self.production_window()
            self.assertTrue(window.home.updates.buttons["controller_update"].isHidden())
            self.assertFalse(window.home.updates.buttons["controller_check"].isEnabled())
            self.assertIn("Source checkout", window.home.updates.controller.detail.text())
            check.assert_not_called()

    def test_managed_handoff_closes_without_claiming_update_or_stopping_logos(self):
        from unittest.mock import Mock
        from logos_desktop import controller_install

        controller = self.root / "controller"
        controller.mkdir(mode=0o700)
        identity = controller_install.Identity("venv", "1.0.0", "1.0.0", root=controller)
        terminal = Mock()

        def accept(*args, **kwargs):
            window.app.cancel_operation()
            return terminal

        with (patch.object(controller_install, "identify", return_value=identity),
              patch.object(Path, "home", return_value=self.root),
              patch.object(controller_install, "check", return_value=controller_install.Result("available", "B")),
              patch.object(controller_install.shutil, "which", return_value="/fixture/konsole"),
              patch.object(controller_install.subprocess, "Popen", side_effect=accept) as popen):
            window = self.production_window()
            stop = Mock(side_effect=AssertionError("Controller close must not stop Logos"))
            window.app.logos.stop = stop
            window.app.approve = Mock(return_value=True)
            failure = Mock()
            window.dialogs.failure = failure
            window.start(window.services.operation("controller_update"))
            self.pump(lambda: window._final_close)
            popen.assert_called_once()
            terminal.terminate.assert_not_called()
            terminal.kill.assert_not_called()
            self.assertIn("helper launches the new controller", window.app.approve.call_args.args[1])
            failure.assert_not_called()
            stop.assert_not_called()
            self.assertEqual(window.services.snapshot().controller_status, "handed-off")

    def test_cancel_during_real_handoff_provider_restores_lease_without_opening_terminal(self):
        from logos_desktop import constants, controller_install, instance

        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        identity = controller_install.Identity("venv", "1.0.0", "1.0.0")

        def provider(owner):
            entered.set()
            if not finish.wait(5):
                raise AssertionError("Delayed provider was not released")
            return controller_install.Result("available", "Private provider has B")

        with (patch.object(constants, "COORDINATION_DIR", self.root / "coordination"),
              patch.object(instance, "check_legacy"),
              patch.object(controller_install, "identify", return_value=identity),
              patch.object(controller_install, "recover", return_value=controller_install.Result("recovered", "Ready")),
              patch.object(controller_install, "check", side_effect=provider),
              patch.object(controller_install.shutil, "which", return_value="/fixture/konsole"),
              patch.object(controller_install.subprocess, "Popen") as popen):
            window = self.production_window()
            session = instance.Session(profile=self.config)
            self.addCleanup(session.close)
            session.acquire()
            session.listen()
            window.app._instance_session = session
            window.app.approve = lambda *args: True
            window.start(window.services.operation("controller_update"))
            try:
                self.pump(entered.is_set)
                self.assertIsNone(window.app._instance_session)
                window.app.cancel_operation()
            finally:
                finish.set()
            self.pump(lambda: window.app.active is None)
            popen.assert_not_called()
            self.assertIs(window.app._instance_session, session)
            self.assertTrue(session.listening)
            with self.assertRaises(instance.InstanceBusy):
                instance.Session(profile=self.config).acquire()
            self.assertFalse(window._closing)
            self.assertFalse(window.services.controller_handed_off)
            self.assertIn("cancelled", window.status.text().lower())

    def test_manual_and_unknown_controller_results_stay_in_provider_state(self):
        from unittest.mock import Mock
        from logos_desktop import controller_install, utils

        identity = controller_install.Identity("venv", "1.0.0", "1.0.0")
        command = ("/private root/python", "-I", "-m", "logos_desktop.controller_install", "recover")
        with patch.object(controller_install, "identify", return_value=identity):
            window = self.production_window()
            window.app.approve = lambda *args: True
            failure = Mock()
            window.dialogs.failure = failure
            for state in ("manual", "unknown", "offline", "provider-lag", "restart-required"):
                with patch.object(utils, "handoff_controller", return_value=controller_install.Result(
                        state, "Inspect the installation", command=command)):
                    window.start(window.services.operation("controller_update"))
                    self.pump(lambda: window.app.active is None)
                self.assertEqual(window.snapshot.controller_status, state)
                self.assertIn("'/private root/python'", window.home.updates.controller.detail.text())
                self.assertFalse(window._closing)
                self.assertFalse(window.home.updates.buttons["controller_update"].isEnabled())
            failure.assert_not_called()

    def production_window(self):
        window = self.window(Services())
        self.pump(lambda: window.app.active is None)
        if not hasattr(window.app, "profile_selection"):
            @contextmanager
            def selection(candidate):
                from logos_desktop.logos import LogosManager
                if not window.app.logos.close():
                    raise maintenance.MaintenanceBusy("Fixture manager did not settle")
                yield
                window.app.conf = candidate
                window.app.logos = LogosManager(window.app)
                window.app.logos.start_monitoring()
            self.stack.enter_context(patch.object(window.app, "profile_selection", selection, create=True))
        return window

    def test_production_bootstrap_and_snapshot_do_not_prompt_or_fetch(self):
        with (patch("logos_desktop.metadata.request", side_effect=AssertionError("Unexpected network"), create=True),
              patch("logos_desktop.wine.get_wine_release", side_effect=AssertionError("Unexpected Wine probe"))):
            window = self.production_window()
            window._render()
        self.assertIn("not checked", window.snapshot.wine_status)
        self.assertFalse(window.snapshot.installed)
        self.assertEqual(window.snapshot.profile, str(self.config))
        self.assertTrue(window.maintenance.buttons["repair"].isEnabled())
        self.assertFalse(window.home.run.isEnabled())
        self.assertFalse(window.home.updates.buttons["controller_update"].isVisible())
        self.assertFalse(window.home.updates.buttons["controller_check"].isEnabled())
        self.assertEqual(window.install.product.count(), 1)
        self.assertEqual(window.install.product.currentText(), "Logos")

    def test_cached_wine_states_flow_from_worker_snapshot_without_probe_fetch_or_write(self):
        from logos_desktop import releases
        from PySide6.QtCore import Qt

        native = "/fixture/selected-wine-10.8"
        document = json.loads(self.config.read_text())
        document["wine_binary"] = native
        self.config.write_text(json.dumps(document))
        self.original = self.config.read_bytes()
        now = time.time()
        asset = releases.SoftwareReleaseInfo("11.20", "https://example.invalid/current.AppImage")
        available = releases.ReleaseLookup(asset, "ok", checked=now)
        cases = (
            (available, "Wine development 11.20 available"),
            (releases.ReleaseLookup(status="unavailable", checked=now), "unavailable"),
            (replace(available, stale=True), "stale, availability unverified"),
            (replace(available, checked=None), "not checked"),
            (replace(available, release=replace(asset, version="11.21")), "Wine development 11.21 available"),
            (releases.ReleaseLookup(), "not checked"),
        )
        with (patch("logos_desktop.metadata.request", side_effect=AssertionError("Unexpected network")),
              patch("logos_desktop.wine.get_wine_release", side_effect=AssertionError("Unexpected Wine probe"))):
            window = self.production_window()
            client = window.app.conf._network
            for cached_provider, expected in cases:
                with (
                    self.subTest(provider=cached_provider),
                    patch.object(client, "wine_release_target", side_effect=AssertionError("Source is not a gate")),
                    patch.object(client, "wine_appimage_release", return_value=cached_provider) as read_provider,
                ):
                    window.start(window.services.refresh)
                    self.pump(lambda: window.app.active is None)
                    snapshot = window.services.snapshot()
                    self.assertEqual(snapshot.runtime, native)
                    self.assertIn("version not checked", snapshot.wine_status)
                    self.assertIn(expected, snapshot.wine_status)
                    self.assertIn(f"AppImage provider {releases.WINE_REPOSITORY}: {expected}.", snapshot.wine_status)
                    read_provider.assert_called_once_with(refresh=False)
                    read_provider.side_effect = AssertionError("Rendering must not read release state")
                    window._render()
                    self.assertEqual(window.profile_settings.wine_status.text(), snapshot.wine_status)
                    self.assertEqual(window.maintenance.wine_status.textFormat(), Qt.TextFormat.PlainText)
                    self.assertEqual(window.maintenance.buttons["runtime_update"].toolTip(), snapshot.wine_status)
                    self.assertEqual(self.config.read_bytes(), self.original)

    def test_refresh_and_discovery_discard_changed_profiles_and_clear_prior_choices(self):
        from unittest.mock import Mock
        from logos_desktop import releases, utils
        from logos_desktop.config import PersistentConfiguration
        from logos_desktop.logos import State

        client = Mock()
        conf = SimpleNamespace(
            config_file_path="/fixture/first.json", _raw=PersistentConfiguration(faithlife_product="Logos"),
            _overrides=SimpleNamespace(install_dir=None), _network=client, logos_exe=None,
            faithlife_product_release_channel="stable", app_release_channel="stable", faithlife_product_logging=False,
        )
        other = SimpleNamespace(config_file_path="/fixture/second.json")
        app = SimpleNamespace(conf=conf, checkpoint=Mock(), logos=SimpleNamespace(
            logos_state=State.STOPPED, indexing_state=State.STOPPED, enforcement_error="",
        ))
        services = Services()
        services._snapshot = replace(services.snapshot(), profile=other.config_file_path, runtimes=("/other/wine",))
        previous = services.snapshot()

        def change_profile(**kwargs):
            app.conf = other
            return releases.ReleaseLookup()

        client.wine_appimage_release.side_effect = change_profile
        services.refresh(app, threading.Event())
        self.assertIs(services.snapshot(), previous)
        app.conf = conf
        client.wine_appimage_release.side_effect = None
        client.wine_appimage_release.return_value = releases.ReleaseLookup()
        services.refresh(app, threading.Event())
        self.assertEqual(services.snapshot().profile, conf.config_file_path)
        self.assertEqual(services.snapshot().runtimes, ())
        current = services.snapshot()

        def changed_discovery(owner):
            app.conf = other
            return ["/first/wine"]

        with patch.object(utils, "get_wine_options", side_effect=changed_discovery):
            services.discover(app, threading.Event())
        self.assertIs(services.snapshot(), current)

    def test_enabled_controller_update_uses_stable_exact_qt_restart_and_cleans_cancelled_stage(self):
        from unittest.mock import Mock
        from logos_desktop import constants
        with (patch.multiple(constants, APP_SELF_UPDATES_ENABLED=True, APP_RELEASE_VERSION="1.0.0"),
              patch("logos_desktop.updates.eligible")):
            window = self.production_window()
            self.assertIn("controller_update", window.home.updates.buttons)
            for cancel in (False, True):
                prepared = Mock()

                def prepare(app, *, channel):
                    self.assertEqual(channel, "stable")
                    if cancel:
                        app.cancel_operation()
                    return prepared

                with (patch.object(window.app, "approve", return_value=True),
                      patch("logos_desktop.updates.prepare", side_effect=prepare)):
                    window._action("controller_update")
                    self.pump(lambda: window.app.active is None)
                if cancel:
                    prepared.restart.assert_not_called()
                    self.assertIn("cancelled", window.status.text())
                else:
                    prepared.restart.assert_called_once_with(["-c", str(self.config), "--dialog", "qt"])
                prepared.cancel.assert_called_once()
                self.assertIsNone(self.services.prepared)

    def test_cancel_check_reaps_real_metadata_probe_and_restores_controls(self):
        from logos_desktop import processes
        from logos_desktop.network import ProductLookup

        document = json.loads(self.config.read_text())
        document["faithlife_product_version"] = "10"
        self.config.write_text(json.dumps(document))
        self.original = self.config.read_bytes()
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        product.mkdir(parents=True)
        (product / "Logos.exe").write_bytes(b"fixture, never executed")
        window = self.production_window()
        children = []
        original = subprocess.Popen

        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process

        def lookup(*args, **kwargs):
            if kwargs.get("refresh") is False:
                return ProductLookup((), "unavailable", "Cancelled lookup", stale=True)
            try:
                processes.capture([sys.executable, "-B", "-c", "import time\ntime.sleep(60)"],
                                  env=dict(os.environ), timeout=70, limit=4096, label="Fixture metadata")
            except processes.CaptureError:
                return SimpleNamespace(status="unavailable", stale=True, versions=[])
            raise AssertionError("Cancellation did not interrupt the metadata helper")

        with (patch.object(window.app.conf._network, "product_release", side_effect=lookup),
              patch.object(processes.subprocess, "Popen", side_effect=spawn)):
            window.home.updates.buttons["check"].click()
            self.pump(lambda: bool(children))
            self.assertFalse(window.home.updates.buttons["check"].isEnabled())
            window.cancel.click()
            self.pump(lambda: window.app.active is None)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertIn("cancelled", window.status.text())
        self.assertTrue(window.home.updates.buttons["check"].isEnabled())

    def test_successor_bootstrap_failure_preserves_recoverable_journal_and_success_confirms_once(self):
        from contextlib import nullcontext
        from PySide6.QtCore import QTimer
        from logos_desktop import controller_install, processes, updates
        from logos_desktop.config import EphemeralConfiguration
        from logos_desktop.qt import startup
        from logos_desktop.qt.app import QtApp
        from tests.test_controller_update import controller_elf

        original_window = startup.MainWindow
        original_show = startup.show_initial_window
        for failed in (True, False):
            old = controller_elf("1.0.0")
            new = controller_elf("1.1.0")
            target = self.root / f"successor-{failed}"
            target.write_bytes(new)
            target.chmod(0o700)
            destination = updates.Destination(target)
            self.addCleanup(destination.close)
            backup = self.root / (".logos-desktop-previous-" + ("a" if failed else "b") * 24)
            backup.write_bytes(old)
            backup.chmod(0o700)
            record = {"schema": 1, "target": target.name, "parent": list(destination.parent),
                      "backup": backup.name, "old_hash": hashlib.sha256(old).hexdigest(),
                      "new_hash": hashlib.sha256(new).hexdigest(), "mode": 0o700,
                      "version": "1.1.0", "token": "a" * 48}
            destination.write(record)

            def bootstrap_failure(app, config):
                app.initialization_error = "Controlled successor bootstrap failure"
                app.ready.set()
                app.bridge.initialized.emit()

            def create_window(*args, **kwargs):
                window = original_window(*args, **kwargs)
                deadline = time.monotonic() + 3

                def close_after_begin():
                    if (failed and window.app.ready.is_set()) or updates._startup is None:
                        QTimer.singleShot(100, window.close)
                    elif time.monotonic() > deadline:
                        window.close()
                    else:
                        QTimer.singleShot(20, close_after_begin)

                QTimer.singleShot(100, close_after_begin)
                return window

            injected = patch.object(QtApp, "_initialize", bootstrap_failure) if failed else nullcontext()

            def show_install_page(window, token):
                self.assertIs(window.pages.currentWidget().widget(), window.install)
                original_show(window, token)

            try:
                with (patch.object(updates, "_startup", (destination, record)),
                      patch.object(updates, "confirm_startup", wraps=updates.confirm_startup) as confirm,
                      patch.object(controller_install, "confirm_startup") as managed_confirm,
                      patch.object(startup, "MainWindow", side_effect=create_window),
                      patch.object(startup, "show_initial_window", side_effect=show_install_page),
                      patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "gui")}), injected):
                    configuration = EphemeralConfiguration.project({}, str(self.config), environment={})
                    startup.start(configuration, install_only=True)
                    if failed:
                        confirm.assert_not_called()
                        managed_confirm.assert_not_called()
                        self.assertEqual(updates._read_record(destination), record)
                        self.assertEqual(target.read_bytes(), new)
                        updates._rollback(destination, record)
                        self.assertEqual(target.read_bytes(), old)
                    else:
                        confirm.assert_called_once()
                        managed_confirm.assert_called_once()
                        self.assertEqual(target.read_bytes(), new)
                    self.assertFalse(destination.record.exists())
            finally:
                processes.resume()

    def test_dependency_choice_survives_release_persistence_and_both_states_reach_executor(self):
        window = self.production_window()
        app = window.app
        seen = []

        def install(owner):
            owner.conf._change(faithlife_product_release="46.2.0.5")
            seen.append(owner.conf.skip_install_system_dependencies)

        for checked in (False, True):
            with (patch.object(app, "approve", return_value=True), patch.object(app, "ask", return_value="OK"),
                  patch("logos_desktop.installer.install", install),
                  patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                      status="ok", stale=False, versions=["46.2.0.5"]))):
                result = self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine",
                                                          checked))(app, threading.Event())
            self.assertEqual(result.outcome, "succeeded")
            self.original = self.config.read_bytes()
        self.assertEqual(seen, [True, False])

    def test_new_destination_rejects_saved_prefix_and_binary_overrides_without_writes(self):
        window = self.production_window()
        for field in ("wine_prefix", "installer_binary_dir", "install_dir"):
            overrides = window.app.conf._overrides
            original = getattr(overrides, field)
            setattr(overrides, field, str(self.root / "old-prefix"))
            try:
                with patch("logos_desktop.installer.install") as install:
                    with self.assertRaises(maintenance.MaintenanceError):
                        self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine",
                                                          True))(window.app, threading.Event())
                    install.assert_not_called()
                self.assertEqual(self.config.read_bytes(), self.original)
            finally:
                setattr(overrides, field, original)

    def test_declined_install_and_settings_failure_preserve_selected_file(self):
        window = self.production_window()
        with patch.object(window.app, "approve", return_value=False):
            result = self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine",
                                                      True))(window.app, threading.Event())
        self.assertEqual(result.outcome, "cancelled")
        with self.assertRaises(ValueError):
            self.services.settings("relative", "stable", "stable")(window.app, threading.Event())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_conflicting_saved_appimage_and_environment_runtime_refuse_before_writes(self):
        for values, environment in (
            ({"SELECTED_APPIMAGE_FILENAME": str(self.root / "old.AppImage")}, {}),
            ({}, {"wine_binary": str(self.root / "environment-wine")}),
        ):
            document = json.loads(self.config.read_text())
            document.update(values)
            self.config.write_text(json.dumps(document))
            self.original = self.config.read_bytes()
            window = self.production_window()
            window.app.conf._overrides.environment.update(environment)
            with patch("logos_desktop.installer.install") as install:
                with self.assertRaises(maintenance.MaintenanceError):
                    self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine",
                                                      True))(window.app, threading.Event())
                install.assert_not_called()
            self.assertEqual(self.config.read_bytes(), self.original)
            window.close()
            self.pump(lambda: window._final_close)
            document.pop("SELECTED_APPIMAGE_FILENAME", None)
            self.config.write_text(json.dumps(document))
            self.original = self.config.read_bytes()

    def test_profile_picker_switch_reloads_settings_from_new_profile_before_save(self):
        window = self.production_window()
        second = self.root / "second.json"
        product = self.root / "second/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        product.mkdir(parents=True)
        executable = product / "Logos.exe"
        executable.write_bytes(b"synthetic")
        executable.chmod(0o755)
        second.write_text(json.dumps({"install_dir": str(self.root / "second"), "faithlife_product": "Logos",
                                      "backup_dir": str(self.root), "faithlife_product_release_channel": "beta"}))
        from PySide6.QtWidgets import QDialog
        window._select_profile()
        dialog = next(iter(window.dialogs.dialogs))
        dialog.selectFile(str(second))
        with patch.object(window.app, "approve", return_value=True):
            QDialog.accept(dialog)
            self.pump(lambda: window.app.active is None and window.snapshot.profile == str(second))
        form = window.profile_settings
        self.assertEqual(form.profile.text(), str(second))
        self.assertEqual(form.backup.text(), str(self.root))
        self.assertFalse(hasattr(form, "product_channel"))
        window._save_settings()
        self.pump(lambda: window.app.active is None)
        self.assertEqual(json.loads(second.read_text())["faithlife_product_release_channel"], "stable")

    def test_relative_saved_appimage_cannot_rebase_to_a_different_runtime(self):
        document = json.loads(self.config.read_text())
        document["SELECTED_APPIMAGE_FILENAME"] = "data/bin/old.AppImage"
        self.config.write_text(json.dumps(document))
        self.original = self.config.read_bytes()
        old = self.root / "Logos/data/bin/old.AppImage"
        new = self.root / "new/data/bin/old.AppImage"
        for path, content in ((old, b"old runtime"), (new, b"different runtime")):
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        window = self.production_window()
        with patch("logos_desktop.installer.install") as install:
            with self.assertRaises(maintenance.MaintenanceError):
                self.services.install(InstallDraft("Logos", str(self.root / "new"), str(old),
                                                  True))(window.app, threading.Event())
            install.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_saved_and_environment_legacy_product_version_refuse_before_installation(self):
        document = json.loads(self.config.read_text())
        document["LOGOS_VERSION"] = "9"
        self.config.write_text(json.dumps(document))
        self.original = self.config.read_bytes()
        window = self.production_window()
        for environment in ({}, {"LOGOS_VERSION": "9"}):
            window.app.conf._overrides.environment.update(environment)
            with patch("logos_desktop.installer.install") as install:
                with self.assertRaises(maintenance.MaintenanceError):
                    self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine",
                                                      True))(window.app, threading.Event())
                install.assert_not_called()
            self.assertEqual(self.config.read_bytes(), self.original)

    def test_malformed_new_profile_metadata_publishes_identity_and_refuses_stale_settings(self):
        window = self.production_window()
        second = self.root / "broken.json"
        product = self.root / "broken/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "Logos.exe").write_bytes(b"synthetic")
        (product / "System/Logos.deps.json").write_text("invalid JSON")
        second.write_text(json.dumps({"install_dir": str(self.root / "broken"), "faithlife_product": "Logos",
                                      "backup_dir": str(self.root), "faithlife_product_release_channel": "beta"}))
        before = second.read_bytes()
        with patch.object(window.app, "approve", return_value=True):
            window.start(self.services.select_profile(str(second)))
            self.pump(lambda: window.app.active is None)
        self.assertEqual(window.snapshot.profile, str(second))
        self.assertEqual(window.profile_settings.profile.text(), str(second))
        self.assertIn("Inspection failed", window.snapshot.health)
        self.assertFalse(window.home.run.isEnabled())
        with self.assertRaises(maintenance.MaintenanceError):
            self.services.settings("", "stable", "stable", profile=str(self.config))(
                window.app, threading.Event())
        self.assertEqual(second.read_bytes(), before)

    def test_settings_persist_only_selected_profile_and_preserve_unknown_fields(self):
        document = json.loads(self.config.read_text())
        document["sentinel"] = "keep"
        self.config.write_text(json.dumps(document))
        window = self.production_window()
        result = self.services.settings(str(self.root), "stable", "stable")(window.app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["sentinel"], "keep")
        self.assertEqual(saved.get("faithlife_product_release_channel", "stable"), "stable")
        self.original = self.config.read_bytes()

    def test_install_uses_latest_stable_and_unavailable_feed_preserves_configuration(self):
        window = self.production_window()
        app = window.app
        draft = InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine", True)
        with (patch.object(app, "approve", return_value=True), patch.object(app, "ask", return_value="OK")):
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="unavailable", stale=True, versions=[])):
                with self.assertRaises(maintenance.MaintenanceError):
                    self.services.install(draft)(app, threading.Event())
            self.assertEqual(self.config.read_bytes(), self.original)

            seen = []
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="ok", stale=False, versions=["46.2.0.5", "40.1.0.0"])) as lookup:
                with patch("logos_desktop.installer.install", lambda owner: seen.append((
                        owner.conf.faithlife_product_release, owner.conf.faithlife_product_release_channel))):
                    result = self.services.install(draft)(app, threading.Event())
            self.assertEqual(result.outcome, "succeeded")
            self.assertEqual(seen, [("46.2.0.5", "stable")])
            self.assertIn(unittest.mock.call("Logos", "10", "stable"), lookup.call_args_list)
        self.original = self.config.read_bytes()

    def test_failed_upgrade_enables_repair_but_not_launch(self):
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "System/Logos.exe").write_bytes(b"fixture")
        self.services.refresh(app, threading.Event())
        window._render()
        self.assertFalse(window.snapshot.installed)
        self.assertTrue(window.maintenance.buttons["repair"].isEnabled())
        self.assertFalse(window.home.run.isEnabled())

    def test_running_product_and_indexer_disable_conflicting_mutation_but_keep_stops_available(self):
        window = self.production_window()
        original = self.services.snapshot()
        self.services._snapshot = replace(original, installed=True, running=True, state="Running", indexing="Running",
                                          product_update="Update available: fixture", controller_owner="venv",
                                          controller_status="available")
        window._render()
        for name in ("backup", "restore", "runtime_update", "repair", "uninstall", "index"):
            self.assertFalse(window.maintenance.buttons[name].isEnabled())
        self.assertTrue(window.home.run.isEnabled())
        self.assertTrue(window.maintenance.buttons["stop_index"].isEnabled())
        self.assertFalse(window.home.updates.buttons["product_update"].isEnabled())
        self.assertTrue(window.home.updates.buttons["check"].isEnabled())
        self.assertTrue(window.home.updates.buttons["controller_update"].isEnabled())
        self.services._snapshot = replace(original, installed=True, state="Stopped", indexing="Stopped")
        window._render()
        self.assertTrue(window.maintenance.buttons["backup"].isEnabled())
        self.assertTrue(window.maintenance.buttons["index"].isEnabled())
        self.assertFalse(window.maintenance.buttons["stop_index"].isEnabled())

    def test_runtime_unsupported_and_manual_dependencies_have_truthful_results(self):
        window = self.production_window()
        app = window.app
        app.conf._raw.wine_binary = "/usr/bin/wine"
        with patch.object(type(app.conf), "wine_binary_code", new_callable=unittest.mock.PropertyMock,
                          side_effect=AssertionError("System Wine classification must not fetch a release catalog")):
            result = self.services.operation("runtime_update")(app, threading.Event())
        self.assertEqual(result.outcome, "unknown")
        app.conf.skip_install_system_dependencies = True
        seen = []
        with patch.object(app, "approve", return_value=True), patch("logos_desktop.utils.install_dependencies",
                                                                  lambda owner: seen.append(
                                                                      owner.conf.skip_install_system_dependencies)):
            result = self.services.operation("dependencies")(app, threading.Event())
        self.assertEqual(seen, [False])
        self.assertEqual(result.outcome, "succeeded")

    def test_real_backup_restore_and_failed_copy_through_queued_services(self):
        window = self.production_window()
        app = window.app
        live = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        for name in ("Data", "Documents", "Users"):
            (live / name).mkdir(parents=True)
            (live / name / "sentinel").write_text(name)
        backups = self.root / "backups"
        backups.mkdir()
        app.conf._change(backup_dir=str(backups))
        self.original = self.config.read_bytes()
        with patch.object(app, "approve", return_value=True):
            window._action("backup")
            self.pump(lambda: app.active is None and any(backups.glob("Logos-*")))
        snapshot = next(backups.glob("Logos-*"))
        for name in ("Data", "Documents", "Users"):
            (live / name / "sentinel").write_text("newer")
        with patch.object(app, "approve", return_value=True), patch.object(app, "ask", return_value=snapshot.name):
            window._action("restore")
            self.pump(lambda: app.active is None)
        for name in ("Data", "Documents", "Users"):
            self.assertEqual((live / name / "sentinel").read_text(), name)

        with patch.object(app, "approve", return_value=True), patch.object(app, "ask", return_value=snapshot.name):
            with patch("logos_desktop.backup_store.copy_payload", side_effect=OSError("Injected copy failure")):
                window._action("restore")
                self.pump(lambda: app.active is None and bool(window.dialogs.dialogs))
        self.assertIn("could not be completed", window.status.text())
        for name in ("Data", "Documents", "Users"):
            self.assertEqual((live / name / "sentinel").read_text(), name)
        next(iter(window.dialogs.dialogs)).reject()

    def test_cancelled_backup_service_does_not_publish_snapshot(self):
        window = self.production_window()
        app = window.app
        live = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        for name in ("Data", "Documents", "Users"):
            (live / name).mkdir(parents=True)
            (live / name / "sentinel").write_text(name)
        backups = self.root / "backups"
        backups.mkdir()
        app.conf._change(backup_dir=str(backups))
        self.original = self.config.read_bytes()
        from logos_desktop import backup_store
        original = backup_store._hash_file
        reached = []

        def interrupted(path, stop=None, target=None, progress=None):
            result = original(path, stop, target, progress)
            if target is not None:
                reached.append(target)
                app.cancel_operation()
                raise backup_store.CopyCancelled("Cancelled after a real copy")
            return result

        with patch.object(app, "approve", return_value=True), patch.object(backup_store, "_hash_file", interrupted):
            window._action("backup")
            self.pump(lambda: app.active is None)
        self.assertTrue(reached)
        self.assertFalse(list(backups.glob("Logos-*")))
        self.assertIn("cancelled", window.status.text())

    def test_product_update_failure_is_not_reported_as_completed(self):
        window = self.production_window()
        app = window.app
        with (patch.object(app, "approve", return_value=True),
              patch.object(type(app.conf), "installed_faithlife_product_release",
                           new_callable=unittest.mock.PropertyMock, return_value="40.1.0.0")):
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="ok", stale=False, versions=["46.2.0.5"])):
                with patch("logos_desktop.installer.install", side_effect=RuntimeError("Injected MSI failure")):
                    window._action("product_update")
                    self.pump(lambda: app.active is None and bool(window.dialogs.dialogs))
        self.assertIsNone(app.conf._raw.faithlife_product_release)
        self.assertNotIn("faithlife_product_release", json.loads(self.config.read_text()))
        self.assertIn("could not be completed", window.status.text())
        self.original = self.config.read_bytes()
        next(iter(window.dialogs.dialogs)).reject()

    def test_update_rejects_pinned_release_and_legacy_version_before_writes(self):
        window = self.production_window()
        app = window.app
        for overlay in ({"faithlife_product_release": "40.1.0.0"}, {"LOGOS_VERSION": "9"}):
            app.conf._overrides.environment = overlay
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="ok", stale=False, versions=["46.2.0.5"])):
                with patch("logos_desktop.installer.install") as install:
                    with self.assertRaises(maintenance.MaintenanceError):
                        self.services.operation("product_update")(app, threading.Event())
                    install.assert_not_called()
            self.assertEqual(self.config.read_bytes(), self.original)

        app.conf._overrides.environment = {"faithlife_product_release": "46.2.0.5"}
        with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                status="ok", stale=False, versions=["46.2.0.5"])):
            with (patch.object(app, "approve", return_value=True), patch("logos_desktop.installer.install") as install,
                  patch.object(type(app.conf), "installed_faithlife_product_release",
                               new_callable=unittest.mock.PropertyMock, return_value="40.1.0.0")):
                result = self.services.operation("product_update")(app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        install.assert_called_once_with(app)
        self.assertEqual(app.conf.faithlife_product_release, "46.2.0.5")
        self.original = self.config.read_bytes()

    def test_repair_menu_consumes_checked_stable_target_without_a_release_prompt(self):
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "System/Logos.exe").write_bytes(b"synthetic")
        app.conf._change(faithlife_product_release_channel="beta")
        self.original = self.config.read_bytes()
        seen = []
        with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                status="ok", stale=False, versions=["46.2.0.5"])):
            with (patch.object(app, "approve", return_value=True),
                  patch.object(app, "ask", side_effect=AssertionError("No manual release selection")),
                  patch("logos_desktop.installer.install", lambda owner: seen.append((
                      owner.conf.faithlife_product_release, owner.conf.faithlife_product_release_channel)))):
                window.maintenance.buttons["repair"].click()
                self.pump(lambda: app.active is None)
        self.assertEqual(seen, [("46.2.0.5", "stable")])
        self.original = self.config.read_bytes()

    def test_custom_installer_sources_refuse_for_install_update_and_repair(self):
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "System/Logos.exe").write_bytes(b"synthetic")
        draft = InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine", True)
        for saved in (False, True):
            if saved:
                document = json.loads(self.config.read_text())
                document["LOGOS64_URL"] = "https://example.invalid/OtherProduct.msi"
                self.config.write_text(json.dumps(document))
                app.conf.reload()
                app.conf._overrides.environment = {}
                self.original = self.config.read_bytes()
            else:
                app.conf._overrides.environment = {"LOGOS64_URL": "https://example.invalid/older.msi"}
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="ok", stale=False, versions=["46.2.0.5"])):
                with patch("logos_desktop.installer.install") as install:
                    for operation in (self.services.install(draft), self.services.operation("product_update"),
                                      self.services.operation("repair")):
                        with self.assertRaises(maintenance.MaintenanceError):
                            operation(app, threading.Event())
                    install.assert_not_called()
            self.assertEqual(self.config.read_bytes(), self.original)

    def test_concurrent_source_edit_refuses_update_and_repair_without_overwriting_it(self):
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        sentinel = product / "System/Logos.exe"
        sentinel.write_bytes(b"keep this installation")
        baseline = self.config.read_bytes()
        for action in ("product_update", "repair"):
            for field in ("LOGOS64_URL", "LOGOS_EXECUTABLE"):
                self.config.write_bytes(baseline)
                app.conf.reload()
                edited = json.loads(self.config.read_text())
                edited[field] = "https://example.invalid/old.msi" if field.endswith("URL") else "other.msi"
                self.config.write_text(json.dumps(edited))
                self.original = self.config.read_bytes()
                with (patch.object(app, "approve", return_value=True),
                      patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                          status="ok", stale=False, versions=["46.2.0.5"])),
                      patch("logos_desktop.installer.install") as install):
                    with self.assertRaises(maintenance.MaintenanceError):
                        self.services.operation(action)(app, threading.Event())
                    install.assert_not_called()
                self.assertEqual(self.config.read_bytes(), self.original)
                self.assertEqual(sentinel.read_bytes(), b"keep this installation")

    def test_graphical_repair_failure_restores_all_changed_fields(self):
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "System/Logos.exe").write_bytes(b"synthetic")
        app.conf._change(faithlife_product_release="40.1.0.0", faithlife_product_release_channel="beta")
        before = {name: getattr(app.conf._raw, name) for name in (
            "faithlife_product_release", "faithlife_product_release_channel", "faithlife_product_version")}
        with (patch.object(app, "approve", return_value=True),
              patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch("logos_desktop.installer.install", side_effect=RuntimeError("Injected MSI failure"))):
            with self.assertRaisesRegex(RuntimeError, "Injected MSI failure"):
                self.services.operation("repair")(app, threading.Event())
        for field, value in before.items():
            self.assertEqual(getattr(app.conf._raw, field), value)
        self.original = self.config.read_bytes()

    def test_failed_repair_preserves_later_selection_edits_and_original_exception(self):
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "System/Logos.exe").write_bytes(b"synthetic")
        app.conf._change(faithlife_product_release="40.1.0.0", faithlife_product_release_channel="beta")
        baseline = self.config.read_bytes()
        failure = RuntimeError("original installer failure")

        for field in ("faithlife_product_release", "TARGET_RELEASE_VERSION"):
            with self.subTest(field=field):
                self.config.write_bytes(baseline)
                app.conf.reload()

                def fail_after_edit(owner):
                    document = json.loads(self.config.read_text())
                    document[field] = "47.1.0.0"
                    document["unrelated"] = {"keep": True}
                    self.config.write_text(json.dumps(document))
                    raise failure

                with (patch.object(app, "approve", return_value=True),
                      patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                          status="ok", stale=False, versions=["46.2.0.5"])),
                      patch("logos_desktop.installer.install", side_effect=fail_after_edit)):
                    with self.assertRaises(RuntimeError) as raised:
                        self.services.operation("repair")(app, threading.Event())
                self.assertIs(raised.exception, failure)
                stored = json.loads(self.config.read_text())
                self.assertEqual(stored[field], "47.1.0.0")
                self.assertEqual(stored["unrelated"], {"keep": True})
                self.assertEqual(app.conf.faithlife_product_release_channel, "beta")
                self.original = self.config.read_bytes()

    def test_failed_repair_keeps_installer_exception_when_restoration_fails(self):
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "System/Logos.exe").write_bytes(b"synthetic")
        failure = RuntimeError("original installer failure")
        with (patch.object(app, "approve", return_value=True),
              patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch("logos_desktop.installer.install", side_effect=failure),
              patch.object(app.conf, "restore_changes", side_effect=OSError("disk full"))):
            with self.assertRaises(RuntimeError) as raised:
                self.services.operation("repair")(app, threading.Event())
        self.assertIs(raised.exception, failure)
        self.original = self.config.read_bytes()

    def test_explicit_qt_repair_dispatch_uses_graphical_policy_and_terminal_dispatch_stays_terminal(self):
        from logos_desktop import main, repair
        with patch("logos_desktop.qt.startup.start") as start, patch("logos_desktop.cli.CLI") as cli:
            parser = main.get_parser()
            config, action = main.setup_config(parser.parse_args([
                "-c", str(self.config), "--dialog", "qt", "--repair",
            ]))
            main.run(config, action)
            start.assert_called_once_with(config, repair.repair_installation, install_only=False)
            cli.assert_not_called()

        with patch("logos_desktop.qt.startup.start") as start, patch("logos_desktop.cli.CLI") as cli:
            cli.return_value.close.return_value = True
            config, action = main.setup_config(parser.parse_args([
                "-c", str(self.config), "--dialog", "tui", "--repair",
            ]))
            main.run(config, action)
            cli.return_value.repair_app.assert_called_once()
            start.assert_not_called()

    def test_runtime_adopts_validated_publication_without_consuming_a_postpublication_edit(self):
        from logos_desktop import artifacts, installer, runtime
        from tests.artifact_fixtures import appimage
        window = self.production_window()
        app = window.app
        app.conf._change(faithlife_product_version="10", faithlife_product_release="46.2.0.5")
        candidate = self.root / "fixture.AppImage"
        candidate.write_bytes(appimage())
        accepted = artifacts.import_local(candidate, self.root / "Logos/data/bin",
                                          artifacts.ArtifactSpec("local:fixture", candidate.name, "appimage"))
        original_replace = runtime._replace_bytes
        _, configs = runtime._targets(app)
        edits = []

        def replace_then_edit(path, content):
            original_replace(path, content)
            if path == configs[-1]:
                document = json.loads(self.config.read_text())
                document["LOGOS64_URL"] = "https://example.invalid/wrong.msi"
                self.config.write_text(json.dumps(document))
                edits.append(self.config.read_bytes())

        with (patch("logos_desktop.wine.require_compatible"),
              patch.object(runtime, "_replace_bytes", side_effect=replace_then_edit)):
            with app.conf.validated_writes(lambda document: self.services._validate_release_document(
                    app.conf, "46.2.0.5", document)):
                runtime.activate(app, accepted)
        self.assertEqual(len(edits), 1)
        self.assertIsNone(app.conf._overrides.faithlife_installer_download_url)
        with (patch.object(installer, "ensure_winetricks_executable"),
              patch.object(installer.network, "logos_reuse_download", return_value=accepted) as download):
            installer.ensure_product_installer_download(app)
        self.assertEqual(download.call_args.args[0],
                         "https://downloads.logoscdn.com/LBS10/Installer/46.2.0.5/Logos-x64.msi")
        self.assertEqual(self.config.read_bytes(), edits[0])
        self.original = self.config.read_bytes()

    def test_real_uninstall_can_save_and_begin_reinstallation_in_the_same_controller(self):
        window = self.production_window()
        app = window.app
        (self.root / "Logos/data/bin").mkdir(parents=True)
        (self.root / "Logos/data/wine64_bottle").mkdir()
        app.conf._change(faithlife_product_version="10", wine_binary="/usr/bin/wine")
        (self.root / "Logos/logos-desktop.json").write_bytes(self.config.read_bytes())
        app.conf._overrides.runtime_selection = {"wine_binary": "data/bin/stale.AppImage"}
        with patch.object(app, "approve", side_effect=lambda question, *args:
                          False if "clear the cache" in question else True):
            result = self.services.operation("uninstall")(app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        self.assertFalse((self.root / "Logos").exists())
        self.assertEqual(app.conf._raw._saved, {})
        self.assertEqual(app.conf._overrides.runtime_selection, {})
        self.services.settings("", "stable", "stable")(app, threading.Event())
        with (patch.object(app, "approve", return_value=True),
              patch.object(app, "ask", return_value="OK"),
              patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch("logos_desktop.installer.install") as install):
            result = self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine",
                                                      True))(app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        install.assert_called_once_with(app)
        self.assertEqual(app.conf._raw.wine_binary, "/usr/bin/wine")
        self.original = self.config.read_bytes()

    def test_legacy_first_mutation_uninstall_retires_migration_and_preserves_competing_destination(self):
        from logos_desktop import constants
        from logos_desktop.config import EphemeralConfiguration
        install = self.root / "legacy-install"
        (install / "data/bin").mkdir(parents=True)
        (install / "data/wine64_bottle").mkdir()
        sentinel = install / "data/wine64_bottle/old-data"
        sentinel.write_text("old disposable installation")
        document = {"install_dir": str(install), "faithlife_product": "Logos",
                    "faithlife_product_version": "10", "wine_binary": "/usr/bin/wine"}
        legacy = self.root / "legacy.conf"
        legacy.write_text(json.dumps(document))
        (install / f"{constants.BINARY_NAME}.json").write_text(json.dumps(document))
        destination = self.root / "new-default.json"
        with patch.dict(os.environ, {"WINEDEBUG": "-all"}, clear=True):
            discovered = EphemeralConfiguration.project(document, str(legacy), destination=str(destination))
        self.assertEqual(discovered.config_path, str(legacy))
        self.assertEqual(discovered.config_destination, str(destination))
        window = self.window(Services(), configuration=discovered)
        self.pump(lambda: window.app.active is None)
        app = window.app
        destination.write_text('{"sentinel":"competing destination"}')
        competing = destination.read_bytes()
        with patch.object(app, "approve", side_effect=lambda question, *args:
                          False if "clear the cache" in question else True):
            result = self.services.operation("uninstall")(app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        self.assertFalse(install.exists())
        self.assertEqual(app.conf._overrides.config_destination, app.conf.config_file_path)
        self.assertEqual(app.conf._raw._file_path, str(legacy))
        self.assertIsNone(app.conf._raw._file_bytes)
        self.assertEqual(app.conf._overrides.wine_debug, "-all")
        self.services.settings("", "stable", "stable")(app, threading.Event())
        self.assertFalse(install.exists())
        with (patch.object(app, "approve", return_value=True),
              patch.object(app, "ask", return_value="OK"),
              patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch("logos_desktop.installer.install") as installer):
            result = self.services.install(InstallDraft("Logos", str(install), "/usr/bin/wine", True))(
                app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        installer.assert_called_once_with(app)
        self.assertFalse(sentinel.exists())
        self.assertEqual(destination.read_bytes(), competing)

    def test_background_observation_is_not_attributed_to_an_active_operation(self):
        window = self.production_window()
        entered, settle = threading.Event(), threading.Event()
        from logos_desktop.qt.bridge import Result

        def operation(app, cancel):
            entered.set()
            settle.wait(3)
            return Result("succeeded", "Unrelated operation settled")

        window.start(operation)
        self.assertTrue(entered.wait(2))
        worker = threading.Thread(target=window.app.observe_manager,
                                  args=(window.app.logos, "Indexing stopped. Worker exit status unavailable"))
        worker.start()
        worker.join()
        self.application.processEvents()
        self.assertIn("Process observation: Indexing stopped", window.home.activity.toPlainText())
        self.assertNotIn("Indexing stopped", window.status.text())
        settle.set()
        self.pump(lambda: window.app.active is None and not window._refresh_pending)

    def test_missing_ownership_api_refuses_profile_selection(self):
        window = self.production_window()
        with patch.object(window.app, "profile_selection", None):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "ownership transfer"):
                self.services._selection(window.app, window.app.conf)

    def test_doctor_is_copyable_and_install_preflight_precedes_config_mutation(self):
        window = self.production_window()
        app = window.app
        with patch.object(app, "info") as info:
            self.services.operation("doctor")(app, threading.Event())
        window._render()
        self.assertEqual(window.diagnostics.doctor.toPlainText(), info.call_args.args[0])
        self.assertTrue(window.diagnostics.doctor.isReadOnly())
        report = {"summary": "Unsupported host", "checks": [
            {"name": "architecture", "status": "error", "summary": "Unsupported host"}]}
        with (patch.object(app, "approve", return_value=True),
              patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch("logos_desktop.doctor.inspect", return_value=report),
              patch("logos_desktop.installer.install") as install):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "preflight"):
                self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine", True))(
                    app, threading.Event())
        install.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_preflight_requests_review_actions_and_preserves_errors_and_cancellation(self):
        from logos_desktop.app import UserExitedFromAsk

        window = self.production_window()
        app = window.app
        report = {"summary": "Fixture preflight report", "checks": []}
        with patch("logos_desktop.doctor.inspect", return_value=report):
            for answer in ("OK", "Cancel", None):
                with self.subTest(answer=answer), patch.object(app, "_ask", return_value=answer) as ask:
                    if answer == "OK":
                        self.services._preflight(app, app.conf)
                    else:
                        with self.assertRaises(UserExitedFromAsk):
                            self.services._preflight(app, app.conf)
                    ask.assert_called_once_with("Fixture preflight report\n\nReview installation preflight",
                                                ["OK", "Cancel"])
                    self.assertEqual(self.services.doctor_report, report["summary"])
                    self.assertEqual(self.services._doctor_profile, app.conf.config_file_path)

        report["checks"] = [{"status": "error", "summary": "Fixture unsupported host"}]
        with (patch("logos_desktop.doctor.inspect", return_value=report), patch.object(app, "_ask") as ask):
            with self.assertRaisesRegex(maintenance.MaintenanceError,
                                        "Installation preflight failed:\nFixture unsupported host"):
                self.services._preflight(app, app.conf)
            ask.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_relative_backup_display_and_folder_opener_are_scoped_and_bounded(self):
        window = self.production_window()
        app = window.app
        folder = self.root / "Logos/backups with spaces"
        folder.mkdir(parents=True)
        app.conf._raw.backup_dir = "backups with spaces"
        self.services.refresh(app, threading.Event())
        self.assertEqual(self.services.snapshot().backup, str(folder))
        with (patch("logos_desktop.qt.services.shutil.which", return_value="/usr/bin/xdg-open"),
              patch("logos_desktop.processes.capture", return_value=(b"", 0)) as capture):
            self.services.open_folder("open_backup")(app, threading.Event())
        self.assertEqual(capture.call_args.args[0], ["/usr/bin/xdg-open", str(folder)])
        self.assertEqual(capture.call_args.kwargs["timeout"], 10)
        folder.rmdir()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.services.open_folder("open_backup")(app, threading.Event())
        self.assertFalse(folder.exists())

    def test_fresh_install_preflight_inspects_proposed_paths_before_any_write(self):
        from logos_desktop import doctor
        self.config.write_text("{}")
        self.original = self.config.read_bytes()
        window = self.production_window()
        destination = self.root / "fresh installation"
        seen = []
        inspect = doctor.inspect

        def inspect_proposal(candidate):
            self.assertEqual(self.config.read_bytes(), self.original)
            self.assertFalse(destination.exists())
            seen.append(doctor.selected_paths(candidate))
            return inspect(candidate)

        with (patch.object(window.app, "approve", return_value=True),
              patch.object(window.app, "ask", return_value="OK") as ask,
              patch.object(window.app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch.object(doctor, "inspect", side_effect=inspect_proposal),
              patch("logos_desktop.installer.install") as install):
            result = self.services.install(InstallDraft("Logos", str(destination), "/usr/bin/wine", False))(
                window.app, threading.Event())
        self.assertEqual(seen, [(destination, destination / "data/wine64_bottle", None)])
        ask.assert_called_once_with(self.services.doctor_report + "\n\nReview installation preflight",
                                    ["OK", "Cancel"])
        self.assertEqual(result.outcome, "succeeded")
        install.assert_called_once_with(window.app)
        self.original = self.config.read_bytes()

    def test_support_cancellation_after_inspection_is_reported_as_cancelled(self):
        window = self.production_window()
        cancel = threading.Event()

        def inspect(conf):
            cancel.set()
            return {"summary": "Fixture diagnostics", "checks": []}

        with (patch("logos_desktop.doctor.inspect", side_effect=inspect),
              patch("logos_desktop.support.export") as export):
            result = self.services.operation("support")(window.app, cancel)
        self.assertEqual(result.outcome, "cancelled")
        export.assert_not_called()

    def test_update_rechecks_actual_installed_version_under_admission_before_write(self):
        from logos_desktop import utils
        window = self.production_window()
        app = window.app
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "Logos.exe").write_bytes(b"fixture executable")
        metadata = product / "System/Logos.deps.json"

        def write_version(version):
            metadata.write_text(json.dumps({"libraries": {f"Logos/{version}": {}}}))

        for installed in (None, "46.2.0.5", "50.0.0.0"):
            if installed is None:
                metadata.unlink(missing_ok=True)
            else:
                write_version(installed)
            app.conf.invalidate_context()
            self.assertEqual(app.conf.installed_faithlife_product_release, installed)
            with (patch.object(app, "approve", return_value=True),
                  patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                      status="ok", stale=False, versions=["46.2.0.5"])),
                  patch("logos_desktop.installer.install") as install):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "not newer"):
                    self.services.operation("product_update")(app, threading.Event())
            install.assert_not_called()
            self.assertEqual(self.config.read_bytes(), self.original)

        for changed in ("46.2.0.5", "50.0.0.0", None):
            with self.subTest(changed_during_confirmation=changed):
                write_version("40.0.0.0")
                app.conf.invalidate_context()
                self.assertEqual(app.conf.installed_faithlife_product_release, "40.0.0.0")

                def confirm(question, details):
                    self.assertEqual(question, "Update Logos?")
                    if changed is None:
                        metadata.unlink()
                    else:
                        write_version(changed)
                    return True

                admitted_reads = []
                read_version = utils.get_current_logos_version

                def read_metadata(appdata):
                    self.assertEqual(os.path.normpath(appdata), str(product))
                    admitted_reads.append(maintenance.installation_owned())
                    return read_version(appdata)

                with (patch.object(app, "approve", side_effect=confirm) as approve,
                      patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                          status="ok", stale=False, versions=["46.2.0.5"])),
                      patch.object(utils, "get_current_logos_version", side_effect=read_metadata),
                      patch.object(app.conf, "_change",
                                   side_effect=AssertionError("Unexpected config mutation")) as change,
                      patch("logos_desktop.installer.install") as install):
                    with self.assertRaisesRegex(maintenance.MaintenanceError, "not newer"):
                        self.services.operation("product_update")(app, threading.Event())
                approve.assert_called_once()
                self.assertIn(True, admitted_reads)
                change.assert_not_called()
                install.assert_not_called()
                self.assertEqual(app.conf.installed_faithlife_product_release, changed)
                self.assertEqual(self.config.read_bytes(), self.original)

    def test_update_failure_and_delayed_promotion_refresh_same_controller(self):
        from logos_desktop import utils
        from logos_desktop.logos import State
        from logos_desktop.network import ProductLookup

        document = json.loads(self.config.read_text())
        document.update(faithlife_product_version="10", faithlife_product_release="46.2.0.0005")
        self.config.write_text(json.dumps(document))
        self.original = self.config.read_bytes()
        product = self.root / "Logos/data/wine64_bottle/drive_c/users/test/AppData/Local/Logos"
        system = product / "System"
        system.mkdir(parents=True)
        (product / "Logos.exe").write_bytes(b"public fixture launcher")
        metadata = system / "Logos.deps.json"
        metadata.write_text(json.dumps({"libraries": {"Logos/46.2.0.5": {}}}))
        window = self.production_window()
        app = window.app
        conf = app.conf
        self.assertEqual(self.services.snapshot().version, "46.2.0.5")

        def failed_install(owner):
            self.assertTrue(maintenance.installation_owned())
            self.assertEqual(owner.conf.faithlife_product_release, "54.3.0.0005")
            raise maintenance.MaintenanceError("Fixture MSI failed")

        with (patch.object(app, "approve", return_value=True),
              patch.object(conf._network, "product_release", return_value=ProductLookup(("54.3.0.0005",), "ok"))):
            with patch("logos_desktop.installer.install", side_effect=failed_install):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "Fixture MSI failed"):
                    self.services.operation("product_update")(app, threading.Event())
            self.assertEqual(conf.faithlife_product_release, "46.2.0.0005")
            self.assertEqual(json.loads(self.config.read_text())["faithlife_product_release"], "46.2.0.0005")
            self.assertEqual(self.services.snapshot().version, "46.2.0.5")
            self.assertTrue(self.services.snapshot().product_update.startswith("Update available:"))

            pending = product / "Pending"

            def install_pending(owner):
                pending.mkdir()
                (pending / metadata.name).write_text(json.dumps({"libraries": {"Logos/54.3.0.5": {}}}))

            with patch("logos_desktop.installer.install", side_effect=install_pending):
                window.start(self.services.operation("product_update"))
                self.pump(lambda: app.active is None)
            self.assertEqual(conf.faithlife_product_release, "54.3.0.0005")
            self.assertEqual(self.services.snapshot().version, "46.2.0.5")

            with patch.object(app.logos, "start") as launch:
                window.start(self.services.operation("launch"))
                self.pump(lambda: app.active is None)
            launch.assert_called_once()
            self.assertEqual(self.services.snapshot().version, "46.2.0.5")

            # Vendor promotion can finish after the launch request has settled.
            (pending / metadata.name).replace(metadata)
            app.logos.logos_state = State.RUNNING
            with patch.object(utils, "get_current_logos_version", wraps=utils.get_current_logos_version) as read:
                for _ in range(2):
                    window.start(self.services.refresh)
                    self.pump(lambda: app.active is None)
                    self.assertEqual(window.snapshot.version, "54.3.0.5")
                    self.assertTrue(window.snapshot.running)
                    self.assertTrue(window.snapshot.product_update.startswith("current:"))
                self.assertEqual(read.call_count, 1)
                read.side_effect = AssertionError("Rendering must use the frozen snapshot")
                for _ in range(10):
                    window._render()
            app.logos.logos_state = State.STOPPED
        self.assertIs(app.conf, conf)
        self.assertEqual(conf.faithlife_product_release, "54.3.0.0005")
        self.original = self.config.read_bytes()

    def test_failed_update_preserves_later_selection_edits_and_primary_exception(self):
        from logos_desktop.network import ProductLookup

        document = json.loads(self.config.read_text())
        document.update(faithlife_product_release="46.2.0.0005",
                        faithlife_product_release_channel="dev", app_release_channel="dev")
        self.config.write_text(json.dumps(document))
        self.original = self.config.read_bytes()
        window = self.production_window()
        app = window.app
        failure = RuntimeError("Fixture installer failed after external edit")
        external = {}

        def failed_install(owner):
            self.assertTrue(maintenance.installation_owned())
            current = json.loads(self.config.read_text())
            current["faithlife_product_release"] = "50.0.0.0"
            current["faithlife_product_release_channel"] = "beta"
            current["external_sentinel"] = {"preserved": [1, False]}
            external.update(current)
            self.config.write_text(json.dumps(current))
            raise failure

        with (patch.object(app, "approve", return_value=True),
              patch.object(type(app.conf), "installed_faithlife_product_release",
                           new_callable=unittest.mock.PropertyMock, return_value="46.2.0.5"),
              patch.object(app.conf._network, "product_release", return_value=ProductLookup(("54.3.0.0005",), "ok")),
              patch("logos_desktop.installer.install", side_effect=failed_install),
              patch("logos_desktop.qt.services.logging.warning") as report_conflict):
            with self.assertRaises(RuntimeError) as raised:
                self.services.operation("product_update")(app, threading.Event())
        self.assertIs(raised.exception, failure)
        saved = json.loads(self.config.read_text())
        for name in ("faithlife_product_release", "TARGET_RELEASE_VERSION",
                     "faithlife_product_release_channel", "logos_release_channel", "external_sentinel"):
            self.assertEqual((name in saved, saved.get(name)), (name in external, external.get(name)))
        self.assertEqual(saved["app_release_channel"], "dev")
        self.assertNotIn("lli_release_channel", saved)
        self.assertEqual(app.conf.faithlife_product_release, "50.0.0.0")
        self.assertEqual(app.conf.faithlife_product_release_channel, "beta")
        self.assertEqual(app.conf.app_release_channel, "dev")
        self.assertEqual(app.conf._raw._saved["external_sentinel"], external["external_sentinel"])
        self.assertTrue(any("rollback preserved later edits" in call.args[0]
                            for call in report_conflict.call_args_list))
        self.original = self.config.read_bytes()

    def test_failed_update_keeps_primary_exception_when_rollback_publication_races(self):
        from logos_desktop import config_store
        from logos_desktop.network import ProductLookup

        window = self.production_window()
        app = window.app
        failure = RuntimeError("Fixture installer failure")
        stage = config_store.Target.stage
        edited = []

        def edit_during_restore(target, content):
            stage(target, content)
            if target.path == self.config:
                document = json.loads(self.config.read_text())
                document["faithlife_product_release"] = "50.0.0.0"
                document["external_sentinel"] = "edit after rollback snapshot"
                edited.append(document)
                self.config.write_text(json.dumps(document))

        def failed_install(owner):
            self.stack.enter_context(patch.object(config_store.Target, "stage", edit_during_restore))
            raise failure

        with (patch.object(app, "approve", return_value=True),
              patch.object(type(app.conf), "installed_faithlife_product_release",
                           new_callable=unittest.mock.PropertyMock, return_value="46.2.0.5"),
              patch.object(app.conf._network, "product_release", return_value=ProductLookup(("54.3.0.0005",), "ok")),
              patch("logos_desktop.installer.install", side_effect=failed_install),
              patch("logos_desktop.qt.services.logging.exception") as report_failure):
            with self.assertRaises(RuntimeError) as raised:
                self.services.operation("product_update")(app, threading.Event())
        self.assertIs(raised.exception, failure)
        self.assertEqual(json.loads(self.config.read_text()), edited[0])
        report_failure.assert_called_once_with("Cannot restore product selection after failed update")
        self.original = self.config.read_bytes()

    def test_desktop_integration_is_explicit_and_previews_profile_paths(self):
        window = self.production_window()
        app = window.app
        plan = SimpleNamespace(config=self.config, journal=self.root / "desktop-journal.json",
                               files=[SimpleNamespace(path=self.root / "applications/profile.desktop")],
                               defaults=[("x-scheme-handler/logos4", "other.desktop")],
                               issues=["Existing MIME default preserved"])
        with (patch("logos_desktop.desktop.preview", return_value=plan) as preview,
              patch("logos_desktop.desktop.adopt") as commit,
              patch.object(type(app.conf), "logos_exe_windows_path",
                           new_callable=unittest.mock.PropertyMock, return_value="C:\\Logos\\Logos.exe"),
              patch.object(app, "approve", return_value=False) as approve):
            result = self.services.operation("desktop")(app, threading.Event())
        self.assertEqual(result.outcome, "cancelled")
        commit.assert_not_called()
        self.assertEqual(preview.call_args.args[0], str(self.config))
        self.assertIn(str(plan.files[0].path), approve.call_args.args[1])
        self.assertIn("other.desktop", approve.call_args.args[1])

    def test_update_projection_distinguishes_current_stale_and_fresh(self):
        from logos_desktop.network import ProductLookup
        window = self.production_window()
        conf = window.app.conf
        for lookup, expected in (
            (ProductLookup(("40.0.0.0",), "ok"), "current:"),
            (ProductLookup(("46.2.0.5",), "ok"), "Update available:"),
            (ProductLookup(("46.2.0.5",), "ok", "Cached while offline", stale=True), "stale:"),
            (ProductLookup((), "empty"), "no-release:"),
        ):
            with (patch.object(conf._network, "product_release", return_value=lookup),
                  patch.object(type(conf), "installed_faithlife_product_release",
                               new_callable=unittest.mock.PropertyMock, return_value="40.0.0.0")):
                self.assertTrue(self.services._product_update(conf, True).startswith(expected))
