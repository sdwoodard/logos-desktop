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
subprocess.Popen = spawn
class SyntheticDisplay:
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

    def launch(self):
        selected = boundary.Boundary(shutil.which("bwrap"), self.root / "display", ())
        arguments = boundary.command(selected, source=self.source, copied=self.copied, private=self.private,
                                     runner=self.runner, worker=self.adapter, dependencies=self.dependencies,
                                     wine_user=self.wine_user)
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
        self.assertFalse((self.private / "session-ended").exists(), "Session shutdown ran before the app closed")
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
