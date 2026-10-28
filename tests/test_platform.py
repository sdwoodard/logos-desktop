import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import constants, maintenance, main, network, releases, system, utils, wine
from logos_desktop.config import Config, EphemeralConfiguration


class TestDistributionDiscovery(unittest.TestCase):
    def test_cold_file_discovery_never_uses_command_fallbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "os-release"
            legacy = root / "fixture-release"
            legacy.write_text("")
            for identity, version in (("ubuntu", "24.04"), ("fedora", "43"),
                                      ("arch", ""), ("steamos", "3.7")):
                with self.subTest(identity=identity):
                    release.write_text(f'ID={identity}\nVERSION_ID="{version}"\n')
                    distribution = system.distro.LinuxDistribution(
                        os_release_file=str(release), distro_release_file=str(legacy),
                        include_lsb=system.DISTRIBUTION.include_lsb,
                        include_uname=system.DISTRIBUTION.include_uname,
                        include_oslevel=system.DISTRIBUTION.include_oslevel,
                    )
                    with (patch.object(system, "DISTRIBUTION", distribution),
                          patch("subprocess.check_output", side_effect=AssertionError("implicit discovery child"))):
                        self.assertEqual(system.get_os(), (identity, version))
                        self.assertEqual(distribution.major_version(), version.split(".")[0])


