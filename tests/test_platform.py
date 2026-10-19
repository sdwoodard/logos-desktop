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

from ou_dedetai import constants, maintenance, main, network, system, utils, wine
from ou_dedetai.config import Config, EphemeralConfiguration


class TestPackageAdapters(unittest.TestCase):
    def manager(self, family, os_name="fixture"):
        def lookup(name):
            return f"/usr/bin/{name}" if name == family else None

        with (
            patch.object(system.shutil, "which", side_effect=lookup),
            patch.object(system.distro, "id", return_value=os_name),
            patch.object(system.distro, "major_version", return_value="42"),
        ):
            return system.get_package_manager()

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
                patch.object(system.distro, "id", return_value="arch"),
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
            patch.object(system.distro, "id", return_value="steamos"),
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
            patch.object(system.distro, "id", return_value="steamos"),
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
