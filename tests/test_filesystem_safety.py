import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ou_dedetai import constants, control, installer, maintenance, network, paths, utils, wine
from ou_dedetai.config import PersistentConfiguration


class TestUninstallBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / "Logos Workspace 文"
        for child in ("data/bin", "data/wine64_bottle"):
            (self.install / child).mkdir(parents=True)
        portable = {"install_dir": str(self.install)}
        (self.install / f"{constants.BINARY_NAME}.json").write_text(json.dumps(portable))
        self.config = self.root / "selected.json"
        self.config.write_text(json.dumps(portable))
        self.base = self.root / "shared cache"
        self.cache = self.base / "logos-desktop"
        self.state = self.root / "state/FaithLife-Community"
        self.state.mkdir(parents=True)
        overrides = SimpleNamespace(config_path=str(self.config), install_dir=None)
        self.app = SimpleNamespace(
            conf=SimpleNamespace(_raw=PersistentConfiguration(install_dir=str(self.install)), _overrides=overrides),
            approve=Mock(return_value=True), status=Mock(), _config_updated_event=Mock(),
        )
        self.addCleanup(patch.stopall)
        patch.multiple(
            constants, CACHE_BASE=self.base, CACHE_DIR=str(self.cache),
            NETWORK_CACHE_PATH=str(self.cache / "network.json"), STATE_DIR=str(self.state),
            CONFIG_DIR=str(self.root / "config/FaithLife-Community"), XDG_DATA_HOME=str(self.root / "data"),
        ).start()

    def test_valid_cleanup_preserves_shared_sentinels_and_link_targets(self):
        paths.ensure_cache_directory(self.cache, self.base)
        unrelated = self.base / "unrelated-app"
        unrelated.write_bytes(b"unrelated cache")
        unknown_log = self.state / "unrelated.log"
        unknown_log.write_bytes(b"unrelated log")
        known_log = self.state / "wine.log"
        known_log.write_bytes(b"owned log")
        (self.install / "external-link").symlink_to(unrelated)
        network.CachedRequests()._write()

        control.uninstall(self.app)

        self.assertFalse(self.install.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.cache.exists())
        self.assertFalse(known_log.exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated cache")
        self.assertEqual(unknown_log.read_bytes(), b"unrelated log")
        self.app.status.assert_called_with("Uninstalled")

    def test_new_only_portable_installation_can_be_uninstalled(self):
        legacy = self.install / f"{constants.BINARY_NAME}.json"
        portable = self.install / constants.PORTABLE_CONFIG_NAME
        legacy.rename(portable)
        self.app.conf._overrides.config_path = str(portable)
        self.app.approve.side_effect = [False, True]

        self.assertTrue(control.uninstall(self.app))

        self.assertFalse(self.install.exists())
        self.assertTrue(self.config.exists())
        self.app.status.assert_called_with("Uninstalled")

    def test_mismatched_portable_metadata_preserves_all_targets(self):
        original_config = self.config.read_bytes()
        legacy = self.install / f"{constants.BINARY_NAME}.json"
        matching_legacy = legacy.read_bytes()
        sentinel = self.install / "data/wine64_bottle/library"
        sentinel.write_bytes(b"library sentinel")

        for filename in (constants.PORTABLE_CONFIG_NAME, legacy.name):
            portable = self.install / filename
            with self.subTest(filename=filename):
                portable.write_text(json.dumps({"install_dir": str(self.root / "other")}))
                with self.assertRaisesRegex(ValueError, "ownership is not established"):
                    control.uninstall(self.app)

                self.assertEqual(self.config.read_bytes(), original_config)
                self.assertEqual(sentinel.read_bytes(), b"library sentinel")
                self.assertTrue(portable.exists())
                self.app.approve.assert_not_called()

                if portable != legacy:
                    self.assertEqual(legacy.read_bytes(), matching_legacy)
                    portable.unlink()

    def test_writer_started_during_confirmation_prevents_any_removal(self):
        processes = []
        prefix = self.install / "data/wine64_bottle"
        original = self.config.read_bytes()

        def approve(question, context):
            if "Are you sure" not in question:
                return False
            program = "import time\nprint('ready',flush=True)\ntime.sleep(20)\n"
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", program, str(prefix)],
                stdout=subprocess.PIPE, text=True, env=dict(os.environ, WINEPREFIX=str(prefix)),
            )
            processes.append(process)
            self.assertEqual(process.stdout.readline().strip(), "ready")
            return True

        self.app.approve.side_effect = approve
        try:
            with self.assertRaises(maintenance.MaintenanceBusy):
                control.uninstall(self.app)
            self.assertEqual(self.config.read_bytes(), original)
            self.assertTrue(self.install.exists())
            self.assertFalse(list(self.root.glob(".logos-desktop-uninstall-*")))
        finally:
            for process in processes:
                process.terminate()
                process.wait(timeout=5)
                process.stdout.close()

    def test_invalid_cache_and_installation_fail_before_any_removal(self):
        original = self.config.read_bytes()
        for scenario in ("unmarked", "symlink", "nested", "bad-portable", "bad-config"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                cache_base = root / "cache"
                cache_base.mkdir()
                cache = cache_base / "logos-desktop"
                portable = self.install / f"{constants.BINARY_NAME}.json"
                original_portable = portable.read_bytes()
                if scenario == "symlink":
                    cache.symlink_to(self.install, target_is_directory=True)
                elif scenario == "nested":
                    cache_base = self.install / "nested"
                    cache = paths.ensure_cache_directory(cache_base / "logos-desktop", cache_base)
                elif scenario == "bad-portable":
                    portable.write_text('{"install_dir": "/"}')
                elif scenario == "bad-config":
                    self.app.conf._overrides.config_path = str(self.install)
                else:
                    cache.mkdir()
                    (cache / "sentinel").write_bytes(b"unclaimed")
                try:
                    with patch.multiple(constants, CACHE_BASE=cache_base, CACHE_DIR=str(cache)):
                        with self.assertRaises((ValueError, OSError)):
                            control.uninstall(self.app)
                    self.assertEqual(self.config.read_bytes(), original)
                    self.assertTrue(self.install.is_dir())
                finally:
                    portable.write_bytes(original_portable)
                    self.app.conf._overrides.config_path = str(self.config)
                    if scenario == "nested":
                        import shutil

                        shutil.rmtree(cache_base)

    def test_protected_roots_and_foreign_ownership_are_refused(self):
        for candidate in (Path("/"), Path.home(), self.base, Path("relative"), self.root / "../ambiguous"):
            with self.subTest(candidate=candidate), self.assertRaises((ValueError, OSError)):
                paths.validate_removal_path(candidate, (self.base,))
        with patch.object(paths.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ValueError):
                paths.validate_removal_path(self.install)
        self.assertTrue(self.config.is_file())

    def test_contained_config_is_removed_once_and_refusal_changes_nothing(self):
        self.app.conf._overrides.config_path = str(self.install / f"{constants.BINARY_NAME}.json")
        self.app.approve.side_effect = [False, False]
        control.uninstall(self.app)
        self.assertTrue(self.install.exists())
        self.app.approve.side_effect = [False, True]
        control.uninstall(self.app)
        self.assertFalse(self.install.exists())
        self.assertTrue(self.config.exists())

    def test_confirmation_substitution_and_removal_error_never_report_success(self):
        def substitute(question, context):
            if "Are you sure" in question:
                self.install.rename(self.root / "saved")
                self.install.symlink_to(self.root / "saved", target_is_directory=True)
            return "Are you sure" in question

        self.app.approve.side_effect = substitute
        with self.assertRaises(ValueError):
            control.uninstall(self.app)
        self.assertTrue(self.config.exists())
        self.install.unlink()
        (self.root / "saved").rename(self.install)
        self.app.approve.side_effect = [False, True]
        with patch.object(paths.shutil, "rmtree", side_effect=PermissionError("fixture failure")):
            with self.assertRaises(PermissionError):
                control.uninstall(self.app)
        self.assertNotIn(unittest.mock.call("Uninstalled"), self.app.status.call_args_list)

    def test_metadata_replacement_does_not_follow_a_foreign_file_link(self):
        paths.ensure_cache_directory(self.cache, self.base)
        sentinel = self.root / "outside"
        sentinel.write_bytes(b"outside")
        (self.cache / "network.json").symlink_to(sentinel)
        network.CachedRequests()._write()
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertFalse((self.cache / "network.json").is_symlink())

    def test_installation_cannot_contain_shared_roots_even_without_optional_cleanup(self):
        shared = self.install / "shared/data"
        shared.mkdir(parents=True)
        sentinel = shared / "unrelated"
        sentinel.write_bytes(b"shared user data")
        self.app.approve.return_value = False
        with patch.object(constants, "XDG_DATA_HOME", str(shared)):
            with self.assertRaises(ValueError):
                control.uninstall(self.app)
        self.assertEqual(sentinel.read_bytes(), b"shared user data")
        self.assertTrue(self.config.is_file())

    def test_ancestor_redirection_cannot_redirect_descriptor_bound_deletion(self):
        paths.ensure_cache_directory(self.cache, self.base)
        rename = os.rename
        saved_base = self.root / "saved-cache-base"

        def redirect(source, destination, **kwargs):
            if source == "logos-desktop":
                rename(self.base, saved_base)
                self.cache.mkdir(parents=True)
                (self.cache / "unrelated").write_bytes(b"replacement tree")
            return rename(source, destination, **kwargs)

        with patch.object(paths.os, "rename", side_effect=redirect):
            control.uninstall(self.app)
        self.assertEqual((self.cache / "unrelated").read_bytes(), b"replacement tree")
        self.assertEqual(list(saved_base.iterdir()), [])

    def test_regular_directory_substitution_is_restored_without_deleting_either_tree(self):
        rename = os.rename
        saved = self.root / "saved-install"

        def substitute(source, destination, **kwargs):
            if source == self.install.name:
                rename(self.install, saved)
                self.install.mkdir()
                (self.install / "unrelated").write_bytes(b"replacement tree")
            return rename(source, destination, **kwargs)

        self.app.approve.side_effect = [False, True]
        with patch.object(paths.os, "rename", side_effect=substitute):
            with self.assertRaises(ValueError):
                control.uninstall(self.app)
        self.assertTrue((saved / "data/bin").is_dir())
        self.assertEqual((self.install / "unrelated").read_bytes(), b"replacement tree")
        self.assertTrue(self.config.is_file())

    def test_same_config_instance_reinitializes_removed_cache_for_reinstall(self):
        from tests.test_runtime_paths import fixture_config

        config = fixture_config(self.install)
        config._overrides.config_path = str(self.config)
        config._network = Mock()
        config._network.wine_appimage_versions.return_value = network.GithubSoftwareReleasesInfo(
            network.SoftwareReleaseInfo("1", "https://example.invalid/stable.AppImage", name="stable.AppImage"), None,
        )
        self.app.conf = config
        self.assertEqual(config.download_dir, str(self.cache))
        config._wine_user = "stale-user"
        control.uninstall(self.app)
        self.assertFalse(self.cache.exists())
        self.assertIsNone(config._wine_user)

        config._raw = PersistentConfiguration(
            install_dir=str(self.install), faithlife_product="Logos", faithlife_product_version="10",
        )
        config.wine_binary = constants.WINE_RECOMMENDED_SIGIL

        def transfer(url, filename, directory, **kwargs):
            from ou_dedetai import artifacts
            from tests.artifact_fixtures import appimage

            source = Path(config.download_dir) / filename
            source.write_bytes(appimage())
            return artifacts.import_local(source, Path(directory), artifacts.ArtifactSpec(url, filename, "appimage"))

        with (
            patch.object(installer, "ensure_sys_deps"),
            patch.object(installer, "check_system_compatibility"),
            patch.object(wine, "require_compatible"),
            patch.object(installer.network, "logos_reuse_download", side_effect=transfer),
        ):
            self.app.installer_step = self.app.installer_step_count = 0
            self.app.checkpoint = Mock()
            installer.ensure_appimage_download(self.app)
        from tests.artifact_fixtures import appimage

        self.assertEqual((self.cache / "stable.AppImage").read_bytes(), appimage())
        paths.validate_cache_directory(self.cache, self.base, require_marker=True)

    def test_failed_removal_preserves_replacement_and_exposes_recovery_tree(self):
        self.app.approve.side_effect = [False, True]

        def fail_after_replacement(*args, **kwargs):
            self.install.mkdir()
            (self.install / "replacement").write_bytes(b"new unrelated tree")
            raise OSError("fixture failure")

        with patch.object(paths.shutil, "rmtree", side_effect=fail_after_replacement):
            with self.assertRaisesRegex(RuntimeError, "Preserved entries need recovery"):
                control.uninstall(self.app)
        recovery = list(self.root.glob(".logos-desktop-uninstall-*/target/data/bin"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual((self.install / "replacement").read_bytes(), b"new unrelated tree")
        self.assertTrue(self.config.is_file())

    def test_quarantine_replacement_or_permission_change_is_rejected_before_claiming(self):
        original_open = os.open
        for scenario in ("replacement", "permissions"):
            with self.subTest(scenario=scenario):
                self.app.approve.side_effect = [False, True]
                substituted = []

                def substitute(name, flags, *args, **kwargs):
                    if isinstance(name, str) and name.startswith(".logos-desktop-uninstall-"):
                        directory = self.root / name
                        if scenario == "replacement":
                            directory.rename(self.root / (name + "-saved"))
                            directory.mkdir(mode=0o777)
                        else:
                            directory.chmod(0o777)
                        (directory / "unrelated").write_bytes(b"untrusted quarantine")
                        substituted.append(directory)
                    return original_open(name, flags, *args, **kwargs)

                with patch.object(paths.os, "open", side_effect=substitute):
                    with self.assertRaisesRegex(ValueError, "quarantine"):
                        control.uninstall(self.app)
                self.assertEqual(len(substituted), 1)
                self.assertEqual((substituted[0] / "unrelated").read_bytes(), b"untrusted quarantine")
                self.assertTrue((self.install / "data/bin").is_dir())
                self.assertTrue(self.config.is_file())

    def test_nonsticky_shared_parent_is_rejected_before_claiming(self):
        self.app.approve.side_effect = [False, True]
        self.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, "untrusted parent"):
                control.uninstall(self.app)
            self.assertTrue((self.install / "data/bin").is_dir())
            self.assertTrue(self.config.is_file())
        finally:
            self.root.chmod(0o700)

    def test_recovery_reports_current_location_after_parent_rename_and_name_conflict(self):
        import shutil

        moved = self.root.with_name(self.root.name + "-moved")
        self.addCleanup(lambda: shutil.rmtree(moved) if moved.exists() else None)
        self.app.approve.side_effect = [False, True]

        def fail_after_parent_move(*args, **kwargs):
            self.root.rename(moved)
            replacement = moved / self.install.name
            replacement.mkdir()
            (replacement / "unrelated").write_bytes(b"replacement")
            raise OSError("fixture failure after parent move")

        with patch.object(paths.shutil, "rmtree", side_effect=fail_after_parent_move):
            with self.assertRaises(RuntimeError) as failure:
                control.uninstall(self.app)
        recovery = list(moved.glob(".logos-desktop-uninstall-*/target"))
        self.assertEqual(len(recovery), 1)
        self.assertIn(str(recovery[0]), str(failure.exception))
        self.assertTrue((recovery[0] / "data/bin").is_dir())
        self.assertEqual((moved / self.install.name / "unrelated").read_bytes(), b"replacement")


class TestArchiveConfinement(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prefix = self.root / "prefix 文"
        (self.prefix / "drive_c/windows").mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.write_bytes(b"outside sentinel")
        self.archive = self.root / "icu-v1.tar.gz"
        self.app = SimpleNamespace(
            conf=SimpleNamespace(
                wine_prefix=str(self.prefix), download_dir=str(self.root),
                _raw=PersistentConfiguration(install_dir=str(self.root), faithlife_product="Logos"),
                _overrides=SimpleNamespace(install_dir=None, wine_prefix=str(self.prefix)),
                icu_latest_version_url="https://example.invalid/icu.tar.gz", icu_latest_version="v1",
                _network=SimpleNamespace(icu_latest_version=lambda: network.SoftwareReleaseInfo("v1", "url")),
            ), status=Mock(),
        )

    def write_archive(self, entries):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                else:
                    member.linkname = str(content)
                    archive.addfile(member)

    def install_archive(self):
        accepted = SimpleNamespace(check=lambda: self.archive)
        with patch.object(wine.network, "logos_reuse_download", return_value=accepted):
            wine.enforce_icu_data_files(self.app)

    def test_normal_layout_and_destination_links_preserve_external_targets(self):
        self.write_archive([
            ("icu-win/windows/system32/icu.dll", tarfile.REGTYPE, b"new ICU"),
            ("icu-win/windows/system32/hard.dll", tarfile.REGTYPE, b"new hard"),
            ("README", tarfile.REGTYPE, b"metadata"),
        ])
        destination = self.prefix / "drive_c/windows/system32"
        destination.mkdir()
        (destination / "icu.dll").symlink_to(self.outside)
        os.link(self.outside, destination / "hard.dll")
        self.install_archive()
        self.assertEqual(self.outside.read_bytes(), b"outside sentinel")
        self.assertEqual((destination / "icu.dll").read_bytes(), b"new ICU")
        self.assertFalse((destination / "icu.dll").is_symlink())
        self.assertEqual((destination / "hard.dll").read_bytes(), b"new hard")
        self.assertFalse((self.prefix / "drive_c/README").exists())
        self.app.status.assert_called_with("ICU files copied.", 100)

    def test_verified_current_layout_is_supported_and_ambiguous_layout_is_rejected(self):
        self.write_archive([
            (".", tarfile.DIRTYPE, ""),
            ("./windows/system32/icu.dll", tarfile.REGTYPE, b"current layout"),
        ])
        self.install_archive()
        target = self.prefix / "drive_c/windows/system32/icu.dll"
        self.assertEqual(target.read_bytes(), b"current layout")
        self.write_archive([
            ("windows/system32/icu.dll", tarfile.REGTYPE, b"ambiguous"),
            ("icu-win/windows/system32/icu.dll", tarfile.REGTYPE, b"ambiguous"),
        ])
        with self.assertRaises(ValueError):
            self.install_archive()
        self.assertEqual(target.read_bytes(), b"current layout")

    def test_unsafe_members_and_missing_subtrees_do_not_mutate_prefix(self):
        safe = ("icu-win/windows/safe.dll", tarfile.REGTYPE, b"safe")
        cases = [
            [safe, ("../outside", tarfile.REGTYPE, b"bad")],
            [(str(self.outside), tarfile.REGTYPE, b"bad")],
            [("icu-win/windows/link", tarfile.SYMTYPE, str(self.root)), safe],
            [("icu-win/windows/link", tarfile.SYMTYPE, "."),
             ("icu-win/windows/link/child", tarfile.REGTYPE, b"bad")],
            [("icu-win/windows/hard", tarfile.LNKTYPE, "../../outside")],
            [("icu-win/windows/pipe", tarfile.FIFOTYPE, "")],
            [safe, safe],
            [safe, ("icu-win/windows/safe.dll/child", tarfile.REGTYPE, b"bad")],
            [("unrelated/file", tarfile.REGTYPE, b"metadata")],
            [("icu-win/windows", tarfile.DIRTYPE, "")],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                self.write_archive(entries)
                with self.assertRaises((ValueError, OSError, tarfile.TarError)):
                    self.install_archive()
                self.assertEqual(list((self.prefix / "drive_c/windows").iterdir()), [])
                self.assertEqual(self.outside.read_bytes(), b"outside sentinel")
        self.assertNotIn(unittest.mock.call("ICU files copied.", 100), self.app.status.call_args_list)

    def test_truncated_gzip_and_pax_escape_fail(self):
        self.write_archive([("icu-win/windows/icu.dll", tarfile.REGTYPE, b"new")])
        self.archive.write_bytes(self.archive.read_bytes()[:-6])
        with self.assertRaises((EOFError, OSError, tarfile.TarError)):
            self.install_archive()
        with tarfile.open(self.archive, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo("safe")
            member.pax_headers = {"path": "../outside"}
            archive.addfile(member)
        with self.assertRaises(ValueError):
            self.install_archive()
        self.assertEqual(list((self.prefix / "drive_c/windows").iterdir()), [])
        self.assertEqual(self.outside.read_bytes(), b"outside sentinel")

    def test_linked_parent_and_copy_failure_preserve_existing_bytes(self):
        self.write_archive([("icu-win/windows/system32/icu.dll", tarfile.REGTYPE, b"new")])
        destination = self.prefix / "drive_c/windows/system32"
        destination.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install_archive()
        self.assertFalse((self.root / "icu.dll").exists())
        destination.unlink()
        destination.mkdir()
        target = destination / "icu.dll"
        target.write_bytes(b"old")
        with patch.object(wine.shutil, "copyfileobj", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.install_archive()
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(destination.iterdir()), [target])

    def test_self_nested_and_preexisting_extraction_roots_are_rejected(self):
        self.write_archive([("icu-win/windows/icu.dll", tarfile.REGTYPE, b"new")])
        with self.assertRaises(ValueError):
            utils.untar_file(self.archive, self.root)
        for source in (self.prefix, self.prefix / "drive_c", self.root):
            with self.subTest(source=source), self.assertRaises(ValueError):
                wine.copy_icu_files(source, self.prefix)


class TestRemovedCleanup(unittest.TestCase):
    def test_end_of_install_preserves_unowned_cwd_files(self):
        with tempfile.TemporaryDirectory(prefix="Logos 文 $() ") as directory:
            root = Path(directory)
            sentinels = [root / "wget-log.1", root / "unrelated"]
            for file in sentinels:
                file.write_bytes(b"unrelated")
            previous = Path.cwd()
            try:
                os.chdir(root)
                app = SimpleNamespace(
                    checkpoint=Mock(),
                    installer_step_count=0, installer_step=0, status=Mock(),
                    is_installed=lambda: True,
                    conf=SimpleNamespace(
                        installed_faithlife_product_release="1.0.0.0", logos_exe=None, faithlife_product="Logos",
                        _raw=SimpleNamespace(faithlife_product_release="1.0.0.0", install_dir=str(root / "install"),
                                             faithlife_product="Logos"),
                        _overrides=SimpleNamespace(install_dir=None, wine_prefix=None),
                    ),
                )
                with (patch.object(installer, "ensure_icu_data_files",
                                   return_value=installer.ProductInstaller("1.0.0.0", None)),
                      patch.object(utils.os, "system") as shell):
                    installer.ensure_product_installed(app)
                shell.assert_not_called()
                self.assertTrue(all(file.read_bytes() == b"unrelated" for file in sentinels))
            finally:
                os.chdir(previous)
