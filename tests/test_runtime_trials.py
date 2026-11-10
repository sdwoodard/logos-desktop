"""Trial ownership, persisted status and failure boundaries use disposable state."""

import copy
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from logos_desktop import maintenance, runtime_boundary as boundary, runtime_trials as trials
from logos_desktop import runtime_trial_worker as worker
from logos_desktop import artifacts, constants, runtime_runners, runtime_stacks
from tests.artifact_fixtures import appimage
from tests.test_artifacts import response


class TrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def supervised_fixture(self):
        for name, value in (("STATE_DIR", str(self.root / "state")),
                            ("COORDINATION_DIR", self.root / "coordination")):
            setting = patch.object(constants, name, value)
            setting.start()
            self.addCleanup(setting.stop)

        def filesystem(path):
            info = Path(path).stat()
            return {"canonical_path": str(path), "filesystem_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "filesystem_root": str(path), "inode": info.st_ino, "owner_uid": info.st_uid,
                    "boot_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "device": info.st_dev, "mount_id": 1}

        setting = patch.object(trials.runtime_copies.runtime_filesystem, "identity", side_effect=filesystem)
        setting.start()
        self.addCleanup(setting.stop)
        expansion = patch.object(boundary, "expansion_budget", return_value={"bytes": 1024, "entries": 1})
        expansion.start()
        self.addCleanup(expansion.stop)
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        store = runtime_runners.RunnerStore(self.root / "store", self.root / "runner-coordination")
        content = appimage()
        runner = replace(next(iter(runtime_stacks.bundled().runners.values())),
                         size=len(content), sha256=hashlib.sha256(content).hexdigest())
        with patch.object(artifacts.requests, "get", return_value=response(content)):
            store.download(runner)
        selected = store.inventory()[0]
        recipe = SimpleNamespace(id="fixture-recipe", revision=1, sha256="c" * 64, settings={}, dependencies=())
        stack = SimpleNamespace(recipe_id=recipe.id, runner_id=runner.id)
        catalog = SimpleNamespace(sha256="a" * 64, stacks={"fixture": stack}, recipes={recipe.id: recipe},
                                  runners={runner.id: runner})
        setting = patch.object(runtime_stacks, "bundled", return_value=catalog)
        setting.start()
        self.addCleanup(setting.stop)
        prefix = self.root / "install/data/wine64_bottle"
        prefix.mkdir(parents=True)
        (prefix / "drive_c/users/fixture").mkdir(parents=True)
        (prefix / "database").write_text("source")
        profile = self.root / "profile.json"
        trials.write(profile, {"install_dir": str(self.root / "install"), "faithlife_product": "Logos"})
        operation_id = str(uuid.uuid4())
        directory = self.root / operation_id
        directory.mkdir(mode=0o700)
        trials.write(directory / "capability.json", {"capability": "test-capability"})
        descriptor = os.open(profile, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        info = os.fstat(descriptor)
        job = {"schema": 1, "id": operation_id, "source": str(prefix), "install": str(self.root / "install"),
               "product": "Logos", "runner": str(selected.path), "sha256": selected.sha256,
               "store": str(store.root), "coordination": str(store.coordination), "generation": selected.generation,
               "executable": "C:\\Logos.exe", "profile": trials.profile_snapshot(profile),
               "controller": trials.identity(os.getpid()), "session_fd": descriptor,
               "session_identity": [info.st_dev, info.st_ino], "settings": {}, "wine_user": "fixture",
               "resource_uri": None,
               "accepted": {"catalog": "a" * 64, "stack": "fixture", "recipe": recipe.sha256,
                            "recipe_id": recipe.id, "recipe_revision": recipe.revision,
                            "dependencies": []}}
        trials.write(directory / "job.json", job)
        return directory, prefix, store

    def test_source_drift_keeps_guard_and_durable_runner_pin(self):
        directory, prefix, store = self.supervised_fixture()
        original = trials.runtime_copies.copy

        def mutate(*args, **kwargs):
            def progress(value):
                (prefix / "database").write_text("external source change")
            kwargs["status"] = progress
            return original(*args, **kwargs)

        with (patch.object(boundary, "inspect", return_value=Mock()),
              patch.object(trials.os, "getppid", return_value=os.getpid()),
              patch.object(trials.runtime_copies, "copy", side_effect=mutate)):
            trials.supervise(directory, "test-capability")
        record = trials.status(directory)
        self.assertEqual((record["phase"], record["result"]), ("conflict", "unknown"))
        context = maintenance.Context(prefix, self.root / "install", "Logos")
        self.assertTrue(context.pending.exists())
        self.assertIn("trial", " ".join(store.removal_reasons(store.inventory()[0].sha256)))

    def test_prelaunch_cancel_releases_guard_only_after_unchanged_source_check(self):
        directory, prefix, store = self.supervised_fixture()
        with (patch.object(boundary, "inspect", return_value=Mock()),
              patch.object(trials.os, "getppid", return_value=os.getpid()),
              patch.object(trials.runtime_copies, "copy", side_effect=trials.runtime_copies.Cancelled())):
            trials.supervise(directory, "test-capability")
        record = trials.status(directory)
        self.assertEqual((record["phase"], record["result"]), ("ended", "cancelled"))
        self.assertFalse(maintenance.Context(prefix).pending.exists())
        self.assertEqual((prefix / "database").read_text(), "source")
        self.assertFalse(store._registry()["references"])

    def test_busy_admission_becomes_durable_failure(self):
        directory, prefix, store = self.supervised_fixture()
        with (patch.object(trials.os, "getppid", return_value=os.getpid()),
              patch.object(maintenance, "Lease", side_effect=maintenance.MaintenanceBusy("busy"))):
            with self.assertRaises(maintenance.MaintenanceBusy):
                trials.run(directory, "test-capability")
        self.assertEqual(trials.status(directory)["result"], "failed")
        self.assertFalse(maintenance.Context(prefix).pending.exists())
        self.assertFalse(store._registry()["references"])

    def test_late_manifest_publication_failure_never_accepts_ready_or_starts_wine(self):
        directory, prefix, store = self.supervised_fixture()
        before = trials.runtime_copies.manifest(prefix)
        rename = trials.paths.rename_no_replace
        write = trials.write
        phases = []

        def fail_manifest(source, destination, **kwargs):
            if Path(source).name == "prefix.manifest.partial":
                raise OSError(28, "injected late manifest publication ENOSPC")
            return rename(source, destination, **kwargs)

        def publication(path, value):
            if Path(path).name == "operation.json":
                phases.append(value["phase"])
            return write(path, value)

        with (patch.object(boundary, "inspect", return_value=Mock()),
              patch.object(trials.os, "getppid", return_value=os.getpid()),
              patch.object(trials.paths, "rename_no_replace", side_effect=fail_manifest),
              patch.object(trials, "write", side_effect=publication),
              patch.object(trials.subprocess, "Popen") as launch):
            trials.supervise(directory, "test-capability")
        self.assertTrue((directory / "prefix").is_dir())
        self.assertFalse((directory / "prefix.manifest").exists())
        self.assertEqual(trials.status(directory)["result"], "failed")
        self.assertFalse(set(phases) & {"ready", "launching", "running"})
        launch.assert_not_called()
        self.assertEqual(trials.runtime_copies.manifest(prefix), before)
        self.assertFalse(maintenance.Context(prefix).pending.exists())
        self.assertFalse(store._registry()["references"])

    def supervise_events(self, events, *, lose_controller_after=None):
        directory, prefix, store = self.supervised_fixture()
        original_identity = trials.identity
        original_write = trials.write
        calls = 0
        controller_lost = False
        settled = False
        cancellation_commands = []
        child = Mock(returncode=0)
        child.poll.side_effect = lambda: 0 if settled else None

        def wait(timeout=None):
            nonlocal settled
            settled = True
            return 0

        child.wait.side_effect = wait

        def identity(pid):
            if controller_lost and pid == os.getpid():
                raise ProcessLookupError("Disposed synthetic preparation controller")
            return original_identity(pid)

        def observed(private, child, logical, copied, receipt, operation_id):
            nonlocal calls, controller_lost
            phase, reason = events[calls]
            calls += 1
            with patch.dict(os.environ, {"WINEPREFIX": str(copied)}):
                owned = worker.identity()
            value = {"schema": 1, "operation": operation_id, "phase": phase, "identity": owned,
                     "reason": reason, "returncode": 0 if phase == "ended" else None}
            original_write(private / "worker-status.json", value)
            result = dict(value, host_identity=original_identity(os.getpid()))
            if calls == lose_controller_after:
                controller_lost = True
            return result

        def write(path, value):
            if value == {"action": "cancel"}:
                cancellation_commands.append(value)
            return original_write(path, value)

        with (patch.object(boundary, "inspect", return_value=Mock()),
              patch.object(trials.os, "getppid", return_value=os.getpid()),
              patch.object(boundary, "command", return_value=["synthetic-owned-worker"]),
              patch.object(boundary, "probe"), patch.object(trials.subprocess, "Popen", return_value=child),
              patch.object(trials, "worker_status", side_effect=observed),
              patch.object(trials, "identity", side_effect=identity), patch.object(trials, "write", side_effect=write)):
            trials.supervise(directory, "test-capability")
        self.assertFalse(maintenance.Context(prefix).pending.exists())
        self.assertFalse(store._registry()["references"])
        return trials.status(directory), cancellation_commands

    def test_controller_loss_is_latched_and_settled_timeout_cannot_become_success(self):
        result, commands = self.supervise_events([
            ("preparing", ""), ("prepared", ""), ("prepared", ""),
            ("unknown", "shutdown-timeout"), ("ended", "processes-settled"),
        ], lose_controller_after=3)
        self.assertEqual(len(commands), 1)
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["failure_reason"], "shutdown-timeout")

    def test_cancelled_launch_racing_running_acknowledgment_stays_cancelled(self):
        result, commands = self.supervise_events([
            ("preparing", ""), ("prepared", ""), ("prepared", ""), ("running", ""),
            ("ended", "processes-settled"),
        ], lose_controller_after=3)
        self.assertEqual(len(commands), 1)
        self.assertFalse(result["running_acknowledged"])
        self.assertEqual(result["result"], "cancelled")

    def test_display_failure_followed_by_zero_exit_stays_failed(self):
        result, _ = self.supervise_events([
            ("preparing", ""), ("prepared", ""), ("running", ""),
            ("unknown", "display-lost"), ("ended", "processes-settled"),
        ])
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["failure_reason"], "display-lost")

    def test_worker_descendant_inventory_excludes_namespace_infrastructure(self):
        child = subprocess.Popen([sys.executable, "-c", "import time\ntime.sleep(30)"], close_fds=True)
        try:
            self.assertIn(child.pid, worker.children())
            self.assertNotIn(os.getppid(), worker.children())
            self.assertNotIn(1, worker.children())
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_delegated_quiescence_excludes_only_authenticated_controller(self):
        child = subprocess.Popen([sys.executable, "-c", "import time\ntime.sleep(30)"], close_fds=True)
        try:
            controller = trials.identity(child.pid)
            process = Mock(pid=child.pid)
            process.info = {"name": "logos-desktop", "cmdline": ["logos-desktop"], "uids": None}
            process.environ.return_value = {}
            process.is_running.return_value = True
            context = maintenance.Context(self.root / "prefix")
            with patch.object(maintenance.psutil, "process_iter", return_value=[process]):
                with self.assertRaises(maintenance.MaintenanceBusy):
                    maintenance.assert_quiescent(context)
                maintenance.assert_quiescent(context, controller=controller)
                stale = dict(controller, start_ticks=controller["start_ticks"] + 1)
                with self.assertRaises(maintenance.MaintenanceBusy):
                    maintenance.assert_quiescent(context, controller=stale)
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_worker_terminal_validator_refuses_bool_and_unknown_identity(self):
        prefix = self.root / "prefix"
        prefix.mkdir()
        with patch.dict(os.environ, {"WINEPREFIX": str(prefix)}):
            identity = worker.identity()
        operation_id = str(uuid.uuid4())
        value = {"schema": 1, "operation": operation_id, "phase": "ended", "identity": identity,
                 "reason": "processes-settled", "returncode": False}
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.validate_worker(value, operation_id)
        value["returncode"] = 0
        trials.validate_worker(value, operation_id)
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.validate_worker(value, str(uuid.uuid4()))

    def test_namespace_identity_is_not_a_prefix_string(self):
        prefix = self.root / "prefix"
        prefix.mkdir()
        with patch.dict(os.environ, {"WINEPREFIX": str(prefix)}):
            identity = worker.identity()
        self.assertEqual(identity["prefix_inode"], prefix.stat().st_ino)
        self.assertEqual(identity["start_ticks"], trials.identity(os.getpid())["start_ticks"])
        self.assertEqual(identity["namespaces"], trials.identity(os.getpid())["namespaces"])

    def test_live_root_checks_boot_mount_and_inode_without_process_discovery(self):
        filesystem = trials.runtime_copies.runtime_filesystem
        with patch.object(filesystem.subprocess, "Popen", side_effect=AssertionError("Live polling spawned a helper")):
            saved = filesystem.live_identity(self.root)
            self.assertTrue(filesystem.same_live_root(saved, self.root))
            for key in ("device", "inode", "mount_id"):
                with self.subTest(key=key):
                    self.assertFalse(filesystem.same_live_root(dict(saved, **{key: saved[key] + 1}), self.root))
            self.assertFalse(filesystem.same_live_root(dict(saved, boot_id=str(uuid.uuid4())), self.root))

    def test_private_account_preserves_unambiguous_source_identity_and_refuses_ambiguity(self):
        prefix = self.root / "prefix"
        (prefix / "drive_c/users/fixture").mkdir(parents=True)
        (prefix / "drive_c/users/Public").mkdir()
        self.assertEqual(trials.source_account(prefix), "fixture")
        private = self.root / "private"
        boundary.prepare_private(private, wine_user=trials.source_account(prefix))
        self.assertTrue((private / "passwd").read_text().startswith("fixture:x:"))
        (prefix / "drive_c/users/other").mkdir()
        with self.assertRaises(maintenance.MaintenanceError):
            trials.source_account(prefix)

    def test_record_no_follow_permissions_duplicates_and_size(self):
        target = self.root / "record.json"
        trials.write(target, {"id": "first"})
        self.assertEqual(trials.read(target), {"id": "first"})
        link = self.root / "link.json"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            trials.read(link)
        target.write_text('{"id":"first","id":"second"}')
        with self.assertRaises(ValueError):
            trials.read(target)
        target.write_text(" " * (trials.MAX_RECORD + 1))
        with self.assertRaises(ValueError):
            trials.read(target)
        target.write_text("{}")
        target.chmod(0o644)
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.read(target)

    def test_atomic_record_replacement_between_open_and_stat_retries_current_generation(self):
        target = self.root / "record.json"
        trials.write(target, {"revision": 1})
        original = trials.os.fstat
        replaced = False

        def replace_opened_record(descriptor):
            nonlocal replaced
            if not replaced:
                replaced = True
                trials.write(target, {"revision": 2})
            return original(descriptor)

        with patch.object(trials.os, "fstat", side_effect=replace_opened_record):
            self.assertEqual(trials.read(target), {"revision": 2})
        self.assertTrue(replaced)

    def test_dead_supervisor_is_not_reconnected_or_unlocked(self):
        record = {"supervisor": trials.identity(os.getpid())}
        record["supervisor"]["start_ticks"] += 1
        with patch.object(trials, "status", return_value=record):
            with self.assertRaises(maintenance.RecoveryRequired):
                trials.request(self.root, "status")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_close_uses_authoritative_cancel_disposition(self):
        process = Mock()
        task = trials.TrialTask(self.root, process)
        with (patch.object(task, "snapshot", return_value={"phase": "copying"}),
              patch.object(task, "cancel", return_value={"phase": "running", "running_acknowledged": True})):
            task.close()
        process.wait.assert_not_called()
        with (patch.object(task, "snapshot", return_value={"phase": "copying"}),
              patch.object(task, "cancel", return_value={"phase": "copying"})):
            task.close()
        process.wait.assert_called_once_with(timeout=30)

    def test_profile_change_and_shared_profile_are_refused(self):
        prefix = self.root / "install/data/wine64_bottle"
        prefix.mkdir(parents=True)
        context = maintenance.Context(prefix, self.root / "install", "Logos")
        profile = self.root / "config.json"
        profile.write_text('{"install_dir":"' + str(context.install) + '","faithlife_product":"Logos"}')
        snapshot = trials.profile_snapshot(profile)
        trials.verify_profile(snapshot, context)
        profile.write_text("{}")
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.verify_profile(snapshot, context)
        directory = self.root / "coordination"
        directory.mkdir()
        with (patch.object(maintenance, "_operation_directory",
                           side_effect=lambda context: os.open(directory, os.O_RDONLY)),
              patch.object(constants, "COORDINATION_DIR", directory)):
            trials.register_profile(snapshot, context)
            other = dict(snapshot, path=str(self.root / "other.json"))
            trials.write(self.root / "other.json", {"install_dir": str(context.install), "faithlife_product": "Logos"})
            with self.assertRaises(maintenance.MaintenanceError):
                trials.register_profile(other, context)

    def test_missing_isolation_prerequisite_does_not_start_any_process(self):
        with (patch.object(boundary.shutil, "which", return_value=None),
              patch.object(boundary.subprocess, "Popen", side_effect=AssertionError("unprotected execution"))):
            with self.assertRaises(boundary.Unavailable):
                boundary.inspect((self.root,))

    def test_missing_display_and_uninspectable_system_dependency_refuse_before_execution(self):
        environment = {"XDG_RUNTIME_DIR": str(self.root), "WAYLAND_DISPLAY": "display"}
        with (patch.dict(os.environ, environment),
              patch.object(boundary.shutil, "which", side_effect=lambda name: "/usr/bin/" + name),
              patch.object(boundary.subprocess, "Popen", side_effect=AssertionError("Unprotected execution"))):
            with self.assertRaises(boundary.Unavailable) as absent:
                boundary.inspect((self.root / "source",))
            self.assertEqual(absent.exception.reason, "display-unavailable")
            import socket

            with socket.socket(socket.AF_UNIX) as display:
                display.bind(str(self.root / "display"))
                with patch.object(boundary, "validate_host_binds",
                                  side_effect=PermissionError("restricted system tree")):
                    with self.assertRaises(boundary.Unavailable) as unreadable:
                        boundary.inspect((self.root / "source",))
            self.assertEqual(unreadable.exception.reason, "dependency-inspection-unavailable")

    def test_host_dependency_socket_is_refused(self):
        import socket

        dependency = self.root / "dependency"
        dependency.mkdir()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(dependency / "service.sock"))
            with self.assertRaisesRegex(boundary.Unavailable, "IPC"):
                boundary.validate_host_binds((self.root / "source",), [dependency])

    def test_recipe_rejects_unbounded_or_executable_settings(self):
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.registry_plan({"command": "run arbitrary code"})
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.registry_plan({"wpf-software": 1})
        self.assertTrue(trials.registry_plan({"wpf-software": True}))

    def test_local_resource_inspection_refuses_callbacks_and_remote_or_unbounded_arguments(self):
        reference = "logosres:kjv1900;ref=BibleKJV.Ge1.1"
        self.assertEqual(trials.local_resource(reference), reference)
        self.assertIsNone(trials.local_resource(None))
        for value in ("https://example.invalid", "logos4:auth", "logosres:../../secret",
                      reference + ";token=not-allowed", "logosres:" + "x" * 200, False):
            with self.subTest(value=value), self.assertRaises(maintenance.MaintenanceError):
                trials.local_resource(value)

    def test_operation_contradiction_and_immutable_plan_drift(self):
        trials.write(self.root / "job.json", {"schema": 1})
        record = {"schema": 1, "id": str(uuid.uuid4()), "revision": 1, "phase": "running", "result": "succeeded",
                  "reason": "", "supervisor": trials.identity(os.getpid()), "source": str(self.root),
                  "copy": None, "worker": None, "progress": None, "budget": None,
                  "running_acknowledged": False,
                  "cancel_requested": False, "failure_reason": "",
                  "job_sha256": trials.artifacts.regular_hash(self.root / "job.json")}
        trials.write(self.root / "operation.json", record)
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.status(self.root)

        record = copy.deepcopy(record)
        record.update(phase="ended", result="failed")
        trials.write(self.root / "operation.json", record)
        self.assertEqual(trials.status(self.root)["result"], "failed")
        record["result"] = "succeeded"
        trials.write(self.root / "operation.json", record)
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.status(self.root)
        record["result"] = "failed"
        trials.write(self.root / "operation.json", record)
        trials.write(self.root / "job.json", {"schema": True})
        with self.assertRaises(maintenance.RecoveryRequired):
            trials.status(self.root)

    def test_status_reports_lost_owner_without_rewriting_record_or_guard(self):
        trials.write(self.root / "job.json", {"schema": 1})
        owned = trials.identity(os.getpid())
        owned["start_ticks"] += 1
        record = {"schema": 1, "id": str(uuid.uuid4()), "revision": 1, "phase": "copying", "result": "pending",
                  "reason": "", "supervisor": owned, "source": str(self.root / "source"),
                  "copy": None, "worker": None, "progress": None, "budget": None,
                  "running_acknowledged": False, "cancel_requested": False, "failure_reason": "",
                  "job_sha256": trials.artifacts.regular_hash(self.root / "job.json")}
        trials.write(self.root / "operation.json", record)
        trials.write(self.root / "restore.pending", {"operation": record["id"]})
        observed = trials.status(self.root)
        self.assertEqual((observed["phase"], observed["result"]), ("conflict", "unknown"))
        self.assertEqual(trials.read(self.root / "operation.json"), record)
        self.assertEqual(trials.read(self.root / "restore.pending"), {"operation": record["id"]})


if __name__ == "__main__":
    unittest.main()
