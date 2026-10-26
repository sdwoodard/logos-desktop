"""Installed resources required at the modern installer boundary."""

from pathlib import Path
import unittest

from ou_dedetai import constants


class PackagedResources(unittest.TestCase):
    def test_installer_transform_and_attributed_project_artwork_exist(self):
        transform = constants.APP_ASSETS_DIR / "LogosStubFailOK.mst"
        self.assertEqual(transform.read_bytes()[:8], bytes.fromhex("d0cf11e0a1b11ae1"))

        for name in ("logos-desktop-icon.png", "logos-desktop.svg", "lucide-LICENSE.txt", "ATTRIBUTION.md"):
            with self.subTest(resource=name):
                resource = Path(constants.APP_IMAGE_DIR) / name
                self.assertGreater(resource.stat().st_size, 0)
