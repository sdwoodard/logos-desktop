"""Offline release-bootstrap validation before loading any wheel implementation."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import zipfile


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = self.home / ".local/lib/logos-desktop"
        self.prefix = self.root / "versions/1.0.0-fixture"
        self.marker = self.home / "executed.json"
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home)}))
        path = Path(__file__).resolve().parents[1] / "scripts/install-wheel.py"
        spec = importlib.util.spec_from_file_location("bootstrap_fixture", path)
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        self.stack.enter_context(patch.object(self.script.os, "geteuid", return_value=1000))
        self.stack.enter_context(patch.object(self.script.platform, "machine", return_value="x86_64"))
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.addCleanup(sys.modules.pop, "_logos_desktop_verified_install", None)

        content = io.BytesIO()
        implementation = (
            "import json\nfrom pathlib import Path\n"
            f"Path({str(self.marker)!r}).write_text('loaded')\n"
            "def bootstrap(arguments):\n"
            f"    Path({str(self.prefix)!r}).mkdir(parents=True)\n"
            f"    Path({str(self.root / 'current')!r}).symlink_to('versions/1.0.0-fixture')\n"
            f"    Path({str(self.marker)!r}).write_text(json.dumps(arguments))\n"
            "    return 0\n"
        )
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("logos_desktop/controller_install.py", implementation)
        self.wheel = content.getvalue()
        self.url = self.script.RELEASES + "/download/v1.0.0/logos_desktop-1.0.0-py3-none-any.whl"
        self.data = {"version": "1.0.0", "filename": "logos_desktop-1.0.0-py3-none-any.whl",
                     "size": len(self.wheel), "sha256": hashlib.sha256(self.wheel).hexdigest(), "url": self.url}
        self.responses = {self.url: self.wheel}
        self.opened = []

        def open_response(url, timeout):
            self.opened.append(url)
            self.assertEqual(timeout, 10)
            content = json.dumps(self.data).encode() if url == self.script.MANIFEST_URL else self.responses[url]
            return io.BytesIO(content)

        self.opener = Mock(open=Mock(side_effect=open_response))
        self.stack.enter_context(patch.object(self.script.urllib.request, "build_opener", return_value=self.opener))
        self.provider = self.stack.enter_context(patch.object(self.script.subprocess, "run",
                                                              return_value=SimpleNamespace(returncode=0)))

    def test_latest_verifies_wheel_uses_default_root_and_configures_installed_provider(self):
        self.assertEqual(self.script.main(["--latest"]), 0)
        arguments = json.loads(self.marker.read_text())
        self.assertEqual(arguments[arguments.index("--root") + 1], str(self.root))
        self.assertEqual(arguments[arguments.index("--sha256") + 1], self.data["sha256"])
        self.assertEqual(self.opened, [self.script.MANIFEST_URL, self.url])
        self.provider.assert_called_once_with(
            [str(self.prefix / "bin/python"), "-I", "-m",
             "logos_desktop.controller_install", "configure-provider"],
            check=False,
        )
        self.assertIn("Updates are configured", self.output.getvalue())

    def test_invalid_metadata_never_downloads_or_executes_the_wheel(self):
        original = dict(self.data)
        changes = ({"version": "1.0.0rc1"}, {"filename": "other.whl"}, {"url": "http://example.invalid/wheel"},
                   {"url": self.url.replace("v1.0.0", "v2.0.0")}, {"sha256": "bad"}, {"size": True},
                   {"size": 0}, {"size": self.script.MAX_WHEEL_BYTES + 1}, {"extra": "unrecognized"})
        for change in changes:
            with self.subTest(change=change):
                self.data = original | change
                self.opened.clear()
                with self.assertRaises(ValueError):
                    self.script.main(["--latest"])
                self.assertEqual(self.opened, [self.script.MANIFEST_URL])
                self.assertFalse(self.marker.exists())
                self.assertFalse(self.root.exists())
        self.provider.assert_not_called()

    def test_size_and_digest_fail_before_loading_packaged_code(self):
        for change in ({"sha256": "0" * 64}, {"size": len(self.wheel) - 1}, {"size": len(self.wheel) + 1}):
            original = dict(self.data)
            with self.subTest(change=change):
                self.data.update(change)
                with self.assertRaises(ValueError):
                    self.script.main(["--latest"])
                self.assertFalse(self.marker.exists())
                self.assertFalse(self.root.exists())
                self.data = original
        self.provider.assert_not_called()

    def test_existing_roots_and_platform_preflight_refuse_without_network_or_writes(self):
        sentinel = self.home / "sentinel"
        sentinel.write_text("unchanged")
        link = self.home / "dangling-root"
        link.symlink_to(self.home / "absent")
        for root in (self.home, sentinel, link):
            with self.subTest(root=root), self.assertRaises(SystemExit):
                self.script.main(["--latest", "--root", str(root)])
        for target, name, value in ((self.script.sys, "version_info", (3, 11)),
                                    (self.script.sys, "platform", "win32")):
            with patch.object(target, name, value), self.assertRaises(SystemExit):
                self.script.main(["--latest"])
        with patch.object(self.script.os, "geteuid", return_value=0), self.assertRaises(SystemExit):
            self.script.main(["--latest"])
        with patch.object(self.script.platform, "machine", return_value="aarch64"), self.assertRaises(SystemExit):
            self.script.main(["--latest"])
        self.assertEqual(self.opened, [])
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse(self.root.exists())

    def test_transfer_errors_and_limits_leave_no_installation(self):
        for error in (urllib.error.HTTPError(self.script.MANIFEST_URL, 404, "Not found", {}, None),
                      TimeoutError("stalled transfer")):
            with patch.object(self.opener, "open", side_effect=error), self.assertRaises(OSError):
                self.script.main(["--latest"])
        with patch.object(self.opener, "open", return_value=io.BytesIO(b"x" * 16385)), self.assertRaises(ValueError):
            self.script.main(["--latest"])
        with patch.object(self.script.time, "monotonic", side_effect=[0, 61]), self.assertRaises(ValueError):
            self.script.main(["--latest"])
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.root.exists())

    def test_insecure_downloads_and_redirects_are_rejected(self):
        for url in ("http://example.invalid/wheel", "https://user:password@example.invalid/wheel",
                    "https://@example.invalid/wheel", "https://example.invalid/wheel#fragment"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self.script.download(url, 1024)
                with self.assertRaises(ValueError):
                    self.script.Redirects().redirect_request(
                        urllib.request.Request(self.url), None, 302, "Found", {}, url)
        self.opener.open.assert_not_called()

    def test_provider_failure_preserves_installation_and_reports_retry(self):
        self.provider.return_value = SimpleNamespace(returncode=1)
        with self.assertRaisesRegex(RuntimeError, "Controller installed, but update setup failed. Retry with:"):
            self.script.main(["--latest"])
        self.assertTrue(self.root.is_dir())
        self.assertTrue(self.marker.is_file())
        self.assertNotIn("Updates are configured", self.output.getvalue())

    def test_explicit_local_wheel_keeps_manual_provider_setup(self):
        wheel = self.home / self.data["filename"]
        wheel.write_bytes(self.wheel)
        result = self.script.main(["--root", str(self.root), "--wheel", str(wheel), "--version", "1.0.0",
                                   "--sha256", self.data["sha256"], "--size", str(self.data["size"])])
        self.assertEqual(result, 0)
        self.assertEqual(self.opened, [])
        self.provider.assert_not_called()
