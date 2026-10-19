from contextlib import ExitStack
import errno
import hashlib
import multiprocessing
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ou_dedetai import artifacts, constants, installer, main, processes, releases, updates
from tests.test_artifacts import response


def contender(target, connection):
    try:
        destination = updates.Destination(Path(target))
    except BlockingIOError:
        connection.send("busy")
    else:
        destination.close()
        connection.send("admitted")
    finally:
        connection.close()


def controller_elf(version="1.1.0", *, loop=False, product="LogosDesktop", close_output=False):
    """A native write/exit fixture with real ELF executable mappings."""
    text = f"{product}, {version}\n".encode()
    ending = b"\xeb\xfe" if loop else b"\xb8\x3c\0\0\0\x31\xff\x0f\x05"
    if close_output:
        ending = (b"\xb8\x03\0\0\0\xbf\x01\0\0\0\x0f\x05"
                  b"\xb8\x03\0\0\0\xbf\x02\0\0\0\x0f\x05" + ending)
    code = (b"\xb8\x01\0\0\0\xbf\x01\0\0\0\x48\x8d\x35"
            + struct.pack("<i", 7 + len(ending)) + b"\xba" + struct.pack("<I", len(text)) + b"\x0f\x05" + ending)
    size = 120 + len(code) + len(text)
    header = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
                         2, 62, 1, 0x400078, 64, 0, 0, 64, 56, 1, 64, 0, 0)
    program = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, size, size, 4096)
    return header + program + code + text


