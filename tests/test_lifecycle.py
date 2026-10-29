from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import psutil

from logos_desktop import cli, constants, logos, main, maintenance, wine
from logos_desktop.app import UserExitedFromAsk
from tests.test_app_exit import TestApp
from tests.test_backup import fixture_app


class TestLifecycle(unittest.TestCase):
    def test_cancelling_either_theme_helper_never_starts_logos_or_retains_its_lease(self):
        self.app.system_dark = True
        self.app.conf.wine64_binary = self.app.conf.wine_binary
        for name in ('AppsUseLightTheme', 'SystemUsesLightTheme'):
            for failed in (False, True):
                entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
                errors = []

                def checkpoint():
                    if cancelled.is_set():
                        raise UserExitedFromAsk('cancelled')

                def helper(*args, **kwargs):
                    if kwargs['exe_args'][3] == name:
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError('Test did not release the registry helper')
                        if failed:
                            raise maintenance.MaintenanceError('Synthetic registry failure')
                    return SimpleNamespace(returncode=0)

                def start():
                    try:
                        self.manager.start()
                    except Exception as error:
                        errors.append(error)

                with (self.subTest(name=name, failed=failed),
                      patch('logos_desktop.installer.ensure_login_handler'),
                      patch.object(self.manager, 'start_monitoring'),
                      patch.object(self.app, 'checkpoint', side_effect=checkpoint),
                      patch.object(wine, 'run_wine_completed_process', side_effect=helper),
                      patch.object(wine, 'run_wine_application') as launch):
                    worker = threading.Thread(target=start)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        cancelled.set()
                    finally:
                        release.set()
                        worker.join(10)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], UserExitedFromAsk)
                    launch.assert_not_called()
                    self.assertEqual(self.manager.logos_state, logos.State.STOPPED)
                    lease = maintenance.Lease(self.context, role='logos')
                    lease.close()

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
        with patch("logos_desktop.config.Config", return_value=conf):
            self.app = TestApp(None)
        self.app.is_installed = lambda: True
        self.app.status = Mock()
        self.manager = self.app.logos
        self.context = maintenance.Context.from_app(self.app)
        self.addCleanup(self.app.close)
        self.stack.enter_context(patch("logos_desktop.runtime.executable", return_value="fixture-runtime"))
        self.stack.enter_context(patch.object(
            wine, "get_wine_release", return_value=(wine.WineRelease(10, 8, "staging"), "yes"),
        ))
        self.stack.enter_context(patch.object(wine, "check_wine_rules", return_value=(True, None)))
        self.stack.enter_context(patch("logos_desktop.repair.inspect", return_value=None))

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

    def process_candidate(self, pid, *, parent=0, query="unrelated", uid=None, status=psutil.STATUS_SLEEPING):
        process = MagicMock(spec=psutil.Process)
        process.pid = pid
        process.oneshot.side_effect = nullcontext
        process.uids.return_value = SimpleNamespace(real=os.geteuid() if uid is None else uid)
        process.status.return_value = status
        process.ppid.return_value = parent
        process.cmdline.return_value = [query]
        process.is_running.return_value = True
        process.environ.return_value = {"WINEPREFIX": str(self.context.prefix)}
        process.name.return_value = "python"
        process.as_dict.side_effect = lambda attrs: psutil.Process.as_dict(process, attrs=attrs)
        return process

    def test_stopped_monitor_scans_every_five_seconds(self):
        with (patch.object(logos.time, "monotonic", return_value=100) as clock,
              patch.object(self.manager, "get_logos_pids", wraps=self.manager.get_logos_pids) as scan):
            self.manager.monitor()
            self.assertEqual(scan.call_count, 1)
            for now in (101, 102, 103, 104, 104.999):
                clock.return_value = now
                self.manager.monitor()
            self.assertEqual(scan.call_count, 1)
            clock.return_value = 105
            self.manager.monitor()
            self.assertEqual(scan.call_count, 2)

    def test_observer_waits_for_running_or_idle_deadline_without_scan_duration_drift(self):
        for interval in (4, 5):
            with self.subTest(interval=interval):
                now = [100.0]
                scan_starts = []
                original_scan = self.manager.get_logos_pids
                closed = Mock()
                closed.is_set.return_value = False
                self.manager.logos_state = logos.State.RUNNING if interval == 4 else logos.State.STOPPED
                self.manager.indexing_state = logos.State.STOPPED
                self.manager._published_state = (self.manager.logos_state, logos.State.STOPPED, "")

                def scan():
                    scan_starts.append(now[0])
                    original_scan()
                    now[0] += 0.027

                def wait(delay):
                    now[0] += delay
                    return len(scan_starts) == 2 and now[0] >= scan_starts[1] + 1

                closed.wait.side_effect = wait
                with (patch.object(logos.time, "monotonic", side_effect=lambda: now[0]),
                      patch.object(self.manager, "_closed", closed),
                      patch.object(self.manager, "_monitor_thread", None),
                      patch.object(self.manager, "get_logos_pids", side_effect=scan),
                      patch.object(self.manager, "_role_members", side_effect=lambda role:
                                   [object()] if interval == 4 and role == "logos" else []),
                      patch.object(self.app, "start_thread") as start_thread):
                    self.manager.start_monitoring()
                    observe = start_thread.call_args.args[0]
                    observe()

                self.assertEqual(len(scan_starts), 2)
                self.assertAlmostEqual(scan_starts[1] - scan_starts[0], interval)
                delays = [call.args[0] for call in closed.wait.call_args_list]
                self.assertTrue(all(0 <= delay <= 1 for delay in delays))
                self.assertTrue(any(0 < delay < 1 for delay in delays))

    def test_active_and_transition_states_confirm_before_four_second_scans(self):
        for role, attribute in (("logos", "logos_state"), ("index", "indexing_state")):
            for state in (logos.State.RUNNING, logos.State.STARTING, logos.State.STOPPING):
                with self.subTest(role=role, state=state):
                    self.manager.logos_state = self.manager.indexing_state = logos.State.STOPPED
                    self.manager._published_state = (logos.State.STOPPED, logos.State.STOPPED, "")
                    setattr(self.manager, attribute, state)
                    self.manager._next_scan = 0
                    with (patch.object(logos.time, "monotonic", return_value=100) as clock,
                          patch.object(self.manager, "_role_members",
                                       side_effect=lambda selected: [object()] if selected == role else []),
                          patch.object(self.manager, "get_logos_pids", wraps=self.manager.get_logos_pids) as scan):
                        self.manager.monitor()
                        clock.return_value = 100.999
                        self.manager.monitor()
                        self.assertEqual(scan.call_count, 1)
                        clock.return_value = 101
                        self.manager.monitor()
                        self.assertEqual(scan.call_count, 2)
                        self.assertEqual(getattr(self.manager, attribute), logos.State.RUNNING)
                        self.assertEqual(self.manager._next_scan, 105)

    def test_external_start_is_found_at_idle_deadline_then_steady_scans_slow(self):
        with (patch.object(logos.time, "monotonic", return_value=100) as clock,
              patch.object(self.manager, "get_logos_pids", wraps=self.manager.get_logos_pids) as scan):
            self.manager.monitor()
            child = self.child(self.app.conf.logos_system_exe_windows_path)
            clock.return_value = 104.999
            self.manager.monitor()
            self.assertEqual(self.manager.logos_state, logos.State.STOPPED)
            self.assertEqual(scan.call_count, 1)
            clock.return_value = 105
            self.manager.monitor()
            self.assertEqual(self.manager.logos_state, logos.State.RUNNING)
            self.assertEqual(scan.call_count, 2)
            clock.return_value = 106
            self.manager.monitor()
            self.assertEqual(scan.call_count, 3)
            self.assertEqual(self.manager._next_scan, 110)
            clock.return_value = 109.999
            self.manager.monitor()
            self.assertEqual(scan.call_count, 3)
            clock.return_value = 110
            self.manager.monitor()
            self.assertEqual(scan.call_count, 4)
            self.assertIsNone(child.poll())

    def test_healthy_running_roles_use_four_second_deadline(self):
        for roles in (("logos",), ("index",), ("logos", "index")):
            with self.subTest(roles=roles):
                self.manager.logos_state = self.manager.indexing_state = logos.State.STOPPED
                self.manager._published_state = (logos.State.STOPPED, logos.State.STOPPED, "")
                self.manager._next_scan = 0
                for role in roles:
                    setattr(self.manager, "logos_state" if role == "logos" else "indexing_state", logos.State.RUNNING)
                with (patch.object(logos.time, "monotonic", return_value=100) as clock,
                      patch.object(self.manager, "_role_members",
                                   side_effect=lambda role: [object()] if role in roles else []),
                      patch.object(self.manager, "get_logos_pids", wraps=self.manager.get_logos_pids) as scan):
                    self.manager.monitor()
                    self.assertEqual(self.manager._next_scan, 101)
                    clock.return_value = 101
                    self.manager.monitor()
                    self.assertEqual(self.manager._next_scan, 105)
                    clock.return_value = 104.999
                    self.manager.monitor()
                    self.assertEqual(scan.call_count, 2)
                    clock.return_value = 105
                    self.manager.monitor()
                    self.assertEqual(scan.call_count, 3)
                    self.assertEqual(self.manager._next_scan, 109)

    def test_enforcement_errors_and_recovery_keep_one_second_deadline(self):
        for running in (False, True):
            with self.subTest(running=running):
                self.manager.logos_state = logos.State.RUNNING if running else logos.State.STOPPED
                self.manager.indexing_state = logos.State.STOPPED
                self.manager.enforcement_error = ""
                self.manager._next_scan = 0
                health = SimpleNamespace(error="blocked")
                self.manager._watcher_health = {"fixture": health}
                with (patch.object(logos.time, "monotonic", return_value=100) as clock,
                      patch.object(self.manager, "_role_members",
                                   side_effect=lambda role: [object()] if running and role == "logos" else [])):
                    self.manager.monitor()
                    self.assertEqual(self.manager._next_scan, 101)
                    clock.return_value = 101
                    self.manager.monitor()
                    self.assertEqual(self.manager._next_scan, 102)
                    health.error = ""
                    clock.return_value = 102
                    self.manager.monitor()
                    self.assertEqual(self.manager._next_scan, 103)
                    clock.return_value = 103
                    self.manager.monitor()
                    self.assertEqual(self.manager._next_scan, 107 if running else 108)

    def test_running_index_exit_switches_to_one_second_result_settlement(self):
        process = Mock(poll=Mock(return_value=None))
        run = logos.ManagedRun(self.context, process, Mock(), started=99, direct_worker=True)
        run.empty_since = 99
        self.manager._runs["index"] = run
        self.manager.indexing_state = logos.State.RUNNING
        self.manager._published_state = (logos.State.STOPPED, logos.State.RUNNING, "")
        with (patch.object(logos.time, "monotonic", return_value=100) as clock,
              patch.object(self.manager, "_run_members", return_value=[])):
            self.manager.monitor()
            self.assertIsNone(run.empty_since)
            self.assertEqual(self.manager._next_scan, 101)
            clock.return_value = 101
            self.manager.monitor()
            self.assertEqual(self.manager._next_scan, 105)
            process.poll.return_value = 0
            clock.return_value = 104.999
            self.manager.monitor()
            self.assertIsNone(run.empty_since)
            clock.return_value = 105
            self.manager.monitor()
            self.assertEqual(run.empty_since, 105)
            self.assertEqual(self.manager._next_scan, 106)
            clock.return_value = 106
            self.manager.monitor()
            self.assertIsNone(run.result)
            self.assertEqual(self.manager._next_scan, 107)
            clock.return_value = 107
            self.manager.monitor()
            self.assertEqual(run.result.outcome, "succeeded")
            run.lease.close.assert_called_once()
            self.assertEqual(self.manager._next_scan, 108)

    def test_unsettled_index_run_keeps_one_second_scans_until_result(self):
        run = logos.ManagedRun(self.context, Mock(poll=Mock(return_value=0)), Mock(),
                               started=99, direct_worker=True)
        self.manager._runs["index"] = run
        with (patch.object(logos.time, "monotonic", return_value=100) as clock,
              patch.object(self.manager, "_run_members", return_value=[])):
            self.manager.monitor()
            self.assertEqual(self.manager._next_scan, 101)
            clock.return_value = 101
            self.manager.monitor()
            run.lease.close.assert_not_called()
            self.assertIsNone(run.result)
            self.assertEqual(self.manager._next_scan, 102)
            clock.return_value = 102
            self.manager.monitor()
            run.lease.close.assert_called_once()
            self.assertEqual(run.result.outcome, "succeeded")
            self.assertNotIn("index", self.manager._runs)
            self.assertEqual(self.manager._next_scan, 103)
            clock.return_value = 103
            self.manager.monitor()
            self.assertEqual(self.manager._next_scan, 108)

    def test_explicit_start_and_stop_scan_fresh_despite_idle_deadline(self):
        self.manager.monitor()
        logos_child = self.child(self.app.conf.logos_system_exe_windows_path)
        with patch.object(wine, "run_wine_application") as spawn:
            with self.assertRaisesRegex(maintenance.MaintenanceBusy, "already running"):
                self.manager.start()
            spawn.assert_not_called()
        self.assertEqual(self.manager._next_scan, 0)

        index_child = self.child(self.app.conf.logos_indexer_exe_windows_path)
        self.manager._next_scan = time.monotonic() + 60
        self.manager.stop()
        self.assertEqual(logos_child.wait(3), -15)
        self.assertEqual(index_child.wait(3), -15)
        self.assertEqual(self.manager._next_scan, 0)

        self.manager._next_scan = time.monotonic() + 60
        with patch.object(self.manager, "start_monitoring"):
            child = self.launch(index=True)
        self.assertEqual(self.manager._next_scan, 0)
        now = time.monotonic()
        with patch.object(logos.time, "monotonic", return_value=now) as clock:
            self.manager.monitor()
            self.assertEqual(self.manager._next_scan, now + 1)
            clock.return_value = now + 1
            self.manager.monitor()
            self.assertEqual(self.manager._next_scan, now + 5)
        self.assertEqual(self.manager.indexing_state, logos.State.RUNNING)
        self.assertGreater(self.manager._next_scan, time.monotonic())
        self.manager.stop_indexing()
        self.assertEqual(child.wait(3), -15)
        self.assertEqual(self.manager._next_scan, 0)

    def test_explicit_start_checks_new_external_role_during_running_deadline(self):
        index_child = self.child(self.app.conf.logos_indexer_exe_windows_path)
        now = time.monotonic()
        with patch.object(logos.time, "monotonic", return_value=now) as clock:
            self.manager.monitor()
            clock.return_value = now + 1
            self.manager.monitor()
            self.assertEqual(self.manager._next_scan, now + 5)
        logos_child = self.child(self.app.conf.logos_system_exe_windows_path)
        self.assertGreater(self.manager._next_scan, time.monotonic())
        with patch.object(wine, "run_wine_application") as spawn:
            with self.assertRaisesRegex(maintenance.MaintenanceBusy, "already running"):
                self.manager.start()
            spawn.assert_not_called()
        self.assertEqual(self.manager._next_scan, 0)
        self.assertIsNone(index_child.poll())
        self.assertIsNone(logos_child.poll())

    def test_unmonitored_profile_transfer_does_not_reuse_idle_deadline(self):
        with patch.object(logos.time, "monotonic", return_value=100) as clock:
            self.manager.monitor()
            self.assertEqual(self.manager._next_scan, 105)
            candidate = deepcopy(self.app.conf)
            candidate._raw.install_dir = str(self.root / "other-install")
            candidate._overrides.config_path = str(self.root / "other.json")
            with self.app.profile_selection(candidate):
                pass
            self.assertIs(self.app.logos, self.manager)
            self.assertIsNone(self.manager._context)
            selected = maintenance.Context.from_app(self.app)
            child = self.child(candidate.logos_system_exe_windows_path, prefix=selected.prefix)
            clock.return_value = 101
            self.manager.monitor()
            self.assertEqual(self.manager._context, selected)
            self.assertEqual(self.manager.logos_state, logos.State.RUNNING)
            self.assertEqual(self.manager._next_scan, 102)
            self.assertIsNone(child.poll())

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

    def test_discovery_skips_names_but_verifies_matching_uid_live(self):
        unrelated = self.child("unrelated")
        selected = self.child(self.app.conf.logos_system_exe_windows_path)
        original_name = psutil.Process.name
        original_uids = psutil.Process.uids
        deny_selected_uid = False
        uid_reads = []

        def name(process):
            if process.pid in (unrelated.pid, selected.pid):
                raise AssertionError("Discovery does not need process names")
            return original_name(process)

        @wraps(original_uids)
        def uids(process):
            if process.pid in (unrelated.pid, selected.pid):
                uid_reads.append(process.pid)
            if process.pid == selected.pid and deny_selected_uid:
                raise psutil.AccessDenied(process.pid)
            return original_uids(process)

        with (patch.object(psutil.Process, "name", name),
              patch.object(psutil.Process, "uids", uids)):
            groups = self.manager.get_logos_pids(self.context)
            members = groups[self.app.conf.logos_system_exe_windows_path]
            self.assertEqual([process.pid for process in members], [selected.pid])
            self.assertIn(unrelated.pid, [process.pid for process in self.manager._snapshot])
            self.assertIn(unrelated.pid, uid_reads)
            self.assertGreaterEqual(uid_reads.count(selected.pid), 2)

            deny_selected_uid = True
            with self.assertRaisesRegex(maintenance.MaintenanceBusy, "process identity"):
                self.manager.get_logos_pids(self.context)

        self.assertIsNone(unrelated.poll())
        self.assertIsNone(selected.poll())

    def test_discovery_excludes_invalid_vertices_without_pruning_live_children(self):
        parents = [self.process_candidate(900001, uid=os.geteuid() + 1),
                   self.process_candidate(900002, status=psutil.STATUS_ZOMBIE),
                   self.process_candidate(900003, status=psutil.STATUS_DEAD)]
        children = [self.process_candidate(900011 + index, parent=parent.pid)
                    for index, parent in enumerate(parents)]
        with patch.object(psutil, "process_iter", return_value=[*parents, *children]):
            self.manager.get_logos_pids(self.context)
        self.assertEqual(self.manager._snapshot, children)
        for parent in parents:
            parent.as_dict.assert_not_called()
            parent.cmdline.assert_not_called()
            parent.ppid.assert_not_called()
        for child in children:
            child.ppid.assert_called()
            child.cmdline.assert_called()

        run = logos.ManagedRun(self.context, Mock(), Mock(), members={children[0].pid: children[0]})
        self.assertEqual(self.manager._run_members("index", run), [children[0]])
        self.assertEqual(self.manager._snapshot, children)

    def test_unknown_classification_keeps_full_unmatched_snapshot_entry(self):
        for denied in (("uids",), ("status",), ("uids", "status")):
            with self.subTest(denied=denied):
                process = self.process_candidate(900001)
                for field in denied:
                    getattr(process, field).side_effect = psutil.AccessDenied(process.pid)
                with patch.object(psutil, "process_iter", return_value=[process]):
                    self.manager.get_logos_pids(self.context)
                self.assertEqual(self.manager._snapshot, [process])
                process.ppid.assert_called()
                process.cmdline.assert_called()
                process.environ.assert_not_called()

    def test_unknown_matching_uid_or_status_fails_closed(self):
        for denied in (("uids",), ("status",), ("uids", "status")):
            with self.subTest(denied=denied):
                process = self.process_candidate(900001, query=self.app.conf.logos_system_exe_windows_path)
                for field in denied:
                    getattr(process, field).side_effect = psutil.AccessDenied(process.pid)
                with patch.object(psutil, "process_iter", return_value=[process]):
                    with self.assertRaisesRegex(maintenance.MaintenanceBusy, "process identity"):
                        self.manager.get_logos_pids(self.context)
                process.ppid.assert_called()
                process.cmdline.assert_called()

    def test_matching_uid_is_rechecked_after_classification_before_group_or_stop(self):
        process = self.process_candidate(900001, query=self.app.conf.logos_system_exe_windows_path)
        for action in (lambda: self.manager.get_logos_pids(self.context), self.manager.stop):
            with self.subTest(action=action):
                process.uids.side_effect = [SimpleNamespace(real=os.geteuid()), psutil.AccessDenied(process.pid)]
                with patch.object(psutil, "process_iter", return_value=[process]):
                    with self.assertRaisesRegex(maintenance.MaintenanceBusy, "process identity"):
                        action()
                process.environ.assert_not_called()
                process.terminate.assert_not_called()
                process.kill.assert_not_called()

    def test_vanished_or_reused_identity_is_not_adopted(self):
        query = self.app.conf.logos_system_exe_windows_path
        vanished = self.process_candidate(900001, query=query)
        vanished.ppid.side_effect = psutil.NoSuchProcess(vanished.pid)
        reused = self.process_candidate(900002, query=query)
        reused.is_running.return_value = False
        with patch.object(psutil, "process_iter", return_value=[vanished, reused]):
            groups = self.manager.get_logos_pids(self.context)
        self.assertEqual(groups[query], [])
        self.assertEqual(self.manager._snapshot, [reused])
        vanished.environ.assert_not_called()
        reused.environ.assert_not_called()
        run = logos.ManagedRun(self.context, Mock(), Mock(), members={reused.pid: reused})
        self.assertEqual(self.manager._run_members("logos", run), [])

    def test_failed_scan_keeps_one_second_retry_despite_steady_running_state(self):
        process = self.process_candidate(900001, query=self.app.conf.logos_system_exe_windows_path)
        process.uids.side_effect = psutil.AccessDenied(process.pid)
        self.manager.logos_state = logos.State.RUNNING
        self.manager._context = self.context
        self.manager._published_state = (logos.State.RUNNING, logos.State.STOPPED, "")
        with (patch.object(psutil, "process_iter", return_value=[process]),
              patch.object(logos.time, "monotonic", return_value=100) as clock):
            for now in (100, 101):
                clock.return_value = now
                with self.assertRaises(maintenance.MaintenanceBusy):
                    self.manager.monitor()
                self.assertEqual(self.manager._next_scan, now + 1)

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
from logos_desktop import constants,maintenance
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
            self.manager.prevent_logos_updates = lambda **kwargs: self.manager._watch_database(
                self.root / "absent/policy.db", ["DELETE FROM policy"],
            )
            with self.assertRaisesRegex(maintenance.MaintenanceError, "protection"):
                self.manager.start()
        launch.assert_not_called()
        self.assertEqual(self.manager._runs, {})
        self.assertEqual(self.manager._watchers, {})

    def policy_root(self, name="Logos"):
        return self.context.prefix / "drive_c/users/fixture/AppData/Local" / name

    def policy_files(self, root, identity="fixture-identity"):
        return (root / "Data" / identity / "UpdateManager/Updates.db",
                root / "Documents" / identity / "LocalUserPreferences/PreferencesManager.db")

    def create_updates(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE Updates (UpdateId INTEGER, Source TEXT);
                CREATE TABLE UpdateUrls (UpdateId INTEGER);
                CREATE TABLE Resources (ResourceId INTEGER, Status INTEGER, UpdateId INTEGER);
                INSERT INTO Updates VALUES (1, 'Application Update'), (2, 'Resource Update');
                INSERT INTO UpdateUrls VALUES (1), (2);
                INSERT INTO Resources VALUES (1, 7, 1), (2, 8, 2);
            """)

    def create_preferences(self, path, *, row=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE Preferences (Type TEXT, Data TEXT)")
            connection.execute("INSERT INTO Preferences VALUES ('AppLocalPreferences', 'unchanged')")
            if row:
                connection.execute("INSERT INTO Preferences VALUES ('UpdateManagerPreferences', 'OptIn=true')")

    def pending_policy_run(self, name="Logos"):
        root = self.policy_root(name)
        self.app.conf._logos_appdata_dir = str(root)
        with patch.object(self.manager, "start_monitoring"):
            child = self.launch()
        run = self.manager._runs["logos"]
        self.assertTrue(run.policies.pending)
        self.assertIn("pending", self.manager.enforcement_error)
        self.assertEqual(self.manager._watchers, {})
        return root, run, child

    def wait_for_policies(self, run):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.tick()
            if not run.policies.pending and not self.manager.enforcement_error:
                return
            time.sleep(0.05)
        self.fail("Both real database policies did not become ready")

    def assert_policy_rows(self, paths, *, enforced):
        with sqlite3.connect(paths[0]) as connection:
            count = connection.execute("SELECT COUNT(*) FROM Updates WHERE Source='Application Update'").fetchone()[0]
            self.assertEqual(count, 0 if enforced else 1)
            self.assertEqual(connection.execute("SELECT * FROM Resources WHERE ResourceId=2").fetchone(), (2, 8, 2))
        with sqlite3.connect(paths[1]) as connection:
            data = connection.execute(
                "SELECT Data FROM Preferences WHERE Type='UpdateManagerPreferences'",
            ).fetchone()[0]
            self.assertIn('OptIn="false"' if enforced else "OptIn=true", data)
            other = connection.execute("SELECT Data FROM Preferences WHERE Type='AppLocalPreferences'").fetchone()
            self.assertEqual(other, ("unchanged",))

    def test_known_identity_both_policies_commit_before_first_spawn(self):
        root = self.policy_root()
        paths = self.policy_files(root)
        self.app.conf._logos_appdata_dir = str(root)
        self.create_updates(paths[0])
        self.create_preferences(paths[1])

        def spawn(*args, **kwargs):
            self.assert_policy_rows(paths, enforced=True)
            self.assertTrue(all(health.ready.is_set() and not health.error
                                for health in self.manager._watcher_health.values()))
            return self.child(kwargs["exe"])

        with (patch.object(self.manager, "start_monitoring"),
              patch.object(wine, "run_wine_application", side_effect=spawn) as launch):
            run = self.manager.start()
        launch.assert_called_once()
        self.assertFalse(run.policies.pending)
        self.assertEqual(len(self.manager._watchers), 2)
        self.assertEqual(self.manager.enforcement_error, "")

    def test_known_identity_missing_second_policy_cancels_spawn_and_joins_first(self):
        root = self.policy_root()
        paths = self.policy_files(root)
        self.app.conf._logos_appdata_dir = str(root)
        self.create_updates(paths[0])
        with (patch.object(self.manager, "start_monitoring"),
              patch.object(wine, "run_wine_application") as launch):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "protection"):
                self.manager.start()
        launch.assert_not_called()
        self.assertEqual(self.manager._runs, {})
        self.assertEqual(self.manager._watchers, {})
        self.assertEqual(self.manager._watcher_health, {})
        self.assertFalse(paths[1].parent.exists())

    def test_late_identity_attaches_before_and_after_one_login_help_notice(self):
        for elapsed in (10, 106):
            with self.subTest(elapsed=elapsed):
                root, run, child = self.pending_policy_run(f"case-{elapsed}")
                run.started = time.monotonic() - elapsed
                self.app.status.reset_mock()
                self.tick()
                self.tick()
                notices = [call for call in self.app.status.call_args_list if "Trouble signing in?" in str(call)]
                self.assertEqual(len(notices), int(elapsed > 105))
                if elapsed > 105:
                    with patch.object(self.manager, "start_monitoring"):
                        index = self.launch(index=True)
                    self.tick()
                    notices = [call for call in self.app.status.call_args_list if "Trouble signing in?" in str(call)]
                    self.assertEqual(len(notices), 1)
                    self.manager.stop_indexing()
                    index.wait(3)
                    self.assertIsNone(child.poll())
                self.assertFalse((root / "Data").exists())
                paths = self.policy_files(root)
                self.create_updates(paths[0])
                self.create_preferences(paths[1])
                self.wait_for_policies(run)
                self.assertTrue(self.manager._login_checked)
                self.assert_policy_rows(paths, enforced=True)
                handles = dict(self.manager._watchers)
                for _ in range(3):
                    self.tick()
                self.assertEqual(handles, self.manager._watchers)
                self.assertEqual(len(handles), 2)
                self.manager.stop()
                child.wait(3)
                self.assertEqual(self.manager._watchers, {})

    def test_late_parent_file_schema_and_preference_row_are_never_fabricated(self):
        root, run, child = self.pending_policy_run()
        paths = self.policy_files(root)
        (root / "Data/fixture-identity").mkdir(parents=True)
        before = time.monotonic()
        self.tick()
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertFalse(paths[0].parent.exists())
        self.assertFalse(paths[1].parent.exists())
        paths[0].parent.mkdir()
        self.tick()
        self.assertFalse(paths[0].exists())
        sqlite3.connect(paths[0]).close()
        before = time.monotonic()
        self.tick()
        self.assertLess(time.monotonic() - before, 0.5)
        handle = self.manager._watchers[paths[0].resolve()]
        time.sleep(3.2)
        self.tick()
        self.assertIsNone(child.poll())
        self.assertTrue(run.policies.pending)
        self.assertFalse(self.manager._watcher_health[paths[0].resolve()].ready.is_set())
        self.assertTrue(self.manager.enforcement_error)
        self.assertNotIn("fixture-identity", self.manager.enforcement_error)
        self.assertEqual(handle, self.manager._watchers[paths[0].resolve()])
        with sqlite3.connect(paths[0]) as connection:
            tables = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
            self.assertEqual(tables, (0,))
        self.create_updates(paths[0])
        self.assertTrue(self.manager._watcher_health[paths[0].resolve()].ready.wait(3))
        self.tick()
        self.assertTrue(run.policies.pending)
        self.assertEqual(len(self.manager._watchers), 1)
        self.assertFalse(paths[1].exists())
        self.create_preferences(paths[1], row=False)
        self.wait_for_policies(run)
        with sqlite3.connect(paths[1]) as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM Preferences WHERE Type='UpdateManagerPreferences'",
            ).fetchone()
            self.assertEqual(rows, (0,))
            connection.execute("INSERT INTO Preferences VALUES ('UpdateManagerPreferences', 'OptIn=true')")
        with sqlite3.connect(paths[0]) as connection:
            connection.execute("INSERT INTO Updates VALUES (3, 'Application Update')")
            connection.execute("INSERT INTO UpdateUrls VALUES (3)")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with sqlite3.connect(paths[1]) as connection:
                data = connection.execute(
                    "SELECT Data FROM Preferences WHERE Type='UpdateManagerPreferences'",
                ).fetchone()[0]
            with sqlite3.connect(paths[0]) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM Updates WHERE Source='Application Update'",
                ).fetchone()[0]
            if 'OptIn="false"' in data and count == 0:
                break
            time.sleep(0.05)
        self.assert_policy_rows(paths, enforced=True)

    def test_external_discovery_and_index_only_runs_do_not_attach_policies(self):
        root = self.policy_root()
        paths = self.policy_files(root)
        self.app.conf._logos_appdata_dir = str(root)
        self.create_updates(paths[0])
        self.create_preferences(paths[1])
        external = self.child(self.app.conf.logos_system_exe_windows_path)
        with patch.object(self.manager, "_policy_target", side_effect=AssertionError("No owned Logos run")):
            self.tick()
            with patch.object(self.manager, "start_monitoring"):
                index = self.launch(index=True)
            self.tick()
        self.assertEqual(self.manager._watchers, {})
        self.assertIsNone(self.manager._runs["index"].policies)
        self.assert_policy_rows(paths, enforced=False)
        self.assertIsNone(external.poll())
        self.assertIsNone(index.poll())

    def test_ambiguous_late_identity_stays_visible_without_selecting_or_writing(self):
        root, run, _child = self.pending_policy_run()
        for identity in ("first", "second"):
            paths = self.policy_files(root, identity)
            self.create_updates(paths[0])
            self.create_preferences(paths[1])
        self.tick()
        self.assertTrue(run.policies.pending)
        self.assertTrue(self.manager.enforcement_error)
        self.assertEqual(self.manager._watchers, {})
        for identity in ("first", "second"):
            self.assert_policy_rows(self.policy_files(root, identity), enforced=False)

    def test_identity_change_retires_old_watchers_before_binding_new_paths(self):
        root, run, _child = self.pending_policy_run()
        old_paths = self.policy_files(root)
        self.create_updates(old_paths[0])
        self.create_preferences(old_paths[1])
        self.wait_for_policies(run)
        old_handles = list(self.manager._watchers.values())
        retired = root / "retired-data"
        (root / "Data/fixture-identity").rename(retired)
        new_paths = self.policy_files(root, "new-identity")
        self.create_updates(new_paths[0])
        self.create_preferences(new_paths[1])
        self.wait_for_policies(run)
        self.assertEqual(set(self.manager._watchers), {path.resolve() for path in new_paths})
        self.assertTrue(all(not thread.is_alive() for _stop, thread in old_handles))
        with sqlite3.connect(retired / "UpdateManager/Updates.db") as connection:
            connection.execute("INSERT INTO Updates VALUES (9, 'Application Update')")
            connection.execute("INSERT INTO UpdateUrls VALUES (9)")
        with sqlite3.connect(old_paths[1]) as connection:
            connection.execute("UPDATE Preferences SET Data='OptIn=true' WHERE Type='UpdateManagerPreferences'")
        time.sleep(0.4)
        with sqlite3.connect(retired / "UpdateManager/Updates.db") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM Updates WHERE UpdateId=9").fetchone(), (1,))
        with sqlite3.connect(old_paths[1]) as connection:
            preference = connection.execute(
                "SELECT Data FROM Preferences WHERE Type='UpdateManagerPreferences'",
            ).fetchone()
            self.assertEqual(preference, ("OptIn=true",))
        self.assert_policy_rows(new_paths, enforced=True)

    def test_late_worker_failure_is_visible_and_replaced_without_duplicate_handles(self):
        root, run, child = self.pending_policy_run()
        paths = self.policy_files(root)
        self.create_updates(paths[0])
        self.create_preferences(paths[1])
        failed, recover = threading.Event(), threading.Event()
        original = logos.database.watch_db

        def watch(path, sql, stop, health):
            if Path(path) == paths[0].resolve() and not recover.is_set():
                health.error = "Database protection worker stopped"
                health.ready.set()
                failed.set()
                return
            original(path, sql, stop, health)

        with patch.object(logos.database, "watch_db", side_effect=watch):
            self.tick()
            self.assertTrue(failed.wait(2))
            self.tick()
            self.assertTrue(run.policies.pending)
            self.assertTrue(self.manager.enforcement_error)
            self.assertIsNone(child.poll())
            self.assertEqual(len(self.manager._watchers), 2)
            recover.set()
            self.wait_for_policies(run)
        self.assertEqual(len(self.manager._watchers), 2)
        self.assertTrue(all(thread.is_alive() for _stop, thread in self.manager._watchers.values()))
        self.assert_policy_rows(paths, enforced=True)

    def redirect_policy_files(self, root):
        old = self.policy_files(root)
        alternative = (root / "alternate/Updates.db", root / "alternate/PreferencesManager.db")
        retired = (root / "retired-updates.db", root / "retired-preferences.db")
        self.create_updates(alternative[0])
        self.create_preferences(alternative[1])
        for source, destination, replacement in zip(old, retired, alternative, strict=True):
            source.rename(destination)
            source.symlink_to(replacement)
        return alternative, retired

    def test_resolved_path_change_with_same_identity_replaces_both_bindings(self):
        root, run, _child = self.pending_policy_run()
        paths = self.policy_files(root)
        self.create_updates(paths[0])
        self.create_preferences(paths[1])
        self.wait_for_policies(run)
        old_handles = list(self.manager._watchers.values())
        alternative, _retired = self.redirect_policy_files(root)
        self.wait_for_policies(run)
        self.assertEqual(run.policies.paths, tuple(path.resolve() for path in alternative))
        self.assertEqual(set(self.manager._watchers), set(run.policies.paths))
        self.assertTrue(all(not thread.is_alive() for _stop, thread in old_handles))
        self.assert_policy_rows(alternative, enforced=True)

    def test_ready_workers_with_conflicting_sql_are_not_reported_as_protected(self):
        root, run, child = self.pending_policy_run()
        paths = self.policy_files(root)
        self.create_updates(paths[0])
        self.create_preferences(paths[1])
        self.wait_for_policies(run)
        self.manager.stop_watchers()
        self.manager.prevent_logos_updates(target=run.policies.target)
        self.manager.set_auto_updates(True, target=run.policies.target)
        self.tick()
        self.assertTrue(run.policies.pending)
        self.assertTrue(self.manager.enforcement_error)
        self.assertIsNone(child.poll())
        self.manager.stop_watchers()
        self.wait_for_policies(run)
        self.assert_policy_rows(paths, enforced=True)

    @contextmanager
    def paused_policy_attachment(self, *, pause_paths=False):
        root, run, child = self.pending_policy_run()
        paths = self.policy_files(root)
        self.create_updates(paths[0])
        self.create_preferences(paths[1])
        entered, release = threading.Event(), threading.Event()
        method = "_policy_paths" if pause_paths else "_policy_target"
        original = getattr(self.manager, method)
        failures = []
        paused = [False]

        def target(value):
            result = original(value)
            if threading.current_thread() is worker and not paused[0]:
                paused[0] = True
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Fixture policy barrier timed out")
            return result

        def observe():
            try:
                self.tick()
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=observe)
        with patch.object(self.manager, method, side_effect=target):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.manager._transition.acquire(blocking=False))
                self.manager._transition.release()
                yield root, run, child
            finally:
                release.set()
                worker.join(4)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])

    def test_stop_invalidates_a_scanned_late_attachment(self):
        with self.paused_policy_attachment() as (root, _run, child):
            self.manager.stop()
            child.wait(3)
        self.assertEqual(self.manager._watchers, {})
        self.assert_policy_rows(self.policy_files(root), enforced=False)

    def test_close_invalidates_late_attachment_without_stopping_application(self):
        with self.paused_policy_attachment() as (root, _run, child):
            self.assertTrue(self.manager.close(timeout=0.5))
            self.assertIsNone(child.poll())
        self.assertEqual(self.manager._watchers, {})
        self.assert_policy_rows(self.policy_files(root), enforced=False)

    def test_close_request_invalidates_late_attachment_before_manager_close(self):
        with self.paused_policy_attachment() as (root, _run, child):
            self.app.request_close()
            self.assertIsNone(child.poll())
        self.assertEqual(self.manager._watchers, {})
        self.assert_policy_rows(self.policy_files(root), enforced=False)

    def test_natural_exit_prevents_new_policy_registration_during_settlement(self):
        with self.paused_policy_attachment() as (root, run, child):
            child.terminate()
            child.wait(3)
            self.tick()
            self.assertIsNotNone(run.empty_since)
        self.assertEqual(self.manager._watchers, {})
        self.assert_policy_rows(self.policy_files(root), enforced=False)

    def test_old_attachment_cannot_clear_a_new_runs_pending_state(self):
        with self.paused_policy_attachment() as (root, old_run, child):
            self.manager.stop()
            child.wait(3)
            other_root, new_run, other = self.pending_policy_run("new-run")
            self.assertIsNot(new_run, old_run)
        self.assertTrue(new_run.policies.pending)
        self.assertEqual(self.manager._watchers, {})
        self.assertFalse((other_root / "Data").exists())
        self.assertIsNone(other.poll())
        self.assert_policy_rows(self.policy_files(root), enforced=False)

    def test_replaced_configuration_invalidates_scanned_policy_work(self):
        with self.paused_policy_attachment() as (root, _run, _child):
            self.app.conf = deepcopy(self.app.conf)
        self.assertEqual(self.manager._watchers, {})
        self.assert_policy_rows(self.policy_files(root), enforced=False)

    def test_context_change_on_same_configuration_invalidates_scanned_policy_work(self):
        with self.paused_policy_attachment() as (root, _run, _child):
            self.app.conf._raw.install_dir = str(self.root / "different-installation")
        self.assertEqual(self.manager._watchers, {})
        self.assertTrue(self.manager.enforcement_error)
        self.assert_policy_rows(self.policy_files(root), enforced=False)

    def test_identity_change_during_preflight_does_not_write_the_old_target(self):
        with self.paused_policy_attachment() as (root, run, _child):
            old_paths = self.policy_files(root)
            retired = root / "retired-data"
            (root / "Data/fixture-identity").rename(retired)
            new_paths = self.policy_files(root, "new-identity")
            self.create_updates(new_paths[0])
            self.create_preferences(new_paths[1])
        self.assertEqual(self.manager._watchers, {})
        self.assertTrue(run.policies.pending)
        self.assertFalse(old_paths[0].exists())
        self.assert_policy_rows((retired / "UpdateManager/Updates.db", old_paths[1]), enforced=False)
        self.assert_policy_rows(new_paths, enforced=False)
        self.wait_for_policies(run)
        self.assert_policy_rows(new_paths, enforced=True)

    def test_path_change_after_preflight_cannot_register_against_reselected_paths(self):
        with self.paused_policy_attachment(pause_paths=True) as (root, run, _child):
            alternative, retired = self.redirect_policy_files(root)
        self.assertTrue(run.policies.pending)
        self.assertEqual(self.manager._watchers, {})
        self.assert_policy_rows(alternative, enforced=False)
        self.assert_policy_rows(retired, enforced=False)
        self.wait_for_policies(run)
        self.assert_policy_rows(alternative, enforced=True)

    def test_profile_guard_is_nonblocking_and_transfer_drops_owned_policy_state(self):
        root, _run, child = self.pending_policy_run()
        paths = self.policy_files(root)
        self.create_updates(paths[0])
        self.create_preferences(paths[1])
        with self.app._profile_lock:
            worker = threading.Thread(target=self.tick)
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.manager._watchers, {})
        candidate = deepcopy(self.app.conf)
        candidate._raw.install_dir = str(self.root / "new-selection")
        candidate._overrides.config_path = str(self.root / "new-selection.json")
        with self.app.profile_selection(candidate):
            pass
        self.tick()
        self.assertEqual(self.manager._runs, {})
        self.assertEqual(self.manager._watchers, {})
        self.assertIsNone(child.poll())
        self.assert_policy_rows(paths, enforced=False)

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
