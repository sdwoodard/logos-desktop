import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from ou_dedetai import artifacts, constants, metadata, network
from tests.test_artifacts import response


class TestNetwork(unittest.TestCase):
    def test_head_downgrade_is_not_authenticated_cache_evidence(self):
        from base64 import b64encode
        import hashlib
        from tests.artifact_fixtures import winetricks_zip

        old = winetricks_zip(b"#!/bin/sh\nexit 1\n")
        fresh = winetricks_zip()
        head = response(headers={"Content-MD5": b64encode(hashlib.md5(old).digest()).decode()})
        head.url = "http://example.invalid/downgrade"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tag.zip").write_bytes(old)
            with (
                patch.object(artifacts.requests, "head", return_value=head),
                patch.object(artifacts.requests, "get", return_value=response(fresh)) as get,
            ):
                accepted = artifacts.acquire(
                    artifacts.ArtifactSpec("https://example.invalid/tag", "tag.zip", "winetricks-zip"),
                    root, [root / "tag.zip"],
                )
            get.assert_called_once()
            self.assertEqual(accepted.path.read_bytes(), fresh)
            self.assertEqual((root / "tag.zip").read_bytes(), old)

    def test_s3_etags_are_not_checksums_and_explicit_md5_is_parsed(self):
        from base64 import b64encode

        for etag in ('"abc-2"', '"' + "a" * 32 + '"', "opaque"):
            self.assertIsNone(artifacts.response_metadata({"Server": "AmazonS3", "ETag": etag})[1])
        encoded = b64encode(b"x" * 16).decode()
        self.assertEqual(artifacts.response_metadata({"Content-MD5": encoded})[1].value, (b"x" * 16).hex())

    def test_request_timeouts_cover_metadata_release_and_feed(self):
        with patch.object(artifacts.requests, "head", return_value=response()) as head:
            artifacts.head_metadata("https://example.invalid")
        self.assertEqual(head.call_args.kwargs["timeout"], (10, 30))
        for load in (lambda: network._get_release_data("example/repo"),
                     lambda: network._get_faithlife_product_releases("Logos", "10", "stable")):
            with (patch.object(artifacts.requests, "get", side_effect=requests.ReadTimeout) as get,
                  patch.object(metadata, "request", side_effect=metadata.read)):
                result = load()
            self.assertIsNotNone(result)
            self.assertEqual(get.call_args.kwargs["timeout"], (10, 30))

    def test_malformed_metadata_is_explicit_and_legacy_digest_cache_is_dropped(self):
        for content, load in (
            (b"not JSON", lambda: network._get_release_data("example/repo")),
            (b"not XML", lambda: network._get_faithlife_product_releases("Logos", "10", "stable")),
        ):
            with (patch.object(artifacts.requests, "get", return_value=response(content)),
                  patch.object(metadata, "request", side_effect=metadata.read)):
                with self.assertRaises(artifacts.ArtifactError):
                    load()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cache = base / "logos-desktop"
            with patch.multiple(constants, CACHE_BASE=base, CACHE_DIR=str(cache),
                                NETWORK_CACHE_PATH=str(cache / "network.json")):
                network.CachedRequests.load()
                self.assertFalse(cache.exists())
                cache.mkdir(mode=0o700)
                (cache / "network.json").write_text(json.dumps({
                    "url_size_and_hash": {"url": [1, "unsafe old ETag"]},
                    "repository_latest_version": {"example/repo": "1"},
                }))
                loaded = network.CachedRequests.load()
            self.assertNotIn("url_size_and_hash", loaded._as_dict())
            self.assertEqual(loaded.repository_latest_version, {"example/repo": "1"})
