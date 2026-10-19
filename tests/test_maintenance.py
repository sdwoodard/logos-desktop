from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ou_dedetai import backup, constants, control, database, installer, logos, maintenance, system, utils, wine
from ou_dedetai.config import Config
from ou_dedetai import repair
from tests.test_backup import fixture_app


class TestMaintenance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(constants, "STATE_DIR", str(self.root / "state/FaithLife-Community")).start()
        self.app = fixture_app(self.root)
        self.context = maintenance.Context.from_app(self.app)
        self.app.conf.install_dir = str(self.context.install)
        self.app.conf.installer_binary_dir = str(self.context.install / "data/bin")

    def child(self, program, *arguments, env=None):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", program, *map(str, arguments)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        self.addCleanup(stop)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        return process

    @contextmanager
    def peer_lease(self, exclusive):
        program = """
import sys,time
from pathlib import Path
from ou_dedetai import constants,maintenance
constants.STATE_DIR=sys.argv[2]
with maintenance.use(maintenance.Context(Path(sys.argv[1])),exclusive=sys.argv[3]=='yes'):
    print('ready',flush=True)
    time.sleep(20)
"""
        child = self.child(program, self.context.prefix, constants.STATE_DIR, "yes" if exclusive else "no")
        self.assertEqual(child.stdout.readline().strip(), "ready", child.stderr.read() if child.poll() else "")
        try:
            yield
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_real_peer_lock_refuses_all_direct_writer_preparation(self):
        self.app.logos = logos.LogosManager(self.app)
        operations = [
            lambda: wine.enforce_icu_data_files(self.app),
            lambda: wine.copy_icu_files(self.root / "source", self.context.prefix),
            lambda: utils.set_appimage_symlink(self.app),
            lambda: utils.update_to_latest_recommended_appimage(self.app),
            lambda: installer.create_wine_appimage_symlinks(self.app),
            lambda: installer.install(self.app),
            lambda: installer.create_launcher_shortcuts(self.app),
            lambda: utils.update_faithlife_product(self.app),
            lambda: system.ensure_winetricks(self.app),
            lambda: self.app.logos.start(),
            lambda: self.app.logos.index(),
            lambda: self.app.logos.switch_logging(),
            lambda: self.app.logos.get_app_logging_state(),
            lambda: wine.get_registry_value("fixture", "fixture", self.app),
            lambda: Config.winetricks_binary.fget(SimpleNamespace(app=self.app)),
        ]
        with self.peer_lease(True):
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(maintenance.MaintenanceBusy):
                    operation()
        self.app.status.assert_not_called()
        self.assertFalse(self.context.prefix.exists())
        with maintenance.use(self.context):
            pass

    def test_shared_owner_blocks_maintenance_and_exclusive_owner_blocks_nested_wine(self):
        with self.peer_lease(False), self.assertRaises(maintenance.MaintenanceBusy):
            backup.BackupTask(self.app).run()
        with maintenance.maintain(self.app):
            with self.assertRaises(maintenance.MaintenanceBusy):
                wine.get_registry_value("fixture", "fixture", self.app)

    def test_real_process_with_stale_stopped_state_blocks_without_signals(self):
        program = """
import ctypes,time
ctypes.CDLL(None).prctl(15,b'LogosIndexer',0,0,0)
print('ready',flush=True)
time.sleep(20)
"""
        environment = dict(os.environ, WINEPREFIX=str(self.context.prefix))
        process = self.child(program, self.context.prefix, env=environment)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        self.app.logos = logos.LogosManager(self.app)
        self.assertEqual(self.app.logos.logos_state, logos.State.STOPPED)
        with self.assertRaises(maintenance.MaintenanceBusy):
            backup.BackupTask(self.app).run()
        self.assertIsNone(process.poll())
        other = maintenance.Context(self.root / "unrelated")
        maintenance.assert_quiescent(other)
        self.assertIsNone(process.poll())
        process.terminate()
        process.wait(timeout=5)

    def test_transitions_and_unknown_relevant_process_fail_closed(self):
        self.app.logos = logos.LogosManager(self.app)
        for attribute in ("logos_state", "indexing_state"):
            for state in (logos.State.STARTING, logos.State.STOPPING):
                setattr(self.app.logos, attribute, state)
                with self.assertRaises(maintenance.MaintenanceBusy):
                    backup.BackupTask(self.app).run()
                setattr(self.app.logos, attribute, logos.State.STOPPED)
        process = Mock(pid=987654)
        process.info = {"name": "wine", "cmdline": [], "uids": SimpleNamespace(real=os.geteuid())}
        process.environ.side_effect = maintenance.psutil.AccessDenied(process.pid)
        with patch.object(maintenance.psutil, "process_iter", return_value=[process]):
            with self.assertRaises(maintenance.MaintenanceBusy):
                maintenance.assert_quiescent(self.context)

    def test_logging_retains_admission_through_final_config_write(self):
        entered = threading.Event()
        release = threading.Event()

        class Conf:
            _raw = self.app.conf._raw
            _overrides = self.app.conf._overrides
            wine_binary = "fixture"

            @property
            def faithlife_product_logging(self):
                return False

            @faithlife_product_logging.setter
            def faithlife_product_logging(self, value):
                entered.set()
                release.wait(5)

        self.app.conf = Conf()
        manager = logos.LogosManager(self.app)
        with patch.object(wine, "run_wine_during_install"), patch.object(wine, "wineserver_wait"):
            thread = threading.Thread(target=manager.switch_logging, kwargs={"action": "enable"})
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(maintenance.MaintenanceBusy), maintenance.maintain(self.app):
                    pass
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_uninstall_rejects_external_override_before_removing_anything(self):
        install = self.context.install
        (install / "data/wine64_bottle").mkdir(parents=True)
        sentinel = install / "sentinel"
        sentinel.write_bytes(b"unchanged installation")
        self.app.conf._overrides.wine_prefix = str(self.root / "external-prefix")
        with self.peer_lease(False), self.assertRaises(maintenance.MaintenanceError):
            control.uninstall(self.app)
        self.assertEqual(sentinel.read_bytes(), b"unchanged installation")
        self.app.approve.assert_not_called()

    def test_sqlite_context_closes_after_normal_and_exceptional_exit(self):
        path = self.root / "Documents/user/LocalUserPreferences/PreferencesManager.db"
        path.parent.mkdir(parents=True)
        sqlite3.connect(path).close()
        for exceptional in (False, True):
            manager = database.LocalUserPreferencesManager(self.root, "user")
            try:
                with manager:
                    connection = manager.database
                    if exceptional:
                        raise ValueError("fixture exception")
            except ValueError:
                pass
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                connection.execute("SELECT 1")
            manager.close()

    def test_real_watcher_stops_before_mutation_and_releases_connections_and_fds(self):
        path = self.root / "watch.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE settings(value INTEGER)")
            connection.execute("INSERT INTO settings VALUES (0)")
        connection.close()
        self.context.state
        baseline = len(os.listdir("/proc/self/fd"))
        manager = logos.LogosManager(self.app)
        self.app.logos = manager

        def start_thread(task):
            thread = threading.Thread(target=task)
            thread.start()
            return thread

        self.app.start_thread = start_thread
        for _ in range(3):
            manager._watch_database(path, ["UPDATE settings SET value=1 WHERE value<>1"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with sqlite3.connect(path) as check:
                    ready = check.execute("SELECT value FROM settings").fetchone() == (1,)
                check.close()
                if ready:
                    break
                time.sleep(0.02)
            self.assertTrue(ready)
            with maintenance.maintain(self.app):
                self.assertEqual(manager._watchers, {})
                with sqlite3.connect(path, timeout=0.1) as write:
                    write.execute("BEGIN EXCLUSIVE")
                    write.execute("UPDATE settings SET value=0")
                write.close()
        self.assertEqual(len(os.listdir("/proc/self/fd")), baseline)

    def test_stalled_watcher_prevents_mutation(self):
        manager = logos.LogosManager(self.app)
        self.app.logos = manager
        stop = threading.Event()
        thread = Mock()
        thread.is_alive.return_value = True
        manager._watchers["fixture"] = (stop, thread)
        with self.assertRaises(maintenance.MaintenanceBusy):
            backup.BackupTask(self.app).run()
        self.assertTrue(stop.is_set())
        self.assertFalse(self.context.prefix.exists())

    def test_repair_sqlite_lifetime_is_admitted_and_uses_selected_prefix(self):
        selected = self.root / "selected-prefix"
        self.app.conf._overrides.wine_prefix = str(selected)
        context = maintenance.Context.from_app(self.app)
        live = selected / "drive_c/users/fixture/AppData/Local/Logos"
        path = live / "Documents/user/LocalUserPreferences/PreferencesManager.db"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE Preferences(Type TEXT, Data TEXT)")
            connection.execute("INSERT INTO Preferences VALUES ('AppLocalPreferences','fixture')")
        connection.close()
        entered = threading.Event()
        release = threading.Event()
        retained = []
        errors = []

        def inspect(actual, product):
            self.assertEqual(Path(actual), live)
            with database.LocalUserPreferencesManager(live, "user") as manager:
                retained.append(manager.database)
                self.assertEqual(manager.app_local_preferences, "fixture")
                entered.set()
                release.wait(5)
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                retained[-1].execute("SELECT 1")
            return None

        def run():
            try:
                repair.detect_and_recover(self.app.conf._overrides)
            except BaseException as error:
                errors.append(error)

        with (
            patch.object(repair.PersistentConfiguration, "load_from_path", return_value=self.app.conf._raw),
            patch.object(repair.ou_dedetai.config, "get_wine_user", return_value="fixture"),
            patch.object(repair, "detect_broken_install", side_effect=inspect),
        ):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(maintenance.MaintenanceBusy), maintenance.use(context, exclusive=True):
                    pass
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(errors, errors)
        self.assertEqual(len(retained), 1)
        self.assertFalse(self.context.prefix.exists())
        with maintenance.use(context, exclusive=True):
            pass

    def test_selection_changes_refuse_while_actual_config_is_admitted(self):
        from tests.test_runtime_paths import fixture_config

        configuration = fixture_config(self.context.install)
        self.app.conf = configuration
        configuration.app = self.app
        configuration._overrides.config_path = str(self.root / "selected.json")
        entered = threading.Event()
        release = threading.Event()

        def admitted():
            with maintenance.configured(self.app) as selected, maintenance.use(selected, exclusive=True):
                entered.set()
                release.wait(5)

        thread = threading.Thread(target=admitted)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            for attribute, value in (("install_dir", self.root / "other"), ("faithlife_product", "Verbum")):
                with self.assertRaises(maintenance.MaintenanceBusy):
                    setattr(configuration, attribute, value)
            with patch.object(repair.PersistentConfiguration, "load_from_path", return_value=configuration._raw):
                with self.assertRaises(maintenance.MaintenanceBusy):
                    configuration.reload()
            self.assertEqual(configuration._raw.install_dir, str(self.context.install))
            configuration._write.assert_not_called()
        finally:
            release.set()
            thread.join(5)

    def test_failed_launch_returns_error_resets_state_and_releases_admission(self):
        executable = self.root / "fixture-runtime"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        self.app.conf.wine_binary = str(executable)
        self.app.conf.installed_faithlife_product_release = "1"
        self.app.conf.faithlife_product_version = "10"
        self.app.conf.logos_exe = "Logos.exe"
        self.app.conf.logos_indexer_exe_windows_path = "LogosIndexer.exe"
        manager = logos.LogosManager(self.app)
        self.app.logos = manager
        for action in (manager.start, manager.index):
            with (
                patch.object(wine, "get_wine_release", return_value=(None, None)),
                patch.object(wine, "check_wine_rules", return_value=(True, None)),
                patch.object(wine, "wineserver_kill", side_effect=ValueError("missing wineserver")),
                self.assertRaises(ValueError),
            ):
                action()
            self.assertEqual(manager.logos_state, logos.State.STOPPED)
            self.assertEqual(manager.indexing_state, logos.State.STOPPED)
            with maintenance.maintain(self.app):
                pass
        with (
            patch.object(wine, "get_wine_release", return_value=(None, None)),
            patch.object(wine, "check_wine_rules", return_value=(True, None)),
            patch.object(wine, "wineserver_kill"),
            patch.object(manager, "prevent_logos_updates", side_effect=OSError("preparation failed")),
            self.assertRaises(OSError),
        ):
            manager.start()
        self.assertEqual(manager.logos_state, logos.State.STOPPED)
        with maintenance.maintain(self.app):
            pass

    def test_index_cleanup_refuses_links_and_parent_substitution(self):
        live = Path(self.app.conf._logos_appdata_dir)
        catalog = live / "Data/user/LibraryCatalog"
        catalog.mkdir(parents=True)
        (catalog / "catalog.db").write_bytes(b"old catalog")
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside")
        saved = catalog.with_name("saved")
        catalog.rename(saved)
        catalog.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            control.remove_library_catalog(self.app)
        self.assertEqual(sentinel.read_bytes(), b"outside")
        catalog.unlink()
        saved.rename(catalog)
        inspection = maintenance.assert_quiescent
        count = 0

        def substitute(context):
            nonlocal count
            count += 1
            inspection(context)
            if count == 3:
                catalog.rename(saved)
                catalog.symlink_to(outside, target_is_directory=True)

        with patch.object(maintenance, "assert_quiescent", side_effect=substitute):
            with self.assertRaises(maintenance.MaintenanceError):
                control.remove_library_catalog(self.app)
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertEqual((saved / "catalog.db").read_bytes(), b"old catalog")

    def test_failed_upgrade_clears_the_config_consumed_by_installer(self):
        from tests.test_runtime_paths import fixture_config

        configuration = fixture_config(self.context.install)
        configuration._raw.faithlife_product_release = "old-target"
        self.app.conf = configuration
        configuration.app = self.app
        configuration._overrides.config_path = str(self.root / "selected.json")
        configuration._overrides.wine_prefix = None
        loaded = replace(configuration._raw)
        observed = []

        def install(app):
            observed.append(app.conf._raw.faithlife_product_release)

        with (
            patch.object(repair.PersistentConfiguration, "load_from_path", return_value=loaded),
            patch.object(repair.ou_dedetai.config, "get_wine_user", return_value="fixture"),
            patch.object(repair, "detect_broken_install", return_value=repair.FailureType.FailedUpgrade),
            patch.object(repair, "run_under_app", side_effect=lambda ephemeral, callback: callback(self.app)),
            patch.object(repair.ou_dedetai.installer, "install", side_effect=install),
            patch.object(repair.time, "sleep"),
        ):
            repair.detect_and_recover(configuration._overrides)
        self.assertEqual(observed, [None])
        configuration._write.assert_called_once_with(checked=True)
        self.assertEqual(loaded.faithlife_product_release, "old-target")

    def test_checked_logging_persistence_failure_is_reported(self):
        from tests.test_runtime_paths import fixture_config

        configuration = fixture_config(self.context.install)
        configuration._overrides.config_path = str(self.root / "selected.json")
        configuration._write = Config._write.__get__(configuration)
        configuration.app._config_updated_event = Mock()
        with patch("builtins.open", side_effect=PermissionError("injected config write failure")):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "Runtime logging changed"):
                configuration.faithlife_product_logging = True
        with patch.object(configuration._raw, "write_config") as writer:
            configuration.faithlife_product_logging = True
            writer.assert_called_once_with(checked=True, config_file_path=configuration._overrides.config_path)

    def test_neutral_runtime_process_keeps_lease_until_exit_and_is_detected_by_prefix(self):
        self.app.conf.wine_binary = "neutral-runtime"
        windows_path = r"C:\users\fixture\AppData\Local\Logos\System\LogosIndexer.exe"
        self.app.conf.logos_indexer_exe_windows_path = windows_path
        manager = logos.LogosManager(self.app)
        self.app.logos = manager
        threads = []

        def start(task):
            thread = threading.Thread(target=task)
            threads.append(thread)
            thread.start()
            return thread

        self.app.start_thread = start
        alias = self.root / "prefix-alias"
        alias.symlink_to(self.context.prefix, target_is_directory=True)
        process = self.child(
            "import time\nprint('ready',flush=True)\ntime.sleep(20)", windows_path,
            env=dict(os.environ, WINEPREFIX=str(alias)),
        )
        self.assertEqual(process.stdout.readline().strip(), "ready")
        with patch.object(wine, "wineserver_kill"), patch.object(wine, "run_wine_application", return_value=process):
            manager.index()
        try:
            with self.assertRaises(maintenance.MaintenanceBusy), maintenance.use(self.context, exclusive=True):
                pass
            with self.assertRaises(maintenance.MaintenanceBusy):
                maintenance.assert_quiescent(self.context)
            program = """
import sys
from pathlib import Path
from ou_dedetai import constants,maintenance
constants.STATE_DIR=sys.argv[2]
try:
    with maintenance.use(maintenance.Context(Path(sys.argv[1])),exclusive=True):
        raise AssertionError('peer acquired a live process lease')
except maintenance.MaintenanceBusy:
    print('busy',flush=True)
"""
            peer = self.child(program, self.context.prefix, constants.STATE_DIR)
            self.assertEqual(peer.stdout.readline().strip(), "busy")
            self.assertEqual(peer.wait(timeout=5), 0)
        finally:
            process.terminate()
            process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        with maintenance.use(self.context, exclusive=True):
            pass

    def test_logging_directory_preparation_failure_retries_persistence(self):
        from tests.test_runtime_paths import fixture_config

        configuration = fixture_config(self.context.install)
        configuration._overrides.config_path = str(self.root / "selected.json")
        configuration._write = Config._write.__get__(configuration)
        configuration.app._config_updated_event = Mock()
        with patch("ou_dedetai.config.os.makedirs", side_effect=PermissionError("injected directory failure")):
            with self.assertRaises(maintenance.MaintenanceError):
                configuration.faithlife_product_logging = True
        self.assertIsNone(configuration._raw.faithlife_product_logging)
        with patch.object(configuration._raw, "write_config") as writer:
            configuration.faithlife_product_logging = True
            writer.assert_called_once_with(checked=True, config_file_path=configuration._overrides.config_path)

    def test_installer_cancel_preserves_selection_and_releases_it(self):
        from ou_dedetai.app import UserExitedFromAsk
        from tests.test_runtime_paths import fixture_config

        configuration = fixture_config(self.context.install)
        self.app.conf = configuration
        configuration.app = self.app
        with patch.object(installer, "ensure_launcher_shortcuts", side_effect=UserExitedFromAsk):
            with self.assertRaises(UserExitedFromAsk):
                installer.install(self.app)
        self.assertEqual(configuration._raw.faithlife_product, "Logos")
        with maintenance.use(self.context, exclusive=True):
            pass

    def test_real_nonzero_wine_command_and_missing_logging_spawn_are_errors(self):
        runtime = self.root / "failure-runtime"
        runtime.write_text(f"#!{sys.executable}\nraise SystemExit(23)\n")
        runtime.chmod(0o755)
        with patch.object(wine, "get_wine_env", return_value=dict(os.environ)):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "exit code 23"):
                wine.run_wine_during_install(self.app, str(runtime), exe="fixture")
        self.app.conf.wine_binary = str(runtime)
        self.app.conf.faithlife_product_logging = False
        manager = logos.LogosManager(self.app)
        with patch.object(wine, "run_wine_during_install", return_value=None):
            with self.assertRaises(maintenance.MaintenanceError):
                manager.switch_logging(action="enable")
        self.assertFalse(self.app.conf.faithlife_product_logging)
