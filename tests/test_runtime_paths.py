import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ou_dedetai import artifacts, constants, control, installer, network, paths, utils, wine
from ou_dedetai.config import Config, PersistentConfiguration
from tests.artifact_fixtures import appimage


def fixture_config(install):
    config = object.__new__(Config)
    config._raw = PersistentConfiguration(
        install_dir=str(install), wine_binary="data/bin/runtime.AppImage",
        faithlife_product="Logos", faithlife_product_version="10",
    )
    config._overrides = SimpleNamespace(
        install_dir=None, installer_binary_dir=None, wine_appimage_path=None, faithlife_product_version=None,
        config_path=str(install.parent / "selected.json"), wine_appimage_link_file_name=None,
    )
    config._write = Mock()
    config.app = SimpleNamespace(conf=config, ask=Mock())
    config._wine64_path = None
    return config


class TestRuntimePaths(unittest.TestCase):
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
                    reloaded = PersistentConfiguration(**json.loads(json.dumps(config._raw._as_dict())))
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
        for choice, prerelease, filename in (
            (constants.WINE_RECOMMENDED_SIGIL, True, "stable.AppImage"),
            (constants.WINE_BETA_SIGIL, True, "beta.AppImage"),
            (constants.WINE_BETA_SIGIL, False, "stable.AppImage"),
        ):
            with self.subTest(choice=choice, prerelease=prerelease), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = fixture_config(root / "install")
                cache_base = root / "cache"
                cache = cache_base / "logos-desktop"
                config._download_dir = None
                config._raw.faithlife_product_version = "10"
                config._network = Mock()
                beta = network.SoftwareReleaseInfo("2", "https://example.invalid/beta.AppImage")
                config._network.wine_appimage_versions.return_value = network.GithubSoftwareReleasesInfo(
                    latest=network.SoftwareReleaseInfo("1", "https://example.invalid/stable.AppImage"),
                    pre_release=beta if prerelease else None,
                )
                config._raw.wine_binary = choice
                self.assertEqual(config.wine_binary, str(root / "install/data/bin" / filename))
                config.app.ask.assert_not_called()
                app = SimpleNamespace(
                    conf=config, installer_step=0, installer_step_count=0, status=Mock(), _config_updated_event=Mock(),
                )

                def transfer(url, name, directory, **kwargs):
                    source = root / "selected.AppImage"
                    source.write_bytes(appimage())
                    return artifacts.import_local(
                        source, Path(directory), artifacts.ArtifactSpec(url, name, "appimage"),
                    )

                with (
                    patch.object(installer, "ensure_sys_deps"),
                    patch.object(installer, "check_system_compatibility"),
                    patch.object(installer.network, "logos_reuse_download", side_effect=transfer) as download,
                    patch.multiple(constants, CACHE_BASE=cache_base, CACHE_DIR=str(cache)),
                ):
                    installer.ensure_appimage_download(app)
                download.assert_called_once_with(
                    f"https://example.invalid/{filename}", filename, config.installer_binary_dir,
                    app=app, kind="appimage", digest=None,
                )
                self.assertEqual(Path(config.wine_binary).name, filename)
                self.assertEqual(artifacts.installed(config.wine_binary, "appimage").path.read_bytes(), appimage())

    def test_external_appimage_with_release_basename_remains_an_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = fixture_config(root / "install")
            custom = root / "custom/stable.AppImage"
            config.wine_binary = str(custom)
            app = SimpleNamespace(conf=config, installer_step=0, installer_step_count=0, status=Mock())
            with (
                patch.object(installer, "ensure_sys_deps"),
                patch.object(installer, "check_system_compatibility"),
                patch.object(installer.network, "logos_reuse_download") as transfer,
            ):
                installer.ensure_appimage_download(app)
            transfer.assert_not_called()
            self.assertEqual(config.wine_binary, str(custom))

    def test_support_uses_selected_absolute_runtime_without_prompting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.write_text("fixture")
            runtime.chmod(0o755)
            absent = str(root / "absent")
            conf = SimpleNamespace(
                _raw=SimpleNamespace(wine_binary="runtime", install_dir=str(root), faithlife_product="Logos",
                                     _as_dict=lambda: {}), wine_binary=str(runtime),
                installer_binary_dir=str(root / "data/bin"),
                config_file_path=absent, app_log_path=absent, app_wine_log_path=absent,
                app_wine_log_previous_path=absent, _faithlife_crash_log=None,
                _network=SimpleNamespace(_cache=SimpleNamespace(_as_dict=lambda: {})), _overrides=SimpleNamespace(),
            )
            app = SimpleNamespace(
                conf=conf, ask=Mock(side_effect=[str(root / "support.zip"), "Keep archive local"]), status=Mock(),
            )
            with (
                patch.object(control.subprocess, "check_output", return_value="fixture") as commands,
                patch.object(control.system, "get_dialog", return_value="cli"),
            ):
                control.get_support(app)
            expected = unittest.mock.call([str(runtime), "--version"], text=True, env=unittest.mock.ANY, timeout=15)
            self.assertIn(expected, commands.call_args_list)

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
