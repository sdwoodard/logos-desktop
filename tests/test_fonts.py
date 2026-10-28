"""Offline Arial prerequisite regressions using disposable, unlicensed fixtures."""

from contextlib import ExitStack
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import fonts, installer, maintenance, wine
from logos_desktop.app import UserExitedFromAsk


REGULAR = b"synthetic Arial identity, not a font binary"
PAYLOAD = {name: REGULAR if name == "arial.ttf" else name.encode() for name in fonts.FACES}


def cabinet(members):
    """Build a tiny uncompressed CAB for the real extractor boundary."""
    files = bytearray()
    content = bytearray()
    for name, data in members.items():
        files.extend(struct.pack("<IIHHHH", len(data), len(content), 0, 0, 0, 0x20))
        files.extend(name.encode() + b"\0")
        content.extend(data)
    offset = 44 + len(files)
    header = struct.pack("<4sIIIIIBBHHHHH", b"MSCF", 0, offset + 8 + len(content), 0, 44, 0,
                         3, 1, 1, len(members), 0, 0, 0)
    block = struct.pack("<IHH", 0, len(content), len(content)) + content
    return header + struct.pack("<IHH", offset, 1, 0) + files + block


class FontPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prefix = self.root / "prefix"
        self.directory = self.prefix / "selected-c" / "Windows" / "fOnTs"
        self.directory.mkdir(parents=True)
        devices = self.prefix / "dosdevices"
        devices.mkdir()
        (devices / "c:").symlink_to("../selected-c")
        # A plausible drive_c must not distract from the selected mapping.
        (self.prefix / "drive_c" / "windows" / "Fonts").mkdir(parents=True)
        self.app = SimpleNamespace(
            conf=SimpleNamespace(wine_prefix=str(self.prefix), wine64_binary="fixture-wine",
                                 _overrides=SimpleNamespace(assume_yes=False)),
            checkpoint=Mock(), status=Mock(), approve_or_exit=Mock(return_value=True),
            installer_step_count=0, installer_step=0,
        )
        self.registry = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(fonts, "ARIAL_SHA256", hashlib.sha256(REGULAR).hexdigest()))
        self.parse_query = wine.wine_reg_query
        self.query = self.stack.enter_context(patch.object(
            wine, "wine_reg_query", side_effect=lambda *args: self.registry,
        ))
        self.register = self.stack.enter_context(patch.object(
            wine, "run_wine_during_install", side_effect=self.set_value,
        ))
        self.acquire = self.stack.enter_context(patch.object(fonts, "_acquire_fonts", return_value=PAYLOAD))
        self.winetricks = self.stack.enter_context(patch.object(wine, "run_winetricks"))

    def set_value(self, *args, **kwargs):
        values = kwargs["exe_args"]
        if values[values.index("/v") + 1] == "Arial (TrueType)":
            self.registry = values[values.index("/d") + 1]

    def put_regular(self, name="arial.ttf", data=REGULAR):
        (self.directory / name).write_bytes(data)

    def test_ready_accepts_bound_local_case_and_dos_names_without_consent(self):
        self.put_regular("ARIAL.TTF")
        for value in ("arial.ttf", "ARIAL.TTF", r"C:\Windows\Fonts\Arial.ttf"):
            with self.subTest(value=value):
                self.registry = value
                wine.install_fonts(self.app)
        self.acquire.assert_not_called()
        self.register.assert_not_called()
        self.app.approve_or_exit.assert_not_called()
        self.winetricks.assert_not_called()

    def test_unbound_values_are_registration_repairs_only_with_proven_bytes(self):
        self.put_regular()
        original = (self.directory / "arial.ttf").stat()
        for value in (None, "", r"\??\unix\usr\share\arial.ttf", r"Z:\missing\Arial.ttf",
                      r"C:\other\arial.ttf", r"C:\Windows\Fonts\..\arial.ttf"):
            with self.subTest(value=value):
                self.registry = value
                self.register.reset_mock()
                wine.install_fonts(self.app)
                self.assertEqual(self.registry, "arial.ttf")
                self.assertEqual(self.register.call_count, 2)
                for call in self.register.call_args_list:
                    self.assertEqual(call.kwargs["exe_args"][3], "Arial (TrueType)")
        self.assertEqual(original, (self.directory / "arial.ttf").stat())
        self.acquire.assert_not_called()
        self.app.approve_or_exit.assert_not_called()

    def test_wrong_family_and_tampered_local_bytes_require_acquisition(self):
        for data in (b"DejaVu labelled arial.ttf", REGULAR + b"tampered", b""):
            with self.subTest(data=data):
                self.put_regular(data=data)
                self.registry = "arial.ttf"
                wine.install_fonts(self.app)
                self.assertEqual((self.directory / "arial.ttf").read_bytes(), REGULAR)
        self.assertEqual(self.acquire.call_count, 3)
        self.assertEqual(self.app.approve_or_exit.call_count, 3)

    def test_download_publishes_five_styles_and_only_fixed_registrations(self):
        sentinel = self.directory / "unrelated.ttf"
        sentinel.write_bytes(b"preserve")
        wine.install_fonts(self.app)
        self.assertEqual({name: (self.directory / name).read_bytes() for name in fonts.FACES}, PAYLOAD)
        self.assertEqual(sentinel.read_bytes(), b"preserve")
        self.assertFalse(list(self.directory.glob(".arial-*")))
        self.assertEqual(self.register.call_count, 10)
        seen = {(call.kwargs["exe_args"][1], call.kwargs["exe_args"][3], call.kwargs["exe_args"][7])
                for call in self.register.call_args_list}
        self.assertEqual(seen, {(key, family + " (TrueType)", name)
                                for key in fonts.FONT_KEYS for name, family in fonts.FACES.items()})
        self.assertEqual(self.query.call_count, 2)

    def test_all_five_unsafe_destinations_refuse_before_prompt_or_acquisition(self):
        outside = self.root / "outside"
        outside.write_bytes(b"sentinel")
        for name in fonts.FACES:
            for kind in ("link", "directory", "hardlink", "fifo"):
                with self.subTest(name=name, kind=kind):
                    destination = self.directory / name
                    if kind == "link":
                        destination.symlink_to(outside)
                    elif kind == "directory":
                        destination.mkdir()
                    elif kind == "hardlink":
                        os.link(outside, destination)
                    else:
                        os.mkfifo(destination)
                    with self.assertRaises(maintenance.MaintenanceError):
                        wine.install_fonts(self.app)
                    if kind == "directory":
                        destination.rmdir()
                    else:
                        destination.unlink()
        self.assertEqual(outside.read_bytes(), b"sentinel")
        self.acquire.assert_not_called()
        self.register.assert_not_called()
        self.app.approve_or_exit.assert_not_called()

    def test_mapping_escape_symlink_parent_and_ambiguous_case_refuse(self):
        mapping = self.prefix / "dosdevices" / "c:"
        mapping.unlink()
        mapping.symlink_to(self.root)
        with self.assertRaises(maintenance.MaintenanceError):
            wine.install_fonts(self.app)
        mapping.unlink()
        mapping.symlink_to("../alias")
        (self.prefix / "alias").symlink_to(self.prefix / "selected-c")
        with self.assertRaises(maintenance.MaintenanceError):
            wine.install_fonts(self.app)
        mapping.unlink()
        mapping.symlink_to("../selected-c")
        (self.directory.parent / "Fonts").mkdir()
        with self.assertRaises(maintenance.MaintenanceError):
            wine.install_fonts(self.app)
        self.acquire.assert_not_called()
        self.app.approve_or_exit.assert_not_called()

    def test_mapping_parent_traversal_cannot_hide_symlink_or_missing_component(self):
        self.put_regular()
        self.registry = "arial.ttf"
        pivot = self.prefix / "pivot"
        pivot.symlink_to(self.root)
        mapping = self.prefix / "dosdevices" / "c:"
        mapping.unlink()
        mapping.symlink_to("../pivot/../selected-c")
        for kind in ("symlink", "missing"):
            with self.subTest(kind=kind):
                with self.assertRaises(maintenance.MaintenanceError):
                    wine.install_fonts(self.app)
                if pivot.is_symlink():
                    pivot.unlink()
        self.acquire.assert_not_called()
        self.register.assert_not_called()
        self.app.approve_or_exit.assert_not_called()

    def test_basename_directory_cannot_satisfy_readiness(self):
        self.registry = "arial.ttf"
        (self.directory / "arial.ttf").mkdir()
        with self.assertRaises(maintenance.MaintenanceError):
            wine.install_fonts(self.app)

    def test_symlinked_windows_or_fonts_and_duplicate_font_case_refuse(self):
        for directory in (self.directory, self.directory.parent):
            with self.subTest(directory=directory.name):
                saved = directory.with_name(directory.name + "-saved")
                directory.rename(saved)
                directory.symlink_to(saved, target_is_directory=True)
                try:
                    with self.assertRaises(maintenance.MaintenanceError):
                        wine.install_fonts(self.app)
                finally:
                    directory.unlink()
                    saved.rename(directory)
        self.put_regular()
        self.put_regular("ARIAL.TTF")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Ambiguous"):
            wine.install_fonts(self.app)
        self.acquire.assert_not_called()
        self.app.approve_or_exit.assert_not_called()

    def test_decline_cancel_and_generic_assume_yes_do_not_acquire(self):
        self.app.approve_or_exit.side_effect = UserExitedFromAsk
        with self.assertRaises(UserExitedFromAsk):
            wine.install_fonts(self.app)
        self.app.approve_or_exit.side_effect = None
        self.app.checkpoint.side_effect = UserExitedFromAsk
        with self.assertRaises(UserExitedFromAsk):
            wine.install_fonts(self.app)
        self.app.checkpoint.side_effect = None
        self.app.conf._overrides.assume_yes = True
        with self.assertRaisesRegex(maintenance.MaintenanceError, "interactive font-terms"):
            wine.install_fonts(self.app)
        self.acquire.assert_not_called()
        self.register.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_successful_noop_and_registry_failure_stop_before_icu(self):
        self.put_regular()
        self.register.side_effect = None
        with (patch.object(installer, "ensure_wineprefix_config", return_value=object()),
              patch.object(wine, "enforce_icu_data_files") as icu):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "still unmet"):
                installer.ensure_icu_data_files(self.app)
            icu.assert_not_called()
        self.register.side_effect = maintenance.MaintenanceError("registry failed")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "registry failed"):
            wine.install_fonts(self.app)

    def test_cross_style_change_during_acquisition_stops_all_publication(self):
        outside = self.root / "sentinel"
        outside.write_bytes(b"preserve")

        def substituted(app):
            (self.directory / "arialbi.ttf").symlink_to(outside)
            return PAYLOAD

        self.acquire.side_effect = substituted
        with self.assertRaises(maintenance.MaintenanceError):
            wine.install_fonts(self.app)
        self.assertEqual(outside.read_bytes(), b"preserve")
        self.assertFalse((self.directory / "arial.ttf").exists())
        self.assertFalse(list(self.directory.glob(".arial-*")))
        self.register.assert_not_called()

    def test_mapping_change_during_acquisition_refuses(self):
        other = self.prefix / "other" / "windows" / "fonts"
        other.mkdir(parents=True)

        def substituted(app):
            mapping = self.prefix / "dosdevices" / "c:"
            mapping.unlink()
            mapping.symlink_to("../other")
            return PAYLOAD

        self.acquire.side_effect = substituted
        with self.assertRaisesRegex(maintenance.MaintenanceError, "mapping changed"):
            wine.install_fonts(self.app)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(list(other.iterdir()), [])
        self.register.assert_not_called()

    def test_postrepair_byte_tamper_is_not_readiness(self):
        def changed(*args, **kwargs):
            self.set_value(*args, **kwargs)
            (self.directory / "arial.ttf").write_bytes(b"substitution")

        self.register.side_effect = changed
        with self.assertRaisesRegex(maintenance.MaintenanceError, "still unmet"):
            wine.install_fonts(self.app)

    def test_malformed_query_stops_before_any_repair(self):
        self.query.side_effect = maintenance.MaintenanceError("unexpected value or type")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "unexpected"):
            wine.install_fonts(self.app)
        self.acquire.assert_not_called()
        self.register.assert_not_called()
        self.app.approve_or_exit.assert_not_called()

    def test_leading_space_registration_is_repaired_instead_of_skipped(self):
        self.put_regular()
        self.registry = " arial.ttf"
        self.query.side_effect = self.parse_query

        def output(*args, **kwargs):
            text = f"{fonts.FONT_KEYS[0]}\n    Arial (TrueType)    REG_SZ    {self.registry}\n"
            return subprocess.CompletedProcess([], 0, text)

        with patch.object(wine, "run_wine_completed_process", side_effect=output):
            wine.install_fonts(self.app)
        self.assertEqual(self.registry, "arial.ttf")
        self.assertEqual(self.register.call_count, 2)
        self.acquire.assert_not_called()

    def test_cancel_before_publication_cleans_staging_without_replacement(self):
        self.put_regular(data=b"old unaccepted regular")
        original = (self.directory / "arial.ttf").read_bytes()
        with fonts._font_directory(self.prefix) as descriptor:
            entries = fonts._destinations(descriptor)
            checkpoint = Mock(side_effect=[None] * 5 + [UserExitedFromAsk])
            with self.assertRaises(UserExitedFromAsk):
                fonts._publish_fonts(self.prefix, descriptor, entries, PAYLOAD, checkpoint)
        self.assertEqual((self.directory / "arial.ttf").read_bytes(), original)
        self.assertEqual([entry.name for entry in self.directory.iterdir()], ["arial.ttf"])

    def test_noop_file_publication_does_not_pass_postcondition(self):
        with patch.object(fonts, "_publish_fonts"):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "still unmet"):
                wine.install_fonts(self.app)


class FontArchiveTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(checkpoint=Mock())
        self.blobs = {}
        specs = []
        for archive, _, members in fonts.ARCHIVES:
            blob = cabinet({member: PAYLOAD[member.lower()] for member in members})
            self.blobs[archive] = blob
            specs.append((archive, hashlib.sha256(blob).hexdigest(), members))
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(fonts, "ARCHIVES", tuple(specs)))
        stack.enter_context(patch.object(fonts, "ARIAL_SHA256", hashlib.sha256(REGULAR).hexdigest()))
        self.get = stack.enter_context(patch.object(fonts.requests, "get", side_effect=self.response))

    def response(self, url, **kwargs):
        blob = self.blobs[url.rsplit("/", 1)[1]]
        response = Mock(status_code=200, url=url, history=(), headers={})
        response.iter_content.return_value = [blob[:30], blob[30:]]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    @unittest.skipUnless(shutil.which("cabextract"), "cabextract not installed")
    def test_real_extraction_consumes_exact_sealed_archives(self):
        run = subprocess.run

        def checked(args, **kwargs):
            self.assertNotIn("XDG_ACTIVATION_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["env"]["SYNTHETIC_WORKER_MARKER"], "preserved")
            descriptor = kwargs["pass_fds"][0]
            seals = fcntl.fcntl(descriptor, getattr(fcntl, "F_GET_SEALS", 1034))
            self.assertTrue(seals & 0x0008)
            with self.assertRaises(OSError):
                os.pwrite(descriptor, b"tamper", 0)
            self.assertEqual(os.pread(descriptor, fonts.MAX_ARCHIVE_BYTES, 0), self.blobs[
                "arialb32.exe" if "AriBlk.TTF" in args else "arial32.exe"])
            return run(args, **kwargs)

        with (patch.dict(os.environ, {"XDG_ACTIVATION_TOKEN": "synthetic-stale-token",
                                     "SYNTHETIC_WORKER_MARKER": "preserved"}),
              patch.object(fonts.subprocess, "run", side_effect=checked)):
            try:
                os.putenv("XDG_ACTIVATION_TOKEN", "synthetic-c-only-handoff")
                self.assertEqual(fonts._acquire_fonts(self.app), PAYLOAD)
            finally:
                os.unsetenv("XDG_ACTIVATION_TOKEN")
        self.assertEqual(self.get.call_count, 2)

    def test_either_archive_mismatch_stops_before_any_extraction(self):
        for name in self.blobs:
            with (self.subTest(name=name),
                  patch.object(fonts.shutil, "which", return_value="/fixture/cabextract"),
                  patch.object(fonts.subprocess, "run") as extract):
                original = self.blobs[name]
                self.blobs[name] += b"substitution"
                with self.assertRaisesRegex(maintenance.MaintenanceError, "checksum"):
                    fonts._acquire_fonts(self.app)
                self.blobs[name] = original
                extract.assert_not_called()

    def test_cancel_during_transfer_never_extracts(self):
        self.app.checkpoint.side_effect = UserExitedFromAsk
        with (patch.object(fonts.shutil, "which", return_value="/fixture/cabextract"),
              patch.object(fonts.subprocess, "run") as extract):
            with self.assertRaises(UserExitedFromAsk):
                fonts._acquire_fonts(self.app)
            extract.assert_not_called()

    def test_extraction_failure_and_wrong_identity_propagate(self):
        with (patch.object(fonts.shutil, "which", return_value="/fixture/cabextract"),
              patch.object(fonts.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "cabextract"))):
            with self.assertRaises(subprocess.CalledProcessError):
                fonts._acquire_fonts(self.app)
        with (patch.object(fonts.shutil, "which", return_value="/fixture/cabextract"),
              patch.object(fonts.subprocess, "run", return_value=SimpleNamespace(stdout=b"wrong family"))):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "identity"):
                fonts._acquire_fonts(self.app)

    def test_missing_extractor_refuses_before_transfer_or_extraction(self):
        with (patch.object(fonts.shutil, "which", return_value=None),
              patch.object(fonts.subprocess, "run") as extract):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "requires cabextract"):
                fonts._acquire_fonts(self.app)
            self.get.assert_not_called()
            extract.assert_not_called()


class FontRegistryParserTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(conf=SimpleNamespace(wine64_binary="fixture-wine"))
        self.key = fonts.FONT_KEYS[0]
        self.value = "Arial (TrueType)"

    def query(self, output, status=0):
        result = subprocess.CompletedProcess([], status, output)
        with patch.object(wine, "run_wine_completed_process", return_value=result):
            return wine.wine_reg_query(self.app, self.key, self.value)

    def test_exact_typed_value_and_spaces_are_preserved(self):
        for data in (r"C:\Font Files\Arial REG_SZ.ttf  ", " arial.ttf", "    arial.ttf", "\tarial.ttf"):
            with self.subTest(data=data):
                self.assertEqual(self.query(f"\n{self.key}\n    {self.value}    REG_SZ    {data}\n"), data)
        self.assertEqual(self.query(f"{self.key}\n    {self.value}    REG_SZ\n"), "")
        self.assertIsNone(self.query("not found", 1))

    def test_successful_malformed_output_never_becomes_absence(self):
        for output in ("", "private locator", f"{self.key}\n    {self.value} Extra    REG_SZ    secret",
                       f"{self.key}\n    {self.value}    REG_EXPAND_SZ    secret",
                       f"{self.key}\n    {self.value}    REG_SZ_MORE    secret",
                       f"wrong key\n    {self.value}    REG_SZ    secret",
                       f"{self.key}\n    {self.value}    REG_SZ    a\n    {self.value}    REG_SZ    b",
                       f"{self.key}\n    {self.value}    REG_SZ    a\x00b"):
            with self.subTest(output=output), self.assertNoLogs():
                with self.assertRaises(maintenance.MaintenanceError) as caught:
                    self.query(output)
                self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(maintenance.MaintenanceError, "exit code 2"):
            self.query("private locator", 2)
