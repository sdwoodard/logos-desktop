from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import (
    artifacts, cli, compatibility, constants, installer, maintenance, network, processes, releases, runtime,
    system, utils, wine,
)
from logos_desktop.app import UserExitedFromAsk
from logos_desktop.config import Config, EphemeralConfiguration
from tests.artifact_fixtures import appimage, msi
from tests.test_artifact_consumers import fixture_app


class TestInstallation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = patch.object(constants, "STATE_DIR", str(self.root / "state/app"))
        state.start()
        self.addCleanup(state.stop)
        self.app = fixture_app(self.root)
        self.drive_c = Path(self.app.conf.wine_prefix) / "custom-c"
        self.drive_c.mkdir(parents=True)
        devices = Path(self.app.conf.wine_prefix) / "dosdevices"
        devices.mkdir()
        (devices / "c:").symlink_to(self.drive_c)
        self.app.conf.skip_install_system_dependencies = False
        self.app.superuser_command = "/fixture/elevate 文"
        self.app.approve = Mock(return_value=True)

    def test_dependencies_stop_at_each_failed_step_and_verify_final_packages(self):
        manager = SimpleNamespace(packages="required", incompatible_packages="conflict",
                                  install=["pkg", "install"], remove=["pkg", "remove"])
        expected = [["/fixture/pre", "argument with space"],
                    [self.app.superuser_command, "pkg", "install", "required"],
                    [self.app.superuser_command, "pkg", "remove", "conflict"], ["/fixture/post"]]
        for failure in range(4):
            with self.subTest(failure=failure), ExitStack() as stack:
                stack.enter_context(patch.object(system, "get_package_manager", return_value=manager))
                query = stack.enter_context(patch.object(
                    system, "query_packages", side_effect=[["required"], ["conflict"]],
                ))
                stack.enter_context(patch.object(system, "preinstall_dependencies", return_value=[expected[0]]))
                stack.enter_context(patch.object(system, "postinstall_dependencies", return_value=[expected[-1]]))
                results = [subprocess.CompletedProcess([], 0)] * failure
                results.append(subprocess.CalledProcessError(100, expected[failure], output="intended failure"))
                run = stack.enter_context(patch.object(system, "run_command", side_effect=results))
                self.app.status.reset_mock()
                with self.assertRaisesRegex(maintenance.MaintenanceError, "100"):
                    utils.install_dependencies(self.app)
                self.assertEqual([call.args[0] for call in run.call_args_list], expected[:failure + 1])
                self.assertEqual(query.call_count, 2)
                self.assertNotIn(unittest.mock.call("Installed dependencies.", 100), self.app.status.call_args_list)

        with (patch.object(system, "get_package_manager", return_value=manager),
              patch.object(system, "query_packages", side_effect=[["required"], [], ["required"], []]),
              patch.object(system, "preinstall_dependencies", return_value=[]),
              patch.object(system, "postinstall_dependencies", return_value=[]),
              patch.object(system, "run_command", return_value=subprocess.CompletedProcess([], 0))):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "remain unmet"):
                utils.install_dependencies(self.app)

        with (patch.object(system, "get_package_manager", return_value=manager),
              patch.object(system, "query_packages", side_effect=[["required"], [], [], []]),
              patch.object(system, "preinstall_dependencies", return_value=[]),
              patch.object(system, "postinstall_dependencies", return_value=[]),
              patch.object(system, "run_command", return_value=subprocess.CompletedProcess([], 0))):
            utils.install_dependencies(self.app)
        self.assertEqual(self.app.status.call_args, unittest.mock.call("Installed dependencies.", 100))

    def test_failed_package_query_is_never_an_empty_success_and_cancel_runs_nothing(self):
        manager = SimpleNamespace(query=["pkg", "query"], packages="required", incompatible_packages="")
        for result in (None, subprocess.CompletedProcess([], 3, ""),
                       subprocess.CalledProcessError(2, manager.query), FileNotFoundError("query tool")):
            with self.subTest(result=result), patch.object(system, "run_command") as run:
                if isinstance(result, BaseException):
                    run.side_effect = result
                else:
                    run.return_value = result
                with self.assertRaises(maintenance.MaintenanceError):
                    system.query_packages(manager, ["required"])
        with (patch.object(system, "get_package_manager", return_value=manager),
              patch.object(system, "query_packages", side_effect=[["required"], []]),
              patch.object(system, "run_command") as run):
            self.app.approve.return_value = False
            with self.assertRaises(UserExitedFromAsk):
                utils.install_dependencies(self.app)
        run.assert_not_called()

    def test_missing_elevation_is_a_domain_failure_and_retry_runs_checked_commands(self):
        domain = object.__new__(cli.CLI)
        domain.conf, domain.status, domain.approve = self.app.conf, Mock(), Mock(return_value=True)
        manager = SimpleNamespace(packages="required", incompatible_packages="", install=["pkg", "install"])
        with (patch.object(system, "get_package_manager", return_value=manager),
              patch.object(system, "query_packages", side_effect=[["required"], [], ["required"], [], [], []]),
              patch.object(system, "preinstall_dependencies", return_value=[]),
              patch.object(system, "postinstall_dependencies", return_value=[]),
              patch.object(cli.shutil, "which", return_value=None) as lookup,
              patch.object(system, "run_command", return_value=subprocess.CompletedProcess([], 0)) as run):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "sudo"):
                utils.install_dependencies(domain)
            run.assert_not_called()
            lookup.return_value = "/fixture/sudo"
            utils.install_dependencies(domain)
        run.assert_called_once_with(["sudo", "pkg", "install", "required"])

    def test_unavailable_update_never_reports_cli_success(self):
        self.app.conf._network.product_release.return_value = network.ProductLookup(status="empty")
        self.app.conf.refresh_installed_faithlife_product_release = Mock(return_value="38.0.0.0")
        self.app.exit = Mock(side_effect=AssertionError("Update must not report success"))
        with patch.object(installer, "install") as install:
            with self.assertRaisesRegex(maintenance.MaintenanceError, "No stable product release"):
                cli.CLI.update_installed_app(self.app)
        self.app.conf._network.product_release.assert_called_once_with("Logos", "10", "stable", force=True)
        self.app.conf.refresh_installed_faithlife_product_release.assert_called_once_with()
        self.assertEqual(self.app.conf.faithlife_product_release, "38.0.0.0")
        install.assert_not_called()
        self.app.exit.assert_not_called()

    def test_dependency_failure_stops_actual_installer_chain(self):
        with (patch.object(installer, "ensure_install_dirs"),
              patch.object(system, "install_dependencies",
                           side_effect=maintenance.MaintenanceError("package exit 100")),
              patch.object(wine, "initializeWineBottle") as initialize,
              patch.object(wine, "install_msi") as msi_install):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "100"):
                installer.install(self.app)
        initialize.assert_not_called()
        msi_install.assert_not_called()
        self.assertFalse(any("Complete" in call.args[0] for call in self.app.status.call_args_list))

    def test_prefix_configuration_preserves_renderer_and_remaining_registry_setup(self):
        registry = Path(self.app.conf.wine_prefix) / "user.reg"
        for renderer in (None, "gdi", "gl"):
            contents = "WINE REGISTRY Version 2\n"
            if renderer is not None:
                contents += f'\n[Software\\\\Wine\\\\Direct3D]\n"renderer"="{renderer}"\n'
            registry.write_text(contents)
            before = registry.read_bytes()
            accepted = object()
            scripts = []

            def capture_registry(**kwargs):
                self.assertIs(kwargs["app"], self.app)
                self.assertEqual(kwargs["wine_binary"], self.app.conf.wine64_binary)
                self.assertEqual(kwargs["exe"], "regedit.exe")
                scripts.append(Path(kwargs["exe_args"][0]).read_text())
                return SimpleNamespace(returncode=0)

            with (
                self.subTest(renderer=renderer),
                patch.object(installer, "ensure_wineprefix_init", return_value=accepted) as initialize,
                patch.object(wine, "run_wine_during_install", side_effect=capture_registry) as execute,
                patch.object(wine, "wineserver_wait") as wait,
                patch.object(wine, "set_renderer", wraps=wine.set_renderer) as set_renderer,
                patch.object(wine, "wine_reg_query", side_effect=AssertionError("No renderer query needed")),
            ):
                self.assertIs(installer.ensure_wineprefix_config(self.app), accepted)
            initialize.assert_called_once_with(app=self.app)
            set_renderer.assert_not_called()
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(wait.call_count, 2)
            self.assertIn('"winemenubuilder.exe"=""', scripts[0])
            self.assertIn('"FontSmoothingType"=dword:00000002', scripts[1])
            self.assertTrue(all("Direct3D" not in script and '"renderer"' not in script for script in scripts))
            self.assertEqual(registry.read_bytes(), before)

    def test_prefix_configuration_respects_prerequisite_cancel_and_registry_failures(self):
        for failure in ("initialization", "cancel", "menubuilder", "fonts"):
            error = (UserExitedFromAsk("cancelled") if failure == "cancel"
                     else maintenance.MaintenanceError(f"{failure} refused"))
            self.app.checkpoint.reset_mock(side_effect=True)
            with (
                self.subTest(failure=failure),
                patch.object(installer, "ensure_wineprefix_init", return_value=object()) as initialize,
                patch.object(wine, "disable_winemenubuilder") as disable,
                patch.object(wine, "set_fontsmoothing_to_rgb") as fonts,
                patch.object(wine, "set_renderer") as set_renderer,
            ):
                if failure == "initialization":
                    initialize.side_effect = error
                elif failure == "cancel":
                    self.app.checkpoint.side_effect = error
                elif failure == "menubuilder":
                    disable.side_effect = error
                else:
                    fonts.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    installer.ensure_wineprefix_config(self.app)
                self.assertIs(caught.exception, error)
            set_renderer.assert_not_called()
            if failure in ("initialization", "cancel"):
                disable.assert_not_called()
                fonts.assert_not_called()
            elif failure == "menubuilder":
                fonts.assert_not_called()
            else:
                disable.assert_called_once_with(app=self.app, wine64_binary=self.app.conf.wine64_binary)

    def accepted_msi(self):
        source = self.root / "target.msi"
        source.write_bytes(msi())
        return artifacts.import_local(source, self.root / "msi-store",
                                      artifacts.ArtifactSpec("local:target", "target.msi", "msi"))

    def test_msi_transform_tracks_target_not_installed_version(self):
        accepted = self.accepted_msi()
        cases = [(None, "40.1.0.1", True), ("38.0.0.0", "40.1.0.1", True),
                 ("39.0.0.0", "40.1.0.1", True), ("40.1.0.1", "39.0.0.0", False),
                 ("38.0.0.0", "39.0.0.0", False), ("40.0.0.0", "40.1.0.1", True)]
        for installed, target, transformed in cases:
            self.app.conf.installed_faithlife_product_release = installed
            with (self.subTest(installed=installed, target=target),
                  patch.object(wine, "run_wine_completed_process") as convert,
                  patch.object(wine, "run_wine_during_install", return_value=Mock(returncode=0)) as run):
                wine.install_msi(self.app, accepted, target_release=target)
            arguments = run.call_args.kwargs["exe_args"]
            self.assertEqual(arguments[0], "/i")
            self.assertRegex(arguments[1], r"^\\\\\?\\C:\\LogosDesktopInstall-[0-9a-f]{12}\\installer\.msi$")
            self.assertLess(len(arguments[1]), 80)
            self.assertEqual(any(arg.startswith("TRANSFORMS=") for arg in arguments), transformed)
            convert.assert_not_called()
            self.assertIsNone(run.call_args.kwargs["timeout"])
            self.assertEqual(list(self.drive_c.iterdir()), [])
            accepted.check()

    def test_bad_target_transform_and_conversion_never_start_msi(self):
        accepted = self.accepted_msi()
        with patch.object(wine, "run_wine_during_install") as run:
            with self.assertRaises(maintenance.MaintenanceError):
                wine.install_msi(self.app, accepted, target_release="not-a-release")
            with patch.object(constants, "APP_ASSETS_DIR", self.root):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "transform is missing"):
                    wine.install_msi(self.app, accepted, target_release="40.1.0.1")
            (Path(self.app.conf.wine_prefix) / "dosdevices/c:").unlink()
            with self.assertRaisesRegex(maintenance.MaintenanceError, "C drive"):
                wine.install_msi(self.app, accepted, target_release="40.1.0.1")
        run.assert_not_called()

    def test_staged_msi_preserves_acceptance_and_identical_transform(self):
        accepted = self.accepted_msi()
        transform = self.root / "transform 文 space.mst"
        transform.write_bytes(b"trusted transform fixture")
        original = accepted.path.read_bytes()
        with maintenance.installing(self.app), wine._msi_inputs(self.app, accepted, transform) as inputs:
            staged = inputs.accepted
            self.assertIs(staged.spec, accepted.spec)
            self.assertEqual((staged.sha256, staged.size, staged.evidence),
                             (accepted.sha256, accepted.size, accepted.evidence))
            self.assertNotEqual(staged.path.stat().st_ino, accepted.path.stat().st_ino)
            self.assertEqual(staged.path.read_bytes(), original)
            self.assertEqual(staged.path.with_suffix(".mst").read_bytes(), transform.read_bytes())
            self.assertEqual(staged.path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(staged.path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(staged.check(), staged.path)
            self.assertIn("C:\\LogosDesktopInstall-", inputs.windows_msi)
        self.assertFalse(staged.path.parent.exists())
        self.assertEqual(accepted.check().read_bytes(), original)

    def test_copy_tampering_and_interruption_never_publish_inputs(self):
        accepted = self.accepted_msi()
        original = wine._copy_msi_input

        def tamper(source, directory_fd, name, size, digest, checkpoint):
            original(source, directory_fd, name, size, digest, checkpoint)
            os.chmod(name, 0o600, dir_fd=directory_fd)
            descriptor = os.open(name, os.O_WRONLY, dir_fd=directory_fd)
            with os.fdopen(descriptor, "wb") as output:
                output.write(b"wrong staged bytes")

        def cancel(source, directory_fd, name, size, digest, checkpoint):
            original(source, directory_fd, name, size, digest, checkpoint)
            raise UserExitedFromAsk

        for copier, error in ((tamper, artifacts.ArtifactError), (cancel, UserExitedFromAsk)):
            with self.subTest(copier=copier.__name__), patch.object(wine, "_copy_msi_input", side_effect=copier):
                with self.assertRaises(error), maintenance.installing(self.app):
                    with wine._msi_inputs(self.app, accepted, None):
                        self.fail("Invalid input must not be published")
            self.assertEqual(list(self.drive_c.iterdir()), [])
            accepted.check()

    def test_msi_copy_checks_cancellation_between_finite_chunks(self):
        source = self.root / "copy.bin"
        content = b"chunk data" * (256 * 1024)
        source.write_bytes(content)
        descriptor = os.open(self.drive_c, os.O_RDONLY | os.O_DIRECTORY)
        try:
            def cancel_after_chunk():
                output = self.drive_c / "partial"
                if output.exists() and output.stat().st_size > 0:
                    raise UserExitedFromAsk

            with self.assertRaises(UserExitedFromAsk):
                wine._copy_msi_input(source, descriptor, "partial", len(content),
                                     hashlib.sha256(content).hexdigest(), cancel_after_chunk)
            self.assertLess((self.drive_c / "partial").stat().st_size, len(content))
            self.assertEqual(source.read_bytes(), content)
        finally:
            os.close(descriptor)

    def test_unknown_settlement_keeps_admission_then_propagates_original_error(self):
        observed = Event()
        settle = Event()
        original = OSError("process metadata unavailable")
        failures = []

        def inspect(context):
            if not observed.is_set():
                observed.set()
                raise original
            if not settle.is_set():
                raise maintenance.MaintenanceBusy("owned child still live")

        def wait():
            with maintenance.installing(self.app):
                with patch.object(maintenance, "assert_quiescent", side_effect=inspect):
                    try:
                        wine._wait_for_msi_prefix(maintenance.Context.from_app(self.app))
                    except BaseException as error:
                        failures.append(error)

        thread = Thread(target=wait)
        thread.start()
        try:
            self.assertTrue(observed.wait(5))
            with self.assertRaises(maintenance.MaintenanceBusy):
                maintenance.Lease(maintenance.Context.from_app(self.app))
        finally:
            settle.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [original])

    def test_changed_transform_and_cancel_before_copy_leave_cache_untouched(self):
        accepted = self.accepted_msi()
        transform = self.root / "trusted.mst"
        transform.write_bytes(b"trusted")
        copier = wine._copy_msi_input

        def change_transform(source, directory_fd, name, size, digest, checkpoint):
            copier(source, directory_fd, name, size, digest, checkpoint)
            if name == "installer.mst":
                transform.write_bytes(b"changed")

        with patch.object(wine, "_copy_msi_input", side_effect=change_transform):
            with self.assertRaises(artifacts.ArtifactError), maintenance.installing(self.app):
                with wine._msi_inputs(self.app, accepted, transform):
                    self.fail("Changed trusted source must refuse")
        self.app.checkpoint.side_effect = UserExitedFromAsk
        with self.assertRaises(UserExitedFromAsk), patch.object(wine, "_copy_msi_input") as copy:
            with wine._msi_inputs(self.app, accepted, None):
                self.fail("Canceled staging must refuse")
        copy.assert_not_called()
        self.assertEqual(list(self.drive_c.iterdir()), [])
        accepted.check()

    def test_msi_staging_rejects_escaped_c_and_preexisting_directory_symlink(self):
        accepted = self.accepted_msi()
        drive = Path(self.app.conf.wine_prefix) / "dosdevices/c:"
        drive.unlink()
        drive.symlink_to(self.root)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "inside"), maintenance.installing(self.app):
            with wine._msi_inputs(self.app, accepted, None):
                self.fail("Escaped C mapping must refuse")
        drive.unlink()
        drive.symlink_to(self.drive_c)
        planted = self.drive_c / "LogosDesktopInstall-0123456789ab"
        planted.symlink_to(accepted.path.parent, target_is_directory=True)
        with patch.object(wine.secrets, "token_hex", return_value="0123456789ab"):
            with self.assertRaises(maintenance.MaintenanceError), maintenance.installing(self.app):
                with wine._msi_inputs(self.app, accepted, None):
                    self.fail("Existing directory entry must refuse")
        self.assertTrue(planted.is_symlink())
        accepted.check()

    def test_staging_capacity_and_source_change_refuse_before_execution(self):
        accepted = self.accepted_msi()
        with patch.object(wine.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "available"), maintenance.installing(self.app):
                with wine._msi_inputs(self.app, accepted, None):
                    self.fail("Insufficient capacity must refuse")
        accepted.path.chmod(0o600)
        accepted.path.write_bytes(b"changed original")
        with self.assertRaises(artifacts.ArtifactError), patch.object(wine, "run_wine_during_install") as run:
            wine.install_msi(self.app, accepted, target_release="39.0.0.0")
        run.assert_not_called()
        self.assertEqual(list(self.drive_c.iterdir()), [])

    def test_installer_keeps_inputs_and_admission_until_detached_child_settles(self):
        accepted = self.accepted_msi()
        for outcome in (0, 23, "cancel"):
            with self.subTest(outcome=outcome):
                ready = self.root / "child-ready"
                capture = self.root / "child-hash"
                ready.unlink(missing_ok=True)
                capture.unlink(missing_ok=True)
                binary = self.root / "capture-launcher"
                child = (
                    "import hashlib,time\nfrom pathlib import Path\n"
                    f"Path({str(ready)!r}).write_text('ready')\n"
                    "time.sleep(.5)\n"
                    f"Path({str(capture)!r}).write_text(hashlib.sha256(Path(__import__('sys').argv[1]).read_bytes()).hexdigest())\n"
                )
                binary.write_text(
                    f"#!{sys.executable}\nimport subprocess,sys\nfrom pathlib import Path\n"
                    "assert sys.argv[1:3] == ['msiexec', '/i']\n"
                    f"assert sys.argv[3].startswith({'\\\\?\\C:\\LogosDesktopInstall-'!r})\n"
                    f"source=Path({str(self.drive_c)!r}).joinpath(*sys.argv[3][7:].split('\\\\'))\n"
                    f"subprocess.Popen([sys.executable, '-c', {child!r}, str(source)])\n"
                    f"raise SystemExit({23 if outcome == 23 else 0})\n"
                )
                binary.chmod(0o700)
                self.app.conf.wine64_binary = str(binary)
                environment = dict(os.environ, WINEPREFIX=self.app.conf.wine_prefix)
                failures = []

                def consume():
                    try:
                        wine.install_msi(self.app, accepted, target_release="39.0.0.0")
                    except BaseException as error:
                        failures.append(error)

                def cancel_after_spawn(app, wine_binary, **kwargs):
                    with (self.root / "cancel-launcher.log").open("w+") as output:
                        wine.run_wine_process(app, wine_binary, output, output, exe=kwargs["exe"],
                                              exe_args=kwargs["exe_args"], detach_on_close=False)
                        raise UserExitedFromAsk

                with patch.object(wine, "get_wine_env", return_value=environment), ExitStack() as stack:
                    if outcome == "cancel":
                        stack.enter_context(patch.object(
                            wine, "run_wine_during_install", side_effect=cancel_after_spawn,
                        ))
                    thread = Thread(target=consume)
                    thread.start()
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists(), failures)
                    self.assertTrue(list(self.drive_c.glob("LogosDesktopInstall-*/installer.msi")))
                    with self.assertRaises(maintenance.MaintenanceBusy):
                        maintenance.Lease(maintenance.Context.from_app(self.app))
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                diagnostic = self.root / "cancel-launcher.log"
                self.assertTrue(capture.exists(), diagnostic.read_text() if diagnostic.exists() else failures)
                self.assertEqual(capture.read_text(), accepted.sha256)
                self.assertEqual(list(self.drive_c.iterdir()), [])
                if outcome == 0:
                    self.assertEqual(failures, [])
                else:
                    self.assertEqual(len(failures), 1)
                    expected = UserExitedFromAsk if outcome == "cancel" else maintenance.MaintenanceError
                    self.assertIsInstance(failures[0], expected)
                accepted.check()

    def test_windows_installer_paths_require_successful_absolute_conversion(self):
        for code, output, allowed in ((0, "Z:\\long path 文\\target.msi\n", True),
                                      (0, "\\\\?\\Z:\\long\\target.msi", True),
                                      (1, "Z:\\failed.msi", False), (0, "", False),
                                      (0, "relative.msi", False), (0, "Z:\\first\nZ:\\second", False)):
            with (self.subTest(output=output),
                  patch.object(wine, "run_wine_completed_process",
                               return_value=subprocess.CompletedProcess([], code, output))):
                if allowed:
                    converted = wine.windows_path(self.app, "/fixture/wine", self.root / "target.msi")
                    self.assertTrue(converted.startswith("\\\\?\\Z:\\"))
                else:
                    with self.assertRaisesRegex(maintenance.MaintenanceError, "winepath"):
                        wine.windows_path(self.app, "/fixture/wine", self.root / "target.msi")

    def test_target_and_accepted_msi_stay_bound_when_config_changes_during_download(self):
        conf = self.app.conf
        conf.faithlife_product_release = "40.1.0.1"
        conf._overrides.faithlife_installer_name = None
        conf._overrides.faithlife_installer_download_url = None
        accepted = self.accepted_msi()

        def acquire(url, name, *args, **kwargs):
            self.assertIn("/40.1.0.1/", url)
            self.assertEqual(name, "Logos_v40.1.0.1-x64.msi")
            self.assertEqual(kwargs["msi_expectation"], artifacts.MsiExpectation("Logos", "40.1.0.1"))
            conf.faithlife_product_release = "38.0.0.0"
            return accepted

        with (patch.object(installer, "ensure_winetricks_executable"),
              patch.object(installer.network, "logos_reuse_download", side_effect=acquire)):
            selected = installer.ensure_product_installer_download(self.app)
        self.assertEqual(selected.release, "40.1.0.1")
        self.assertIs(selected.artifact, accepted)

    def test_wine_failure_and_timeout_keep_original_command_and_reap_helper(self):
        binary = self.root / "failure-runtime"
        binary.write_text(f"#!{sys.executable}\nraise SystemExit(23)\n")
        binary.chmod(0o755)
        with patch.object(wine, "get_wine_env", return_value=dict(os.environ)):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "exit code 23.*regedit"):
                wine.run_wine_during_install(self.app, binary, exe="regedit.exe")
            binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
            with self.assertRaisesRegex(maintenance.MaintenanceError, "timed out.*-w"):
                wine.run_wine_during_install(self.app, binary, exe_args=["-w"], timeout=.1)
        with maintenance.installing(self.app):
            pass
        original = subprocess.CalledProcessError(17, [str(binary), "regedit"])
        with patch.object(wine, "run_wine_during_install", side_effect=original):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                wine.wine_reg_install(self.app, "fixture.reg", "REGEDIT4", str(binary))
        self.assertIs(raised.exception, original)

    def test_install_owner_allows_own_commands_but_not_shared_upgrade_or_other_context(self):
        context = maintenance.Context.from_app(self.app)
        with maintenance.installing(self.app):
            with maintenance.use(context):
                pass
            with self.assertRaises(maintenance.MaintenanceBusy):
                maintenance.Lease(context)
            with (self.assertRaises(maintenance.MaintenanceBusy),
                  maintenance.use(maintenance.Context(self.root / "other"))):
                pass
        with maintenance.use(context):
            with self.assertRaises(maintenance.MaintenanceBusy), maintenance.installing(self.app):
                pass

    def test_busy_update_and_switch_do_not_touch_target_or_download(self):
        busy = maintenance.MaintenanceBusy("Close Logos then retry")
        original = self.app.conf.faithlife_product_release
        with (patch.object(maintenance, "assert_quiescent", side_effect=busy),
              patch.object(installer, "install") as install,
              patch.object(artifacts, "import_local") as copy):
            with self.assertRaises(maintenance.MaintenanceBusy):
                utils.update_faithlife_product(self.app)
            with self.assertRaises(maintenance.MaintenanceBusy):
                utils.set_appimage_symlink(self.app, self.root / "not-read.AppImage")
        install.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(self.app.conf.faithlife_product_release, original)

    def test_competing_reservation_after_spawn_does_not_kill_admitted_launch(self):
        binary = self.root / "launch-runtime"
        marker = self.root / "finished"
        binary.write_text(
            f"#!{sys.executable}\nimport time\nfrom pathlib import Path\n"
            f"time.sleep(.2)\nPath({str(marker)!r}).touch()\n"
        )
        binary.chmod(0o755)
        reserved, release = Event(), Event()
        errors, threads = [], []
        original = system.popen_command

        def preparing(context):
            reserved.set()
            self.assertTrue(release.wait(5))

        def compete():
            try:
                with maintenance.installing(self.app):
                    errors.append(AssertionError("Maintenance must refuse an admitted launch"))
            except maintenance.MaintenanceBusy:
                pass
            except BaseException as error:
                errors.append(error)

        def spawned(*args, **kwargs):
            process = original(*args, **kwargs)
            thread = Thread(target=compete)
            threads.append(thread)
            thread.start()
            self.assertTrue(reserved.wait(5))
            return process

        with (patch.object(wine, "get_wine_env", return_value=dict(os.environ)),
              patch.object(maintenance, "assert_quiescent", side_effect=preparing),
              patch.object(system, "popen_command", side_effect=spawned),
              tempfile.TemporaryFile(mode="w+") as output):
            try:
                process = wine.run_wine_process(self.app, binary, output, output)
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
            self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(errors, [])
        self.assertTrue(marker.exists())

    def test_shared_cache_generation_is_copied_to_owned_runtime_store_before_use(self):
        source = self.root / "source.AppImage"
        source.write_bytes(appimage())
        cached = artifacts.import_local(source, self.root / "shared-cache",
                                        artifacts.ArtifactSpec("local:shared", source.name, "appimage"))
        selected = Path(runtime.executable(cached.path, self.app))
        self.assertTrue(selected.is_relative_to(Path(self.app.conf.install_dir) / "data/bin/.artifacts"))
        cached.path.unlink()
        self.assertEqual(selected.read_bytes(), appimage())

    def test_incompatible_runtime_is_not_persisted_or_activated(self):
        source = self.root / "outside/wine.AppImage"
        source.parent.mkdir()
        source.write_bytes(appimage())
        before = [runtime._snapshot(path) for path in sum(runtime._targets(self.app), [])]
        with patch.object(wine, "get_wine_release", return_value=(wine.WineRelease(6, 0, "stable"), "yes")):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "below minimum"):
                utils.set_appimage_symlink(self.app, source)
        self.assertEqual([runtime._snapshot(path) for path in sum(runtime._targets(self.app), [])], before)

    def test_explicit_recommendation_preserves_real_config_on_unavailable_source_and_provider(self):
        selected = Path(self.app.conf._overrides.config_path)
        overrides = EphemeralConfiguration.load_from_path(str(selected))
        with patch.object(Config, "_create_network_requests_with_hook", return_value=Mock()):
            self.app.conf = Config(overrides, self.app)
        conf = self.app.conf
        conf._overrides.runtime_selection = {"wine_binary": "prior explicit override"}
        original = selected.read_bytes(), conf._raw.wine_binary, dict(conf._overrides.runtime_selection)
        for source in (
            releases.WineTargetLookup(status="offline", message="Wine source offline"),
            releases.WineTargetLookup(releases.WineTarget("11.20", "devel", "a" * 40), "ok"),
        ):
            conf._network.wine_release_target.return_value = source
            conf._network.wine_appimage_release.return_value = releases.ReleaseLookup(
                status="unavailable", message="Wine devel 11.20 unavailable from community AppImage provider",
            )
            with patch.object(utils, "current_native_wine", return_value=None):
                with self.assertRaises(maintenance.MaintenanceError):
                    conf.wine_binary = constants.WINE_RECOMMENDED_SIGIL
            self.assertEqual((selected.read_bytes(), conf._raw.wine_binary,
                              conf._overrides.runtime_selection), original)

    def test_explicit_recommendation_can_select_an_already_validated_current_native_provider(self):
        selected = Path(self.app.conf._overrides.config_path)
        overrides = EphemeralConfiguration.load_from_path(str(selected))
        with patch.object(Config, "_create_network_requests_with_hook", return_value=Mock()):
            self.app.conf = Config(overrides, self.app)
        conf = self.app.conf
        candidate = self.root / "external-native/wine"
        candidate.parent.mkdir()
        candidate.write_bytes(b"fixture, version and pair probes mocked")
        candidate.chmod(0o700)
        conf._network.wine_appimage_release.return_value = releases.ReleaseLookup(
            releases.SoftwareReleaseInfo("11.0", "https://example.invalid/wine.AppImage"), "ok",
        )
        with patch.object(utils, "current_native_wine", return_value=str(candidate)):
            conf.wine_binary = constants.WINE_RECOMMENDED_SIGIL
        self.assertEqual(conf.wine_binary, str(candidate))
        self.assertEqual(conf.wine_binary_code, "Custom")
        conf._network.wine_appimage_release.assert_called_once()
        conf._network.wine_appimage_release.reset_mock()
        conf._network.wine_release_target.reset_mock()
        conf._network.wine_appimage_release.side_effect = AssertionError("No current AppImage provider")
        with (patch.object(system, "check_incompatibilities"), patch.object(utils, "install_dependencies"),
              patch.object(installer, "check_system_compatibility")):
            installer.ensure_choices(self.app)
            installer.ensure_appimage_download(self.app)
        conf._network.wine_appimage_release.assert_not_called()
        conf._network.wine_release_target.assert_not_called()

    def prepare_unselected_recommended_config(self):
        selected = Path(self.app.conf._overrides.config_path)
        document = json.loads(selected.read_bytes())
        document.update(wine_binary=None, WINE_EXE=None)
        selected.write_text(json.dumps(document))
        overrides = EphemeralConfiguration.load_from_path(str(selected))
        with patch.object(Config, "_create_network_requests_with_hook", return_value=Mock()):
            self.app.conf = Config(overrides, self.app)
        self.app.ask = Mock(return_value=constants.WINE_RECOMMENDED_SIGIL)
        conf = self.app.conf
        release = network.SoftwareReleaseInfo("11.0", "https://example.invalid/current.AppImage",
                                              name="current.AppImage")
        conf._network.wine_appimage_release.return_value = releases.ReleaseLookup(release, "ok")
        return selected

    def test_recommended_install_prepares_dependencies_before_activation_probe(self):
        self.prepare_unselected_recommended_config()
        source = self.root / "current.AppImage"
        source.write_bytes(appimage())
        accepted = artifacts.import_local(source, Path(self.app.conf.installer_binary_dir),
                                          artifacts.ArtifactSpec("local:fixture", source.name, "appimage"))
        phases = []

        def prerequisites(app):
            self.app.ask.assert_not_called()
            phases.append("prerequisites")

        def probe(app, binary):
            self.assertEqual(phases, ["prerequisites"])
            phases.append("activation probe")

        with (patch.object(system, "check_incompatibilities"),
              patch.object(utils, "install_dependencies", side_effect=prerequisites),
              patch.object(utils, "current_native_wine", return_value=None),
              patch.object(utils, "get_wine_options", return_value=[constants.WINE_RECOMMENDED_SIGIL]),
              patch.object(network, "logos_reuse_download", return_value=accepted),
              patch.object(installer, "check_system_compatibility"),
              patch.object(wine, "require_compatible", side_effect=probe)):
            installer.ensure_appimage_download(self.app)
        self.assertEqual(phases, ["prerequisites", "activation probe"])
        self.assertEqual(Path(self.app.conf.wine_binary).name, "current.AppImage")

    def test_prerequisite_refusal_preserves_pending_recommendation_and_original_selection(self):
        selected = self.prepare_unselected_recommended_config()
        self.app.conf._overrides.runtime_selection = {"wine_binary": "prior effective override"}
        before = selected.read_bytes(), self.app.conf._raw.wine_binary, dict(
            self.app.conf._overrides.runtime_selection,
        )
        with (patch.object(system, "check_incompatibilities"),
              patch.object(utils, "install_dependencies", side_effect=UserExitedFromAsk("prerequisites refused")),
              patch.object(runtime, "activate") as activate):
            with self.assertRaises(UserExitedFromAsk):
                installer.ensure_appimage_download(self.app)
        self.assertEqual((selected.read_bytes(), self.app.conf._raw.wine_binary,
                          self.app.conf._overrides.runtime_selection), before)
        self.app.ask.assert_not_called()
        self.app.conf._network.wine_release_target.assert_not_called()
        activate.assert_not_called()

    def test_two_switches_persist_through_fresh_config_and_matching_consumers(self):
        selected_config = Path(self.app.conf._overrides.config_path)
        old_runtime = Path(self.app.conf.wine_binary)
        custom_prefix = Path(self.app.conf.install_dir) / "data/custom-prefix"
        custom_bin = Path(self.app.conf.install_dir) / "tools 文"
        saved = json.loads(selected_config.read_text())
        saved.update(WINEPREFIX=str(custom_prefix), APPDIR_BINDIR=str(custom_bin),
                     SELECTED_APPIMAGE_FILENAME=str(old_runtime), WINE_EXE=str(old_runtime), WINEBIN_CODE="AppImage",
                     DEBUG="false", SKIP_DEPENDENCIES="true")
        selected_config.write_text(json.dumps(saved))
        overrides = EphemeralConfiguration.load_from_path(str(selected_config))
        conf = object.__new__(Config)
        with patch.object(Config, "_create_network_requests_with_hook", return_value=Mock()):
            Config.__init__(conf, overrides, self.app)
        self.app.conf = conf
        self.app.ask = Mock(side_effect=AssertionError("No interactive fallback"))
        custom = self.root / "Applications 文/custom.AppImage"
        custom.parent.mkdir()
        custom.write_bytes(appimage(b"custom"))
        recommended = self.root / "recommended.AppImage"
        recommended.write_bytes(appimage(b"recommended"))
        candidate = artifacts.import_local(
            recommended, Path(conf.installer_binary_dir),
            artifacts.ArtifactSpec("https://example.invalid/recommended", recommended.name, "appimage"),
        )
        conf.wine64_binary
        conf.wineserver_binary
        with patch.object(wine, "get_wine_release", return_value=(wine.WineRelease(10, 8, "staging"), "yes")):
            utils.set_appimage_symlink(self.app, custom)
            first = conf.wine_binary
            custom.unlink()
            old_runtime.unlink()
            self.assertTrue(Path(first).is_file())
            self.reload_and_check(first)
            conf._write(checked=True)
            self.reload_and_check(first)
            conf._network.wine_appimage_release.return_value = releases.ReleaseLookup(
                releases.SoftwareReleaseInfo("11.0", "https://example.invalid/recommended.AppImage",
                                             name="recommended.AppImage"), "ok",
            )
            with patch.object(utils.network, "download_recommended_appimage", return_value=candidate):
                utils.update_to_latest_recommended_appimage(self.app)
            self.assertNotEqual(first, conf.wine_binary)
            self.reload_and_check(conf.wine_binary)

    def reload_and_check(self, expected):
        program = """
import json,sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock,patch
from logos_desktop import constants,wine
from logos_desktop.config import Config,EphemeralConfiguration
constants.STATE_DIR=sys.argv[2]
app=SimpleNamespace(_config_updated_event=Event())
with patch.object(Config,'_create_network_requests_with_hook',return_value=Mock()):
    app.conf=Config(EphemeralConfiguration.load_from_path(sys.argv[1]),app)
conf=app.conf
conf.faithlife_product_logging=not conf.faithlife_product_logging
command,env=wine._wine_invocation(app,conf.wine_binary,None)
print(json.dumps([conf.wine_binary,conf.wine64_binary,conf.wineserver_binary,
                  command[0],env['WINE'],env['WINESERVER'],
                  conf.wine_prefix,conf.installer_binary_dir,conf._raw._legacy.DEBUG,
                  conf._overrides.install_dependencies_skip]))
"""
        for config_path in runtime._targets(self.app)[1]:
            for restart in range(2):
                output = subprocess.check_output(
                    [sys.executable, "-B", "-c", program, str(config_path), constants.STATE_DIR],
                    text=True, timeout=10,
                )
                values = json.loads(output)
                self.assertEqual(values[:2], [expected, expected])
                self.assertEqual(values[2], str(Path(expected).parent / "wineserver"))
                self.assertEqual(values[3:5], [expected, expected])
                self.assertEqual(values[5], values[2])
                context = [self.app.conf.wine_prefix, self.app.conf.installer_binary_dir, False, True]
                self.assertEqual(values[6:], context)

    def test_malformed_legacy_boolean_has_a_controlled_diagnostic(self):
        path = Path(self.app.conf._overrides.config_path)
        saved = json.loads(path.read_text())
        saved["DEBUG"] = ["false"]
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "Boolean settings"):
            EphemeralConfiguration.load_from_path(str(path))


