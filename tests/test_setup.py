import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import REPODIR


class TestContributorSetup(unittest.TestCase):
    def test_script_uses_own_root_and_preserves_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("renamed checkout 文", "another-name"):
                project = root / name
                (project / "scripts").mkdir(parents=True)
                (project / "logos_desktop").mkdir()
                (project / "pyproject.toml").touch()
                (project / "logos_desktop/main.py").touch()
                script = project / "scripts/ensure-venv.sh"
                shutil.copy2(REPODIR / "scripts/ensure-venv.sh", script)
                stub = root / "python fixture"
                stub.write_text(f'''#!{sys.executable}
import json, pathlib, sys
if sys.argv[1] == '-c':
    sys.exit(0)
pathlib.Path({str(root / 'arguments')!r}).write_text(json.dumps(sys.argv[1:]))
pathlib.Path(sys.argv[-1]).mkdir()
''')
                stub.chmod(0o700)
                environment = dict(os.environ, PYTHON=str(stub))
                result = subprocess.run([str(script)], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((root / "arguments").read_text()), ["-m", "venv", str(project / ".venv")])
                sentinel = project / ".venv/sentinel"
                sentinel.write_text("preserve")
                result = subprocess.run([str(script)], cwd=project, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(sentinel.read_text(), "preserve")

    def test_pid_test_callbacks_cannot_remove_restored_path_at_normal_exit(self):
        program = '''
import sys, unittest
from logos_desktop import constants
from tests.test_utils import TestAppUtils
constants.PID_FILE = sys.argv[1]
names = ('test_die_if_running_nofile', 'test_die_if_running_withfile')
suite = unittest.TestSuite(TestAppUtils(name) for name in names)
result = unittest.TextTestRunner().run(suite)
assert constants.PID_FILE == sys.argv[1]
sys.exit(not result.wasSuccessful())
'''
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "not-the-test-pid"
            sentinel.write_text("preserve")
            result = subprocess.run([sys.executable, "-B", "-c", program, str(sentinel)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sentinel.read_text(), "preserve")