class TestPackageAdapters(unittest.TestCase):
    def manager(self, family, os_name="fixture", major_version="42"):
        def lookup(name):
            return f"/usr/bin/{name}" if name == family else None

        with (
            patch.object(system.shutil, "which", side_effect=lookup),
            patch.object(system.DISTRIBUTION, "id", return_value=os_name),
            patch.object(system.DISTRIBUTION, "major_version", return_value=major_version),
        ):
            return system.get_package_manager()

    def test_apt_appimage_prerequisite_preserves_fuse2_abi_across_t64_rename(self):
        cases = (
            ("ubuntu", "22", "libfuse2"),
            ("ubuntu", "24", "libfuse2t64"),
            ("ubuntu", "26", "libfuse2t64"),
            ("debian", "12", "libfuse2"),
            ("debian", "13", "libfuse2t64"),
            ("debian", "9", "libfuse2"),
            ("linuxmint", "21", "libfuse2"),
            ("linuxmint", "22", "libfuse2t64"),
            ("elementary", "7", "libfuse2"),
            ("elementary", "8", "libfuse2t64"),
            ("elementary", "10", "libfuse2t64"),
            ("ubuntu", "", "libfuse2"),
            ("fixture", "26", "libfuse2"),
        )
        for os_name, major_version, expected in cases:
            with self.subTest(os_name=os_name, major_version=major_version):
                packages = self.manager("apt", os_name, major_version).packages.split()
                self.assertEqual([name for name in packages if name.startswith("libfuse")], [expected])
                self.assertIn("p7zip-full", packages)
                self.assertIn("winbind", packages)

    def test_ubuntu_prerequisite_failures_propagate_without_success_status(self):
        manager = self.manager("apt", "ubuntu", "26")
        app = SimpleNamespace(
            conf=SimpleNamespace(skip_install_system_dependencies=False),
            approve=Mock(return_value=True), status=Mock(), superuser_command="sudo",
        )
        installed = "\n".join(f"ii  {name} 1 amd64" for name in manager.packages.split()
                              if name != "libfuse2t64")
        installed += "\nii  libfuse3-4 3.18 amd64\n"
        query = subprocess.CompletedProcess(manager.query, 0, installed)
        failure = subprocess.CalledProcessError(100, ["apt", "install"])
        for outcome in (None, subprocess.CompletedProcess([], 100), failure):
            with (
                self.subTest(outcome=outcome),
                patch.object(system, "get_package_manager", return_value=manager),
                patch.object(system, "get_os", return_value=("ubuntu", "26.04")),
                patch.object(system.DISTRIBUTION, "id", return_value="ubuntu"),
                patch.object(system, "run_command", side_effect=[query, query, outcome]) as run,
                self.assertRaises(maintenance.MaintenanceError),
            ):
                system.install_dependencies(app)
            self.assertEqual(run.call_args.args[0], ["sudo", "apt", "install", "-y", "libfuse2t64"])
            self.assertEqual(run.call_count, 3)
            self.assertNotIn(unittest.mock.call("All dependencies are met.", 100), app.status.call_args_list)

    def test_ubuntu_prerequisite_query_failure_stops_before_approval(self):
        manager = self.manager("apt", "ubuntu", "26")
        app = SimpleNamespace(
            conf=SimpleNamespace(skip_install_system_dependencies=False),
            approve=Mock(), status=Mock(),
        )
        failure = subprocess.CalledProcessError(2, manager.query)
        for outcome in (None, subprocess.CompletedProcess([], 2), failure):
            with (
                self.subTest(outcome=outcome),
                patch.object(system, "get_package_manager", return_value=manager),
                patch.object(system, "run_command", side_effect=[outcome]) as run,
                self.assertRaises(maintenance.MaintenanceError),
            ):
                system.install_dependencies(app)
            run.assert_called_once_with(manager.query)
            app.approve.assert_not_called()
            app.status.assert_not_called()

    def test_family_queries_match_names_in_both_modes(self):
        outputs = {
            "apt": "header\nii  fuse3:amd64 3.1 amd64\nii  samba 1 amd64\nrc  fuse 1 amd64\n",
            "dnf": "fuse3\nsamba\nsamba\n",
            "pacman": "fuse3 3.1\nsamba 1\n",
            "pamac": "fuse3 3.1 extra\nsamba 1 extra\n",
            "zypper": "i+ | fuse3 | package | 3\ni | samba | package | 1\n",
        }
        for family, output in outputs.items():
            with self.subTest(family=family):
                manager = self.manager(family)
                result = subprocess.CompletedProcess([], 0, output)
                with patch.object(system, "run_command", return_value=result) as run:
                    self.assertEqual(system.query_packages(manager, ["fuse", "fuse3", "samba"]), ["fuse"])
                    self.assertEqual(system.query_packages(manager, ["fuse", "fuse3", "samba"], "remove"),
                                     ["fuse3", "samba"])
                self.assertEqual(run.call_args.args[0], manager.query)
        self.assertEqual(self.manager("dnf").query, ["rpm", "-qa", "--qf", "%{NAME}\n"])
        self.assertEqual(self.manager("pacman").remove, ["pacman", "-R", "--noconfirm"])
        self.assertEqual(self.manager("pamac").remove, ["pamac", "remove", "--no-confirm"])

    def test_removal_uses_checked_owner_and_does_not_report_success_on_failure(self):
        manager = self.manager("pacman")
        app = SimpleNamespace(superuser_command="sudo", status=Mock())
        owner = subprocess.CompletedProcess([], 0, "appimagelauncher-bin\n")
        for result in (None, subprocess.CompletedProcess([], 4), subprocess.CompletedProcess([], 0)):
            with (
                self.subTest(result=result),
                patch.object(system, "get_package_manager", return_value=manager),
                patch.object(system.DISTRIBUTION, "id", return_value="arch"),
                patch.object(system.shutil, "which", return_value="/fixture/AppImageLauncher"),
                patch.object(system, "run_command", side_effect=[owner, result]) as run,
                self.assertRaises(maintenance.MaintenanceError) as caught,
            ):
                system.remove_appimagelauncher(app)
            self.assertEqual(run.call_args.args[0], ["sudo", "pacman", "-R", "--noconfirm", "appimagelauncher-bin"])
            self.assertEqual("Reboot" in str(caught.exception), result is not None and result.returncode == 0)

    def test_steamos_detects_dependencies_but_refuses_unvalidated_mutation_before_consent(self):
        manager = self.manager("pacman", "steamos")
        self.assertEqual(manager.install, [])
        self.assertEqual(manager.remove, [])
        app = SimpleNamespace(
            conf=SimpleNamespace(skip_install_system_dependencies=False), approve=Mock(), status=Mock(),
        )
        with (
            patch.object(system, "get_package_manager", return_value=manager),
            patch.object(system.DISTRIBUTION, "id", return_value="steamos"),
            patch.object(system, "query_packages", side_effect=[["fuse2"], [], [], []]),
            patch.object(system, "run_command") as run,
        ):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "Install:.*fuse2"):
                system.install_dependencies(app)
            app.approve.assert_not_called()
            system.install_dependencies(app)
            app.status.assert_called_once_with("All dependencies are met.", 100)
            run.assert_not_called()
        with (
            patch.object(system, "get_package_manager", return_value=manager),
            patch.object(system.DISTRIBUTION, "id", return_value="steamos"),
            patch.object(system.shutil, "which", return_value="/fixture/AppImageLauncher"),
            patch.object(system, "run_command") as run,
            self.assertRaisesRegex(maintenance.MaintenanceError, "SteamOS-supported"),
        ):
            system.remove_appimagelauncher(app)
        run.assert_not_called()


