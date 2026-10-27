"""Disposable command/menu publication and shell-startup preservation checks."""

from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from logos_desktop import config_store, constants, file_transactions, user_integration as integration


class UserIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.home = temporary / "home ' % 文"
        self.home.mkdir(mode=0o700)
        self.root = self.home / "controller"
        self.root.mkdir(mode=0o700)
        self.stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.home), "SHELL": "/bin/bash", "PATH": os.defpath,
            "XDG_DATA_HOME": str(self.home / "data"), "XDG_CONFIG_HOME": str(self.home / "config"),
            "ZDOTDIR": str(self.home),
        }))
        self.stack.enter_context(patch.object(constants, "COORDINATION_DIR", self.home / "coordination"))
        self.binary = self.home / ".local/bin/logos-desktop"
        self.entry = self.home / "data/applications/logos-desktop.desktop"
        self.rc = self.home / ".bashrc"
        self.rc.write_bytes(b"# existing shell content\n")
        self.rc.chmod(0o640)
        launcher = self.root / "logos-desktop"
        launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        launcher.chmod(0o700)

    def test_command_and_desktop_setup_preserves_modes_content_and_reruns(self):
        original = self.rc.read_bytes()
        message = integration.install(self.root)
        self.assertIn("new terminal", message)
        self.assertEqual(stat.S_IMODE(self.binary.stat().st_mode), 0o700)
        self.assertTrue(integration.command_matches(self.root, self.binary))
        self.assertEqual(subprocess.check_output([str(self.binary), "argument with spaces", "文"], text=True),
                         "argument with spaces\n文\n")
        self.assertTrue(self.rc.read_bytes().startswith(original))
        self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o640)
        if shutil.which("desktop-file-validate"):
            subprocess.run(["desktop-file-validate", str(self.entry)], check=True, capture_output=True)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.binary, self.entry, self.rc)}
        integration.install(self.root)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})
        self.rc.write_bytes(self.rc.read_bytes() + b"# later user content\n")
        later = self.rc.read_bytes()
        integration.install(self.root)
        self.assertEqual(self.rc.read_bytes(), later)

    def test_supported_shells_find_command_and_existing_login_file_is_preserved(self):
        profile = self.home / ".profile"
        profile.write_text("# untouched because bash_profile exists\n")
        login = self.home / ".bash_profile"
        login.write_text("# existing login\n")
        integration.install(self.root)
        self.assertEqual(profile.read_text(), "# untouched because bash_profile exists\n")
        self.assertIn(integration.POSIX_BLOCK, login.read_bytes())
        for shell, arguments in (("bash", ["--noprofile", "--rcfile", str(self.rc), "-ic", "command -v logos-desktop"]),
                                  ("zsh", ["-d", "-i", "-c", "command -v logos-desktop"]),
                                  ("fish", ["--private", "-c", "command -s logos-desktop"])):
            executable = shutil.which(shell)
            with self.subTest(shell=shell):
                if executable is None:
                    self.skipTest(f"{shell} is not installed")
                with patch.dict(os.environ, {"SHELL": executable}):
                    integration.install(self.root)
                result = subprocess.run([executable, *arguments], env=dict(os.environ), stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(self.binary))

    def test_symlinks_foreign_files_and_edited_blocks_are_preserved(self):
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("foreign command")
        with self.assertRaisesRegex(ValueError, "another installation"):
            integration.install(self.root)
        self.assertEqual(self.binary.read_text(), "foreign command")
        self.assertFalse(self.entry.exists())
        self.binary.unlink()
        self.rc.unlink()
        sentinel = self.home / "untouched"
        sentinel.write_text("user data")
        self.rc.symlink_to(sentinel)
        with self.assertRaises(OSError):
            integration.install(self.root)
        self.assertEqual(sentinel.read_text(), "user data")
        self.assertFalse(self.binary.exists())
        self.rc.unlink()
        self.rc.write_bytes(integration.POSIX_BLOCK.replace(b"export PATH", b"export CUSTOM_PATH"))
        with self.assertRaisesRegex(ValueError, "block was edited"):
            integration.install(self.root)
        self.assertFalse(self.binary.exists())

    def test_unsafe_parent_refuses_before_creating_descendants(self):
        data = self.home / "data"
        data.mkdir(mode=0o777)
        data.chmod(0o777)
        with self.assertRaises(ValueError):
            integration.install(self.root)
        self.assertFalse((data / "applications").exists())
        self.assertFalse(self.binary.exists())

    def test_partial_publication_rolls_back_exact_bytes_and_modes_then_retries(self):
        before = self.rc.read_bytes()
        publish = config_store.Target.publish

        def fail(target):
            publish(target)
            if target.path == self.rc:
                raise OSError("injected post-publication error")

        with patch.object(config_store.Target, "publish", fail), self.assertRaises(OSError):
            integration.install(self.root)
        self.assertEqual(self.rc.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o640)
        # The injected error can also interrupt rollback. A fresh setup recovers its intent.
        integration.install(self.root)
        self.assertTrue(self.binary.exists())
        self.assertEqual(self.rc.read_bytes().count(integration.START), 1)

    def test_interrupted_setup_preserves_later_user_edits_during_recovery(self):
        publish = config_store.Target.publish

        def interrupt(target):
            publish(target)
            if target.path == self.binary:
                raise SystemExit("interrupted after first publication")

        with patch.object(config_store.Target, "publish", interrupt), self.assertRaises(SystemExit):
            integration.install(self.root)
        self.binary.write_text("later user replacement")
        with self.assertRaisesRegex(ValueError, "preserved changed files"):
            integration.install(self.root)
        self.assertEqual(self.binary.read_text(), "later user replacement")
        self.assertEqual(json.loads((self.root / "launchers.json").read_text())["state"], "conflict")

    def test_no_path_leaves_shell_files_untouched_and_reports_menu_route(self):
        original = self.rc.read_bytes()
        with patch.dict(os.environ, {"SHELL": "/bin/unknown-shell"}):
            result = integration.install(self.root, modify_path=False)
        self.assertIn("application menu", result)
        self.assertEqual(self.rc.read_bytes(), original)
        self.assertTrue(self.entry.exists())

    def test_malformed_journal_refuses_before_recovery_or_ownership_acceptance(self):
        write_record = file_transactions.write_record

        def interrupt(path, record):
            if record["state"] == "committed":
                raise SystemExit("all targets published, final journal update interrupted")
            write_record(path, record)

        with patch.object(file_transactions, "write_record", interrupt), self.assertRaises(SystemExit):
            integration.install(self.root)
        journal = self.root / "launchers.json"
        valid = json.loads(journal.read_text())
        targets = [Path(item["path"]) for item in valid["files"]]
        shell_index = next(index for index, item in enumerate(valid["files"]) if item["path"] == str(self.rc))
        cases = (
            (shell_index, "before", None),
            (shell_index, "before_mode", None),
            (0, "after", "not base64!"),
            (0, "after", None),
            (0, "before", 123),
            (0, "path", []),
            (0, "mode", True),
            (0, "after", file_transactions.encode_content(b"x" * (file_transactions.MAX_CONFIG_BYTES + 1))),
        )
        for state in ("prepared", "committed"):
            for index, field, value in cases:
                with self.subTest(state=state, index=index, field=field, value_type=type(value).__name__):
                    malformed = copy.deepcopy(valid)
                    malformed["state"] = state
                    malformed["files"][index][field] = value
                    journal.write_text(json.dumps(malformed))
                    before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                              for path in (*targets, journal)}
                    with self.assertRaises(ValueError):
                        integration.install(self.root)
                    self.assertEqual(before, {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                                              for path in before})

        for malformed in ([], {**valid, "files": [None]}, {**valid, "schema": True}):
            with self.subTest(malformed_type=type(malformed).__name__):
                journal.write_text(json.dumps(malformed))
                before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (*targets, journal)}
                with self.assertRaises(ValueError):
                    integration.install(self.root)
                self.assertEqual(before, {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                                          for path in before})

    def test_permission_change_after_preview_is_not_overwritten(self):
        record = {"state": "prepared", "files": [{"path": str(self.rc),
            "before": file_transactions.encode_content(self.rc.read_bytes()),
            "after": file_transactions.encode_content(b"new content"), "before_mode": 0o600, "mode": 0o600}]}
        with self.assertRaisesRegex(file_transactions.FileTransactionError, "permissions changed"):
            file_transactions.publish_files(self.root / "test-journal.json", record)
        self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o640)
        self.assertFalse((self.root / "test-journal.json").exists())