class TestCLIUpdateEffectiveConfiguration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / "install"
        self.live = self.install / "data/wine64_bottle/drive_c/users/fixture/AppData/Local/Logos"
        (self.live / "System").mkdir(parents=True)
        (self.live / "Logos.exe").write_bytes(b"fixture executable")
        (self.live / "System/Logos.deps.json").write_text(json.dumps({"libraries": {"Logos/38.0.0.0": {}}}))
        self.path = self.root / "selected.json"
        self.path.write_text(json.dumps({
            "install_dir": str(self.install), "faithlife_product": "Logos",
            "faithlife_product_version": "10", "faithlife_product_release": "38.0.0.0",
        }))
        self.original = self.path.read_bytes()
        self.app = SimpleNamespace(_config_updated_event=Event(), status=Mock(),
                                   ask=Mock(side_effect=AssertionError("No prompts")))
        self.requests = Mock()
        self.requests.product_release.return_value = network.ProductLookup(("40.0.0.0",), "ok")
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        state = patch.object(constants, "STATE_DIR", str(self.root / "state/logos-desktop"))
        state.start()
        self.addCleanup(state.stop)

    def load(self, environment=None):
        os.environ.update(environment or {})
        with patch.object(Config, "_create_network_requests_with_hook", return_value=self.requests):
            self.app.conf = Config(EphemeralConfiguration.load_from_path(str(self.path)), self.app)
        self.assertEqual(self.app.conf.refresh_installed_faithlife_product_release(), "38.0.0.0")

    def test_older_environment_target_rejected_before_any_publication(self):
        self.load({"TARGET_RELEASE_VERSION": "37.0.0.0"})
        with patch.object(installer, "install") as install:
            with self.assertRaisesRegex(maintenance.MaintenanceError, "override conflicts"):
                utils.update_faithlife_product(self.app)
        install.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.app._config_updated_event.is_set())
        self.assertFalse((self.install / f"{constants.BINARY_NAME}.json").exists())

    def test_no_source_override_and_matching_target_overlay_use_admitted_release(self):
        for environment in ({}, {"TARGET_RELEASE_VERSION": "40.0.0.0"}):
            with self.subTest(environment=environment):
                self.path.write_bytes(self.original)
                self.load(environment)

                def acquire(app):
                    self.assertTrue(maintenance.installation_owned())
                    self.assertIsNotNone(app.conf.write_validator)
                    self.assertEqual(app.conf.faithlife_product_release, "40.0.0.0")
                    self.assertIsNone(app.conf._overrides.faithlife_installer_download_url)
                    self.assertIsNone(app.conf._overrides.faithlife_installer_name)
                    # Installer-owned publications retain the admitted-target validator.
                    app.conf.faithlife_product_logging = True
                    before = self.path.read_bytes()
                    with self.assertRaisesRegex(maintenance.MaintenanceError, "override conflicts"):
                        app.conf._change(faithlife_product_release_channel="beta")
                    self.assertEqual(self.path.read_bytes(), before)

                with patch.object(installer, "install", side_effect=acquire) as install:
                    utils.update_faithlife_product(self.app)
                install.assert_called_once_with(self.app)
                self.assertEqual(self.app.conf.faithlife_product_release, "40.0.0.0")
                saved_target = "38.0.0.0" if environment else "40.0.0.0"
                self.assertEqual(json.loads(self.path.read_bytes())["faithlife_product_release"], saved_target)
                self.assertIsNone(self.app.conf.write_validator)

    def test_custom_source_overrides_refuse_even_when_target_matches(self):
        for source in ({"LOGOS64_URL": "https://example.invalid/custom.msi"},
                       {"LOGOS_EXECUTABLE": "custom.msi"}):
            with self.subTest(source=source), patch.dict(os.environ, source, clear=True):
                self.load({"TARGET_RELEASE_VERSION": "40.0.0.0"})
                with patch.object(installer, "install") as install:
                    with self.assertRaisesRegex(maintenance.MaintenanceError, "Custom installer sources"):
                        utils.update_faithlife_product(self.app)
                install.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assertFalse(self.app._config_updated_event.is_set())

    def test_current_installed_target_never_publishes_or_installs(self):
        self.load()
        self.requests.product_release.return_value = network.ProductLookup(("38.0.0.0",), "ok")
        with patch.object(installer, "install") as install:
            with self.assertRaisesRegex(maintenance.MaintenanceError, "current"):
                utils.update_faithlife_product(self.app)
        install.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.app._config_updated_event.is_set())


