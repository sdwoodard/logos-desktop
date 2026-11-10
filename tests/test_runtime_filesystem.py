"""Stable filesystem parsing separates retained identity from boot observations."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from logos_desktop import maintenance, runtime_filesystem


class FilesystemIdentity(unittest.TestCase):
    def test_ext4_and_btrfs_findmnt_output_preserve_root_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for filesystem, fsroot in (("ext4", "/"), ("btrfs", "/@home")):
                output = {"filesystems": [{"uuid": "11111111-1111-1111-1111-111111111111",
                                           "fstype": filesystem, "fsroot": fsroot, "target": "/", "id": 1}]}
                with self.subTest(filesystem=filesystem), patch.object(
                    runtime_filesystem.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(output)),
                ):
                    observed = runtime_filesystem.identity(path)
                self.assertEqual(observed["owner_uid"], os.geteuid())
                self.assertEqual(observed["filesystem_root"], str(Path(fsroot) / path.relative_to("/")))
                rebooted = dict(observed, device=observed["device"] + 1, boot_id="22222222-2222-2222-2222-222222222222")
                self.assertTrue(runtime_filesystem.same_retained_root(observed, rebooted))
                replaced = dict(rebooted, filesystem_root="/other")
                self.assertFalse(runtime_filesystem.same_retained_root(observed, replaced))

    def test_unknown_uuid_and_unsupported_filesystem_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            for filesystem, uuid in (("tmpfs", "11111111-1111-1111-1111-111111111111"), ("ext4", None)):
                output = {"filesystems": [{"uuid": uuid, "fstype": filesystem, "fsroot": "/", "target": "/", "id": 1}]}
                with (self.subTest(filesystem=filesystem, uuid=uuid),
                      patch.object(runtime_filesystem.subprocess, "run",
                                   return_value=SimpleNamespace(stdout=json.dumps(output))),
                      self.assertRaises(maintenance.MaintenanceError)):
                    runtime_filesystem.identity(Path(temporary))
