import hashlib
from dataclasses import replace
import multiprocessing
from pathlib import Path
import tempfile
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
import olefile

from logos_desktop import artifacts, constants, installer, maintenance, msi as msi_format, network
from logos_desktop.app import UserExitedFromAsk
from tests.artifact_fixtures import (
    appimage, directory_msi, msi, mszip_cabinet, pe_header, product_msi,
    summary_properties, winetricks_zip,
)


def response(content=b"", status=200, headers=None):
    output = Mock(status_code=status, headers=headers or {}, content=content,
                  url="https://example.invalid/response", history=[])
    output.__enter__ = Mock(return_value=output)
    output.__exit__ = Mock(return_value=False)
    output.iter_content = Mock(return_value=iter((content,)))
    return output


def competing_store_use(state, ready, finish, results):
    constants.STATE_DIR = state
    with maintenance.artifact_use():
        ready.set()
        finish.wait(10)
    results.put("released")


class TestArtifacts(unittest.TestCase):
    def test_summary_vectors_never_reach_general_property_decoder(self):
        template = b"Intel;1033\0"
        scalars = [(1, struct.pack("<IH", 2, 1252)),
                   (7, struct.pack("<II", 30, len(template)) + template),
                   (15, struct.pack("<II", 3, 10))]
        vector = struct.pack("<II", 0x1000, 0xffffffff)
        file = self.root / "summary.msi"
        with patch.object(olefile.OleFileIO, "getproperties", side_effect=AssertionError("general decoder")):
            file.write_bytes(product_msi(summary_override=summary_properties(scalars + [(99, vector)])))
            artifacts.validate_msi(file, expectation=msi_format.MsiExpectation("Logos", "54.3.0.5"))
            for identifier in (1, 7, 15):
                with self.subTest(identifier=identifier):
                    properties = [(key, vector if key == identifier else value) for key, value in scalars]
                    file.write_bytes(product_msi(summary_override=summary_properties(properties)))
                    with self.assertRaises(artifacts.ArtifactError):
                        artifacts.validate_msi(file)

    def test_summary_rejects_malformed_catalogs_and_scalar_extents(self):
        template = struct.pack("<II", 30, 9) + b"x64;1033\0"
        valid = summary_properties([(7, template), (15, struct.pack("<II", 3, 10))])
        malformed = []
        for offset, value in ((52, 0xffffffff), (60, 8), (64, 7), (68, 24), (76, 0xffffffff)):
            content = bytearray(valid)
            struct.pack_into("<I", content, offset, value)
            malformed.append(content)
        malformed.append(summary_properties([(7, struct.pack("<II", 30, 1000) + b"x\0")]))
        malformed.append(summary_properties([(7, struct.pack("<II", 30, 3) + b"abc")]))
        malformed.append(summary_properties([(15, struct.pack("<II", 3, 10))]))
        for content in malformed:
            with self.subTest(content=content), self.assertRaises(ValueError):
                msi_format.summary_information(content)
        unicode_template = "x64;1033\0".encode("utf-16le")
        self.assertEqual(msi_format.summary_information(summary_properties([
            (1, struct.pack("<IH", 2, 1200)),
            (7, struct.pack("<II", 31, 9) + unicode_template),
        ]))[7], "x64;1033")

    def test_cfb_preflight_rejects_budgets_and_trees_before_constructor(self):
        file = self.root / "allocation.msi"
        cases = [
            (directory_msi(count=1, sectors=3), "CFB_DIRECTORY_LIMIT", 8, "entry budget"),
            (directory_msi(count=12), "CFB_DIRECTORY_LIMIT", 8, "entry budget"),
            (directory_msi(count=8), "CFB_DEPTH_LIMIT", 3, "depth budget"),
            (directory_msi(count=8, nested=True), "CFB_DEPTH_LIMIT", 3, "depth budget"),
            (directory_msi(count=8, cycle=True), "CFB_DEPTH_LIMIT", 64, "repeated"),
            (directory_msi(), "CFB_FAT_LIMIT", 511, "FAT byte budget"),
        ]
        for content, budget, limit, message in cases:
            file.write_bytes(content)
            with (self.subTest(budget=budget, message=message),
                  patch.object(msi_format, budget, limit),
                  patch.object(artifacts.olefile, "OleFileIO") as constructor,
                  self.assertRaisesRegex(artifacts.ArtifactError, message)):
                artifacts.validate_msi(file)
            constructor.assert_not_called()

    def test_cfb_preflight_rejects_invalid_allocation_and_references(self):
        file = self.root / "allocation.msi"
        for offset, value in ((44, 0xffffffff), (72, 0xffffffff), (512 + 4, 1),
                              (1024 + 76, 0xffffff00), (1024 + 128 + 68, 1),
                              (1024 + 120, msi_format.METADATA_LIMIT + 1), (64, 0xffffffff)):
            content = bytearray(directory_msi())
            struct.pack_into("<I", content, offset, value)
            file.write_bytes(content)
            with (self.subTest(offset=offset),
                  patch.object(artifacts.olefile, "OleFileIO") as constructor,
                  self.assertRaises(artifacts.ArtifactError)):
                artifacts.validate_msi(file)
            constructor.assert_not_called()

    def test_cfb_fat_catalog_padding_matches_olefile_termination(self):
        header_hole = bytearray(directory_msi(fat_count=3))
        struct.pack_into("<3I", header_hole, 76, 0, 0xffffffff, 2)
        difat_hole = bytearray(directory_msi(fat_count=110))
        difat_offset = (110 + 2 + 1) * 512
        struct.pack_into("<2I", difat_hole, difat_offset, 0xffffffff, 109)
        file = self.root / "catalog.msi"
        for content in (header_hole, difat_hole):
            file.write_bytes(content)
            with (self.subTest(length=len(content)),
                  patch.object(artifacts.olefile, "OleFileIO") as constructor,
                  self.assertRaisesRegex(artifacts.ArtifactError, "references after padding")):
                artifacts.validate_msi(file)
            constructor.assert_not_called()

    def test_cfb_distinct_fat_sectors_have_a_separate_constructor_work_budget(self):
        file = self.root / "fat-work.msi"
        content = directory_msi(fat_count=3)
        file.write_bytes(content.ljust(258 * 512, b"\0"))
        with file.open("rb") as source:
            msi_format.preflight(source)
        with (patch.object(msi_format, "CFB_FAT_LIMIT", 3 * 512),
              patch.object(msi_format, "CFB_FAT_WORK_LIMIT", 5 * 512),
              patch.object(artifacts.olefile, "OleFileIO") as constructor,
              self.assertRaisesRegex(artifacts.ArtifactError, "constructor-work budget")):
            artifacts.validate_msi(file)
        constructor.assert_not_called()
        file.write_bytes(content)
        with (patch.object(artifacts.olefile, "OleFileIO") as constructor,
              self.assertRaisesRegex(artifacts.ArtifactError, "FAT capacity")):
            artifacts.validate_msi(file)
        constructor.assert_not_called()

    def test_cfb_preflight_cancellation_precedes_size_rejection_and_constructor(self):
        file = self.root / "allocation.msi"
        file.write_bytes(directory_msi())
        for stop in (1, 4, 12):
            calls = 0

            def checkpoint():
                nonlocal calls
                calls += 1
                if calls == stop:
                    raise UserExitedFromAsk

            with (self.subTest(stop=stop),
                  patch.object(artifacts.olefile, "OleFileIO") as constructor,
                  self.assertRaises(UserExitedFromAsk)):
                artifacts.validate_msi(file, checkpoint)
            constructor.assert_not_called()
        with (patch.object(msi_format, "CABINET_LIMIT", 0),
              patch.object(msi_format, "METADATA_LIMIT", 0),
              self.assertRaises(UserExitedFromAsk)):
            artifacts.validate_msi(file, Mock(side_effect=UserExitedFromAsk))

    def test_cfb_parser_uses_preflight_descriptor_after_path_replacement(self):
        file = self.root / "original.msi"
        file.write_bytes(msi())
        replacement = self.root / "replacement.msi"
        replacement.write_bytes(b"not a compound file")
        preflight = msi_format.preflight
        identities = []

        def replace_path(source, checkpoint):
            identity = preflight(source, checkpoint)
            identities.append(source)
            replacement.replace(file)
            return identity

        constructor = olefile.OleFileIO

        def open_database(source, **kwargs):
            self.assertIs(source, identities[0])
            return constructor(source, **kwargs)

        with (patch.object(msi_format, "preflight", side_effect=replace_path),
              patch.object(artifacts.olefile, "OleFileIO", side_effect=open_database)):
            # Replacing a path can change the old inode's ctime. Either reject
            # that metadata change or finish validating only the original inode.
            try:
                artifacts.validate_msi(file)
            except artifacts.ArtifactError as error:
                self.assertIn("changed while being read", str(error))
        self.assertEqual(file.read_bytes(), b"not a compound file")

    def test_installer_counters_reset_after_success_failure_and_cancellation(self):
        for error in (None, OSError("fixture failure"), UserExitedFromAsk()):
            app = SimpleNamespace(
                conf=SimpleNamespace(faithlife_product_release="38.0.0.0"),
                status=Mock(), _config_updated_event=Mock(),
            )

            def install_steps(selected_app):
                selected_app.installer_step = 3
                selected_app.installer_step_count = 9
                if error:
                    raise error

            with patch.object(installer, "ensure_launcher_shortcuts", side_effect=install_steps):
                if error:
                    with self.assertRaises(type(error)):
                        installer._install(app)
                else:
                    installer._install(app)
            self.assertEqual(app.installer_step, 0)
            self.assertEqual(app.installer_step_count, 0)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = patch.object(constants, "STATE_DIR", str(self.root / "state/app"))
        self.state.start()
        self.addCleanup(self.state.stop)
        self.content = winetricks_zip()
        self.spec = artifacts.ArtifactSpec("https://example.invalid/tag.zip", "tag.zip", "winetricks-zip")
        self.old = self.root / "tag.zip"
        self.old.write_bytes(b"previous good file")

    def acquire(self, result, *, spec=None, candidates=(), head=None):
        with (
            patch.object(artifacts.requests, "head", return_value=head or response()),
            patch.object(artifacts.requests, "get", return_value=result),
        ):
            return artifacts.acquire(spec or self.spec, self.root, candidates)

    def assert_preserved(self):
        self.assertEqual(self.old.read_bytes(), b"previous good file")
        self.assertEqual(list(self.root.glob(".artifacts/.incoming-*")), [])

    def test_status_failures_preserve_target_and_never_publish(self):
        for status in (404, 500, 204, 206, 304):
            with self.subTest(status=status), self.assertRaises(artifacts.ArtifactError):
                self.acquire(response(b"error", status))
            self.assert_preserved()

    def test_cancel_during_cached_hash_or_copy_never_falls_back_to_network(self):
        content = b"#!/bin/sh\n" + b"# fixture\n" * 300000
        source = self.root / "cached-script"
        source.write_bytes(content)
        spec = artifacts.ArtifactSpec(
            self.spec.url, "winetricks", "winetricks",
            artifacts.Digest("sha256", hashlib.sha256(content).hexdigest(), "fixture"),
        )
        for phase in ("Checking cached", "Copying cached"):
            active = False
            checkpoints = 0

            def status(message, percent):
                nonlocal active
                active = message.startswith(phase)

            def checkpoint():
                nonlocal checkpoints
                if active:
                    checkpoints += 1
                    if checkpoints == 2:
                        raise UserExitedFromAsk

            with (
                self.subTest(phase=phase),
                patch.object(artifacts.requests, "get") as get,
                self.assertRaises(UserExitedFromAsk),
            ):
                artifacts.acquire(spec, self.root, [source], status, checkpoint=checkpoint)
            get.assert_not_called()
            self.assertEqual(source.read_bytes(), content)
            self.assert_preserved()

    def test_cancel_after_received_chunk_cleans_stage_and_preserves_old_target(self):
        cancelled = False
        transfer = response()

        def chunks(chunk_size):
            nonlocal cancelled
            yield b"x" * chunk_size
            yield b"x" * chunk_size
            stages = list(self.root.glob(".artifacts/.incoming-*/tag.zip"))
            self.assertGreater(stages[0].stat().st_size, 0)
            cancelled = True
            yield b"second chunk"

        def checkpoint():
            if cancelled:
                raise UserExitedFromAsk

        transfer.iter_content = chunks
        with (
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=transfer),
            self.assertRaises(UserExitedFromAsk),
        ):
            artifacts.acquire(self.spec, self.root, checkpoint=checkpoint)
        self.assert_preserved()

    def test_late_cancel_after_artifact_publication_returns_accepted_bytes(self):
        cancelled = False
        rename = artifacts.paths.rename_no_replace

        def publish(source, target):
            nonlocal cancelled
            rename(source, target)
            cancelled = True

        def checkpoint():
            if cancelled:
                raise UserExitedFromAsk

        with (
            patch.object(artifacts.paths, "rename_no_replace", side_effect=publish),
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(self.content)),
        ):
            accepted = artifacts.acquire(self.spec, self.root, checkpoint=checkpoint)
        self.assertTrue(cancelled)
        self.assertEqual(accepted.check().read_bytes(), self.content)
        self.assert_preserved()

    def test_get_size_and_digest_mismatch(self):
        from base64 import b64encode

        for headers in (
            {"Content-Length": str(len(self.content) + 1)},
            {"Content-MD5": b64encode(b"x" * 16).decode()},
            {"Content-Encoding": "gzip"},
        ):
            with self.subTest(headers=headers), self.assertRaises(artifacts.ArtifactError):
                self.acquire(response(self.content, headers=headers))
            self.assert_preserved()

    def test_expected_digest_remains_required_without_get_header(self):
        spec = artifacts.ArtifactSpec(self.spec.url, self.spec.name, self.spec.kind,
                                      artifacts.Digest("sha256", "0" * 64, "release"))
        with self.assertRaisesRegex(artifacts.ArtifactError, "digest mismatch"):
            self.acquire(response(self.content), spec=spec)
        self.assert_preserved()

    def test_metadata_free_and_malformed_optional_hints_need_format(self):
        for headers in ({}, {"Content-Length": "garbage", "Content-MD5": "wrong"}):
            with self.subTest(headers=headers):
                accepted = self.acquire(response(self.content, headers=headers))
                self.assertEqual(accepted.path.read_bytes(), self.content)
                self.assertIn("completed HTTPS", accepted.evidence)
                self.assert_preserved()
                with self.assertRaises(artifacts.ArtifactError):
                    self.acquire(response(b"<html>not an archive</html>", headers=headers))

    def test_interruption_writes_staging_then_retry_starts_from_zero(self):
        reached = []
        interrupted = response()

        def chunks(chunk_size):
            self.assertGreater(chunk_size, 0)
            yield b"x" * chunk_size
            yield b"x" * chunk_size
            stages = list(self.root.glob(".artifacts/.incoming-*/tag.zip"))
            self.assertEqual(len(stages), 1)
            self.assertGreater(stages[0].stat().st_size, 0)
            self.assertTrue(stages[0].read_bytes().startswith(b"x" * chunk_size))
            reached.append(stages[0])
            raise requests.exceptions.ChunkedEncodingError("intended post-chunk interruption")

        interrupted.iter_content = chunks
        with self.assertRaisesRegex(artifacts.ArtifactError, "ChunkedEncodingError"):
            self.acquire(interrupted)
        self.assertEqual(len(reached), 1)
        self.assert_preserved()
        with (
            patch.object(artifacts.requests, "head", return_value=response()),
            patch.object(artifacts.requests, "get", return_value=response(self.content)) as get,
        ):
            accepted = artifacts.acquire(self.spec, self.root)
        self.assertNotEqual(accepted.path, reached[0])
        self.assertNotIn("Range", get.call_args.kwargs["headers"])
        self.assertEqual(accepted.path.read_bytes(), self.content)

    def test_size_only_or_opaque_etag_cache_is_not_reused(self):
        for etag in ('"1234567890abcdef1234567890abcdef"', '"abc-2"', 'W/"abc"', ""):
            with self.subTest(etag=etag):
                head = response(headers={"Content-Length": str(len(self.old.read_bytes())),
                                         "Server": "AmazonS3", "ETag": etag})
                accepted = self.acquire(response(self.content), candidates=[self.old], head=head)
                self.assertEqual(accepted.path.read_bytes(), self.content)
                self.assert_preserved()

    def test_digest_candidate_is_snapshotted_and_mutation_is_rejected(self):
        source = self.root / "candidate"
        source.write_bytes(self.content)
        digest = artifacts.Digest("sha256", hashlib.sha256(self.content).hexdigest(), "release")
        spec = artifacts.ArtifactSpec(self.spec.url, self.spec.name, self.spec.kind, digest)
        with patch.object(artifacts.requests, "get", side_effect=AssertionError("cache should be accepted")):
            accepted = artifacts.acquire(spec, self.root, [source])
        source.write_bytes(b"changed")
        self.assertEqual(accepted.check().read_bytes(), self.content)
        self.assert_preserved()

    def test_publication_and_sync_errors_preserve_sources(self):
        for function in ("fsync",):
            with patch.object(artifacts.os, function, side_effect=OSError("fixture I/O")):
                with self.assertRaises(artifacts.ArtifactError):
                    self.acquire(response(self.content))
            self.assert_preserved()
        with patch.object(artifacts.paths, "rename_no_replace", side_effect=PermissionError("denied")):
            with self.assertRaises(artifacts.ArtifactError):
                self.acquire(response(self.content))
        self.assert_preserved()

    def test_same_name_different_bytes_have_stable_distinct_paths(self):
        first = self.acquire(response(self.content))
        second_bytes = winetricks_zip(b"#!/bin/sh\nexit 1\n")
        second = self.acquire(response(second_bytes))
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(first.check().read_bytes(), self.content)
        self.assertEqual(second.check().read_bytes(), second_bytes)

    def test_required_format_and_architecture_use_real_container_readers(self):
        for kind, content in (("appimage", appimage()), ("msi", msi())):
            path = self.root / kind
            path.write_bytes(content)
            artifacts.validate_format(path, kind)
            path.write_bytes(content[:100])
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.validate_format(path, kind)
        for content in (msi(architecture="Intel"), msi(installer=False)):
            path = self.root / "bad.msi"
            path.write_bytes(content)
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.validate_format(path, "msi")
        wrong = bytearray(appimage())
        wrong[18:20] = b"\xb7\x00"
        path.write_bytes(wrong)
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.validate_format(path, "appimage")

    def product_path(self, **kwargs):
        path = self.root / "product.msi"
        path.write_bytes(product_msi(**kwargs))
        return path

    def validate_product(self, path, product="Logos", release="54.3.0.0005", checkpoint=None):
        artifacts.validate_format(path, "msi", checkpoint,
                                  msi_expectation=artifacts.MsiExpectation(product, release))

    def test_intel_product_requires_expectation_and_mapped_amd64_payload(self):
        path = self.product_path()
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.validate_format(path, "msi")
        self.validate_product(path)
        # This exercises the role matcher, not a claim about vendor Verbum branding.
        path = self.product_path(product="Verbum", directory="Verbum", product_name="Verbum fixture")
        self.validate_product(path, product="Verbum")

    def test_product_expectations_strengthen_x64_without_changing_generic_msi_contract(self):
        path = self.product_path(architecture="x64")
        self.validate_product(path)
        with self.assertRaisesRegex(artifacts.ArtifactError, "version mismatch"):
            self.validate_product(path, release="54.3.0.6")
        path.write_bytes(msi())
        artifacts.validate_format(path, "msi")
        with self.assertRaisesRegex(artifacts.ArtifactError, "product"):
            self.validate_product(path)

    def test_product_identity_version_and_primary_mapping_mismatches_fail(self):
        cases = (
            {"product_name": "LogosUnrelated"}, {"product_name": "Verbum fixture"},
            {"release": "54.3.0.6"}, {"release": "54.3.5"},
            {"product_version": "not-a-version"}, {"product_version": "256.3.5"},
            {"directory": "Pending"}, {"directory": "../Logos"}, {"duplicate": True},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(self.product_path(**kwargs))

    def test_pending_duplicate_cannot_replace_root_executable_attestation(self):
        self.validate_product(self.product_path(pending=True))
        with self.assertRaisesRegex(artifacts.ArtifactError, "version mismatch"):
            self.validate_product(self.product_path(pending=True, release="54.3.0.6"))

    def test_windows_target_case_and_short_long_names_preserve_exact_identifier_joins(self):
        for kwargs in ({"filename": "LOGOS.EXE", "directory": "logos", "pending": True},
                       {"filename": "LOGOS~1.EXE|LOGOS.EXE", "directory": "LOGOS~1|logos:."}):
            with self.subTest(kwargs=kwargs):
                self.validate_product(self.product_path(**kwargs))

    def test_case_equivalent_file_and_directory_targets_never_publish(self):
        spec = artifacts.ArtifactSpec("https://example.invalid/product.msi", "Logos.msi", "msi",
                                      msi_expectation=artifacts.MsiExpectation("Logos", "54.3.0.5"))
        for kwargs in ({"duplicate_filename": "LOGOS.EXE"}, {"root_alias": "logos"},
                       {"duplicate_filename": "Logos.exe|Other.exe"}, {"root_alias": "LOGOS|Other"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(artifacts.ArtifactError, "Case-equivalent"):
                self.acquire(response(product_msi(**kwargs)), spec=spec)
            self.assert_preserved()
            self.assertEqual(list(self.root.glob(f".artifacts/{spec.key}/*")), [])

    def test_windows_trailing_dot_space_and_path_aliases_are_rejected(self):
        for filename in ("LOGOS.EXE.", "Logos.exe ", "../Logos.exe", "Logos.exe:stream",
                         "Logos.exe\\..", ".", "..", "Logos.exe\0x"):
            with self.subTest(filename=filename), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(self.product_path(filename=filename))
        for directory in ("logos.", "LOGOS ", "../Logos", "Logos:../source", ".."):
            with self.subTest(directory=directory), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(self.product_path(root_alias=directory))

    def directory_alias_msi(self, directories, alias, filename="Logos.exe", *, machine=0x14c,
                            release="54.3.0.6"):
        image = bytearray(pe_header(machine))
        if machine == 0x14c:
            struct.pack_into("<H", image, 148, 224)
            struct.pack_into("<H", image, 152, 0x10b)
        return product_msi(
            extra_directories=directories, extra_components=(("alias_component", alias),),
            extra_files=(("alias_exe", "alias_component", filename, len(image), release, 512, 2),),
            cabinet=mszip_cabinet(second_image=bytes(image)),
        )

    def test_effective_dot_directory_alias_collisions_never_publish(self):
        layouts = (
            ((("DOT", "INSTALLFOLDER", "."),), "DOT", "Logos.exe"),
            ((("DOT", "INSTALLFOLDER", ".:."),), "DOT", "LOGOS.EXE"),
            ((("DOT", "INSTALLFOLDER", "."), ("NESTED", "DOT", ".")),
             "NESTED", "LOGOS~1.EXE|LoGoS.ExE"),
            ((("DOT", "INSTALLFOLDER", "."),), "DOT", "Logos.exe|Other.exe"),
            ((("DOT_UP", "LocalAppDataFolder", "."), ("OTHER_ROOT", "DOT_UP", "logos")),
             "OTHER_ROOT", "Logos.exe"),
        )
        spec = artifacts.ArtifactSpec("https://example.invalid/product.msi", "Logos.msi", "msi",
                                      msi_expectation=artifacts.MsiExpectation("Logos", "54.3.0.5"))
        for directories, alias, filename in layouts:
            content = self.directory_alias_msi(directories, alias, filename)
            with self.subTest(alias=alias, filename=filename), self.assertRaisesRegex(
                artifacts.ArtifactError, "Case-equivalent",
            ):
                self.acquire(response(content), spec=spec)
            self.assertEqual(list(self.root.glob(f".artifacts/{spec.key}/*")), [])
            self.assert_preserved()

    def test_benign_dot_aliases_and_pending_remain_distinct_from_application_root(self):
        self.validate_product(self.product_path(root_alias="."))
        self.validate_product(self.product_path(
            root_parent="DOT_UP", extra_directories=(("DOT_UP", "LocalAppDataFolder", ".:."),),
        ))
        content = self.directory_alias_msi(
            (("PENDING", "INSTALLFOLDER", "Pending:."), ("PENDING_DOT", "PENDING", ".")),
            "PENDING_DOT", "LOGOS.EXE", machine=0x8664, release="54.3.0.5",
        )
        path = self.root / "pending-dot.msi"
        path.write_bytes(content)
        self.validate_product(path)

    def test_dot_alias_rejection_survives_local_import_and_cache_revalidation(self):
        spec = artifacts.ArtifactSpec("https://example.invalid/product.msi", "Logos.msi", "msi",
                                      msi_expectation=artifacts.MsiExpectation("Logos", "54.3.0.5"))
        good = product_msi()
        accepted = self.acquire(response(good), spec=spec)
        content = self.directory_alias_msi((("DOT", "INSTALLFOLDER", "."),), "DOT")
        source = self.root / "dot-alias-cache.msi"
        source.write_bytes(content)
        with self.assertRaisesRegex(artifacts.ArtifactError, "Case-equivalent"):
            artifacts.import_local(source, self.root, spec)
        digest = artifacts.Digest("sha256", hashlib.sha256(content).hexdigest(), "fixture")
        with (patch.object(artifacts.requests, "get", return_value=response(content)),
              self.assertRaisesRegex(artifacts.ArtifactError, "Case-equivalent")):
            artifacts.acquire(replace(spec, digest=digest), self.root, [source])
        self.assertEqual(source.read_bytes(), content)
        self.assertEqual(accepted.check().read_bytes(), good)
        self.assertEqual(list(accepted.path.parent.parent.iterdir()), [accepted.path.parent])
        self.assert_preserved()

    def test_directory_graph_cycles_missing_parents_and_malformed_rows_fail(self):
        layouts = (
            (("LOOP", "LOOP", "."),),
            (("FIRST", "SECOND", "."), ("SECOND", "FIRST", ".")),
            (("ORPHAN", "MISSING", "."),),
            (("INVALID", "INSTALLFOLDER", None),),
            (("BAD/ID", "INSTALLFOLDER", "."),),
            (("UNSUPPORTED", "TARGETDIR", "."),),
        )
        for directories in layouts:
            with self.subTest(directories=directories), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(self.product_path(extra_directories=directories))
        with self.assertRaisesRegex(artifacts.ArtifactError, "component target directory"):
            self.validate_product(self.product_path(extra_components=(("orphan", "MISSING"),)))

    def test_directory_graph_depth_node_and_work_budgets_and_cancellation(self):
        chain = [(f"DOT{index}", "INSTALLFOLDER" if index == 0 else f"DOT{index - 1}", ".")
                 for index in range(6)]
        path = self.product_path(extra_directories=chain)
        for limit, value in (("DIRECTORY_LIMIT", 4), ("DIRECTORY_DEPTH_LIMIT", 4),
                             ("DIRECTORY_WORK_LIMIT", 16)):
            with self.subTest(limit=limit), patch.object(msi_format, limit, value):
                with self.assertRaisesRegex(artifacts.ArtifactError, "directory-.*budget"):
                    self.validate_product(path)
        database = self.metadata_database(path)
        parsed = 0
        parse = msi_format.target_names

        def count_names(*args, **kwargs):
            nonlocal parsed
            result = parse(*args, **kwargs)
            parsed += 1
            return result

        def checkpoint():
            if parsed == 3 + len(chain):
                raise UserExitedFromAsk

        with (patch.object(msi_format, "target_names", side_effect=count_names),
              patch.object(database, "checkpoint", new=checkpoint)):
            with self.assertRaises(UserExitedFromAsk):
                msi_format.application_directories(database, "Logos")
        self.assertEqual(parsed, 3 + len(chain))

    def test_shared_directory_suffixes_resolve_with_linear_work_budget(self):
        chain = [(f"DOT{index}", "INSTALLFOLDER" if index == 0 else f"DOT{index - 1}", ".")
                 for index in range(8)]
        branches = [(f"LEAF{index}", "DOT7", f"Leaf{index}") for index in range(24)]
        path = self.product_path(extra_directories=[*reversed(chain), *branches])
        with patch.object(msi_format, "DIRECTORY_WORK_LIMIT", 4 * (3 + len(chain) + len(branches))):
            self.validate_product(path)

    def metadata_database(self, path, checkpoint=None):
        database = olefile.OleFileIO(path)
        self.addCleanup(database.close)
        names = {artifacts._msi_name(parts[0]): parts for parts in database.listdir() if len(parts) == 1}
        return msi_format.Database(database, names, checkpoint)

    def test_repeated_pool_references_share_decoded_text_and_project_only_requested_fields(self):
        value = "repeated-value-" * 128
        path = self.product_path(extra_properties=[("Repeated", value)] * 32)
        with patch.object(msi_format, "TEXT_LIMIT", 16 * 1024):
            database = self.metadata_database(path)
            records = list(database.table("Property", ("Property", "Value")))
        shared = records[2]["Value"]
        self.assertEqual(shared, value)
        self.assertTrue(all(record["Value"] is shared for record in records[2:]))
        with patch.object(msi_format, "TEXT_LIMIT", 2048):
            database = self.metadata_database(path)
            records = list(database.table("Property", ("Property",)))
        self.assertEqual(len(records), 34)
        self.assertTrue(all(set(record) == {"Property"} for record in records))

    def test_duplicate_keys_stop_projected_rows_incrementally(self):
        path = self.product_path(extra_properties=[("Repeated", "x" * 2048)] * 32)
        database = self.metadata_database(path)
        before = database.row_count
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            msi_format.keyed(database.table("Property", ("Property", "Value")), "Property")
        self.assertEqual(database.row_count - before, 4)

    def test_metadata_row_object_text_and_catalog_budgets_fail_without_large_allocations(self):
        path = self.product_path(extra_properties=[("Repeated", "x" * 2048)] * 32)
        database = self.metadata_database(path)
        with patch.object(msi_format, "ROW_LIMIT", database.row_count + 2):
            with self.assertRaisesRegex(ValueError, "row budget"):
                list(database.table("Property", ("Property", "Value")))
        with patch.object(msi_format, "OBJECT_LIMIT", database.object_count + 1):
            with self.assertRaisesRegex(ValueError, "object budget"):
                next(database.table("Property", ("Property", "Value")))
        with patch.object(msi_format, "TEXT_LIMIT", 2048):
            with self.assertRaisesRegex(artifacts.ArtifactError, "expanded-text budget"):
                self.validate_product(path)
        for limit, value in (("TABLE_LIMIT", 4), ("COLUMN_LIMIT", 4), ("TABLE_COLUMN_LIMIT", 1),
                             ("STRING_LIMIT", 4), ("STREAM_LIMIT", 1)):
            with self.subTest(limit=limit), patch.object(msi_format, limit, value):
                with self.assertRaisesRegex(artifacts.ArtifactError, "budget"):
                    self.validate_product(path)

    def test_metadata_budget_failure_and_cancel_preserve_good_cache_and_staging(self):
        spec = artifacts.ArtifactSpec("https://example.invalid/product.msi", "Logos.msi", "msi",
                                      msi_expectation=artifacts.MsiExpectation("Logos", "54.3.0.5"))
        good = product_msi()
        accepted = self.acquire(response(good), spec=spec)
        value = "x" * 2048
        content = product_msi(extra_properties=[("Repeated", value)] * 32)
        source = self.root / "repeated-cache.msi"
        source.write_bytes(content)
        digest = artifacts.Digest("sha256", hashlib.sha256(content).hexdigest(), "fixture")
        cached_spec = replace(spec, digest=digest)
        with (patch.object(msi_format, "TEXT_LIMIT", 2048),
              patch.object(artifacts.requests, "get", return_value=response(content)),
              self.assertRaisesRegex(artifacts.ArtifactError, "expanded-text budget")):
            artifacts.acquire(cached_spec, self.root, [source])
        self.assert_preserved()
        cancelled = False
        retain = msi_format.Database.retain_text

        def retain_then_cancel(database, text):
            nonlocal cancelled
            result = retain(database, text)
            if text == value:
                cancelled = True
            return result

        def checkpoint():
            if cancelled:
                raise UserExitedFromAsk

        with (patch.object(msi_format.Database, "retain_text", new=retain_then_cancel),
              patch.object(artifacts.requests, "get") as get, self.assertRaises(UserExitedFromAsk)):
            artifacts.acquire(cached_spec, self.root, [source], checkpoint=checkpoint)
        get.assert_not_called()
        self.assertTrue(cancelled)
        self.assertEqual(source.read_bytes(), content)
        self.assertEqual(accepted.check().read_bytes(), good)
        self.assertEqual(list(accepted.path.parent.parent.iterdir()), [accepted.path.parent])
        self.assert_preserved()

    def test_unsupported_platforms_stay_rejected_with_product_expectation(self):
        for platform in ("Arm", "Arm64", "Intel64", "Intel,x64", "x64,Intel", "", "unknown", "Intel;1033"):
            with self.subTest(platform=platform), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(self.product_path(architecture=platform))
        path = self.product_path()
        with olefile.OleFileIO(path, write_mode=True) as database:
            summary = bytearray(database.openstream("\x05SummaryInformation").read())
            struct.pack_into("<I", summary, 64, 8)
            database.write_stream("\x05SummaryInformation", bytes(summary))
        with self.assertRaises(artifacts.ArtifactError):
            self.validate_product(path)

    def test_wrong_pe_architectures_and_unsupported_cab_compression_fail(self):
        for cabinet in (mszip_cabinet(machine=0x14c), mszip_cabinet(machine=0xaa64),
                        mszip_cabinet(compression=3), mszip_cabinet(member="../root_exe")):
            with self.subTest(prefix=cabinet[:8]), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(self.product_path(cabinet=cabinet))

    def test_primary_pe_signature_extent_and_optional_format_are_checked(self):
        for offset, value in ((0, b"NO"), (60, struct.pack("<I", 65536)), (152, b"\x0b\x01")):
            image = bytearray(pe_header())
            image[offset:offset + len(value)] = value
            with self.subTest(offset=offset), self.assertRaisesRegex(artifacts.ArtifactError, "PE"):
                self.validate_product(self.product_path(cabinet=mszip_cabinet(image=bytes(image))))

    def test_cabinet_is_never_eagerly_opened_by_olefile(self):
        path = self.product_path()
        original = olefile.OleFileIO.openstream

        def bounded_open(database, parts):
            name = parts if isinstance(parts, str) else parts[0]
            self.assertNotEqual(artifacts._msi_name(name), "product.cab")
            return original(database, parts)

        with patch.object(olefile.OleFileIO, "openstream", new=bounded_open):
            self.validate_product(path)

    def test_cab_checksums_lengths_and_deflate_streams_are_validated(self):
        valid = mszip_cabinet()
        data_offset = struct.unpack_from("<I", valid, 36)[0]
        corrupted = bytearray(valid)
        corrupted[data_offset + 12] ^= 1
        wrong_length = bytearray(valid)
        struct.pack_into("<H", wrong_length, data_offset + 6, 32769)
        truncated = bytearray(valid[:-20])
        struct.pack_into("<I", truncated, 8, len(truncated))
        invalid_deflate = bytearray(valid)
        struct.pack_into("<I", invalid_deflate, data_offset, 0)
        invalid_deflate[data_offset + 10] = 0x07
        bad_signature = bytearray(valid)
        struct.pack_into("<I", bad_signature, data_offset, 0)
        bad_signature[data_offset + 8] = 0
        oversized_member = bytearray(valid)
        struct.pack_into("<I", oversized_member, 44 + 4, 41000)
        for cabinet in (corrupted, wrong_length, truncated, invalid_deflate, bad_signature, oversized_member):
            with self.subTest(), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(self.product_path(cabinet=bytes(cabinet)))

    def test_mszip_fixture_really_requires_prior_block_dictionary(self):
        import zlib

        cabinet = mszip_cabinet()
        start = struct.unpack_from("<I", cabinet, 36)[0]
        packed = struct.unpack_from("<H", cabinet, start + 4)[0]
        second = start + 8 + packed
        packed = struct.unpack_from("<H", cabinet, second + 4)[0]
        with self.assertRaises(zlib.error):
            zlib.decompress(cabinet[second + 10:second + 8 + packed], wbits=-15)
        self.validate_product(self.product_path(cabinet=cabinet))

    def test_cab_exact_content_aliases_are_allowed_but_partial_overlaps_are_not(self):
        self.validate_product(self.product_path(cabinet=mszip_cabinet(alias_offset=40000)))
        with self.assertRaisesRegex(artifacts.ArtifactError, "overlapping"):
            self.validate_product(self.product_path(cabinet=mszip_cabinet(alias_offset=40001)))
        duplicate = mszip_cabinet(alias_offset=40000).replace(b"alias_exe\0", b"root_exe\0\0")
        with self.assertRaisesRegex(artifacts.ArtifactError, "duplicate"):
            self.validate_product(self.product_path(cabinet=duplicate))

    def test_cab_attestation_budgets_and_cancellation_are_enforced(self):
        path = self.product_path()
        for name, value in (("OUTPUT_LIMIT", 40000), ("COMPRESSED_LIMIT", 100), ("BLOCK_LIMIT", 1)):
            with self.subTest(limit=name), patch.object(msi_format, name, value):
                with self.assertRaisesRegex(artifacts.ArtifactError, "budget"):
                    self.validate_product(path)
        for name, value in (("METADATA_LIMIT", 4), ("CABINET_LIMIT", 4096)):
            with self.subTest(limit=name), patch.object(msi_format, name, value):
                with self.assertRaises(artifacts.ArtifactError):
                    self.validate_product(path)
        decoding = False
        original = msi_format.zlib.decompressobj

        def decoder(*args, **kwargs):
            nonlocal decoding
            decoding = True
            return original(*args, **kwargs)

        def checkpoint():
            if decoding:
                raise UserExitedFromAsk

        with patch.object(msi_format.zlib, "decompressobj", side_effect=decoder):
            with self.assertRaises(UserExitedFromAsk):
                self.validate_product(path, checkpoint=checkpoint)

    def test_cancel_during_cached_msi_attestation_preserves_old_generation(self):
        content = product_msi()
        source = self.root / "cached.msi"
        source.write_bytes(content)
        spec = artifacts.ArtifactSpec(
            "https://example.invalid/product.msi", "Logos.msi", "msi",
            artifacts.Digest("sha256", hashlib.sha256(content).hexdigest(), "fixture"),
            msi_expectation=artifacts.MsiExpectation("Logos", "54.3.0.0005"),
        )
        original = msi_format.zlib.decompressobj
        decoding = False

        def decoder(*args, **kwargs):
            nonlocal decoding
            decoding = True
            return original(*args, **kwargs)

        def checkpoint():
            if decoding:
                raise UserExitedFromAsk

        with (patch.object(msi_format.zlib, "decompressobj", side_effect=decoder),
              patch.object(artifacts.requests, "get") as get, self.assertRaises(UserExitedFromAsk)):
            artifacts.acquire(spec, self.root, [source], checkpoint=checkpoint)
        get.assert_not_called()
        self.assertEqual(source.read_bytes(), content)
        self.assert_preserved()

    def test_invalid_expectations_never_admit_intel_or_other_artifact_roles(self):
        path = self.product_path()
        for expectation in (artifacts.MsiExpectation("Other", "54.3.0.5"),
                            artifacts.MsiExpectation("Logos", "54.3.5"),
                            artifacts.MsiExpectation("Logos", "54.3.0.65536"), "Logos"):
            with self.subTest(expectation=expectation), self.assertRaises(artifacts.ArtifactError):
                artifacts.validate_format(path, "msi", msi_expectation=expectation)
        with self.assertRaises(artifacts.ArtifactError):
            replace(self.spec, msi_expectation=artifacts.MsiExpectation("Logos", "54.3.0.5")).validate()

    def test_cab_reader_rejects_fat_cycles_and_truncated_sector_chains(self):
        for cycle in (True, False):
            path = self.product_path()
            with olefile.OleFileIO(path) as database:
                parts = next(parts for parts in database.listdir() if artifacts._msi_name(parts[0]) == "product.cab")
                first = database.direntries[database._find(parts)].isectStart
            content = bytearray(path.read_bytes())
            struct.pack_into("<I", content, 512 + first * 4, first if cycle else 0xfffffffe)
            path.write_bytes(content)
            with self.subTest(cycle=cycle), self.assertRaises(artifacts.ArtifactError):
                self.validate_product(path)
        for offset, form, value in ((1024 + 120, "<Q", msi_format.METADATA_LIMIT + 1),
                                    (64, "<I", 0x100000)):
            path = self.product_path()
            content = bytearray(path.read_bytes())
            struct.pack_into(form, content, offset, value)
            path.write_bytes(content)
            with self.subTest(offset=offset), self.assertRaisesRegex(artifacts.ArtifactError, "MiniFAT|mini stream"):
                self.validate_product(path)

    def test_product_expectation_survives_download_publication_and_recheck(self):
        content = product_msi()
        expectation = artifacts.MsiExpectation("Logos", "54.3.0.0005")
        spec = artifacts.ArtifactSpec("https://example.invalid/product.msi", "Logos.msi", "msi",
                                      msi_expectation=expectation)
        accepted = self.acquire(response(content), spec=spec)
        self.assertEqual(accepted.spec.msi_expectation, expectation)
        self.assertEqual(accepted.check().read_bytes(), content)
        wrong = replace(spec, msi_expectation=artifacts.MsiExpectation("Logos", "54.3.0.6"))
        with self.assertRaisesRegex(artifacts.ArtifactError, "version mismatch"):
            replace(accepted, spec=wrong).check()
        source = self.root / "local.msi"
        source.write_bytes(content)
        self.assertEqual(artifacts.import_local(source, self.root, spec).check().read_bytes(), content)
        with self.assertRaisesRegex(artifacts.ArtifactError, "version mismatch"):
            artifacts.import_local(source, self.root, wrong)
        digest = artifacts.Digest("sha256", hashlib.sha256(content).hexdigest(), "fixture")
        with patch.object(artifacts.requests, "get") as get:
            cached = artifacts.acquire(replace(spec, digest=digest), self.root, [source])
            self.assertEqual(cached.check().read_bytes(), content)
        get.assert_not_called()
        with patch.object(artifacts.requests, "get", return_value=response(b"failed", 500)) as get:
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.acquire(replace(wrong, digest=digest), self.root, [source])
        get.assert_called_once()
        self.assertEqual(source.read_bytes(), content)
        self.assertEqual(accepted.check().read_bytes(), content)
        self.assert_preserved()

    def test_download_adapter_preserves_product_expectation_in_spec(self):
        expectation = artifacts.MsiExpectation("Logos", "54.3.0.0005")
        app = SimpleNamespace(conf=SimpleNamespace(download_dir="/fixture/cache", user_download_dir=None),
                              checkpoint=Mock(), status=Mock())
        with patch.object(artifacts, "acquire") as acquire:
            network.logos_reuse_download("https://example.invalid/Logos.msi", "Logos.msi",
                                         "/fixture/target", app, kind="msi", msi_expectation=expectation)
        self.assertEqual(acquire.call_args.args[0].msi_expectation, expectation)

    def test_header_shaped_files_and_corrupt_required_metadata_are_rejected(self):
        path = self.root / "shaped.AppImage"
        used = 135
        superblock = struct.pack(
            "<5I6H8Q", 0x73717368, 1, 0, 4096, 0, 1, 12, 0, 1, 4, 0,
            0, used, 96, 0xffffffffffffffff, 96, 112, 0xffffffffffffffff, 0xffffffffffffffff,
        )
        path.write_bytes(appimage())
        _, extent = artifacts._elf_extent(path)
        path.write_bytes(appimage()[:extent] + superblock + b"\0" * 39)
        with self.assertRaisesRegex(artifacts.ArtifactError, "SquashFS"):
            artifacts.validate_format(path, "appimage")
        path = self.root / "shaped.msi"
        path.write_bytes(msi())
        with olefile.OleFileIO(path, write_mode=True) as database:
            stream = next(parts for parts in database.listdir() if artifacts._msi_name(parts[0]) == "_StringPool")
            size = database.get_size(stream)
            database.write_stream(stream, b"fixture".ljust(size, b"\0"))
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.validate_format(path, "msi")

    def test_failed_fresh_get_preserves_distinct_cache_and_target(self):
        cached = self.root / "cached.zip"
        cached.write_bytes(winetricks_zip(b"#!/bin/sh\nexit 2\n"))
        before = cached.read_bytes()
        with self.assertRaises(artifacts.ArtifactError):
            self.acquire(response(b"failed", 500), candidates=[cached])
        self.assertEqual(cached.read_bytes(), before)
        self.assert_preserved()

    def test_shared_store_exclusion_crosses_process_and_install_boundaries(self):
        context = multiprocessing.get_context("fork")
        ready, finish = context.Event(), context.Event()
        results = context.Queue()
        process = context.Process(target=competing_store_use, args=(constants.STATE_DIR, ready, finish, results))
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaises(maintenance.MaintenanceBusy), maintenance.artifact_use(exclusive=True):
                self.fail("Removal must not enter")
        finally:
            finish.set()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
        self.assertEqual(results.get(timeout=2), "released")
        with maintenance.artifact_use(exclusive=True):
            pass