class TestWinePolicy(unittest.TestCase):
    def test_version_policy_boundaries_and_unknowns(self):
        for release, target, allowed in (
            ((1, 0, "stable"), "40.1.0.1", False), ((6, 0, "staging"), "40.1.0.1", False),
            ((7, 17, "staging"), "29.0.0.0", False), ((7, 18, "staging"), "29.0.0.0", True),
            ((8, 0, "staging"), "29.0.0.0", False), ((8, 15, "devel"), "29.0.0.0", False),
            ((8, 16, "devel"), "29.0.0.0", True), ((9, 9, "staging"), "40.1.0.1", False),
            ((9, 10, "staging"), "40.1.0.1", True), ((10, 0, "stable"), "40.1.0.1", True),
            ((10, 8, "staging"), "40.1.0.1", True), ((11, 19, "devel"), "40.1.0.1", True),
            ((11, 20, "devel"), "40.1.0.1", True), ((25, 0, "stable"), "40.1.0.1", True),
            ((12, 0, "proton"), "40.1.0.1", False),
        ):
            with self.subTest(release=release, target=target):
                self.assertEqual(wine.check_wine_rules(wine.WineRelease(*release), target, "10")[0], allowed)
        self.assertFalse(wine.check_wine_rules(None, "40.1.0.1", "10")[0])

    def test_version_parser_rejects_unidentified_variants_and_nonzero_probe(self):
        for output, code, expected in (
            (b"wine-10.8 (Staging)\n", 0, wine.WineRelease(10, 8, "staging")),
            (b"wine-11.19\n", 0, wine.WineRelease(11, 19, "devel")),
            (b"wine-11.20\n", 0, wine.WineRelease(11, 20, "devel")),
            (b"wine-11.0.2\n", 0, wine.WineRelease(11, 0, "stable", (2,))),
            (b"wine-10.0-rc5\n", 0, wine.WineRelease(10, 0, "devel", rc=5)),
            (b"wine-10.0\n", 2, None), (b"wine-10.0 (Proton unknown)\n", 0, None),
            (b"not Wine", 0, None), (b"wine-0.1", 0, None),
        ):
            with (self.subTest(output=output, code=code),
                  patch.object(wine.runtime, "executable", return_value="/fixture/wine"),
                  patch.object(processes, "capture", return_value=(output, code))):
                self.assertEqual(wine.get_wine_release("/fixture/wine")[0], expected)

    def test_channel_flavor_and_full_version_are_independent_facts(self):
        for output, number, channel, flavor in (
            ("wine-11.20", "11.20", "devel", "upstream"),
            ("wine-10.8 (Staging)", "10.8", "devel", "staging"),
            ("wine-11.0.2 (Stable)", "11.0.2", "stable", "upstream"),
            ("wine-11.0-rc2", "11.0rc2", "rc", "upstream"),
            ("wine-11.0-rc2 (Staging)", "11.0rc2", "rc", "staging"),
        ):
            with self.subTest(output=output):
                facts = compatibility.parse_wine_version(output)
                self.assertEqual(str(facts.version), number)
                self.assertEqual(facts.channel, channel)
                self.assertEqual(facts.flavor, flavor)

        for output in ("wine-11.20 (Proton)", "wine-11.20 (vendor)", "wine-11.20-1",
                       "wine-11.20\nextra", "wine-11.20 (stable)", "wine-11.0-rc1 (stable)"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                compatibility.parse_wine_version(output)

        for version, channel in (("11.0.2", "stable"), ("wine-11.20", "devel"),
                                 ("11.01", "devel"), ("11.0-rc2", "rc")):
            self.assertEqual(wine.get_devel_or_stable(version), channel)

    def test_legacy_policy_reasons_and_product_nine_minimum_are_preserved(self):
        for facts, reason in (
            (wine.WineRelease(7, 0, "stable"), "Wine 7 requires staging"),
            (wine.WineRelease(8, 0, "staging"), "Wine 8.0 will not work"),
            (wine.WineRelease(8, 15, "devel"), "Wine 8 requires staging or devel 8.16 or later"),
            (wine.WineRelease(9, 0, "stable"), "Wine 9 requires devel or staging"),
            (wine.WineRelease(25, 0, "vendor"), "Unrecognized Wine branch: vendor"),
        ):
            with self.subTest(facts=facts):
                self.assertEqual(wine.check_wine_rules(facts, None, "9"), (False, reason))

        self.assertTrue(wine.check_wine_rules(wine.WineRelease(7, 0, "staging"), None, "9")[0])
        allowed, reason = wine.check_wine_rules(wine.WineRelease(11, 20, "devel"), None, "10")
        self.assertTrue(allowed)
        self.assertIn("not native-validated", reason)
        with self.assertRaises(maintenance.MaintenanceError):
            wine.check_wine_rules(wine.WineRelease(11, 20, "devel"), "bad target", "10")

    def test_recommended_comparison_preserves_patch_and_rc_order(self):
        for installed, recommended, status in (
            ("wine-11.0.2", "11.0.1", 2),
            ("wine-11.0.2", "11.0.2", 1),
            ("wine-11.0-rc2", "11.0", 0),
            ("wine-11.0-rc2", "11.0-rc1", 2),
        ):
            app = SimpleNamespace(conf=SimpleNamespace(
                wine_binary="/fixture/wine", wine_appimage_recommended_version=recommended,
            ))
            with (
                self.subTest(installed=installed, recommended=recommended),
                patch.object(runtime, "executable", return_value="/fixture/wine"),
                patch.object(wine, "get_wine_release", return_value=(
                    compatibility.parse_wine_version(installed), "yes",
                )),
            ):
                self.assertEqual(utils.compare_recommended_appimage_version(app)[0], status)

    def test_failed_bounded_probe_is_not_an_identified_version(self):
        with (
            patch.object(runtime, "executable", return_value="/fixture/wine"),
            patch.object(processes, "capture", side_effect=processes.CaptureError("output limit")) as capture,
        ):
            result, reason = wine.get_wine_release("/fixture/wine")
        self.assertIsNone(result)
        self.assertIn("output limit", reason)
        self.assertEqual(capture.call_args.kwargs["limit"], 16384)
        self.assertEqual(capture.call_args.kwargs["timeout"], 15)