class TestControllerUpdate(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = root
        self.target = root / "installed controller"
        self.old = controller_elf("1.0.0")
        self.new = controller_elf()
        self.target.write_bytes(self.old)
        self.target.chmod(0o750)
        self.stack.enter_context(patch.object(Path, "home", return_value=root))
        self.stack.enter_context(patch.multiple(constants, APP_SELF_UPDATES_ENABLED=True, RUNMODE="binary",
                                               CONTROLLER_DISTRIBUTION="standalone-onefile",
                                               APP_RELEASE_VERSION="1.0.0", STATE_DIR=str(root / "state/app")))
        self.stack.enter_context(patch.multiple(sys, frozen=True, _MEIPASS=str(root.parent / "bundle"),
                                               executable=str(self.target), create=True))
        self.stack.enter_context(patch.object(updates, "_package_owned", return_value=False))
        self.stack.enter_context(patch.object(updates, "_running_matches", return_value=True))
        digest = artifacts.Digest("sha256", hashlib.sha256(self.new).hexdigest(), "fixture release metadata")
        self.release = releases.SoftwareReleaseInfo(
            "1.1.0", "https://example.invalid/controller", digest, "logos-desktop-1.1.0-linux-x86_64",
            len(self.new), constants.REPOSITORY_SLUG, "v1.1.0",
        )
        self.app = SimpleNamespace(status=Mock(), conf=SimpleNamespace(
            _raw=SimpleNamespace(install_dir=None), _overrides=SimpleNamespace(install_dir=None),
            _network=Mock(controller_release=Mock(return_value=releases.ReleaseLookup(self.release, "ok"))),
            download_dir=str(root / "cache"), app_release_channel="stable",
        ))
        self.stack.enter_context(patch.object(
            artifacts.requests, "get", side_effect=lambda *a, **kw: response(self.new),
        ))

    def test_real_native_probe_runs_only_final_sibling_and_exact_version(self):
        prepared = updates.prepare(self.app)
        self.addCleanup(prepared.cancel)
        self.assertEqual(prepared.stage.parent, self.target.parent)
        self.assertEqual(updates.probe(prepared.stage), "1.1.0")
        self.assertEqual(self.target.read_bytes(), self.old)
        with self.assertRaises(BlockingIOError):
            updates.Destination(self.target)

    def test_rejected_bytes_and_wrong_version_preserve_installed_controller(self):
        arm = bytearray(self.new)
        arm[18:20] = struct.pack("<H", 183)
        for content in (b"", b"HTML", self.new[:90], bytes(arm), self.new + b"corrupt"):
            with self.subTest(content=content[:10]), patch.object(
                artifacts.requests, "get", return_value=response(content),
            ), patch.object(updates, "probe") as probe:
                with self.assertRaises(artifacts.ArtifactError):
                    updates.prepare(self.app)
                probe.assert_not_called()
                self.assertEqual(self.target.read_bytes(), self.old)
        with patch.object(updates, "probe", return_value="9.0.0"):
            with self.assertRaises(updates.UpdateError):
                updates.prepare(self.app)
        self.assertFalse(list(self.root.glob(".logos-desktop-stage-*")))

    def test_exec_failure_restores_bytes_mode_and_retains_runnable_previous(self):
        prepared = updates.prepare(self.app)
        seen = []

        def failed_exec(target, arguments, environment):
            seen.append((target, arguments, environment, Path(target).read_bytes()))
            raise OSError(errno.EACCES, "fixture restart failure")

        with patch.object(updates.os, "execve", side_effect=failed_exec):
            with self.assertRaises(OSError):
                prepared.restart(updates.resume_arguments(self.root / "selected config.json", "tk"))
        self.assertEqual(seen[0][0], self.target)
        self.assertEqual(
            seen[0][1], [str(self.target), "-c", str(self.root / "selected config.json"), "--dialog", "tk"],
        )
        self.assertEqual(seen[0][2]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(seen[0][3], self.new)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o750)
        backup, = self.root.glob(".logos-desktop-previous-*")
        self.assertEqual(updates.probe(backup), "1.0.0")
        self.assertFalse(list((self.root / ".logos-desktop-controller").glob("*.json")))

    def test_copy_and_rename_failures_never_truncate_installed_bytes(self):
        def partial_copy(source, target):
            target.write(b"partial")
            raise OSError(errno.ENOSPC, "fixture disk full")

        with patch.object(updates.shutil, "copyfileobj", side_effect=partial_copy):
            with self.assertRaises(OSError):
                updates.prepare(self.app)
        self.assertEqual(self.target.read_bytes(), self.old)
        prepared = updates.prepare(self.app)
        original = os.replace

        def denied(source, target):
            if Path(target) == self.target:
                raise PermissionError("fixture target rename denied")
            return original(source, target)

        with patch.object(updates.os, "replace", side_effect=denied), patch.object(updates.os, "execve") as execute:
            with self.assertRaises(PermissionError):
                prepared.restart([])
        execute.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(list(self.root.glob(".logos-desktop-stage-*")))

    def test_pending_recovery_is_repeatable_and_foreign_target_is_preserved(self):
        prepared = updates.prepare(self.app)
        with patch.object(updates.os, "execve", side_effect=OSError("restart failed")), patch.object(
            updates, "_rollback", side_effect=OSError("rollback failed"),
        ):
            with self.assertRaisesRegex(updates.UpdateError, "recovery is required"):
                prepared.restart([])
        self.assertEqual(self.target.read_bytes(), self.new)
        with patch.object(updates.os, "execve", side_effect=OSError("recovered exec denied")):
            with self.assertRaises(OSError):
                updates.recover_startup([])
        self.assertEqual(self.target.read_bytes(), self.old)
        updates.recover_startup([])

        prepared = updates.prepare(self.app)
        foreign = controller_elf("7.0.0")

        def changed_then_failed(*args):
            self.target.write_bytes(foreign)
            raise OSError("restart failed after foreign replacement")

        with patch.object(updates.os, "execve", side_effect=changed_then_failed):
            with self.assertRaisesRegex(updates.UpdateError, "recovery is required"):
                prepared.restart([])
        with self.assertRaisesRegex(updates.UpdateError, "changed controller"):
            updates.recover_startup([])
        self.assertEqual(self.target.read_bytes(), foreign)

    def test_same_path_launcher_is_noop_and_different_source_is_staged(self):
        with patch.object(updates.shutil, "copyfileobj") as copy:
            self.assertEqual(updates.copy_launcher(self.target, self.target), self.target)
            copy.assert_not_called()
        other = self.root / "another launcher"
        other.write_bytes(controller_elf("0.9.0"))
        other.chmod(0o700)
        self.assertTrue(updates.copy_launcher(self.target, other))
        self.assertEqual(other.read_bytes(), self.old)

    def test_target_change_and_hardlink_are_refused(self):
        prepared = updates.prepare(self.app)
        foreign = controller_elf("8.0.0")
        self.target.write_bytes(foreign)
        with self.assertRaisesRegex(updates.UpdateError, "changed during preparation"):
            prepared.restart([])
        self.assertEqual(self.target.read_bytes(), foreign)
        os.link(self.target, self.root / "alias")
        with self.assertRaises(updates.UpdateError):
            updates.eligible()

    def test_probe_output_limit_and_wrong_product_are_failures(self):
        path = self.root / "probe"
        for content in (controller_elf("1.1.0", product="Other"), controller_elf("1" * 20000)):
            path.write_bytes(content)
            path.chmod(0o700)
            with self.assertRaises(updates.UpdateError):
                updates.probe(path)

    def test_probe_timeout_terminates_its_native_process_group(self):
        path = self.root / "looping probe"
        path.write_bytes(controller_elf(loop=True))
        path.chmod(0o700)
        with patch.object(processes.time, "monotonic", side_effect=[0, 16]):
            with self.assertRaisesRegex(updates.UpdateError, "timed out"):
                updates.probe(path)

    def test_destination_lock_excludes_another_real_process(self):
        context = multiprocessing.get_context("fork")
        prepared = updates.prepare(self.app)
        for expected in ("busy", "admitted"):
            parent, child = context.Pipe()
            process = context.Process(target=contender, args=(str(self.target), child))
            process.start()
            child.close()
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), expected)
            parent.close()
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            prepared.cancel()

    def test_commit_sync_failure_restores_target_and_launcher_copy(self):
        original = updates._sync
        failed = []

        def sync(directory):
            if Path(directory) == self.root and not failed:
                failed.append(True)
                raise OSError("fixture directory sync failure")
            original(directory)

        prepared = updates.prepare(self.app)
        with patch.object(updates, "_sync", side_effect=sync):
            with self.assertRaises(OSError):
                prepared.restart([])
        self.assertEqual(self.target.read_bytes(), self.old)
        other = self.root / "other launcher"
        prior = controller_elf("0.9.0")
        other.write_bytes(prior)
        other.chmod(0o700)
        failed.clear()
        with patch.object(updates, "_sync", side_effect=sync):
            with self.assertRaises(OSError):
                updates.copy_launcher(self.target, other)
        self.assertEqual(other.read_bytes(), prior)

    def test_managed_installation_and_invocation_alias(self):
        with patch.object(updates, "_package_owned", return_value=True):
            with self.assertRaises(updates.ManagedInstallation):
                updates.prepare(self.app)
            self.app.conf._network.controller_release.assert_not_called()
        alias = self.root / "invocation alias"
        alias.symlink_to(self.target.name)
        with patch.object(sys, "executable", str(alias)):
            self.assertEqual(updates.eligible(), self.target)
        self.assertTrue(alias.is_symlink())

    def test_ineligible_ordinary_launch_without_recovery_does_not_query_ownership(self):
        with patch.object(updates, "eligible", side_effect=updates.ManagedInstallation("not self-updatable")) as admit:
            updates.recover_startup([])
        admit.assert_not_called()

    def test_managed_launcher_install_does_not_publish_nonexistent_shortcuts(self):
        self.app.conf.install_dir = str(self.root / "prefix")
        self.app.installer_step_count = self.app.installer_step = 0
        with (patch.object(installer, "ensure_config_file"),
              patch.object(updates, "eligible", side_effect=updates.ManagedInstallation("package managed")),
              patch.object(installer, "create_launcher_shortcuts") as shortcuts):
            installer.ensure_launcher_shortcuts(self.app)
        shortcuts.assert_not_called()
        self.assertFalse((self.root / "prefix/oudedetai").exists())


