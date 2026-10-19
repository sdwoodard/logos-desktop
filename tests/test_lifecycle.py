from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import psutil

from ou_dedetai import cli, constants, logos, main, maintenance, wine
from tests.test_app_exit import TestApp
from tests.test_backup import fixture_app


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(constants, "STATE_DIR", str(self.root / "state/app")))
        conf = fixture_app(self.root).conf
        conf.logos_exe = str(self.root / "Logos.exe")
        conf.logos_system_exe_windows_path = r"C:\fixture\Logos.exe"
        conf.logos_indexer_exe_windows_path = r"C:\fixture\LogosIndexer.exe"
        conf.logos_cef_exe_windows_path = r"C:\fixture\LogosCEF.exe"
        conf.wine_binary = str(self.root / "runtime")
        conf.installed_faithlife_product_release = "40.0.0.0"
        conf.faithlife_product_version = "10"
        conf._logos_appdata_dir = None
        conf._overrides.quiet = False
        with patch("ou_dedetai.config.Config", return_value=conf):
            self.app = TestApp(None)
        self.app.is_installed = lambda: True
        self.app.status = Mock()
        self.manager = self.app.logos
        self.context = maintenance.Context.from_app(self.app)
        self.addCleanup(self.app.close)
        self.stack.enter_context(patch("ou_dedetai.runtime.executable", return_value="fixture-runtime"))
        self.stack.enter_context(patch.object(
            wine, "get_wine_release", return_value=(wine.WineRelease(10, 8, "staging"), "yes"),
        ))
        self.stack.enter_context(patch.object(wine, "check_wine_rules", return_value=(True, None)))
        self.stack.enter_context(patch("ou_dedetai.repair.inspect", return_value=None))

    def child(self, query, *, prefix=None, ignore_term=False):
        program = "import signal,time\n"
        if ignore_term:
            program += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        program += "print('ready',flush=True)\ntime.sleep(60)\n"
        process = subprocess.Popen([sys.executable, "-B", "-c", program, query],
                                   env=dict(os.environ, WINEPREFIX=str(prefix or self.context.prefix)),
                                   stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "ready")

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(3)
            process.stdout.close()
        self.addCleanup(cleanup)
        return process

    def launch(self, *, index=False):
        def spawn(*args, **kwargs):
            return self.child(kwargs["exe"])
        with patch.object(wine, "run_wine_application", side_effect=spawn):
            self.manager.index() if index else self.manager.start()
        return self.manager._runs["index" if index else "logos"].process

    def tick(self):
        self.manager._next_scan = 0
        self.manager.monitor()

    def test_repeated_launch_stop_and_detach_preserve_unrelated_process(self):
        sentinel = self.child("unrelated", prefix=self.root / "other")
        for _ in range(3):
            child = self.launch()
            with self.assertRaises(maintenance.MaintenanceBusy):
                self.manager.start()
            self.manager.stop()
            self.assertEqual(child.wait(3), -15)
            self.assertEqual(self.manager.logos_state, logos.State.STOPPED)
            self.assertEqual(self.manager._runs, {})
        detached = self.launch()
        self.assertTrue(self.app.close())
        self.assertIsNone(detached.poll())
        self.assertIsNone(sentinel.poll())
        self.assertEqual(self.app._threads, [])

    def test_indexer_outlives_launcher_and_no_success_is_reported_until_exit(self):
        launcher = self.launch(index=True)
        descendant = self.child(self.app.conf.logos_indexer_exe_windows_path)
        launcher.terminate()
        launcher.wait(3)
        self.tick()
        self.assertEqual(self.manager.indexing_state, logos.State.RUNNING)
        self.assertFalse(any("finished" in str(call) for call in self.app.status.call_args_list))
        with self.assertRaises(maintenance.MaintenanceBusy), maintenance.use(self.context, exclusive=True):
            pass
        self.manager.stop_indexing()
        self.assertEqual(descendant.wait(3), -15)
        self.assertEqual(self.manager.indexing_state, logos.State.STOPPED)

    def test_snapshot_is_single_scan_and_selected_prefix_only(self):
        other = self.child(self.app.conf.logos_system_exe_windows_path, prefix=self.root / "other")
        original = psutil.process_iter
        with patch.object(psutil, "process_iter", wraps=original) as scan:
            self.tick()
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.manager.logos_state, logos.State.STOPPED)
        self.assertIsNone(other.poll())

    def test_duplicate_start_during_validation_does_not_spawn_twice(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def spawn(*args, **kwargs):
            entered.set()
            release.wait(3)
            return self.child(kwargs["exe"])

        def start():
            try:
                self.manager.start()
            except maintenance.MaintenanceBusy as error:
                errors.append(error)

        with patch.object(wine, "run_wine_application", side_effect=spawn) as create:
            first = threading.Thread(target=start)
            second = threading.Thread(target=start)
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            release.set()
            first.join(3)
            second.join(3)
            self.assertEqual(create.call_count, 1)
        self.assertEqual(len(errors), 1)
        self.manager.stop()

    def test_index_stop_escalates_only_verified_processes_and_ignores_finished_handle(self):
        query = self.app.conf.logos_indexer_exe_windows_path
        child = self.child(query, ignore_term=True)
        other = self.child(query, prefix=self.root / "other")
        self.manager.processes["stale"] = Mock(pid=other.pid, poll=Mock(return_value=0))
        self.manager.stop_indexing()
        self.assertEqual(child.wait(3), -9)
        self.assertIsNone(other.poll())

    def test_failed_index_exit_does_not_report_success(self):
        child = self.launch(index=True)
        child.terminate()
        child.wait(3)
        self.tick()
        self.manager._runs["index"].empty_since = time.monotonic() - 3
        self.tick()
        self.assertEqual(self.manager.indexing_state, logos.State.STOPPED)
        self.app.status.assert_called_with("Indexing process failed with exit code -15", 0)

    def test_real_differently_named_descendant_remains_owned_after_launcher_exit(self):
        program = r"""
import subprocess,sys,time
child=subprocess.Popen([sys.executable,'-B','-c','import time\ntime.sleep(60)'])
print(child.pid,flush=True)
time.sleep(2)
"""
        parent = subprocess.Popen([sys.executable, "-B", "-c", program], stdout=subprocess.PIPE, text=True,
                                  env=dict(os.environ, WINEPREFIX=str(self.context.prefix)))
        descendant = psutil.Process(int(parent.stdout.readline()))

        def cleanup():
            if descendant.is_running():
                descendant.kill()
            if parent.poll() is None:
                parent.kill()
            parent.wait(3)
            parent.stdout.close()
        self.addCleanup(cleanup)
        with patch.object(wine, "run_wine_application", return_value=parent):
            run = self.manager.index()
        parent.wait(4)
        self.tick()
        self.assertIn(descendant.pid, run.members)
        self.assertEqual(self.manager.indexing_state, logos.State.RUNNING)
        self.assertIsNone(run.result)
        self.manager.stop_indexing()
        self.assertEqual(run.result.outcome, "cancelled")

    def test_independent_manager_and_process_cannot_acquire_same_role(self):
        with maintenance.Lease(self.context, role="index").entered():
            other = logos.LogosManager(self.app)
            with self.assertRaisesRegex(maintenance.MaintenanceBusy, "controller"):
                other.index()
            program = """
import sys
from pathlib import Path
from ou_dedetai import constants,maintenance
constants.STATE_DIR=sys.argv[2]
try:
    with maintenance.Lease(maintenance.Context(Path(sys.argv[1])),role='index').entered():
        raise AssertionError('duplicate admitted')
except maintenance.MaintenanceBusy:
    print('refused')
"""
            result = subprocess.run(
                [sys.executable, "-B", "-c", program, str(self.context.prefix), constants.STATE_DIR],
                check=True, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.stdout.strip(), "refused")

    def test_cli_dispatch_propagates_failed_and_unknown_index_results(self):
        path = self.root / "selected.json"
        path.write_text(json.dumps({"install_dir": str(self.context.install), "faithlife_product": "Logos"}))
        parser = main.get_parser()
        config, action = main.parse_args(parser.parse_args(["--run-indexing", "-c", str(path)]), parser)
        run_indexing = cli.CLI.run_indexing
        self.app.run_indexing = lambda: run_indexing(self.app)
        for outcome in ("failed", "unknown"):
            run = Mock(result=logos.RunResult(outcome, "index outcome is not success"))
            with (patch.object(self.manager, "index", return_value=run),
                  patch.object(cli, "CLI", return_value=self.app),
                  patch.object(main, "is_app_installed", return_value=True)):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "not success"):
                    main.run(config, action)

    def test_watcher_startup_failure_prevents_launch(self):
        with patch.object(wine, "run_wine_application") as launch:
            self.manager.prevent_logos_updates = lambda: self.manager._watch_database(
                self.root / "absent/policy.db", ["DELETE FROM policy"],
            )
            with self.assertRaisesRegex(maintenance.MaintenanceError, "protection"):
                self.manager.start()
        launch.assert_not_called()
        self.assertEqual(self.manager._runs, {})
        self.assertEqual(self.manager._watchers, {})

    def test_runtime_watcher_loss_is_reported_and_retained(self):
        directory = self.root / "database"
        directory.mkdir()
        path = directory / "policy.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE policy (value INTEGER)")
        connection.close()
        self.manager._watch_database(path, ["DELETE FROM policy"])
        directory.rename(self.root / "moved")
        health = self.manager._watcher_health[path]
        deadline = time.monotonic() + 3
        while not health.error and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(health.error)
        self.tick()
        self.assertEqual(self.manager.enforcement_error, health.error)
        self.app.status.assert_called_with(f"Database protection unavailable: {health.error}", 0)

    def test_index_stop_preserves_other_role_and_shared_service_descendants(self):
        program = r"""
import subprocess,sys,time
for name in ('Logos.exe','services.exe','LogosIndexer.exe'):
    child=subprocess.Popen([sys.executable,'-B','-c',
        'import ctypes,subprocess,sys,time\nctypes.CDLL(None).prctl(15,sys.argv[1].encode(),0,0,0)\n'
        'worker=subprocess.Popen([sys.executable,"-B","-c","import time;time.sleep(60)"])\n'
        'print(worker.pid,flush=True)\ntime.sleep(60)',name],stdout=subprocess.PIPE,text=True)
    grandchild=int(child.stdout.readline())
    print(child.pid,flush=True)
    print(grandchild,flush=True)
time.sleep(60)
"""
        parent = subprocess.Popen([sys.executable, "-B", "-c", program], stdout=subprocess.PIPE, text=True,
                                  env=dict(os.environ, WINEPREFIX=str(self.context.prefix)))
        children = [psutil.Process(int(parent.stdout.readline())) for _ in range(6)]

        def cleanup():
            for child in children:
                if child.is_running():
                    child.kill()
            if parent.poll() is None:
                parent.kill()
            parent.wait(3)
            parent.stdout.close()
        self.addCleanup(cleanup)
        with patch.object(wine, "run_wine_application", return_value=parent):
            self.manager.index()
        self.manager.stop_indexing()
        for child in children[:4]:
            self.assertTrue(child.is_running())
            self.assertNotEqual(child.status(), psutil.STATUS_ZOMBIE)
        for child in children[4:]:
            self.assertTrue(not child.is_running() or child.status() == psutil.STATUS_ZOMBIE)

    def test_only_direct_worker_can_supply_successful_exit_status(self):
        for direct in (False, True):
            child = self.launch(index=True)
            run = self.manager._runs["index"]
            recognized = psutil.Process(child.pid) if direct else self.child("separate-worker")
            member = recognized if direct else psutil.Process(recognized.pid)
            run.members[member.pid] = member
            original_name = psutil.Process.name

            def name(process):
                return "LogosIndexer.ex" if process.pid == member.pid else original_name(process)

            with patch.object(psutil.Process, "name", name):
                self.tick()
            if not direct:
                recognized.kill()
                recognized.wait(3)
            child.terminate()
            child.wait(3)
            with patch.object(child, "poll", return_value=0):
                self.tick()
                run.empty_since = time.monotonic() - 3
                self.tick()
            self.assertEqual(run.result.outcome, "succeeded" if direct else "unknown")

    def test_status_delivery_does_not_hold_transition_lock(self):
        child = self.launch(index=True)
        child.terminate()
        child.wait(3)
        self.tick()
        self.manager._runs["index"].empty_since = time.monotonic() - 3
        acquired = threading.Event()

        def status(*args):
            def competitor():
                with self.manager._transition:
                    acquired.set()
            thread = threading.Thread(target=competitor)
            thread.start()
            self.assertTrue(acquired.wait(2))
            thread.join(2)

        self.app.status = status
        self.tick()
