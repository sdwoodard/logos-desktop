import json
import hashlib
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import (
    artifacts, constants, control, installer, maintenance, network, releases, runtime, system, utils, wine,
)
from logos_desktop.config import Config, EphemeralConfiguration, PersistentConfiguration
from logos_desktop.app import UserExitedFromAsk
from tests.artifact_fixtures import appimage, msi, product_msi, winetricks_zip
from tests.test_artifacts import response


def fixture_app(root):
    install = root / "install 文"
    bindir = install / "data/bin"
    bindir.mkdir(parents=True)
    cache = root / "cache"
    cache.mkdir()
    raw = PersistentConfiguration(
        install_dir=str(install), faithlife_product="Logos", faithlife_product_version="10",
        faithlife_product_release="38.0.0.0", wine_binary="data/bin/old.AppImage", wine_binary_code="AppImage",
    )
    overrides = SimpleNamespace(
        install_dir=None, wine_prefix=None, wine_appimage_path=None, config_path=str(root / "selected.json"),
        agreed_to_faithlife_terms=True,
        faithlife_installer_name="Logos.msi", faithlife_installer_download_url="https://example.invalid/Logos.msi",
    )
    conf = SimpleNamespace(
        _raw=raw, _overrides=overrides, install_dir=str(install), installer_binary_dir=str(bindir),
        wine_prefix=str(install / "data/wine64_bottle"), wine_appimage_link_file_name="selected_wine.AppImage",
        wine_binary=str(bindir / "old.AppImage"), wine_binary_code="System", wine_appimage_path=None,
        wine64_binary="/fixture/wine", download_dir=str(cache), user_download_dir=None,
        faithlife_installer_name="Logos.msi", faithlife_installer_download_url="https://example.invalid/Logos.msi",
        faithlife_product="Logos", faithlife_product_release="38.0.0.0", faithlife_product_version="10",
        installed_faithlife_product_release="38.0.0.0", wine_appimage_recommended_file_name="wine.AppImage",
        logos_exe=None,
        wine_appimage_recommended_url="https://example.invalid/wine.AppImage",
        _network=Mock(), icu_latest_version_url="https://example.invalid/icu.tar.gz", icu_latest_version="1",
    )
    conf._network.wine_appimage_versions.return_value = network.GithubSoftwareReleasesInfo(
        network.SoftwareReleaseInfo("1", conf.wine_appimage_recommended_url, name="wine.AppImage"), None,
    )
    conf._network.icu_latest_version.return_value = network.SoftwareReleaseInfo(
        "1", conf.icu_latest_version_url, name="icu-win.tar.gz",
    )
    app = SimpleNamespace(
        checkpoint=Mock(),
        conf=conf, status=Mock(), installer_step=0, installer_step_count=0, _config_updated_event=Event(),
        is_installed=Mock(return_value=False), approve_or_exit=Mock(side_effect=AssertionError("No real terms")),
    )
    for path in (Path(overrides.config_path), install / f"{constants.BINARY_NAME}.json"):
        path.write_text(json.dumps(raw._as_dict()))
    (bindir / "old.AppImage").write_bytes(appimage(b"old"))
    return app