class TestUpdateAdmission(unittest.TestCase):
    def test_parser_to_main_refuses_before_profile_app_and_recovery(self):
        for enabled, distribution in ((False, "standalone-onefile"), (True, "source"), (True, "package")):
            with self.subTest(enabled=enabled, distribution=distribution), patch.multiple(
                constants, APP_SELF_UPDATES_ENABLED=enabled, CONTROLLER_DISTRIBUTION=distribution,
            ), patch.object(sys, "argv", ["controller", "--update-self", "-c", "/broken/profile.json"]), patch.object(
                main, "setup_config",
            ) as config, patch("ou_dedetai.repair.repair_installation") as recover, patch.object(
                main.PersistentConfiguration, "load_from_path",
            ) as load:
                with self.assertRaises(SystemExit):
                    main.main()
                config.assert_not_called()
                recover.assert_not_called()
                load.assert_not_called()

    def test_force_flag_and_frontend_reach_ephemeral_configuration(self):
        ephemeral = SimpleNamespace(dialog=None)
        parser = main.get_parser()
        with patch.object(main.EphemeralConfiguration, "load", return_value=ephemeral):
            result, _ = main.parse_args(parser.parse_args(["--check-for-updates", "--dialog", "tui"]), parser)
        self.assertTrue(result.check_updates_now)
        self.assertEqual(result.dialog, "tui")

    def test_effective_environment_profile_and_legacy_frontend_are_resumed(self):
        parser = main.get_parser()
        args = parser.parse_args(["--update-self"])
        prepared = Mock()
        with (patch.dict(os.environ, {"CONFIG_FILE": "/fixture/selected.json"}),
              patch.object(constants, "APP_RELEASE_VERSION", "1.0.0"), patch.object(updates, "eligible"),
              patch.object(main.PersistentConfiguration, "load_from_path", return_value=SimpleNamespace(
                  app_release_channel="beta",
              )) as load,
              patch.object(main.EphemeralConfiguration, "load_from_path", return_value=SimpleNamespace(
                  dialog="curses",
              )),
              patch.object(updates, "prepare", return_value=prepared)):
            main.run_controller_update(args)
        load.assert_called_once_with("/fixture/selected.json")
        prepared.restart.assert_called_once_with(["-c", "/fixture/selected.json", "--dialog", "tui"])
        for frontend in ("curses", "cli", "tk", "tui"):
            resumed = updates.resume_arguments("/fixture/selected.json", frontend)
            self.assertIn(parser.parse_args(resumed).dialog, ("tk", "tui"))
        with self.assertRaises(updates.UpdateError):
            updates.resume_arguments(dialog="unknown")

    def test_fresh_environment_does_not_consume_parent_loader_state(self):
        env = {"LD_LIBRARY_PATH": "/old/bundle", "LD_LIBRARY_PATH_ORIG": "/user/lib",
               "DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-0", "TCL_LIBRARY": "/old/bundle/tcl"}
        with patch.dict(os.environ, env, clear=True), patch.multiple(
            sys, frozen=True, _MEIPASS="/old/bundle", create=True,
        ):
            first, second = updates.fresh_environment(), updates.fresh_environment()
            self.assertEqual(first, second)
            self.assertEqual(dict(os.environ), env)
        self.assertEqual(first["LD_LIBRARY_PATH"], "/user/lib")
        self.assertNotIn("TCL_LIBRARY", first)
        self.assertEqual(first["DISPLAY"], ":1")
        self.assertEqual(first["WAYLAND_DISPLAY"], "wayland-0")
