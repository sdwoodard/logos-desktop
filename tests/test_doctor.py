import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ou_dedetai import constants, doctor, maintenance, runtime


class StoredOnly:
    def __init__(self, root):
        self._raw = SimpleNamespace(install_dir=str(root / "install"), backup_dir="backups",
                                    wine_binary=str(root / "untrusted"))
        self._overrides = SimpleNamespace(wine_prefix=str(root / "prefix"))

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected configuration getter: {name}")


class TestDoctor(unittest.TestCase):
    def test_broken_fixture_is_serializable_read_only_and_reports_pending_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = StoredOnly(root)
            with patch.object(constants, "STATE_DIR", str(root / "state/app")):
                context = maintenance.Context(root / "prefix", root / "install")
                context.pending.write_text("secret recovery payload")
                before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
                directories = set(root.rglob("*"))
                with (
                    patch("subprocess.Popen", side_effect=AssertionError("No executables")),
                    patch.object(maintenance, "private_directory", side_effect=AssertionError("No writes")),
                    patch.object(runtime, "recover", side_effect=AssertionError("No recovery")),
                    patch.object(doctor.platform, "machine", return_value="aarch64"),
                    patch.object(doctor.psutil, "process_iter", return_value=[]),
                ):
                    report = doctor.inspect(conf)
                checks = {item["name"]: item for item in report["checks"]}
                self.assertEqual(checks["architecture"]["status"], "error")
                self.assertEqual(checks["installation"]["status"], "warning")
                self.assertEqual(checks["space"]["status"], "unknown")
                self.assertIn("Pending recovery", checks["recovery"]["summary"])
                self.assertIsNone(report["command_preview"])
                self.assertNotIn("secret", json.dumps(report))
                self.assertEqual(set(root.rglob("*")), directories)
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_existing_paths_low_space_and_process_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = StoredOnly(root)
            (root / "install/backups").mkdir(parents=True)
            (root / "prefix").mkdir()
            process = SimpleNamespace(uids=lambda: SimpleNamespace(real=os.geteuid()),
                                      environ=lambda: {"WINEPREFIX": str(root / "prefix"), "TOKEN": "secret"})
            with (
                patch.object(constants, "STATE_DIR", str(root / "state/app")),
                patch.object(doctor.os, "statvfs", return_value=SimpleNamespace(f_bavail=1, f_frsize=4096)),
                patch.object(doctor.psutil, "process_iter", return_value=[process]),
            ):
                report = doctor.inspect(conf)
            checks = {item["name"]: item for item in report["checks"]}
            self.assertEqual(checks["backup"]["status"], "ok")
            self.assertEqual(checks["space"]["status"], "warning")
            self.assertIn("uses the selected prefix", checks["processes"]["summary"])
            self.assertNotIn("secret", json.dumps(report))
            self.assertFalse((root / "state").exists())

    def test_untrusted_runtime_and_appimage_never_get_a_command_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = StoredOnly(root)
            for selected in (str(root / "wine"), "wine", "Recommended", "/usr/bin/evil", "wine.AppImage"):
                conf._raw.wine_binary = selected
                with patch("subprocess.Popen", side_effect=AssertionError("No execution")):
                    self.assertIsNone(doctor.command_preview(conf))

    def test_process_scan_has_a_fixed_limit_and_reports_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = StoredOnly(Path(directory))
            seen = []

            def processes():
                while True:
                    seen.append(True)
                    yield SimpleNamespace(uids=lambda: SimpleNamespace(real=-1))

            with patch.object(doctor.psutil, "process_iter", side_effect=processes):
                report = doctor.inspect(conf)
            self.assertEqual(len(seen), 2049)
            self.assertEqual(report["checks"][-1]["status"], "unknown")

    def test_incompatible_runtime_is_inspected_as_data_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = StoredOnly(root)
            selected = root / "untrusted"
            selected.write_bytes(b"\x7fELF\x01\x01" + bytes(12) + b"\x03\x00")
            before = selected.read_bytes()
            with (
                patch("subprocess.Popen", side_effect=AssertionError("No execution")),
                patch.object(doctor.psutil, "process_iter", return_value=[]),
            ):
                report = doctor.inspect(conf)
            check = next(item for item in report["checks"] if item["name"] == "runtime")
            self.assertEqual(check["status"], "error")
            self.assertEqual(selected.read_bytes(), before)
            self.assertIsNone(report["command_preview"])
