"""Run the actual worker protocol with synthetic children in real private namespaces."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import unittest
import uuid

from logos_desktop import runtime_boundary as boundary, runtime_trial_worker as worker, runtime_trials as trials
from unittest.mock import patch


SERVER = """
import socket
import time
server = socket.socket(socket.AF_UNIX)
server.bind('/tmp/.X11-unix/X77')
time.sleep(60)
"""

RUNNER = """#!/usr/bin/python3
import ctypes
from pathlib import Path
import subprocess
import sys
import time
if len(sys.argv) > 1 and sys.argv[1] == 'wineboot.exe':
    assert sys.argv[2:] == ['-e', '-s']
    Path('/trial/session-ended').touch()
    raise SystemExit(0)
ctypes.CDLL(None).prctl(15, b'Logos.exe', 0, 0, 0)
Path('/trial/application-started').touch()
while not Path('/trial/shutdown').exists():
    time.sleep(0.05)
subprocess.Popen(['/usr/bin/python3', '-c',
                  'import time\\nfrom pathlib import Path\\n'
                  'while not Path("/trial/session-ended").exists():\\n    time.sleep(0.05)\\n'
                  'time.sleep(0.6)'], close_fds=True)
"""

ADAPTER = """
_popen = subprocess.Popen
def spawn(arguments, **kwargs):
    if arguments[0] == 'Xwayland':
        arguments = ['/usr/bin/python3', '-c', SERVER]
    return _popen(arguments, **kwargs)
