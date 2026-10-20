from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from ou_dedetai import constants, control, doctor, maintenance, support
from tests.test_doctor import StoredOnly


class TestSupport(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.conf = StoredOnly(self.root)
        self.app = SimpleNamespace(conf=self.conf)
        self.addCleanup(patch.stopall)
        patch.object(constants, "STATE_DIR", str(self.root / "state/app")).start()
        patch.object(constants, "CACHE_DIR", str(self.root / "cache")).start()
        patch.object(doctor.psutil, "process_iter", return_value=[]).start()

    def test_all_exported_bytes_are_allowlisted_and_match_the_immutable_preview(self):
        secret = "TOKEN-secret-user@example.org-library-notes-https://login/?token=secret"
        for name in ("config.json", "app.log", "wine.log", "crash.log", "network.json"):
            (self.root / name).write_text(secret)
        self.conf._overrides.config_path = str(self.root / "config.json")
        self.conf._overrides.app_log_path = str(self.root / "app.log")
        self.conf._raw.unknown = {"token": secret}
        self.conf._network = SimpleNamespace(secret=secret)
        with patch.dict(os.environ, {"WINEDEBUG": secret, "TOKEN": secret}):
            snapshot = support.prepare(self.app)
        rendered = support.preview(snapshot)
        with self.assertRaises(FrozenInstanceError):
            snapshot.entries = ()
        self.conf._raw.unknown = "changed after preview"
        output = self.root / "report.zip"
        support.export(snapshot, output)
        with ZipFile(output) as archive:
            self.assertEqual(archive.namelist(), [name for name, _ in snapshot.entries])
            for name, text in snapshot.entries:
                content = archive.read(name)
                self.assertEqual(content, text.encode())
                self.assertIn(f"=== {name} ===\n{text}", rendered)
                self.assertNotIn(secret.encode(), content)
                self.assertNotIn(str(self.root).encode(), content)
            for entry in snapshot.manifest["entries"]:
                self.assertEqual(hashlib.sha256(archive.read(entry["name"])).hexdigest(), entry["sha256"])
        self.assertNotIn(secret.encode(), output.read_bytes())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertIn("Logs and crash", json.dumps(snapshot.manifest))

    def test_cancel_and_write_failure_preserve_existing_output_and_clean_staging(self):
        snapshot = support.prepare(self.app)
        output = self.root / "report.zip"
        output.write_bytes(b"previous")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(support.SupportCancelled):
            support.prepare(self.app, cancel)
        with self.assertRaises(support.SupportCancelled):
            support.export(snapshot, output, overwrite=True, cancel=cancel)
        with patch.object(support.os, "replace", side_effect=OSError("publication fault")):
            with self.assertRaises(OSError):
                support.export(snapshot, output, overwrite=True)
        self.assertEqual(output.read_bytes(), b"previous")
        self.assertEqual(set(self.root.iterdir()), {output})

    def test_cancel_during_archive_writing_never_publishes(self):
        snapshot = support.prepare(self.app)
        output = self.root / "report.zip"
        cancel = threading.Event()
        write = ZipFile.writestr

        def cancelling_write(archive, *args, **kwargs):
            result = write(archive, *args, **kwargs)
            cancel.set()
            return result

        with patch.object(ZipFile, "writestr", cancelling_write):
            with self.assertRaises(support.SupportCancelled):
                support.export(snapshot, output, cancel=cancel)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_refuses_sources_symlinks_hardlinks_and_unconfirmed_overwrite(self):
        config = self.root / "config.zip"
        config.write_bytes(b"private configuration")
        self.conf._overrides.config_path = str(config)
        snapshot = support.prepare(self.app)
        link = self.root / "link.zip"
        link.symlink_to(config)
        hardlink = self.root / "hardlink.zip"
        os.link(config, hardlink)
        for output in (config, link, hardlink, self.root / "install/support.zip"):
            with self.subTest(output=output), self.assertRaises(maintenance.MaintenanceError):
                support.export(snapshot, output, overwrite=True)
        ordinary = self.root / "ordinary.zip"
        ordinary.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            support.export(snapshot, ordinary)
        self.assertEqual(config.read_bytes(), b"private configuration")
        self.assertEqual(ordinary.read_bytes(), b"keep")

    def test_preview_and_explicit_confirmation_precede_publication(self):
        app = Mock(conf=self.conf)
        output = self.root / "report.zip"
        app.ask.side_effect = [str(output), "Keep archive local"]
        app.approve.return_value = False
        self.assertIsNone(control.get_support(app))
        app.ask.assert_not_called()
        self.assertFalse(output.exists())
        app.approve.return_value = True
        app.info.side_effect = lambda text: self.assertFalse(output.exists())
        self.assertEqual(control.get_support(app), str(output))
        with ZipFile(output) as archive:
            for name in archive.namelist():
                self.assertIn(archive.read(name).decode(), app.info.call_args.args[0])

    def test_no_clobber_if_destination_appears_at_publication(self):
        snapshot = support.prepare(self.app)
        output = self.root / "report.zip"
        link = os.link

        def race(*args, **kwargs):
            output.write_bytes(b"concurrent writer")
            return link(*args, **kwargs)

        with patch.object(support.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                support.export(snapshot, output)
        self.assertEqual(output.read_bytes(), b"concurrent writer")
        self.assertEqual(set(self.root.iterdir()), {output})

    def test_archive_finalization_or_validation_failure_preserves_previous_output(self):
        snapshot = support.prepare(self.app)
        output = self.root / "report.zip"
        output.write_bytes(b"previous archive")

        class CloseFailure(ZipFile):
            def close(self):
                writing = self.fp is not None and self.mode == "w"
                super().close()
                if writing:
                    raise OSError("archive finalization fault")

        for boundary in (patch.object(support, "ZipFile", CloseFailure),
                         patch.object(ZipFile, "read", return_value=b"corrupt")):
            with boundary, self.assertRaises((OSError, maintenance.MaintenanceError)):
                support.export(snapshot, output, overwrite=True)
            self.assertEqual(output.read_bytes(), b"previous archive")
            self.assertEqual(set(self.root.iterdir()), {output})

    def test_existing_output_requires_separate_confirmation_and_assume_yes_is_refused(self):
        app = Mock(conf=self.conf)
        output = self.root / "report.zip"
        output.write_bytes(b"keep existing output")
        app.ask.return_value = str(output)
        app.approve.side_effect = [True, False]
        self.assertIsNone(control.get_support(app))
        self.assertEqual(app.approve.call_count, 2)
        self.assertEqual(output.read_bytes(), b"keep existing output")
        self.conf._overrides.assume_yes = True
        with self.assertRaisesRegex(maintenance.MaintenanceError, "explicit preview confirmation"):
            control.get_support(app)
