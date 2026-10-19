from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import psutil

from ou_dedetai import artifacts, backup, cli, config_store, constants, installer, logos, main, maintenance
from ou_dedetai import runtime, system, wine
from ou_dedetai.app import App
from tests.test_config import configuration


def manifest(path):
    return {str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in path.rglob("*") if item.is_file()}


class TestRecoveryDispatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(constants, "STATE_DIR", str(self.root / "state/app")).start()
        self.user = "fixture-" + self.root.name
        self.profiles = []
        for name, product in (("other", "Verbum"), ("chosen", "Logos")):
            install = self.root / name
            prefix = self.root / f"{name}-prefix"
            appdata = prefix / f"drive_c/users/{self.user}/AppData/Local/{product}"
            (appdata / "System").mkdir(parents=True)
            (appdata / "System" / f"{product}.exe").write_bytes(b"failed-upgrade fixture")
            path = self.root / f"{name}.json"
            document = {
                "install_dir": str(install), "faithlife_product": product, "faithlife_product_version": "10",
                "faithlife_product_release": "38.0.0.0", "WINEPREFIX": str(prefix),
                "wine_binary": "Recommended", "future": "preserve",
            }
            install.mkdir()
            path.write_text(json.dumps(document))
            (install / f"{constants.BINARY_NAME}.json").write_text(json.dumps(document))
            self.profiles.append((path, install, prefix, appdata))
        self.other, self.selected = self.profiles

    def dispatch(self, action, answers=()):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["controller", "-c", str(self.selected[0]), "--force-root", action]),
            patch.dict(os.environ, {"CONFIG_FILE": str(self.other[0])}),
            patch.object(main.msg, "initialize_logging"), patch.object(main.msg, "update_log_path"),
            patch.object(main.msg, "update_log_level"), patch.object(App, "start_thread"),
            patch.object(cli.CLI, "_ask", side_effect=answers), patch.object(cli.CLI, "_status"),
            redirect_stdout(output),
        ):
            main.main()
        return output.getvalue()

    def forbidden_mutations(self):
        stack = ExitStack()
        for module, name in (
            (installer, "install"), (system, "check_incompatibilities"), (runtime, "recover"),
            (backup, "recover_pending_restore"), (artifacts, "import_local"), (config_store, "save"),
            (wine, "run_wine_process"), (wine, "run_wine_completed_process"),
        ):
            stack.enter_context(patch.object(module, name, side_effect=AssertionError(f"Unexpected mutation {name}")))
        return stack

    def test_support_status_edit_and_declined_repair_preserve_both_profiles_and_pending_records(self):
        before = manifest(self.root)
        chosen = configuration(self.selected[0])
        context = maintenance.Context.from_app(chosen.app)
        context.pending.write_text("preserved pending restore")
        journal = runtime.selection_state(self.selected[1]) / "runtime.json"
        journal.write_text("preserved pending runtime")
        before_prefixes = [manifest(profile[2]) for profile in self.profiles]
        with self.forbidden_mutations():
            status = self.dispatch("--status")
            self.assertIn(str(self.selected[2]), status)
            self.assertIn("Pending recovery", status)
            self.assertIn("--repair", status)
            with patch("ou_dedetai.control.edit_file") as edit:
                self.dispatch("--edit-config")
                edit.assert_called_once_with(str(self.selected[0]))
            archive = self.root / "support.zip"
            with patch("ou_dedetai.control.subprocess.check_output", return_value="fixture"):
                self.dispatch("--get-support", [str(archive), "Keep archive local"])
            with ZipFile(archive) as package:
                member = str(self.selected[0]).lstrip("/")
                self.assertEqual(package.read(member), self.selected[0].read_bytes())
                self.assertNotIn(str(self.other[0]).lstrip("/"), package.namelist())
            self.dispatch("--repair", ["No"])
        for name, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), digest)
        self.assertEqual([manifest(profile[2]) for profile in self.profiles], before_prefixes)
        self.assertEqual(context.pending.read_text(), "preserved pending restore")
        self.assertEqual(journal.read_text(), "preserved pending runtime")

    def child(self, prefix, query):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time\nprint('ready',flush=True)\ntime.sleep(30)", query],
            env=dict(os.environ, WINEPREFIX=str(prefix)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def close():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()

        self.addCleanup(close)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def test_stop_with_identical_windows_argv_only_signals_selected_prefix_without_recovery(self):
        query = f"C:\\users\\{self.user}\\AppData\\Local\\Logos\\System\\Logos.exe"
        other = self.child(self.other[2], query)
        sentinel = self.child(self.root / "unrelated", "unrelated-sentinel")
        for pending in (False, True):
            if pending:
                context = maintenance.Context(self.selected[2], self.selected[1], "Logos")
                context.pending.write_text("keep restore state")
                (runtime.selection_state(self.selected[1]) / "runtime.json").write_text("keep runtime state")
            selected = self.child(self.selected[2], query)
            before = manifest(self.root)
            with self.forbidden_mutations():
                self.dispatch("--stop-installed-app")
            self.assertEqual(selected.wait(timeout=5), -9)
            self.assertIsNone(other.poll())
            self.assertIsNone(sentinel.poll())
            self.assertEqual(manifest(self.root), before)

    def test_stop_refuses_uncertain_or_changed_process_identity(self):
        conf = configuration(self.selected[0])
        manager = logos.LogosManager(conf.app)
        context = maintenance.Context.from_app(conf.app)
        query = conf.logos_system_exe_windows_path
        process = Mock()
        process.uids.return_value = SimpleNamespace(real=os.geteuid())
        process.cmdline.return_value = [query]
        for prefix in (None, "relative", str(self.other[2])):
            process.environ.return_value = {"WINEPREFIX": prefix} if prefix else {}
            self.assertFalse(manager._matches_selection(process, context))
        process.environ.side_effect = psutil.AccessDenied(987654)
        self.assertFalse(manager._matches_selection(process, context))
        process.environ.side_effect = None
        process.environ.return_value = {"WINEPREFIX": str(context.prefix)}
        process.is_running.side_effect = [True, False]
        self.assertFalse(manager._matches_selection(process, context))
        process.kill.assert_not_called()

    def test_explicit_repair_targets_selected_prefix_once_and_failed_install_restores_release(self):
        other_before = [self.other[0].read_bytes(), manifest(self.other[1]), manifest(self.other[2])]
        observed = []

        def install(app):
            context = maintenance.current_context()
            self.assertEqual(context.prefix, self.selected[2])
            self.assertIsNone(app.conf._raw.faithlife_product_release)
            self.assertNotIn("faithlife_product_release", json.loads(self.selected[0].read_bytes()))
            observed.append(context)
            raise maintenance.MaintenanceError("injected installer failure")

        with patch.object(installer, "install", side_effect=install):
            with self.assertRaises(SystemExit):
                self.dispatch("--repair", ["Yes"])
        self.assertEqual(len(observed), 1)
        saved = json.loads(self.selected[0].read_bytes())
        self.assertEqual(saved["faithlife_product_release"], "38.0.0.0")
        self.assertEqual(saved["future"], "preserve")
        self.assertEqual([self.other[0].read_bytes(), manifest(self.other[1]), manifest(self.other[2])], other_before)

    def test_normal_launch_reports_explicit_repair_before_runtime_or_database_mutation(self):
        conf = configuration(self.selected[0])
        manager = logos.LogosManager(conf.app)
        before = [manifest(profile[2]) for profile in self.profiles]
        with self.forbidden_mutations():
            with self.assertRaisesRegex(maintenance.RecoveryRequired, "--repair"):
                manager.start()
        self.assertEqual([manifest(profile[2]) for profile in self.profiles], before)

    def test_cold_diagnostics_do_not_create_cache_or_clear_a_prefix_log(self):
        cache_base = self.root / "cold-cache"
        cache = cache_base / "logos-desktop"
        log = self.selected[2] / "diagnostic.log"
        log.write_text("preserve diagnostic evidence")
        document = json.loads(self.selected[0].read_bytes())
        document.update(LOGOS_LOG=str(log), DELETE_LOG=True)
        self.selected[0].write_text(json.dumps(document))
        before = manifest(self.root)
        with patch.multiple(
            constants, CACHE_BASE=cache_base, CACHE_DIR=str(cache), NETWORK_CACHE_PATH=str(cache / "network.json"),
        ):
            with self.forbidden_mutations():
                self.dispatch("--status")
                self.dispatch("--stop-installed-app")
        self.assertFalse(cache_base.exists())
        self.assertEqual(manifest(self.root), before)

    def test_selected_profile_uninstall_removes_only_its_owned_installation(self):
        path, install, external, _ = self.selected
        document = json.loads(path.read_bytes())
        document.pop("WINEPREFIX")
        path.write_text(json.dumps(document))
        (install / f"{constants.BINARY_NAME}.json").write_text(json.dumps(document))
        (install / "data/bin").mkdir(parents=True)
        (install / "data/wine64_bottle").mkdir()
        other_before = self.other[0].read_bytes(), manifest(self.other[1]), manifest(self.other[2])
        external_before = manifest(external)
        with self.forbidden_mutations():
            self.dispatch("--uninstall", ["No", "Yes"])
        self.assertFalse(path.exists())
        self.assertFalse(install.exists())
        self.assertEqual(manifest(external), external_before)
        self.assertEqual((self.other[0].read_bytes(), manifest(self.other[1]), manifest(self.other[2])), other_before)