class FixtureSurface:
    def __init__(self, log, purpose):
        self.server = spawn(['Xwayland'], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.pids = (self.server.pid,)
    def poll(self):
        return self.server.poll()
    def close_requested(self):
        try:
            Path('/trial/frame-close').unlink()
            return True
        except FileNotFoundError:
            return False
    def close(self):
        self.server.terminate()
        self.server.wait(timeout=5)
PrivateDisplay = FixtureSurface
class SyntheticDisplay:
    def fit_main(self, application_pids):
        pass
    def viewable(self, application_pids):
        return bool(application_pids)
    def close_windows(self, application_pids):
        with Path('/trial/close-requests').open('a') as output:
            output.write('close\\n')
        if not Path('/trial/ignore-close').exists():
            Path('/trial/shutdown').touch()
        return bool(application_pids)
    def close(self):
        pass
Display = SyntheticDisplay
main()
"""


class ProcessEnumerationTests(unittest.TestCase):
    def test_application_exit_during_comm_read_does_not_crash_namespace_owner(self):
        def comm(path, *arguments, **options):
            if path.parent.name == "41111":
                raise ProcessLookupError(3, "process exited during comm read")
            return "Logos.exe\n"

        with patch.object(Path, "read_text", new=comm):
            self.assertEqual(worker.application_pids((41111, 41112), "logos.exe"), {41112})

    def test_descendant_exit_during_stat_read_preserves_other_owned_children(self):
        def process_stat(path, *arguments, **options):
            if path.parent.name == "41111":
                raise ProcessLookupError(3, "process exited during stat read")
            return "41112 (owned child) S " + str(os.getpid()) + " 0 0 0\n"

        with (patch.object(Path, "iterdir", return_value=iter((Path("/proc/41111"), Path("/proc/41112")))),
              patch.object(Path, "read_text", new=process_stat)):
            self.assertEqual(worker.children(), [41112])

    def test_uninspectable_live_process_is_not_treated_as_a_confirmed_exit(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("still-live identity is unavailable")):
            with self.assertRaises(PermissionError):
                worker.application_pids((41111,), "logos.exe")
            with patch.object(Path, "iterdir", return_value=iter((Path("/proc/41111"),))):
                with self.assertRaises(PermissionError):
                    worker.children()

    def test_observed_application_lifetime_survives_comm_change(self):
        fields = ["S", *(["0"] * 19)]
        fields[19] = "17"
        with (patch.object(worker, "application_pids", return_value=set()),
              patch.object(Path, "read_text", return_value="41111 (Renamed.exe) " + " ".join(fields))):
            self.assertEqual(worker.live_applications([41111], "logos.exe", {(41111, 17)}), {41111})

    def test_reused_pid_is_not_the_retained_original_application(self):
        fields = ["S", *(["0"] * 19)]
        fields[19] = "18"
        with (patch.object(worker, "application_pids", return_value=set()),
              patch.object(Path, "read_text", return_value="41111 (helper) " + " ".join(fields))):
            self.assertEqual(worker.live_applications([41111], "logos.exe", {(41111, 17)}), set())

    def test_live_application_identity_denial_does_not_authorize_session_shutdown(self):
        with (patch.object(worker, "application_pids", return_value=set()),
              patch.object(Path, "read_text", side_effect=PermissionError("live identity unavailable"))):
            with self.assertRaises(PermissionError):
                worker.live_applications([41111], "logos.exe", {(41111, 17)})


class WorkerProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise unittest.SkipTest("Bubblewrap is required for namespace worker fixtures")
        lib64 = "usr/lib64" if Path("/usr/lib64/ld-linux-x86-64.so.2").exists() else "usr/lib"
        result = subprocess.run(
            [bwrap, "--unshare-user", "--unshare-net", "--unshare-pid", "--ro-bind", "/usr", "/usr",
             "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib", "--symlink", lib64, "/lib64",
             "--", "/usr/bin/true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, close_fds=True, timeout=10, check=False,
        )
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace")[:512]
            if any(reason in message for reason in ("Operation not permitted", "Permission denied",
                                                    "No permissions to create a namespace")):
                raise unittest.SkipTest("Private user/network namespaces unavailable: " + message)
            raise RuntimeError("Namespace fixture prerequisite failed: " + message)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / "private"
        boundary.prepare_private(self.private)
        self.source = self.root / "source"
        self.source.mkdir()
        self.copied = self.root / "copy"
        self.copied.mkdir()
        self.runner = self.root / "image"
        self.runner.write_text(RUNNER)
        self.runner.chmod(0o500)
        code = Path(worker.__file__).read_text().replace('\nif __name__ == "__main__":\n    main()\n', "\n")
        self.adapter = self.root / "worker.py"
        self.adapter.write_text(code + "\nSERVER = " + repr(SERVER) + "\n" + ADAPTER)
        self.operation_id = str(uuid.uuid4())
        self.dependencies = []
        self.wine_user = None
        trials.write(self.private / "job.json", {"id": self.operation_id, "executable": "C:\\Logos.exe",
                                                "registry": [], "dependencies": []})
        self.display = socket.socket(socket.AF_UNIX)
        self.display.bind(str(self.root / "display"))
        self.addCleanup(self.display.close)

    def launch(self, *, applying=False):
        selected = boundary.Boundary(shutil.which("bwrap"), self.root / "display", ())
        arguments = boundary.command(selected, source=self.source, copied=self.copied, private=self.private,
                                     runner=self.runner, worker=self.adapter, dependencies=self.dependencies,
                                      wine_user=self.wine_user)
        if applying:
            arguments.append("--apply")
        log = (self.root / "log").open("wb")
        self.addCleanup(log.close)
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)

        def settle():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

        self.addCleanup(settle)
        return process

    def wait_phase(self, process, phases):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("Worker exited before acceptance: " + (self.root / "log").read_text())
            try:
                value = json.loads((self.private / "worker-status.json").read_text())
                trials.validate_worker(value, self.operation_id)
                if value["phase"] in phases:
                    return value
            except FileNotFoundError:
                pass
            time.sleep(0.02)
        self.fail("Worker protocol did not reach " + repr(phases))

    def owned_worker_handle(self, process):
        def root_identity(path):
            value = trials.runtime_copies.runtime_filesystem.live_identity(path)
            return dict(value, filesystem_uuid="fixture", filesystem_root=str(path))

        with patch.object(trials.runtime_copies.runtime_filesystem, "identity", side_effect=root_identity):
            live = trials.worker_status(self.private, process, self.source, self.copied,
                                        {"copy": root_identity(self.copied)}, self.operation_id)
        owned = live["host_identity"]
        descriptor = os.pidfd_open(owned["pid"])
        self.assertEqual(trials.identity(owned["pid"]), owned)
        return descriptor

    def test_launch_stop_waits_for_adopted_helper_then_accepts_terminal_identity(self):
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        live = self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        time.sleep(0.15)
        self.assertIsNone(process.poll())
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["identity"], live["identity"])
        self.assertEqual(ended["reason"], "processes-settled")
        self.assertTrue((self.private / "session-ended").exists())
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    @unittest.skipUnless(shutil.which("Xwayland"), "XWayland is required for the unusable-display execution fixture")
    def test_present_but_nonserving_display_refuses_production_preflight_without_wine(self):
        selected = boundary.Boundary(shutil.which("bwrap"), self.root / "display", ())
        arguments = boundary.command(selected, source=self.source, copied=self.copied, private=self.private,
                                     runner=self.runner, worker=Path(worker.__file__), check=True)
        with self.assertRaises(boundary.Unavailable):
            boundary.probe(arguments)
        self.assertFalse((self.private / "application-started").exists())
        self.assertFalse((self.private / "worker-status.json").exists())

    def test_stop_replacing_launch_permission_cancels_instead_of_waiting_forever(self):
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        trials.write(self.private / "request.json", {"action": "stop"})
        value = self.wait_phase(process, ("running", "ended"))
        if value["phase"] == "running":
            trials.write(self.private / "request.json", {"action": "stop"})
            value = self.wait_phase(process, ("ended",))
        self.assertIn(value["reason"], ("cancelled-before-launch", "processes-settled"))
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_cancellation_before_window_readiness_still_settles_session_helpers(self):
        self.adapter.write_text(self.adapter.read_text().replace("return bool(application_pids)", "return False"))
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        deadline = time.monotonic() + 5
        while not (self.private / "application-started").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.private / "application-started").exists())
        trials.write(self.private / "request.json", {"action": "cancel"})
        ended = self.wait_phase(process, ("ended",))
        self.assertTrue((self.private / "session-ended").exists())
        self.assertEqual(ended["reason"], "application-exited-before-readiness")
        self.assertEqual(ended["returncode"], 0)
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_zero_exit_without_application_window_is_not_readiness(self):
        self.runner.chmod(0o700)
        self.runner.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n")
        self.runner.chmod(0o500)
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        value = self.wait_phase(process, ("ended",))
        self.assertEqual(value["reason"], "application-exited-before-readiness")
        self.assertEqual(value["returncode"], 0)
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_ignored_application_close_times_out_without_session_shutdown(self):
        self.adapter.write_text(self.adapter.read_text().replace("Display = SyntheticDisplay\n",
                                                               "Display = SyntheticDisplay\nSHUTDOWN_TIMEOUT = 0.3\n"))
        (self.private / "ignore-close").touch()
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        unknown = self.wait_phase(process, ("unknown",))
        self.assertEqual(unknown["reason"], "shutdown-timeout")
        self.assertFalse((self.private / "session-ended").exists())
        self.assertFalse((self.private / "shutdown").exists())
        self.assertIsNone(process.poll())
        (self.private / "shutdown").touch()
        ended = self.wait_phase(process, ("ended",))
        self.assertTrue((self.private / "session-ended").exists())
        self.assertEqual(ended["reason"], "shutdown-timeout")
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_delayed_application_close_precedes_normal_helper_settlement(self):
        (self.private / "ignore-close").touch()
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        (self.private / "frame-close").touch()
        time.sleep(0.6)
        self.assertTrue((self.private / "close-requests").exists())
        self.assertFalse((self.private / "session-ended").exists())
        self.assertIsNone(process.poll())
        (self.private / "shutdown").touch()
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "processes-settled")
        self.assertTrue((self.private / "session-ended").exists())
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_renamed_live_application_still_blocks_session_shutdown(self):
        self.runner.chmod(0o700)
        self.runner.write_text(RUNNER.replace(
            "while not Path('/trial/shutdown').exists():\n",
            "while not Path('/trial/shutdown').exists():\n"
            "    if Path('/trial/rename-application').exists():\n"
            "        ctypes.CDLL(None).prctl(15, b'Renamed.exe', 0, 0, 0)\n"
            "        Path('/trial/renamed').touch()\n",
        ))
        self.runner.chmod(0o500)
        (self.private / "ignore-close").touch()
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        (self.private / "rename-application").touch()
        deadline = time.monotonic() + 5
        while not (self.private / "renamed").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.private / "renamed").exists())
        trials.write(self.private / "request.json", {"action": "stop"})
        time.sleep(0.6)
        self.assertFalse((self.private / "session-ended").exists())
        (self.private / "shutdown").touch()
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "processes-settled")
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_surviving_same_name_wrapper_conservatively_blocks_session_shutdown(self):
        self.runner.chmod(0o700)
        self.runner.write_text(RUNNER.replace(
            "subprocess.Popen(['/usr/bin/python3', '-c',",
            "while not Path('/trial/release-wrapper').exists():\n"
            "    time.sleep(0.05)\n"
            "subprocess.Popen(['/usr/bin/python3', '-c',",
        ))
        self.runner.chmod(0o500)
        self.adapter.write_text(self.adapter.read_text().replace("Display = SyntheticDisplay\n",
                                                               "Display = SyntheticDisplay\nSHUTDOWN_TIMEOUT = 0.3\n"))
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        unknown = self.wait_phase(process, ("unknown",))
        self.assertTrue((self.private / "shutdown").exists())
        self.assertEqual(unknown["reason"], "shutdown-timeout")
        self.assertFalse((self.private / "session-ended").exists())
        (self.private / "release-wrapper").touch()
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "shutdown-timeout")
        self.assertTrue((self.private / "session-ended").exists())
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_apply_worker_probes_companions_and_settles_without_launching_product(self):
        self.runner.chmod(0o700)
        self.runner.write_text(RUNNER.replace(
            "if len(sys.argv) > 1 and sys.argv[1] == 'wineboot.exe':",
            "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
            "    print('Wine 11.0' if Path(sys.argv[0]).name == 'wineserver' else 'wine-11.0',\n"
            "          file=sys.stderr if Path(sys.argv[0]).name == 'wineserver' else sys.stdout)\n"
            "    raise SystemExit(0)\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'wineboot.exe':",
        ))
        self.runner.chmod(0o500)
        trials.write(self.private / "job.json", {
            "id": self.operation_id, "registry": [], "dependencies": [],
            "companions": {"wine": "wine-11.0", "wine64": "wine-11.0", "wineserver": "Wine 11.0"},
            "prefix_identity": {"device": self.copied.stat().st_dev, "inode": self.copied.stat().st_ino},
        })
        process = self.launch(applying=True)
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "recipe-settled")
        self.assertTrue((self.private / "session-ended").exists())
        self.assertFalse((self.private / "application-started").exists())
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_apply_worker_refuses_replaced_mount_before_any_runner_execution(self):
        expected = {"device": self.copied.stat().st_dev, "inode": self.copied.stat().st_ino}
        original = self.copied.with_name("preserved-original-copy")
        self.copied.rename(original)
        self.copied.mkdir()
        (self.copied / "valuable-canary").write_text("do not mutate this replacement")
        self.runner.chmod(0o700)
        self.runner.write_text("#!/usr/bin/python3\nfrom pathlib import Path\n"
                               "Path('/trial/runner-invoked').touch()\nraise SystemExit(77)\n")
        self.runner.chmod(0o500)
        trials.write(self.private / "job.json", {
            "id": self.operation_id, "registry": [], "dependencies": [], "prefix_identity": expected,
            "companions": {"wine": "wine-11.0", "wine64": "wine-11.0", "wineserver": "Wine 11.0"},
        })
        process = self.launch(applying=True)
        self.assertNotEqual(process.wait(timeout=5), 0)
        self.assertFalse((self.private / "runner-invoked").exists())
        self.assertFalse((self.private / "worker-status.json").exists())
        self.assertEqual((self.copied / "valuable-canary").read_text(), "do not mutate this replacement")
        self.assertTrue(original.exists())
        self.assertIn("prefix identity changed", (self.root / "log").read_text())

    def test_inspection_restart_keeps_the_same_private_copy_and_owned_namespace(self):
        self.runner.chmod(0o700)
        self.runner.write_text(RUNNER.replace(
            "ctypes.CDLL(None).prctl(15, b'Logos.exe', 0, 0, 0)",
            "Path('/trial/shutdown').unlink(missing_ok=True)\n"
            "Path('/trial/session-ended').unlink(missing_ok=True)\n"
            "counter = Path('/trial/launch-count')\n"
            "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')\n"
            "ctypes.CDLL(None).prctl(15, b'Logos.exe', 0, 0, 0)",
        ))
        self.runner.chmod(0o500)
        job = trials.read(self.private / "job.json")
        job["allow_restart"] = True
        trials.write(self.private / "job.json", job)
        (self.source / "canary.txt").write_text("source unchanged")
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        first = self.wait_phase(process, ("running",))
        (self.copied / "document.txt").write_text("private work survives restart")
        trials.write(self.private / "request.json", {"action": "restart"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = trials.read(self.private / "worker-status.json")
            if value["phase"] == "running" and value["reason"] == "restart-1":
                break
            time.sleep(0.05)
        else:
            self.fail("Private inspection did not restart normally")
        self.assertEqual(value["identity"], first["identity"])
        self.assertEqual((self.private / "launch-count").read_text(), "2")
        self.assertEqual((self.copied / "document.txt").read_text(), "private work survives restart")
        self.assertEqual((self.source / "canary.txt").read_text(), "source unchanged")
        trials.write(self.private / "request.json", {"action": "stop"})
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "processes-settled")
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_ready_application_nonzero_exit_is_not_successful_settlement(self):
        self.runner.chmod(0o700)
        self.runner.write_text(RUNNER + "\nraise SystemExit(23)\n")
        self.runner.chmod(0o500)
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "application-exited-with-error")
        self.assertEqual(ended["returncode"], 23)
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def exercise_live_supervision_failure(self, injection, *, malformed=False, storage=False, stop=False):
        code = self.adapter.read_text()
        self.adapter.write_text(code.removesuffix("main()\n") + injection + "\nmain()\n")
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        running = self.wait_phase(process, ("running",))
        descriptor = self.owned_worker_handle(process)
        self.addCleanup(os.close, descriptor)
        (self.private / "inject-failure").touch()
        if malformed:
            (self.private / "request.json").write_text("{invalid request")
        if stop:
            trials.write(self.private / "request.json", {"action": "stop"})
        if storage:
            time.sleep(0.7)
            self.assertIsNone(process.poll())
            (self.private / "inject-failure").unlink()
        unknown = self.wait_phase(process, ("unknown",))
        self.assertEqual(unknown["reason"], "worker-supervision-failed")
        self.assertEqual(unknown["identity"], running["identity"])
        self.assertIsNone(process.poll())
        if not stop:
            self.assertFalse((self.private / "shutdown").exists())
        if not storage:
            (self.private / "inject-failure").unlink()
        time.sleep(0.3)
        self.assertEqual(trials.read(self.private / "worker-status.json")["phase"], "unknown")
        trials.write(self.private / "request.json", {"action": "stop"})
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "worker-supervision-failed")
        self.assertEqual(ended["identity"], running["identity"])
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_shutdown_helper_spawn_failure_retains_live_namespace_owner(self):
        self.exercise_live_supervision_failure("""
_spawn = subprocess.Popen
def failing_spawn(arguments, **kwargs):
    if 'wineboot.exe' in arguments and Path('/trial/inject-failure').exists():
        raise OSError('synthetic helper spawn failure')
    return _spawn(arguments, **kwargs)
subprocess.Popen = failing_spawn
""", stop=True)

    def test_live_process_inspection_denial_retains_namespace_until_normal_settlement(self):
        self.exercise_live_supervision_failure("""
_children = children
def denied_children():
    if Path('/trial/inject-failure').exists():
        raise PermissionError('synthetic live inspection denial')
    return _children()
children = denied_children
""")

    def test_malformed_request_retains_live_namespace_and_failure_history(self):
        self.exercise_live_supervision_failure("", malformed=True)

    def test_status_storage_failure_retains_owner_and_retries_unknown_publication(self):
        self.exercise_live_supervision_failure("""
_write = write
def unavailable_write(value):
    if Path('/trial/inject-failure').exists():
        raise OSError('synthetic status storage failure')
    return _write(value)
write = unavailable_write
""", malformed=True, storage=True)

    def test_failed_application_exit_during_restart_cannot_be_erased(self):
        self.runner.chmod(0o700)
        self.runner.write_text(RUNNER.replace(
            "Path('/trial/application-started').touch()",
            "with Path('/trial/launch-count').open('a') as counter:\n"
            "    counter.write('launch\\n')\n"
            "Path('/trial/application-started').touch()",
        ) + "\nraise SystemExit(23)\n")
        self.runner.chmod(0o500)
        job = trials.read(self.private / "job.json")
        job["allow_restart"] = True
        trials.write(self.private / "job.json", job)
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "restart"})
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "application-exited-with-error")
        self.assertEqual(ended["returncode"], 23)
        self.assertEqual((self.private / "launch-count").read_text(), "launch\n")
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_cancelled_startup_cannot_publish_readiness_after_shutdown_timeout(self):
        self.adapter.write_text(self.adapter.read_text().replace(
            "return bool(application_pids)", "return Path('/trial/allow-readiness').exists()", 1
        ).replace("Display = SyntheticDisplay\n", "Display = SyntheticDisplay\nSHUTDOWN_TIMEOUT = 0.3\n"))
        (self.private / "ignore-close").touch()
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        deadline = time.monotonic() + 5
        while not (self.private / "application-started").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.private / "application-started").exists())
        trials.write(self.private / "request.json", {"action": "cancel"})
        self.wait_phase(process, ("unknown",))
        (self.private / "allow-readiness").touch()
        time.sleep(0.6)
        self.assertEqual(json.loads((self.private / "worker-status.json").read_text())["phase"], "unknown")
        (self.private / "shutdown").touch()
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "shutdown-timeout")
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_stop_sends_one_close_per_attempt_and_does_not_reset_on_duplicate_stop(self):
        (self.private / "ignore-close").touch()
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        deadline = time.monotonic() + 3
        while not (self.private / "close-requests").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.private / "close-requests").exists())
        trials.write(self.private / "request.json", {"action": "stop"})
        time.sleep(1.3)
        self.assertEqual((self.private / "close-requests").read_text().splitlines(), ["close"])
        self.assertEqual(json.loads((self.private / "worker-status.json").read_text())["phase"], "running")
        (self.private / "shutdown").touch()
        self.wait_phase(process, ("ended",))
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_namespace_preserves_source_account_in_passwd_and_environment(self):
        self.private = self.root / "account-private"
        self.wine_user = "fixture"
        boundary.prepare_private(self.private, wine_user=self.wine_user)
        trials.write(self.private / "job.json", {"id": self.operation_id, "executable": "C:\\Logos.exe",
                                                "registry": [], "dependencies": []})
        self.runner.chmod(0o700)
        account_checks = ("import os\nimport pwd\n"
                          "assert pwd.getpwuid(os.getuid()).pw_name == 'fixture'\n"
                          "assert os.environ['USER'] == os.environ['LOGNAME'] == 'fixture'\n")
        self.runner.write_text(RUNNER.replace("import ctypes\n", "import ctypes\n" + account_checks))
        self.runner.chmod(0o500)
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        self.wait_phase(process, ("ended",))
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_local_resource_is_passed_as_one_argument_to_owned_launcher(self):
        reference = "logosres:kjv1900;ref=BibleKJV.Ge1.1"
        trials.write(self.private / "job.json", {"id": self.operation_id, "executable": "C:\\Logos.exe",
                                                "registry": [], "dependencies": [], "resource_uri": reference})
        self.runner.chmod(0o700)
        check = ("import sys\nif len(sys.argv) > 1 and sys.argv[1] != 'wineboot.exe':\n"
                 "    assert sys.argv[1:] == ['C:\\\\Logos.exe', " + repr(reference) + "]\n")
        self.runner.write_text(RUNNER.replace("import ctypes\n", "import ctypes\n" + check))
        self.runner.chmod(0o500)
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        self.wait_phase(process, ("ended",))
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_canonical_icu_members_are_prepared_in_copy_without_touching_source(self):
        content = b"synthetic pinned ICU"
        archive = self.root / "icu.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            member = tarfile.TarInfo("./windows/system32/fixture.dll")
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
        self.dependencies = [{"role": "icu", "path": str(archive),
                              "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                              "files": [["system32/fixture.dll", hashlib.sha256(content).hexdigest()]]}]
        trials.write(self.private / "job.json", {"id": self.operation_id, "executable": "C:\\Logos.exe",
                                                "registry": [], "dependencies": self.dependencies})
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        self.assertEqual((self.copied / "drive_c/windows/system32/fixture.dll").read_bytes(), content)
        self.assertFalse((self.source / "drive_c").exists())
        trials.write(self.private / "request.json", {"action": "cancel"})
        self.wait_phase(process, ("ended",))
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_duplicate_stop_keeps_timeout_and_failure_history_after_later_settlement(self):
        self.adapter.write_text(self.adapter.read_text().replace("Display = SyntheticDisplay\n",
                                                               "Display = SyntheticDisplay\nSHUTDOWN_TIMEOUT = 0.3\n"))
        (self.private / "ignore-close").touch()
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        deadline = time.monotonic() + 2
        timed_out = False
        while time.monotonic() < deadline:
            trials.write(self.private / "request.json", {"action": "stop"})
            value = json.loads((self.private / "worker-status.json").read_text())
            if value["phase"] == "unknown":
                self.assertEqual(value["reason"], "shutdown-timeout")
                timed_out = True
                break
            time.sleep(0.03)
        self.assertTrue(timed_out, "Duplicate stop requests restarted the active shutdown deadline")
        self.assertFalse((self.private / "session-ended").exists(), "Session shutdown raced the live application")
        (self.private / "ignore-close").unlink()
        trials.write(self.private / "request.json", {"action": "stop"})
        ended = self.wait_phase(process, ("ended",))
        self.assertEqual(ended["reason"], "shutdown-timeout")
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_simultaneous_display_and_zero_application_exit_records_display_failure(self):
        coexit_server = SERVER.replace("time.sleep(60)",
                                      "from pathlib import Path\n"
                                      "while not Path('/trial/finish-together').exists():\n    time.sleep(0.01)")
        self.adapter.write_text(self.adapter.read_text().replace("SERVER = " + repr(SERVER),
                                                               "SERVER = " + repr(coexit_server)))
        self.runner.chmod(0o700)
        source = RUNNER.replace("/trial/shutdown", "/trial/finish-together")
        source = source[:source.index("subprocess.Popen(")]
        self.runner.write_text(source)
        self.runner.chmod(0o500)
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))

        descriptor = self.owned_worker_handle(process)
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
            (self.private / "finish-together").touch()
            time.sleep(0.25)
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)
            ended = self.wait_phase(process, ("ended",))
            self.assertEqual(ended["returncode"], 0)
            self.assertEqual(ended["reason"], "display-lost")
        finally:
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)
            os.close(descriptor)
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)

    def test_failed_shutdown_helper_coexit_cannot_become_success(self):
        self.runner.chmod(0o700)
        failing = RUNNER.replace(
            "    Path('/trial/session-ended').touch()\n    raise SystemExit(0)",
            "    Path('/trial/session-helper-started').touch()\n"
            "    while not Path('/trial/coexit-release').exists():\n        time.sleep(0.01)\n"
            "    Path('/trial/session-ended').touch()\n    raise SystemExit(23)",
        )
        self.runner.write_text(failing)
        self.runner.chmod(0o500)
        process = self.launch()
        self.wait_phase(process, ("prepared",))
        trials.write(self.private / "request.json", {"action": "launch"})
        self.wait_phase(process, ("running",))
        trials.write(self.private / "request.json", {"action": "stop"})
        deadline = time.monotonic() + 5
        while not (self.private / "session-helper-started").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.private / "session-helper-started").exists())
        descriptor = self.owned_worker_handle(process)
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
            (self.private / "coexit-release").touch()
            time.sleep(1)
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)
            ended = self.wait_phase(process, ("ended",))
            self.assertEqual(ended["returncode"], 0)
            self.assertEqual(ended["reason"], "normal-session-shutdown-failed")
        finally:
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)
            os.close(descriptor)
        trials.write(self.private / "request.json", {"action": "settle"})
        self.assertEqual(process.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