class TestWineDiscovery(unittest.TestCase):
    def executable(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o700)

    def test_current_native_recommendation_requires_exact_pair_and_stable_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, current = root / "old/wine", root / "current/wine"
            for path in (old, current, current.parent / "wineserver", old.parent / "wineserver"):
                self.executable(path, "fixture bytes, not executed")
            app = SimpleNamespace(checkpoint=Mock(), conf=SimpleNamespace(
                wine_binary_files=[str(old), str(current)], _overrides=SimpleNamespace(),
                faithlife_product_release="40.0.0.0", faithlife_product_version="10",
            ))
            target = releases.WineTarget("11.20", "devel", "a" * 40)

            def version(path):
                return (wine.WineRelease(11, 19 if path == str(old) else 20, "devel"), "yes")

            with (patch.object(wine, "get_wine_release", side_effect=version),
                  patch.object(wine, "check_wine_version_and_branch", return_value=(True, "")) as pair):
                self.assertEqual(utils.current_native_wine(app, target), str(current))
                pair.assert_called_once_with("40.0.0.0", str(current), "10",
                                             server=str(current.parent / "wineserver"))
            with (patch.object(wine, "get_wine_release", side_effect=version),
                  patch.object(wine, "check_wine_version_and_branch", return_value=(False, "mismatch"))):
                self.assertIsNone(utils.current_native_wine(app, target))

            def mutate(*args, **kwargs):
                (current.parent / "wineserver").write_text("changed while probing")
                return True, ""

            with (patch.object(wine, "get_wine_release", side_effect=version),
                  patch.object(wine, "check_wine_version_and_branch", side_effect=mutate)):
                self.assertIsNone(utils.current_native_wine(app, target))

            app.conf.wine_binary_files = [str(current)]
            stable = releases.SoftwareReleaseInfo("11.0", "https://example.invalid/stable.AppImage")
            for flavor, expected in (("stable", str(current)), ("staging", None)):
                with (self.subTest(flavor=flavor),
                      patch.object(wine, "get_wine_release", return_value=(wine.WineRelease(11, 0, flavor), "yes")),
                      patch.object(wine, "check_wine_version_and_branch", return_value=(True, ""))):
                    self.assertEqual(utils.current_native_wine(app, stable), expected)

    def test_discovery_pairs_full_patch_and_rc_identity_without_requiring_staging_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher, server = root / "wine", root / "wineserver"
            app = SimpleNamespace(conf=SimpleNamespace(
                _overrides=SimpleNamespace(custom_binary_path=str(root)), faithlife_product_version="10",
            ))
            for wine_output, server_output, allowed in (
                ("wine-11.20", "Wine 11.20", True),
                ("wine-10.8 (Staging)", "Wine 10.8", True),
                ("wine-11.0.2", "Wine 11.0.2", True),
                ("wine-11.0.2", "Wine 11.0.1", False),
                ("wine-11.0-rc2", "Wine 11.0-rc2", True),
                ("wine-11.0-rc2 (Staging)", "Wine 11.0-rc2", True),
                ("wine-11.0-rc2", "Wine 11.0-rc1", False),
                ("wine-11.0-rc2", "Wine 11.0", False),
                ("wine-11.20", "Wine 11.20 (Proton)", False),
                ("wine-11.20 (vendor)", "Wine 11.20", False),
            ):
                self.executable(launcher, f"#!/bin/sh\nprintf '{wine_output}\\n'\n")
                self.executable(server, f"#!/bin/sh\nprintf '{server_output}\\n'\n")
                with (
                    self.subTest(wine=wine_output, server=server_output),
                    patch.dict(os.environ, HOME=str(root), PATH=str(root / "absent")),
                ):
                    self.assertEqual(utils.find_wine_binary_files(app, "40.0.0.0"),
                                     [str(launcher)] if allowed else [])

            self.executable(server, "#!/bin/sh\nprintf 'Wine 11.20\\n'\nexit 2\n")
            self.executable(launcher, "#!/bin/sh\nprintf 'wine-11.20\\n'\n")
            allowed, reason = wine.check_wine_version_and_branch("40.0.0.0", str(launcher), "10",
                                                                server=str(server))
            self.assertFalse(allowed)
            self.assertIn("matching wineserver", reason)

    def test_real_local_probes_expand_globs_and_leave_environment_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locations = [root / "custom 文", root / "PlayOnLinux/wine/linux-amd64/version/bin",
                         root / ".steam/steam/steamapps/common/Proton Fixture/files/bin"]
            expected = []
            for location in locations:
                self.executable(location / "wine", "#!/bin/sh\nprintf 'wine-11.19\\n'\n")
                self.executable(location / "wineserver", "#!/bin/sh\nprintf 'Wine 11.19\\n'\n")
                expected.append(str(location / "wine"))
            (locations[0] / "wine64").symlink_to("wine")
            expected[0] = str(locations[0] / "wine64")
            app = SimpleNamespace(conf=SimpleNamespace(
                _overrides=SimpleNamespace(custom_binary_path=str(locations[0])), faithlife_product_version="10",
            ))
            with patch.dict(os.environ, HOME=str(root), PATH=str(root / "absent")):
                before = dict(os.environ)
                self.assertEqual(utils.find_wine_binary_files(app, "40.0.0.0"), expected)
                self.assertEqual(dict(os.environ), before)
            (locations[0] / "wine64").unlink()
            (locations[0] / "wine").write_bytes(b"\x7fELF\x01\x01" + b"\0" * 14)
            with self.assertRaises(maintenance.MaintenanceError):
                wine.native_launcher(locations[0] / "wine")

    def test_incomplete_alias_does_not_hide_complete_pair_and_execution_keeps_wrapper_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete, alias = root / "runtime", root / "aliases"
            alias.mkdir()
            capture = root / "capture"
            self.executable(complete / "wine", f'''#!{sys.executable}
import json, os, pathlib, sys
if sys.argv[1:] == ['--version']:
    print('wine-11.19')
else:
    pathlib.Path({str(capture)!r}).write_text(json.dumps([
        __file__, os.environ['WINE'], os.environ['WINELOADER'], os.environ['WINESERVER']]))
''')
            self.executable(complete / "wineserver", "#!/bin/sh\nprintf 'Wine 11.19\\n'\n")
            (alias / "wine").symlink_to(complete / "wine")
            selected = root / "selected.json"
            selected.write_text(json.dumps({
                "install_dir": str(root / "installation"), "faithlife_product": "Logos",
                "faithlife_product_version": "10", "faithlife_product_release": "40.0.0.0",
                "wine_binary": str(alias / "wine"),
            }))
            app = SimpleNamespace(_config_updated_event=threading.Event())
            with (
                patch.multiple(constants, STATE_DIR=str(root / "state/app"), CACHE_BASE=root / "cache",
                               CACHE_DIR=str(root / "cache/app"),
                               NETWORK_CACHE_PATH=str(root / "cache/app/network.json")),
                patch.dict(os.environ, HOME=str(root), PATH=str(complete)),
            ):
                app.conf = Config(EphemeralConfiguration.load_from_path(str(selected)), app)
                app.conf._overrides.custom_binary_path = str(alias)
                self.assertEqual(utils.find_wine_binary_files(app, "40.0.0.0"), [str(complete / "wine")])
                (alias / "wineserver").symlink_to(complete / "wineserver")
                self.assertEqual(utils.find_wine_binary_files(app, "40.0.0.0"),
                                 [str(alias / "wine"), str(complete / "wine")])
                result = wine.run_wine_completed_process(app, str(alias / "wine"))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(capture.read_text()),
                                 [str(alias / "wine")] * 3 + [str(alias / "wineserver")])
                self.executable(complete / "wineserver", "#!/bin/sh\nprintf 'Wine 10.0\\n'\n")
                self.assertEqual(utils.find_wine_binary_files(app, "40.0.0.0"), [])

    def test_effective_custom_path_and_scoped_refresh_survive_real_save_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = root / "custom tools 文"
            self.executable(custom / "wine", "#!/bin/sh\nprintf 'wine-11.19\\n'\n")
            self.executable(custom / "wineserver", "#!/bin/sh\nprintf 'Wine 11.19\\n'\n")
            selected = root / "selected.json"
            selected.write_text(json.dumps({
                "faithlife_product": "Logos", "faithlife_product_version": "10",
                "CUSTOMBINPATH": str(root / "saved"), "CHECK_UPDATES": False,
            }))
            app = SimpleNamespace(_config_updated_event=threading.Event(), status=Mock())
            with (
                patch.multiple(constants, STATE_DIR=str(root / "state/app"), CACHE_BASE=root / "cache",
                               CACHE_DIR=str(root / "cache/app"),
                               NETWORK_CACHE_PATH=str(root / "cache/app/network.json")),
                patch.dict(os.environ, HOME=str(root), PATH=str(root / "empty"),
                           CUSTOMBINPATH=str(root / "environment")),
                patch.object(network, "_fetch_faithlife_product_releases", return_value=["41.0.0.0"]) as fetch,
            ):
                parser = main.get_parser()
                overrides, _ = main.parse_args(parser.parse_args([
                    "--status", "-c", str(selected), "-p", str(custom), "-a",
                ]), parser)
                app.conf = Config(overrides, app)
                app.conf._change(curses_color_scheme="Dark")
                app.conf.reload()
                fetch.assert_not_called()
                self.assertEqual(app.conf.wine_binary_files, [str(custom / "wine")])
                self.assertEqual(app.conf._overrides.custom_binary_path, str(custom))
                app.conf._network._cache.product_entries["feed:Logos:10:stable"] = {
                    "versions": ["40.0.0.0"], "checked": time.time(), "status": "ok",
                }
                self.assertEqual(app.conf._network.product_release("Logos", "10", "stable", refresh=False).versions,
                                 ("40.0.0.0",))
                fetch.assert_not_called()
                self.assertEqual(app.conf.faithlife_product_releases, ["41.0.0.0"])
                self.assertEqual(app.conf.faithlife_product_releases, ["41.0.0.0"])
                fetch.assert_called_once_with("Logos", "10", "stable")
            saved = json.loads(selected.read_text())
            self.assertEqual(saved["CUSTOMBINPATH"], str(root / "saved"))
            self.assertFalse(saved["CHECK_UPDATES"])