class TestArtifactConsumers(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        state = patch.object(constants, "STATE_DIR", str(self.root / "state/app"))
        state.start()
        self.addCleanup(state.stop)
        self.app = fixture_app(self.root)
        # These are format-only AppImages. Compatibility has dedicated tests.
        compatibility = patch.object(wine, "require_compatible")
        compatibility.start()
        self.addCleanup(compatibility.stop)
        # The consumer fixture is a Linux reader, not a Windows MSI process.
        windows_view = patch.object(wine, "windows_path", side_effect=lambda app, binary, path: str(path))
        windows_view.start()
        self.addCleanup(windows_view.stop)

    def accepted(self, kind, content, name):
        source = self.root / ("source-" + name)
        source.write_bytes(content)
        return artifacts.import_local(
            source, Path(self.app.conf.installer_binary_dir),
            artifacts.ArtifactSpec("local:" + str(source), name, kind),
        )

    def state(self):
        links, configs = runtime._targets(self.app)
        return [runtime._snapshot(path) for path in (*links, *configs)]

    def prepare_msi_drive(self):
        prefix = Path(self.app.conf.wine_prefix)
        drive = prefix / "drive_c"
        drive.mkdir(parents=True)
        devices = prefix / "dosdevices"
        devices.mkdir()
        (devices / "c:").symlink_to("../drive_c")
        return drive

    def staged_msi_path(self, argument):
        self.assertTrue(argument.startswith("\\\\?\\C:\\LogosDesktopInstall-"))
        parts = argument.removeprefix("\\\\?\\C:\\").split("\\")
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1], "installer.msi")
        return Path(self.app.conf.wine_prefix) / "drive_c" / Path(*parts)

    def test_appimage_acquisition_pins_one_descriptor_across_refresh(self):
        conf = self.app.conf
        conf.wine_binary = str(Path(conf.installer_binary_dir) / "wine.AppImage")
        selected = network.SoftwareReleaseInfo(
            "1", "https://example.invalid/old.AppImage", name="wine.AppImage", size=123,
        )
        refreshed = network.SoftwareReleaseInfo("2", "https://example.invalid/new.AppImage", name="new.AppImage")
        conf._network.wine_appimage_versions.side_effect = [
            network.GithubSoftwareReleasesInfo(selected, None),
            network.GithubSoftwareReleasesInfo(refreshed, None),
        ]
        accepted = self.accepted("appimage", appimage(), "wine.AppImage")
        with (patch.object(installer, "ensure_sys_deps"), patch.object(installer, "check_system_compatibility"),
              patch.object(network, "logos_reuse_download", return_value=accepted) as acquire,
              patch.object(runtime, "activate") as activate):
            installer.ensure_appimage_download(self.app)
        conf._network.wine_appimage_versions.assert_called_once()
        acquire.assert_called_once_with(selected.download_url, selected.name, conf.installer_binary_dir,
                                        app=self.app, kind="appimage", digest=None, size=123)
        activate.assert_called_once_with(self.app, accepted)

    def recommended(self):
        selected = network.SoftwareReleaseInfo(
            "11.0", "https://example.invalid/wine-stable_11.0-x86_64.AppImage",
            name="wine-stable_11.0-x86_64.AppImage", size=123,
        )
        self.app.conf._network.wine_appimage_release.return_value = releases.ReleaseLookup(selected, "ok")
        self.app.conf.wine_binary_files = []
        return selected

    def test_recommended_installer_consumes_exact_descriptor_without_raw_catalog(self):
        selected = self.recommended()
        accepted = self.accepted("appimage", appimage(), selected.name)
        self.app.conf._network.wine_appimage_release.side_effect = [
            releases.ReleaseLookup(selected, "ok"),
            releases.ReleaseLookup(status="unavailable", message="provider changed"),
        ]
        with patch.object(network, "logos_reuse_download", return_value=accepted) as acquire:
            installer.select_recommended_wine(self.app)
        self.app.conf._network.wine_appimage_release.assert_called_once()
        self.app.conf._network.wine_appimage_versions.assert_not_called()
        acquire.assert_called_once_with(selected.download_url, selected.name,
                                        self.app.conf.installer_binary_dir, self.app,
                                        kind="appimage", digest=selected.digest, size=selected.size)
        self.assertEqual(Path(self.app.conf._raw.wine_binary).name, selected.name)

    def test_stable_update_preserves_a_newer_selected_development_runtime(self):
        self.app.conf.wine_binary_code = "AppImage"
        stable = releases.SoftwareReleaseInfo("11.0", "https://example.invalid/stable.AppImage")
        self.app.conf._network.wine_appimage_release.return_value = releases.ReleaseLookup(stable, "ok")
        before = self.state()
        with (patch.object(wine, "get_wine_release", return_value=(wine.WineRelease(11, 20, "devel"), "yes")),
              patch.object(network, "download_recommended_appimage") as acquire,
              patch.object(runtime, "activate") as activate):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "Keep selected Wine 11.20"):
                utils.update_to_latest_recommended_appimage(self.app)
        acquire.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_stable_update_keeps_selection_when_provider_or_runtime_is_unverified(self):
        self.app.conf.wine_binary_code = "AppImage"
        stable = releases.SoftwareReleaseInfo("11.0", "https://example.invalid/stable.AppImage")
        before = self.state()
        for lookup, current in (
            (releases.ReleaseLookup(stable, "offline", message="Provider offline", stale=True),
             (wine.WineRelease(10, 8, "staging"), "yes")),
            (releases.ReleaseLookup(stable, "ok"), (None, "Runtime version is unknown")),
        ):
            self.app.conf._network.wine_appimage_release.return_value = lookup
            with (self.subTest(lookup=lookup, current=current),
                  patch.object(wine, "get_wine_release", return_value=current),
                  patch.object(network, "download_recommended_appimage") as acquire,
                  patch.object(runtime, "activate") as activate):
                with self.assertRaises(maintenance.MaintenanceError):
                    utils.update_to_latest_recommended_appimage(self.app)
            acquire.assert_not_called()
            activate.assert_not_called()
            self.assertEqual(self.state(), before)

    def test_recommendation_failures_keep_saved_and_effective_selection(self):
        selected = self.recommended()
        accepted = self.accepted("appimage", appimage(), selected.name)
        self.app.conf._overrides.runtime_selection = {"wine_binary": "explicit prior runtime"}
        override = dict(self.app.conf._overrides.runtime_selection)
        before = self.state()
        raw = self.app.conf._raw.wine_binary
        for failure in (artifacts.ArtifactError("acquisition"), UserExitedFromAsk("cancelled")):
            with self.subTest(failure=failure), patch.object(network, "download_recommended_appimage",
                                                            side_effect=failure):
                with self.assertRaises(type(failure)):
                    installer.select_recommended_wine(self.app)
            self.assertEqual(self.state(), before)
            self.assertEqual(self.app.conf._raw.wine_binary, raw)
            self.assertEqual(self.app.conf._overrides.runtime_selection, override)

        with (patch.object(network, "download_recommended_appimage", return_value=accepted),
              patch.object(wine, "require_compatible", side_effect=maintenance.MaintenanceError("incompatible"))):
            with self.assertRaises(maintenance.MaintenanceError):
                installer.select_recommended_wine(self.app)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.app.conf._raw.wine_binary, raw)
        self.assertEqual(self.app.conf._overrides.runtime_selection, override)

        original = runtime.os.replace
        config = Path(self.app.conf._overrides.config_path)
        failed = False

        def replace(source, target, **kwargs):
            nonlocal failed
            if Path(target) == config and not failed:
                failed = True
                raise OSError("intended activation publication failure")
            return original(source, target, **kwargs)

        with (patch.object(network, "download_recommended_appimage", return_value=accepted),
              patch.object(runtime.os, "replace", side_effect=replace)):
            with self.assertRaises(artifacts.ArtifactError):
                installer.select_recommended_wine(self.app)
        self.assertTrue(failed)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.app.conf._raw.wine_binary, raw)
        self.assertEqual(self.app.conf._overrides.runtime_selection, override)

    def test_icu_acquisition_pins_version_url_digest_and_size(self):
        conf = self.app.conf
        selected = network.SoftwareReleaseInfo("72.1-custom+4", "https://example.invalid/old.tar.gz", size=123)
        refreshed = network.SoftwareReleaseInfo("75.1", "https://example.invalid/new.tar.gz", size=456)
        conf._network.icu_latest_version.side_effect = [selected, refreshed]
        accepted = SimpleNamespace(check=lambda: self.root / "accepted.tar.gz")

        def extracted(source, staging):
            self.assertEqual(source, self.root / "accepted.tar.gz")
            (staging / "windows").mkdir(parents=True)

        with (patch.object(network, "logos_reuse_download", return_value=accepted) as acquire,
              patch.object(utils, "untar_file", side_effect=extracted), patch.object(wine, "copy_icu_files") as copy):
            wine.enforce_icu_data_files(self.app)
        conf._network.icu_latest_version.assert_called_once()
        acquire.assert_called_once_with(selected.download_url, "icu-win-72.1-custom+4.tar.gz", conf.download_dir,
                                        app=self.app, kind="icu", digest=None, size=123)
        copy.assert_called_once()

    def test_actual_msi_chain_and_argv_ignore_stale_installer_paths(self):
        conf = self.app.conf
        target = Path(conf.install_dir) / "data/Logos.msi"
        target.write_bytes(b"stale installed MSI")
        home = self.root / "HOME"
        home.mkdir()
        (home / "Logos.msi").write_bytes(b"stale HOME MSI")
        self.prepare_msi_drive()
        prefix = Path(conf.wine_prefix)
        (prefix / "system.reg").write_bytes(b"fixture initialized prefix")
        process = Mock(returncode=0)
        content = product_msi(release=conf.faithlife_product_release)
        consumed_paths = []

        def consume(*args, **kwargs):
            consumed = self.staged_msi_path(kwargs["exe_args"][1])
            self.assertEqual(consumed.read_bytes(), content)
            consumed_paths.append(consumed)
            return process

        with (
            patch.object(installer, "ensure_winetricks_executable"),
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(content)),
            patch.object(Path, "home", return_value=home),
            patch.object(wine, "disable_winemenubuilder"), patch.object(wine, "set_renderer"),
            patch.object(wine, "set_fontsmoothing_to_rgb"), patch.object(wine, "install_fonts"),
            patch.object(wine, "enforce_icu_data_files"),
            patch.object(wine, "run_wine_during_install", side_effect=consume) as run,
        ):
            installer.ensure_product_installed(self.app)
        run.assert_called_once()
        consumed, = consumed_paths
        self.assertFalse(consumed.parent.exists())
        self.assertNotEqual(consumed, target)
        self.assertEqual(target.read_bytes(), b"stale installed MSI")
        self.assertEqual((home / "Logos.msi").read_bytes(), b"stale HOME MSI")

    def test_rejected_msi_never_reaches_wine(self):
        with (
            patch.object(installer, "ensure_winetricks_executable"),
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(b"HTML")),
            patch.object(wine, "install_msi") as install,
        ):
            with self.assertRaises(artifacts.ArtifactError):
                installer.ensure_product_installed(self.app)
        install.assert_not_called()

    def test_every_activation_boundary_preserves_prior_selection(self):
        accepted = self.accepted("appimage", appimage(b"new"), "old.AppImage")
        before = self.state()
        raw = self.app.conf._raw.wine_binary
        original = runtime.os.replace
        links, configs = runtime._targets(self.app)
        publications = set((*links, *configs))
        for failure in range(1, len(publications) + 1):
            calls = []

            def replace(source, target, **kwargs):
                if Path(target) in publications:
                    calls.append(target)
                    if len(calls) == failure:
                        raise OSError("intended activation publication failure")
                return original(source, target, **kwargs)

            with self.subTest(boundary=failure), patch.object(runtime.os, "replace", side_effect=replace):
                with self.assertRaises(artifacts.ArtifactError):
                    runtime.activate(self.app, accepted)
            self.assertEqual(self.state(), before)
            self.assertEqual(self.app.conf._raw.wine_binary, raw)
            self.assertEqual((Path(self.app.conf.installer_binary_dir) / "old.AppImage").read_bytes(), appimage(b"old"))
            self.assertEqual(accepted.check().read_bytes(), appimage(b"new"))

    def test_rollback_failure_blocks_use_then_recovery_is_repeatable(self):
        accepted = self.accepted("appimage", appimage(b"new"), "wine.AppImage")
        before = self.state()
        original = runtime._replace_bytes

        def failure(path, content, mode=0o600):
            if path.name != "runtime.json":
                raise OSError("config and rollback failed")
            return original(path, content, mode)

        with patch.object(runtime, "_replace_bytes", side_effect=failure):
            with self.assertRaises(maintenance.RecoveryRequired):
                runtime.activate(self.app, accepted)
        with self.assertRaises(maintenance.RecoveryRequired):
            runtime.executable(accepted.path, self.app)
        runtime.recover(self.app)
        runtime.recover(self.app)
        self.assertEqual(self.state(), before)
        self.assertEqual(runtime.executable(accepted.path, self.app), str(accepted.path))

    def test_activation_uses_accepted_generation_and_matching_companions(self):
        accepted = self.accepted("appimage", appimage(), "wine.AppImage")
        runtime.activate(self.app, accepted)
        conf = self.app.conf
        self.assertEqual(Path(conf._raw.wine_binary), accepted.path.relative_to(conf.install_dir))
        for name in artifacts.COMPANIONS:
            link = Path(conf.installer_binary_dir) / name
            self.assertEqual(runtime.executable(link, self.app), str(accepted.path.parent / name))
        for config in runtime._targets(self.app)[1]:
            self.assertEqual(json.loads(config.read_bytes())["wine_binary"], conf._raw.wine_binary)

    def test_external_same_basename_selection_is_not_replaced_by_recommended(self):
        source = self.root / "wine.AppImage"
        source.write_bytes(appimage(b"explicit external"))
        with patch.object(network, "download_recommended_appimage", side_effect=AssertionError("wrong source")):
            utils.set_appimage_symlink(self.app, source=source)
        selected = Path(self.app.conf.install_dir) / self.app.conf._raw.wine_binary
        self.assertEqual(selected.read_bytes(), source.read_bytes())

    def test_custom_winetricks_ignores_stale_executable_and_consumes_accepted_archive(self):
        target = Path(self.app.conf.installer_binary_dir) / "winetricks"
        target.write_bytes(b"unaccepted legacy script")
        script = b"#!/bin/sh\n# offline fixture\nexit 0\n"
        with (
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(winetricks_zip(script))),
        ):
            executable = system.ensure_winetricks(self.app, version="20250102")
        self.assertEqual(Path(executable).read_bytes(), script)
        self.assertEqual(target.resolve(), Path(executable))

    def test_default_winetricks_pin_applies_to_acquisition_and_cached_generation(self):
        script = b"#!/bin/sh\n# synthetic default fixture, never executed\nexit 0\n"
        digest = hashlib.sha256(script).hexdigest()
        _, configs = runtime._targets(self.app)
        config_before = [runtime._snapshot(path) for path in configs]
        raw = self.app.conf._raw._as_dict()
        with (
            patch.object(constants, "WINETRICKS_SHA256", digest),
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(script)) as request,
            patch.object(network, "logos_reuse_download", wraps=network.logos_reuse_download) as acquire,
        ):
            selected = Path(system.ensure_winetricks(self.app))
        accepted = artifacts.installed(selected, "winetricks")
        self.assertEqual(accepted.sha256, digest)
        url = (f"https://raw.githubusercontent.com/Winetricks/winetricks/{constants.WINETRICKS_VERSION}"
               "/src/winetricks")
        self.assertEqual(acquire.call_args.args[0], url)
        self.assertEqual(acquire.call_args.kwargs["digest"].value, digest)
        self.assertEqual(acquire.call_args.kwargs["kind"], "winetricks")
        self.assertEqual(request.call_args.args[0], url)
        target = Path(self.app.conf.installer_binary_dir) / "winetricks"
        before = os.readlink(target)
        with (patch.object(constants, "WINETRICKS_SHA256", digest),
              patch.object(network, "logos_reuse_download", side_effect=AssertionError("Cache should be reused"))):
            self.assertEqual(Path(system.ensure_winetricks(self.app)), selected)

        with (
            patch.object(constants, "WINETRICKS_SHA256", "0" * 64),
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(script)),
        ):
            with self.assertRaises(artifacts.ArtifactError):
                system.ensure_winetricks(self.app)
        self.assertEqual(os.readlink(target), before)
        self.assertEqual(selected.read_bytes(), script)
        self.assertEqual([runtime._snapshot(path) for path in configs], config_before)
        self.assertEqual(self.app.conf._raw._as_dict(), raw)

    def test_appimage_winetricks_uses_the_official_standalone_pin(self):
        accepted = self.accepted("appimage", appimage(), "wine.AppImage")
        runtime.activate(self.app, accepted)
        self.app.conf.wine_binary_code = "AppImage"
        self.app.conf.wine_appimage_path = accepted.path
        script = b"#!/bin/sh\n# fixture standalone script\nexit 0\n"
        with (patch.object(constants, "WINETRICKS_SHA256", hashlib.sha256(script).hexdigest()),
              patch.object(artifacts.requests, "head", return_value=response()),
              patch.object(artifacts.requests, "get", return_value=response(script))):
            selected = Path(system.ensure_winetricks(self.app))
        self.assertEqual(selected.name, "winetricks")
        self.assertNotEqual(selected.resolve(), accepted.path)
        self.assertEqual(selected.read_bytes(), script)
        self.assertEqual(artifacts.installed(selected, "winetricks").spec.kind, "winetricks")
        self.assertEqual((Path(self.app.conf.installer_binary_dir) / "winetricks").resolve(), selected)

    def test_default_winetricks_rejects_wrong_script_against_the_production_pin(self):
        target = Path(self.app.conf.installer_binary_dir) / "winetricks"
        original = b"previous standalone selection"
        target.write_bytes(original)
        with (
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(b"#!/bin/sh\n# wrong release\nexit 0\n")),
        ):
            with self.assertRaisesRegex(artifacts.ArtifactError, "digest mismatch"):
                system.ensure_winetricks(self.app)
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_rejected_icu_never_reaches_extraction_or_prefix_copy(self):
        with (
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(b"not gzip")),
            patch.object(utils, "untar_file") as extract,
            patch.object(wine, "copy_icu_files") as copy,
        ):
            with self.assertRaises(artifacts.ArtifactError):
                wine.enforce_icu_data_files(self.app)
        extract.assert_not_called()
        copy.assert_not_called()

    def test_invalid_runtime_never_spawns_even_a_version_probe(self):
        source = self.root / "broken.AppImage"
        source.write_bytes(b"bad")
        with patch.object(wine.subprocess, "check_output") as probe:
            self.assertIsNone(wine.get_wine_release(str(source))[0])
        probe.assert_not_called()

    def test_legacy_companions_preserve_argv0_and_environment_without_download(self):
        conf = self.app.conf
        source = Path(conf.wine_binary)
        source.chmod(0o500)
        link = source.parent / "wineserver"
        link.symlink_to(source.name)
        with (
            patch.object(artifacts.requests, "get", side_effect=AssertionError("No launch download")),
            patch.object(wine, "get_wine_env", return_value={}),
        ):
            command, env = wine._wine_invocation(self.app, str(link), None)
        self.assertEqual(Path(command[0]).name, "wineserver")
        self.assertIn(".artifacts", Path(command[0]).parts)
        self.assertEqual(env["WINESERVER"], command[0])
        self.assertEqual(Path(env["WINELOADER"]), Path(command[0]).resolve())
        self.assertEqual(source.read_bytes(), appimage(b"old"))

    def test_registry_diagnostics_refuse_recovery_and_changed_managed_bytes(self):
        accepted = self.accepted("appimage", appimage(), "wine.AppImage")
        self.app.conf.wine64_binary = str(accepted.path)
        self.app.conf._wine_output_encoding = None
        journal = runtime.selection_state(Path(self.app.conf.install_dir)) / "runtime.json"
        journal.write_bytes(b"{}")
        with patch.object(wine.system, "run_command") as run:
            with self.assertRaises(maintenance.RecoveryRequired):
                wine.get_registry_value("fixture", "value", self.app)
            run.assert_not_called()
        journal.unlink()
        accepted.path.chmod(0o700)
        accepted.path.write_bytes(b"invalid changed AppImage")
        with patch.object(wine.system, "run_command") as run:
            with self.assertRaises(artifacts.ArtifactError):
                wine.get_registry_value("fixture", "value", self.app)
            run.assert_not_called()

    def test_activation_excludes_peer_between_recovery_and_new_journal(self):
        accepted = self.accepted("appimage", appimage(), "wine.AppImage")
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        results = context.Queue()

        def peer():
            ready.wait(5)
            try:
                with runtime.activation_guard(self.app, exclusive=True):
                    results.put("entered")
            except maintenance.MaintenanceBusy:
                results.put("excluded")

        process = context.Process(target=peer)
        process.start()
        original = runtime._recover_locked

        def recover(app, selected):
            original(app, selected)
            ready.set()
            self.assertEqual(results.get(timeout=5), "excluded")

        try:
            with patch.object(runtime, "_recover_locked", side_effect=recover):
                runtime.activate(self.app, accepted)
        finally:
            ready.set()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()

    def test_fresh_config_recovers_each_interrupted_config_publication(self):
        accepted = self.accepted("appimage", appimage(b"new"), "wine.AppImage")
        before = self.state()
        conf = self.app.conf
        configs = runtime._targets(self.app)[1]
        context = multiprocessing.get_context("fork")
        for boundary in configs:
            def interrupted():
                original = runtime._replace_bytes

                def write(path, content, mode=0o600):
                    original(path, content, mode)
                    if path == boundary:
                        os._exit(32)

                with patch.object(runtime, "_replace_bytes", side_effect=write):
                    runtime.activate(self.app, accepted)

            process = context.Process(target=interrupted)
            process.start()
            process.join(5)
            self.assertEqual(process.exitcode, 32)
            loaded = PersistentConfiguration.load_from_path(conf._overrides.config_path)
            with self.assertRaises(maintenance.RecoveryRequired):
                loaded.write_config(checked=True, config_file_path=conf._overrides.config_path)
            configuration = object.__new__(Config)
            with patch.object(Config, "_create_network_requests_with_hook", return_value=Mock()):
                overrides = EphemeralConfiguration.load_from_path(conf._overrides.config_path)
                Config.__init__(configuration, overrides, self.app)
            self.assertTrue(runtime.pending(conf.install_dir))
            self.app.conf = configuration
            runtime.recover(self.app)
            self.app.conf = conf
            self.assertEqual(configuration._raw.wine_binary, conf._raw.wine_binary)
            self.assertEqual(self.state(), before)

    def test_checked_lock_failure_restores_logging_cache_and_retry_writes(self):
        conf = object.__new__(Config)
        conf._raw = self.app.conf._raw
        conf._raw.faithlife_product_logging = False
        conf._overrides = self.app.conf._overrides
        conf.app = self.app
        original = runtime.os.open

        def denied(path, *args, **kwargs):
            if isinstance(path, Path) and path.name == "configuration.lock":
                raise PermissionError("intended configuration lock failure")
            return original(path, *args, **kwargs)

        with patch.object(runtime.os, "open", side_effect=denied):
            with self.assertRaises(maintenance.MaintenanceError):
                conf.faithlife_product_logging = True
        self.assertFalse(conf._raw.faithlife_product_logging)
        conf.faithlife_product_logging = True
        saved = json.loads(Path(conf._overrides.config_path).read_bytes())
        self.assertTrue(saved["faithlife_product_logging"])

    def test_real_msi_reader_and_competing_publisher_keep_consumption_identity(self):
        self.prepare_msi_drive()
        accepted = self.accepted("msi", msi(), "Logos.msi")
        gate, captured = self.root / "read-now", self.root / "read-hash"
        started = Event()
        errors = []
        consumed_paths = []
        reader_code = (
            "from pathlib import Path\nimport hashlib,sys,time\n"
            "source,gate,capture=map(Path,sys.argv[1:])\n"
            "deadline=time.monotonic()+10\n"
            "while not gate.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            "assert gate.exists()\n"
            "capture.write_text(hashlib.sha256(source.read_bytes()).hexdigest())\n"
        )

        def spawn(*args, **kwargs):
            consumed = self.staged_msi_path(kwargs["exe_args"][1])
            self.assertNotEqual(consumed, accepted.path)
            self.assertEqual(consumed.read_bytes(), accepted.check().read_bytes())
            consumed_paths.append(consumed)
            process = subprocess.Popen([sys.executable, "-B", "-c", reader_code,
                                        str(consumed), str(gate), str(captured)])
            started.set()
            return process

        def consume():
            try:
                wine.install_msi(self.app, accepted, target_release="38.0.0.0")
            except BaseException as error:
                errors.append(error)

        with patch.object(wine, "run_wine_during_install", side_effect=spawn):
            thread = Thread(target=consume)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                source = self.root / "peer.msi"
                source.write_bytes(msi().replace(b"M04 fixture", b"M05 fixture"))
                publisher = (
                    "import sys\nfrom pathlib import Path\n"
                    f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
                    "from logos_desktop import artifacts,constants\n"
                    f"constants.STATE_DIR={constants.STATE_DIR!r}\n"
                    f"source=Path({str(source)!r})\n"
                    f"root=Path({self.app.conf.installer_binary_dir!r})\n"
                    "accepted=artifacts.import_local(source,root,artifacts.ArtifactSpec('local:peer','Logos.msi','msi'))\n"
                    "print(accepted.path)\n"
                )
                other = subprocess.check_output([sys.executable, "-B", "-c", publisher], text=True, timeout=5).strip()
                self.assertNotEqual(Path(other), accepted.path)
                with self.assertRaises(maintenance.MaintenanceBusy), maintenance.artifact_use(exclusive=True):
                    self.fail("Removal entered during actual consumer wait")
            finally:
                gate.write_bytes(b"read accepted path")
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(captured.read_text(), accepted.sha256)
        self.assertEqual(accepted.check().read_bytes(), msi())
        consumed, = consumed_paths
        self.assertFalse(consumed.parent.exists())
        with maintenance.artifact_use(exclusive=True):
            pass

    def test_appimage_to_system_winetricks_transition_preserves_alias_on_failure(self):
        accepted = self.accepted("appimage", appimage(), "wine.AppImage")
        runtime.activate(self.app, accepted)
        target = Path(self.app.conf.installer_binary_dir) / "winetricks"
        before = os.readlink(target)
        self.app.conf.wine_binary_code = "System"
        self.app.conf.wine_appimage_path = None
        with (
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(b"error", 500)),
        ):
            with self.assertRaises(artifacts.ArtifactError):
                system.ensure_winetricks(self.app)
        self.assertEqual(os.readlink(target), before)
        self.assertEqual(target.resolve(), accepted.path)
        script = b"#!/bin/sh\nexit 0\n"
        with (
            patch.object(constants, "WINETRICKS_SHA256", hashlib.sha256(script).hexdigest()),
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(script)),
        ):
            selected = Path(system.ensure_winetricks(self.app))
        self.assertEqual(selected.read_bytes(), script)
        self.assertEqual(target.resolve(), selected)
        self.assertEqual(accepted.check().read_bytes(), appimage())

    def test_explicit_local_selection_never_reads_recommended_release_properties(self):
        conf = object.__new__(Config)
        conf._raw = self.app.conf._raw
        conf._overrides = self.app.conf._overrides
        conf._overrides.installer_binary_dir = None
        conf._overrides.wine_appimage_link_file_name = None
        conf._network = Mock()
        conf._network.wine_appimage_versions.side_effect = AssertionError("Local selection must stay offline")
        conf.app = self.app
        self.app.conf = conf
        source = self.root / "wine.AppImage"
        source.write_bytes(appimage(b"offline choice"))
        utils.set_appimage_symlink(self.app, source=source)
        self.assertEqual(Path(conf.wine_binary).read_bytes(), source.read_bytes())
        conf._network.wine_appimage_versions.assert_not_called()

    def test_pending_runtime_refuses_uninstall_then_recovered_location_can_reinstall(self):
        accepted = self.accepted("appimage", appimage(), "wine.AppImage")
        original = runtime._replace_bytes

        def denied(path, content, mode=0o600):
            if path.name != "runtime.json":
                raise PermissionError("activation and rollback persistence refused")
            return original(path, content, mode)

        with patch.object(runtime, "_replace_bytes", side_effect=denied):
            with self.assertRaises(maintenance.RecoveryRequired):
                runtime.activate(self.app, accepted)
        before = self.state()
        self.app.approve = Mock()
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "recovery"):
            control.uninstall(self.app)
        self.app.approve.assert_not_called()
        self.assertEqual(self.state(), before)
        runtime.recover(self.app)
        install = Path(self.app.conf.install_dir)
        Path(self.app.conf.wine_prefix).mkdir(parents=True)
        self.app.approve.side_effect = [False, True]
        control.uninstall(self.app)
        self.assertFalse(install.exists())
        replacement = PersistentConfiguration(install_dir=str(install), faithlife_product="Logos")
        replacement.write_config(checked=True, config_file_path=self.app.conf._overrides.config_path)
        saved = json.loads(Path(self.app.conf._overrides.config_path).read_bytes())
        self.assertEqual(saved["install_dir"], str(install))
        self.assertFalse((runtime.selection_state(install) / "runtime.json").exists())
