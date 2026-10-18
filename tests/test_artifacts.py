import hashlib
import multiprocessing
from pathlib import Path
import tempfile
import struct
import unittest
from unittest.mock import Mock, patch

import requests
import olefile

from ou_dedetai import artifacts, constants, maintenance
from tests.artifact_fixtures import appimage, msi, winetricks_zip


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
