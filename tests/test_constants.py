import unittest
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import logos_desktop.constants as constants


class TestConstants(unittest.TestCase):
    def test_get_runmode(self):
        self.assertEqual('script', constants.get_runmode())

    def test_cache_base_is_always_followed_by_the_project_child(self):
        probe = (
            "from logos_desktop import constants as c\n"
            "import json\n"
            "print(json.dumps([str(c.CACHE_BASE), c.CACHE_DIR]))\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (None, "", "relative", str(root / "shared 文")):
                with self.subTest(value=value):
                    environment = dict(os.environ, HOME=str(root))
                    environment.pop("XDG_CACHE_HOME", None)
                    if value is not None:
                        environment["XDG_CACHE_HOME"] = value
                    base = root / "shared 文" if value and Path(value).is_absolute() else root / ".cache"
                    result = subprocess.check_output([sys.executable, "-B", "-c", probe], env=environment, text=True)
                    self.assertEqual(json.loads(result), [str(base), str(base / "logos-desktop")])
