import unittest
from unittest.mock import patch

from ou_dedetai import artifacts, metadata, processes
from tests.test_artifacts import response


class TestMetadataBounds(unittest.TestCase):
    def test_body_limit_checks_advertised_and_actual_length(self):
        for reply in (response(headers={"Content-Length": "99"}), response(b"x" * 17)):
            with patch.object(metadata, "MAX_BYTES", 16), patch.object(metadata.requests, "get", return_value=reply):
                with self.assertRaisesRegex(artifacts.MetadataError, "body limit"):
                    metadata.read("https://example.invalid/metadata")

    def test_helper_spawn_failure_is_a_metadata_outcome(self):
        with patch.object(processes.subprocess, "Popen", side_effect=PermissionError("fixture spawn denied")):
            with self.assertRaises(artifacts.MetadataError):
                metadata.request("https://example.invalid/metadata")
