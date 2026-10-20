import json
from dataclasses import replace
import hashlib
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

try:
    import PySide6  # noqa: F401
except ModuleNotFoundError:
    QT_AVAILABLE = False
    QtCase = unittest.TestCase
else:
    QT_AVAILABLE = True
    from ou_dedetai import maintenance
    from ou_dedetai.qt.services import Services
    from ou_dedetai.qt.window import InstallDraft
    from tests.qt_fixtures import QtCase


@unittest.skipUnless(QT_AVAILABLE, "Optional Qt frontend is not installed")
class TestProductionServices(QtCase):
    def production_window(self):
        window = self.window(Services())
        self.pump(lambda: window.app.active is None)
        return window

    def test_production_bootstrap_and_snapshot_do_not_prompt_or_fetch(self):
        with patch("ou_dedetai.metadata.request", side_effect=AssertionError("Unexpected network"), create=True):
            window = self.production_window()
        self.assertFalse(window.snapshot.installed)
        self.assertEqual(window.snapshot.profile, str(self.config))
        self.assertTrue(window.maintenance.buttons["repair"].isEnabled())
        self.assertFalse(window.home.run.isEnabled())
        self.assertNotIn("controller_update", window.maintenance.buttons)
        self.assertEqual(window.install.product.count(), 1)
        self.assertEqual(window.install.product.currentText(), "Logos")

    def test_enabled_controller_update_uses_stable_exact_qt_restart_and_cleans_cancelled_stage(self):
        from unittest.mock import Mock
        from ou_dedetai import constants
        with patch.multiple(constants, APP_SELF_UPDATES_ENABLED=True, APP_RELEASE_VERSION="1.0.0"):
            window = self.production_window()
            self.assertIn("controller_update", window.maintenance.buttons)
            for cancel in (False, True):
                prepared = Mock()

                def prepare(app, *, channel):
                    self.assertEqual(channel, "stable")
                    if cancel:
                        app.cancel_operation()
                    return prepared

                with (patch.object(window.app, "approve", return_value=True),
                      patch("ou_dedetai.updates.prepare", side_effect=prepare)):
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
        from ou_dedetai import processes
        window = self.production_window()
        children = []
        original = subprocess.Popen

        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process

        def lookup(*args, **kwargs):
            try:
                processes.capture([sys.executable, "-B", "-c", "import time\ntime.sleep(60)"],
                                  env=dict(os.environ), timeout=70, limit=4096, label="Fixture metadata")
            except processes.CaptureError:
                return SimpleNamespace(status="unavailable", stale=True, versions=[])
            raise AssertionError("Cancellation did not interrupt the metadata helper")

        with (patch.object(window.app.conf._network, "product_release", side_effect=lookup),
              patch.object(processes.subprocess, "Popen", side_effect=spawn)):
            window._action("check")
            self.pump(lambda: bool(children))
            window.cancel.click()
            self.pump(lambda: window.app.active is None)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertIn("cancelled", window.status.text())
        self.assertTrue(window.maintenance.buttons["check"].isEnabled())

    def test_successor_bootstrap_failure_preserves_recoverable_journal_and_success_confirms_once(self):
        from contextlib import nullcontext
        from PySide6.QtCore import QTimer
        from ou_dedetai import processes, updates
        from ou_dedetai.config import EphemeralConfiguration
        from ou_dedetai.qt import startup
        from ou_dedetai.qt.app import QtApp
        from tests.test_controller_update import controller_elf

        original_window = startup.MainWindow
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
            try:
                with (patch.object(updates, "_startup", (destination, record)),
                      patch.object(updates, "confirm_startup", wraps=updates.confirm_startup) as confirm,
                      patch.object(startup, "MainWindow", side_effect=create_window),
                      patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "gui")}), injected):
                    startup.start(EphemeralConfiguration.project({}, str(self.config), environment={}))
                    if failed:
                        confirm.assert_not_called()
                        self.assertEqual(updates._read_record(destination), record)
                        self.assertEqual(target.read_bytes(), new)
                        updates._rollback(destination, record)
                        self.assertEqual(target.read_bytes(), old)
                    else:
                        confirm.assert_called_once()
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
            with (patch.object(app, "approve", return_value=True), patch("ou_dedetai.installer.install", install),
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
                with patch("ou_dedetai.installer.install") as install:
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
            with patch("ou_dedetai.installer.install") as install:
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
        with patch("ou_dedetai.installer.install") as install:
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
            with patch("ou_dedetai.installer.install") as install:
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
        with patch.object(app, "approve", return_value=True):
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="unavailable", stale=True, versions=[])):
                with self.assertRaises(maintenance.MaintenanceError):
                    self.services.install(draft)(app, threading.Event())
            self.assertEqual(self.config.read_bytes(), self.original)

            seen = []
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="ok", stale=False, versions=["46.2.0.5", "40.1.0.0"])) as lookup:
                with patch("ou_dedetai.installer.install", lambda owner: seen.append((
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
        self.services._snapshot = replace(original, installed=True, running=True, state="Running", indexing="Running")
        window._render()
        for name in ("backup", "restore", "runtime_update", "repair", "uninstall", "index"):
            self.assertFalse(window.maintenance.buttons[name].isEnabled())
        self.assertTrue(window.home.run.isEnabled())
        self.assertTrue(window.maintenance.buttons["stop_index"].isEnabled())
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
        with patch.object(app, "approve", return_value=True), patch("ou_dedetai.utils.install_dependencies",
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
            with patch("ou_dedetai.backup_store.copy_payload", side_effect=OSError("Injected copy failure")):
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
        from ou_dedetai import backup_store
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
        with patch.object(app, "approve", return_value=True):
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="ok", stale=False, versions=["46.2.0.5"])):
                with patch("ou_dedetai.installer.install", side_effect=RuntimeError("Injected MSI failure")):
                    window._action("product_update")
                    self.pump(lambda: app.active is None and bool(window.dialogs.dialogs))
        self.assertEqual(app.conf._raw.faithlife_product_release, "46.2.0.5")
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
                with patch("ou_dedetai.installer.install") as install:
                    with self.assertRaises(maintenance.MaintenanceError):
                        self.services.operation("product_update")(app, threading.Event())
                    install.assert_not_called()
            self.assertEqual(self.config.read_bytes(), self.original)

        app.conf._overrides.environment = {"faithlife_product_release": "46.2.0.5"}
        with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                status="ok", stale=False, versions=["46.2.0.5"])):
            with patch.object(app, "approve", return_value=True), patch("ou_dedetai.installer.install") as install:
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
                  patch("ou_dedetai.installer.install", lambda owner: seen.append((
                      owner.conf.faithlife_product_release, owner.conf.faithlife_product_release_channel)))):
                window.maintenance.buttons["repair"].trigger()
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
                document["LOGOS64_URL"] = "https://example.invalid/Verbum.msi"
                self.config.write_text(json.dumps(document))
                app.conf.reload()
                app.conf._overrides.environment = {}
                self.original = self.config.read_bytes()
            else:
                app.conf._overrides.environment = {"LOGOS64_URL": "https://example.invalid/older.msi"}
            with patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                    status="ok", stale=False, versions=["46.2.0.5"])):
                with patch("ou_dedetai.installer.install") as install:
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
                      patch("ou_dedetai.installer.install") as install):
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
              patch("ou_dedetai.installer.install", side_effect=RuntimeError("Injected MSI failure"))):
            with self.assertRaisesRegex(RuntimeError, "Injected MSI failure"):
                self.services.operation("repair")(app, threading.Event())
        for field, value in before.items():
            self.assertEqual(getattr(app.conf._raw, field), value)
        self.original = self.config.read_bytes()

    def test_explicit_qt_repair_dispatch_uses_graphical_policy_and_terminal_dispatch_stays_terminal(self):
        from ou_dedetai import main, repair
        with patch("ou_dedetai.qt.startup.start") as start, patch("ou_dedetai.cli.CLI") as cli:
            parser = main.get_parser()
            config, action = main.setup_config(parser.parse_args([
                "-c", str(self.config), "--dialog", "qt", "--repair",
            ]))
            main.run(config, action)
            start.assert_called_once_with(config, repair.repair_installation, install_only=False)
            cli.assert_not_called()

        with patch("ou_dedetai.qt.startup.start") as start, patch("ou_dedetai.cli.CLI") as cli:
            cli.return_value.close.return_value = True
            config, action = main.setup_config(parser.parse_args([
                "-c", str(self.config), "--dialog", "tui", "--repair",
            ]))
            main.run(config, action)
            cli.return_value.repair_app.assert_called_once()
            start.assert_not_called()

    def test_runtime_adopts_validated_publication_without_consuming_a_postpublication_edit(self):
        from ou_dedetai import artifacts, installer, runtime
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

        with (patch("ou_dedetai.wine.require_compatible"),
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
              patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch("ou_dedetai.installer.install") as install):
            result = self.services.install(InstallDraft("Logos", str(self.root / "Logos"), "/usr/bin/wine",
                                                      True))(app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        install.assert_called_once_with(app)
        self.assertEqual(app.conf._raw.wine_binary, "/usr/bin/wine")
        self.original = self.config.read_bytes()

    def test_legacy_first_mutation_uninstall_retires_migration_and_preserves_competing_destination(self):
        from ou_dedetai import constants
        from ou_dedetai.config import EphemeralConfiguration
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
        with (patch.object(constants, "DEFAULT_CONFIG_PATH", str(destination)),
              patch.object(constants, "LEGACY_CONFIG_FILES", [str(legacy)]),
              patch.dict(os.environ, {"WINEDEBUG": "-all"}, clear=True)):
            discovered = EphemeralConfiguration.load()
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
              patch.object(app.conf._network, "product_release", return_value=SimpleNamespace(
                  status="ok", stale=False, versions=["46.2.0.5"])),
              patch("ou_dedetai.installer.install") as installer):
            result = self.services.install(InstallDraft("Logos", str(install), "/usr/bin/wine", True))(
                app, threading.Event())
        self.assertEqual(result.outcome, "succeeded")
        installer.assert_called_once_with(app)
        self.assertFalse(sentinel.exists())
        self.assertEqual(destination.read_bytes(), competing)

    def test_background_observation_is_not_attributed_to_an_active_operation(self):
        window = self.production_window()
        entered, settle = threading.Event(), threading.Event()
        from ou_dedetai.qt.bridge import Result

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
