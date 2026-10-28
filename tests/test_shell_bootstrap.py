"""Exercise shell bootstrap failure boundaries without network or system changes."""

import os
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


class ShellBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.script = Path(__file__).resolve().parents[1] / "scripts/install.sh"
        for name in ("bash", "uname", "mktemp", "stat", "readlink", "tar", "gzip", "sha256sum", "rm", "mkdir"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.command("id", "printf '1000\\n'\n")
        self.environment = dict(os.environ, HOME=str(self.home), PATH=str(self.bin), TMPDIR=str(self.root))

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body)
        path.chmod(0o700)

    def run_installer(self):
        return subprocess.run(["/bin/bash", str(self.script), "--no-path"], env=self.environment,
                              capture_output=True, text=True, timeout=30)

    def test_missing_python_rejects_corrupt_archive_before_extraction_and_cleans_temp(self):
        self.command("curl", "while (($#)); do\n"
                     "  if [[ $1 == -o ]]; then printf 'corrupted runtime' > \"$2\"; exit 0; fi\n"
                     "  shift\ndone\nexit 1\n")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private Python runtime", result.stdout)
        self.assertIn("FAILED", result.stdout)
        self.assertFalse(list(self.root.glob("logos-desktop-install.*")))
        self.assertFalse((self.home / ".local").exists())

    def test_existing_python_accepts_older_curl_and_bootstrap_transfer_failure_is_not_executed(self):
        (self.bin / "python3").symlink_to(sys.executable)
        self.command("curl", "if [[ ${1:-} == --version ]]; then printf 'curl 7.81.0\\n'; exit 0; fi\n"
                     "while (($#)); do\n"
                     "  if [[ $1 == -o ]]; then printf 'raise AssertionError(\"executed partial file\")' > \"$2\"; "
                     "exit 22; fi\n  shift\ndone\nexit 1\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 22)
        self.assertNotIn("executed partial file", result.stderr)
        self.assertNotIn("private Python runtime", result.stdout)
        self.assertFalse(list(self.root.glob("logos-desktop-install.*")))

    def test_bootstrap_size_limit_is_enforced_when_older_curl_accepts_oversized_body(self):
        (self.bin / "python3").symlink_to(sys.executable)
        self.command("curl", "while (($#)); do\n"
                     "  if [[ $1 == -o ]]; then\n"
                     f"    {sys.executable} -I -c 'import sys\n"
                     "open(sys.argv[1], \"wb\").write(b\"x\" * 262145)' \"$2\"\n"
                     "    exit 0\n  fi\n  shift\ndone\nexit 1\n")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("size limit", result.stderr)
        self.assertFalse(list(self.root.glob("logos-desktop-install.*")))

    def test_provisioned_python_is_removed_if_bootstrap_transfer_fails_before_use(self):
        archive = self.root / "fixture.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            content = b"#!/bin/bash\nexit 0\n"
            entry = tarfile.TarInfo("python/bin/python3")
            entry.mode = 0o700
            entry.size = len(content)
            output.addfile(entry, io.BytesIO(content))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        # Only the trusted artifact pin changes for this tiny offline archive.
        self.script = self.root / "install.sh"
        source = Path(__file__).resolve().parents[1] / "scripts/install.sh"
        self.script.write_text(source.read_text().replace(
            "ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20", digest,
        ))
        self.command("curl", "url=''\noutput=''\nwhile (($#)); do\n"
                     "  case $1 in\n    https:*) url=$1 ;;\n    -o) output=$2; shift ;;\n  esac\n"
                     "  shift\ndone\n"
                     f"if [[ $url == *python-build-standalone* ]]; then /bin/cp '{archive}' \"$output\"; "
                     "else exit 22; fi\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 22, result.stderr)
        self.assertIn("OK", result.stdout)
        self.assertFalse(list((self.home / ".local/lib").glob("logos-desktop-python.*")))
        self.assertFalse(list(self.root.glob("logos-desktop-install.*")))

    def test_aliased_temporary_parent_is_refused_before_download(self):
        alias = self.root / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        self.environment["TMPDIR"] = str(alias)
        self.command("curl", "printf 'Unexpected download\\n' >&2\nexit 99\n")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("temporary directory", result.stderr)
        self.assertNotIn("Unexpected download", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])
