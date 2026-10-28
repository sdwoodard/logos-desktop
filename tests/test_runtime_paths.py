import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from logos_desktop import artifacts, constants, control, installer, maintenance, network, paths, releases, utils, wine
from logos_desktop.config import Config, PersistentConfiguration
from tests.artifact_fixtures import appimage, msi


def fixture_config(install):
    config = object.__new__(Config)
    config._raw = PersistentConfiguration(
        install_dir=str(install), wine_binary="data/bin/runtime.AppImage",
        faithlife_product="Logos", faithlife_product_version="10",
        faithlife_product_release="38.0.0.0",
    )
    config._overrides = SimpleNamespace(
        install_dir=None, installer_binary_dir=None, wine_appimage_path=None, faithlife_product_version=None,
        config_path=str(install.parent / "selected.json"), wine_appimage_link_file_name=None, wine_prefix=None,
    )
    config._write = Mock()
    config.app = SimpleNamespace(conf=config, ask=Mock())
    config._wine64_path = None
    return config


class TestRuntimePaths(unittest.TestCase):
    def test_helper_decodes_streams_separately_without_hiding_bad_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            config = fixture_config(Path(directory) / "install")
            binary = Path(directory) / "runtime"
            binary.write_text(
                f"#!{sys.executable}\nimport os,sys\n"
                "os.write(1, b'bad\\xff' if sys.argv[1] == 'bad' else 'path 文\\r\\n'.encode())\n"
                "os.write(2, b'clean diagnostic' if sys.argv[1] == 'bad' else b'diagnostic\\xff\\r\\n')\n"
                "raise SystemExit(23 if sys.argv[1] == 'failure' else 0)\n"
            )
            binary.chmod(0o700)
            with patch.object(wine, "get_wine_env", return_value=dict(os.environ)):
                for argument, code in (("good", 0), ("failure", 23)):
                    result = wine.run_wine_completed_process(config.app, str(binary), exe_args=[argument])
                    self.assertIsInstance(result, subprocess.CompletedProcess)
                    self.assertEqual(result.returncode, code)
                    self.assertEqual(result.stdout, "path 文\n")
                    self.assertEqual(result.stderr, "diagnostic\\xff\n")
                    self.assertEqual(result.args, [str(binary), argument])
                with self.assertRaises(maintenance.MaintenanceError) as raised:
                    wine.run_wine_completed_process(config.app, str(binary), exe_args=["bad"])
                self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)

    def test_helper_timeout_and_spawn_failure_stay_domain_failures(self):
        for error in (subprocess.TimeoutExpired(["/fixture/wine"], 60), OSError("spawn refused")):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(wine, "_wine_invocation", return_value=(["/fixture/wine"], {})),
                patch.object(wine.subprocess, "run", side_effect=error) as run,
            ):
                with self.assertRaises(maintenance.MaintenanceError) as raised:
                    wine.run_wine_completed_process.__wrapped__(None, "/fixture/wine")
                self.assertIs(raised.exception.__cause__, error)
                self.assertEqual(run.call_args.kwargs["timeout"], 60)
                self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_long_paths_use_current_verified_drive_and_preserve_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "install")
            mapped = root / "mapped"
            source = mapped / ("a" * 90) / ("b" * 90) / ("c" * 90) / "file 文 space.msi"
            source.parent.mkdir(parents=True)
            source.touch()
            alias = root / "alias"
            alias.symlink_to(mapped, target_is_directory=True)
            drives = Path(config.wine_prefix) / "dosdevices"
            drives.mkdir(parents=True)
            drive = drives / "z:"
            drive.symlink_to(mapped, target_is_directory=True)
            with patch.object(wine, "run_wine_completed_process") as helper:
                converted = wine.windows_path(config.app, "/fixture/wine", alias / source.relative_to(mapped))
                self.assertEqual(converted, "\\\\?\\Z:\\" + "\\".join(source.relative_to(mapped).parts))
                drive.unlink()
                drive.symlink_to(root, target_is_directory=True)
                self.assertEqual(
                    wine.windows_path(config.app, "/fixture/wine", source),
                    "\\\\?\\Z:\\" + "\\".join(source.relative_to(root).parts),
                )
                helper.assert_not_called()

    def test_long_paths_refuse_missing_wrong_and_reused_drive_maps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "install")
            source = root / ("a" * 90) / ("b" * 90) / ("c" * 90) / "input.msi"
            source.parent.mkdir(parents=True)
            source.touch()
            drives = Path(config.wine_prefix) / "dosdevices"
            drives.mkdir(parents=True)
            wrong = root / "wrong"
            wrong.mkdir()
            with patch.object(wine, "run_wine_completed_process") as helper:
                for target in (None, wrong, root, wrong):
                    drive = drives / "z:"
                    if drive.is_symlink():
                        drive.unlink()
                    if target is not None:
                        drive.symlink_to(target, target_is_directory=True)
                    if target == root:
                        self.assertTrue(wine.windows_path(config.app, "/fixture/wine", source).startswith("\\\\?\\Z:"))
                    else:
                        with self.assertRaises(maintenance.MaintenanceError):
                            wine.windows_path(config.app, "/fixture/wine", source)
                other = fixture_config(root / "other-install")
                with self.assertRaises(maintenance.MaintenanceError):
                    wine.windows_path(other.app, "/fixture/wine", source)
                helper.assert_not_called()

    def test_long_paths_select_mapped_ancestor_without_assuming_z(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "install")
            source = root / ("a" * 90) / ("b" * 90) / ("c" * 90) / "input 文.msi"
            source.parent.mkdir(parents=True)
            source.touch()
            drives = Path(config.wine_prefix) / "dosdevices"
            drives.mkdir(parents=True)
            (drives / "d:").symlink_to(root)
            (drives / "e:").symlink_to(source.parent)
            with patch.object(wine, "run_wine_completed_process") as helper:
                self.assertEqual(wine.windows_path(config.app, "/fixture/wine", source), "\\\\?\\E:\\input 文.msi")
                helper.assert_not_called()

    def test_installer_paths_reject_unrepresentable_components_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "install")
            parent = root / ("a" * 90) / ("b" * 90) / ("c" * 90)
            parent.mkdir(parents=True)
            drives = Path(config.wine_prefix) / "dosdevices"
            drives.mkdir(parents=True)
            (drives / "z:").symlink_to(root)
            for name in ("line\nbreak.msi", "stream:input.msi", "back\\slash", 'quote".msi',
                         "wild*.msi", "trailing.", "trailing ", "CON.msi", "LPT1", "文" * 128):
                with self.subTest(name=name), patch.object(wine, "run_wine_completed_process") as helper:
                    source = parent / name
                    if len(os.fsencode(name)) <= 255:
                        source.touch()
                    with self.assertRaises(maintenance.MaintenanceError):
                        wine.windows_path(config.app, "/fixture/wine", source)
                    helper.assert_not_called()

    def test_long_accepted_msi_reaches_installer_as_one_exact_text_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "install")
            config._overrides.agreed_to_faithlife_terms = False
            config.app.status = Mock()
            config.app.approve_or_exit = Mock(return_value=True)
            source = root / "input.msi"
            source.write_bytes(msi())
            store = root / ("a" * 90) / ("b" * 90) / "store 文 space"
            accepted = artifacts.import_local(source, store, artifacts.ArtifactSpec("local:input", "input.msi", "msi"))
            self.assertGreater(len(str(accepted.path)), 260)
            drives = Path(config.wine_prefix) / "dosdevices"
            drives.mkdir(parents=True)
            (drives / "z:").symlink_to(root)
            drive_c = Path(config.wine_prefix) / "drive_c"
            drive_c.mkdir()
            (drives / "c:").symlink_to(drive_c)
            config.app.checkpoint = Mock()
            with (
                patch.object(wine, "run_wine_completed_process") as helper,
                patch.object(wine, "run_wine_during_install", return_value=Mock(returncode=0)) as install,
            ):
                wine.install_msi(config.app, accepted, target_release="39.0.0.0")
                arguments = install.call_args.kwargs["exe_args"]
                self.assertEqual(arguments[0], "/i")
                self.assertRegex(arguments[1], r"^\\\\\?\\C:\\LogosDesktopInstall-[0-9a-f]{12}\\installer\.msi$")
                self.assertEqual(arguments[2:], ["/passive"])
                self.assertTrue(all(isinstance(argument, str) for argument in arguments))
                self.assertEqual(list(drive_c.iterdir()), [])
                helper.assert_not_called()
                install.reset_mock()
                accepted.path.chmod(0o600)
                accepted.path.write_bytes(b"changed")
                with self.assertRaises(artifacts.ArtifactError):
                    wine.install_msi(config.app, accepted, target_release="39.0.0.0")
                install.assert_not_called()

    def test_component_rules_and_serialization_preserve_external_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory) / "Logos 文"
            config = fixture_config(install)
            cases = {
                str(install / "data/bin/wine"): "data/bin/wine",
                str(install): ".",
                str(install) + "-tools/wine": str(install) + "-tools/wine",
                "data/bin/wine": "data/bin/wine",
                str(install / "../external/wine"): str(install / "../external/wine"),
            }
            for original, stored in cases.items():
                with self.subTest(path=original):
                    self.assertEqual(utils.get_relative_path(original, str(install)), stored)
                    self.assertEqual(config._relative_from_install_dir(original), stored)
                    config.wine_binary = original
                    self.assertEqual(config._raw._as_dict()["wine_binary"], stored)
                    reloaded = PersistentConfiguration.from_document(json.loads(json.dumps(config._raw._as_dict())), {})
                    self.assertEqual(reloaded.wine_binary, stored)
            config._raw.faithlife_product_release = str(install / "not-a-path-field")
            self.assertEqual(config._raw._as_dict()["faithlife_product_release"], str(install / "not-a-path-field"))

    def test_real_probes_and_processes_ignore_cwd_shadow_and_missing_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "Installed 文 Space"
            work = root / "work"
            capture = root / "capture.json"
            for base in (install, work):
                executable = base / "data/bin/runtime"
                executable.parent.mkdir(parents=True)
                executable.write_text(
                    f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                    f"Path({str(capture)!r}).write_text(json.dumps([__file__, *sys.argv[1:]]))\n"
                    "print('wine-10.0 (Staging)')\n"
                )
                executable.chmod(0o755)
            config = fixture_config(install)
            config._raw.wine_binary = "data/bin/runtime"
            previous = Path.cwd()
            try:
                os.chdir(work)
                self.assertEqual(config.wine_binary, str(install / config._raw.wine_binary))
                self.assertIsNotNone(wine.get_wine_release(config.wine_binary)[0])
                self.assertEqual(json.loads(capture.read_text())[0], config.wine_binary)
                with patch.object(wine, "get_wine_env", return_value=dict(os.environ)):
                    result = wine.run_wine_completed_process(config.app, config.wine_binary, exe_args=["space 文"])
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(json.loads(capture.read_text()), [config.wine_binary, "space 文"])
                    with tempfile.TemporaryFile(mode="w+") as output:
                        process = wine.run_wine_process(
                            config.app, config.wine_binary, output, output, exe_args=["literal"],
                        )
                        self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(json.loads(capture.read_text())[0], config.wine_binary)
                Path(config.wine_binary).unlink()
                capture.unlink()
                self.assertIsNone(wine.get_wine_release(config.wine_binary)[0])
                self.assertFalse(capture.exists())
                with self.assertRaises(ValueError), patch.object(wine, "get_wine_env", return_value={}):
                    wine.run_wine_completed_process(config.app, config.wine_binary)
                self.assertFalse(capture.exists())
            finally:
                os.chdir(previous)

    def test_warmed_companion_cache_changes_with_installation_and_symlink_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "first")
            for name in ("first", "second"):
                bindir = root / name / "data/bin"
                bindir.mkdir(parents=True)
                (bindir / "wine").touch()
                (bindir / "wine64").touch()
                (bindir / "wine64").chmod(0o700)
            config._raw.wine_binary = "data/bin/wine"
            self.assertEqual(config.wine64_binary, str(root / "first/data/bin/wine64"))
            config._raw.install_dir = str(root / "second")
            self.assertEqual(config.wine64_binary, str(root / "second/data/bin/wine64"))
            self.assertEqual(config.wineserver_binary, str(root / "second/data/bin/wineserver"))
            (root / "second/data/bin/wine").unlink()
            self.assertEqual(config.wine64_binary, str(root / "second/data/bin/wine"))
            config._raw.wine_binary = "data/bin/missing"
            self.assertEqual(config.wine64_binary, str(root / "second/data/bin/missing"))

    def test_recommended_and_experimental_reach_installer_consumer(self):
        for choice in (constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = fixture_config(root / "install")
                cache_base = root / "cache"
                cache = cache_base / "logos-desktop"
                config._download_dir = None
                config._raw.faithlife_product_version = "10"
                config._network = Mock()
                filename = "wine-stable_11.0-x86_64.AppImage"
                selected = network.SoftwareReleaseInfo("11.0", f"https://example.invalid/{filename}", name=filename)
                config._network.wine_appimage_release.return_value = releases.ReleaseLookup(selected, "ok")
                config._raw.wine_binary = choice
                app = SimpleNamespace(
                    checkpoint=Mock(),
                    conf=config, installer_step=0, installer_step_count=0, status=Mock(), _config_updated_event=Mock(),
                )
                config.app = app

                def transfer(url, name, directory, app, **kwargs):
                    source = root / "selected.AppImage"
                    source.write_bytes(appimage())
                    return artifacts.import_local(
                        source, Path(directory), artifacts.ArtifactSpec(url, name, "appimage"),
                    )

                with (
                    patch.object(installer, "ensure_sys_deps"),
                    patch.object(installer, "check_system_compatibility"),
                    patch.object(wine, "require_compatible"),
                    patch.object(utils, "current_native_wine", return_value=None),
                    patch.object(installer.network, "logos_reuse_download", side_effect=transfer) as download,
                    patch.multiple(constants, CACHE_BASE=cache_base, CACHE_DIR=str(cache)),
                ):
                    selected_path = config.wine_binary
                    installer.ensure_appimage_download(app)
                download.assert_called_once_with(
                    f"https://example.invalid/{filename}", filename, config.installer_binary_dir,
                    app, kind="appimage", digest=None, size=None,
                )
                self.assertEqual(config.wine_binary, selected_path)
                config._network.wine_appimage_versions.assert_not_called()
                self.assertEqual(Path(config.wine_binary).name, filename)
                self.assertEqual(artifacts.installed(config.wine_binary, "appimage").path.read_bytes(), appimage())

    def test_external_appimage_with_release_basename_remains_an_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "install")
            custom = root / "custom/stable.AppImage"
            config.wine_binary = str(custom)
            app = SimpleNamespace(
                conf=config, installer_step=0, installer_step_count=0, status=Mock(), checkpoint=Mock(),
            )
            with (
                patch.object(installer, "ensure_sys_deps"),
                patch.object(installer, "check_system_compatibility"),
                patch.object(installer.network, "logos_reuse_download") as transfer,
            ):
                installer.ensure_appimage_download(app)
            transfer.assert_not_called()
            self.assertEqual(config.wine_binary, str(custom))

    def test_support_inspects_stored_runtime_without_execution_or_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "install"
            install.mkdir()
            runtime = install / "runtime"
            capture = root / "executed"
            runtime.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(capture)!r}).touch()\n")
            runtime.chmod(0o755)
            absent = str(root / "absent")
            conf = SimpleNamespace(
                _raw=SimpleNamespace(wine_binary="runtime", install_dir=str(install), faithlife_product="Logos",
                                      _as_dict=lambda: {}), wine_binary=str(runtime),
                install_dir=str(install),
                installer_binary_dir=str(install / "data/bin"),
                config_file_path=absent, app_log_path=absent, app_wine_log_path=absent,
                app_wine_log_previous_path=absent, _faithlife_crash_log=None,
                _network=SimpleNamespace(_cache=SimpleNamespace(_as_dict=lambda: {})), _overrides=SimpleNamespace(),
            )
            output = root / "support.zip"
            app = SimpleNamespace(
                conf=conf, ask=Mock(side_effect=[str(output), "Keep archive local"]),
                status=Mock(), info=Mock(), approve=Mock(return_value=True),
            )
            original_runtime = runtime.read_bytes()
            original_config = vars(conf._raw).copy()
            with (
                patch.object(
                    subprocess, "Popen", side_effect=AssertionError("Support must not execute commands"),
                ) as commands,
            ):
                control.get_support(app)
            commands.assert_not_called()
            self.assertFalse(capture.exists())
            self.assertEqual(runtime.read_bytes(), original_runtime)
            self.assertEqual(vars(conf._raw), original_config)
            self.assertEqual(set(root.iterdir()), {install, output})
            self.assertEqual(set(install.iterdir()), {runtime})
            with ZipFile(output) as archive:
                report = json.loads(archive.read("doctor.json"))
                self.assertIsNone(report["command_preview"])
                runtime_check = next(check for check in report["checks"] if check["name"] == "runtime")
                self.assertEqual(runtime_check["status"], "unknown")
                self.assertIn("not an ELF binary", runtime_check["summary"])
                self.assertNotIn(str(root), app.info.call_args.args[0])

    def test_configured_symlink_is_preserved_and_invalid_types_are_not_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "external"
            target.write_bytes(b"fixture")
            target.chmod(0o755)
            install = root / "install"
            install.mkdir()
            link = install / "selected"
            link.symlink_to(target)
            config = fixture_config(install)
            config.wine_binary = str(link)
            self.assertEqual(config.wine_binary, str(link))
            self.assertEqual(paths.executable(config.wine_binary), str(link))
            for invalid in (root, root / "absent"):
                with self.assertRaises(ValueError):
                    paths.executable(invalid)
