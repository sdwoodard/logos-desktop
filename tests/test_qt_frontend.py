import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

try:
    import PySide6  # noqa: F401
except ModuleNotFoundError:
    QT_AVAILABLE = False
    QtCase = unittest.TestCase
else:
    QT_AVAILABLE = True
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QComboBox, QDialogButtonBox, QFileDialog
    from ou_dedetai.qt.bridge import Completion, Progress, Result, Ticket
    from tests.qt_fixtures import QtCase


@unittest.skipUnless(QT_AVAILABLE, "Optional Qt frontend is not installed")
class TestQtFrontend(QtCase):
    def test_worker_delivery_liveness_coalescing_and_checked_failure_recovery(self):
        window = self.window()
        worker_entered, release = threading.Event(), threading.Event()
        worker_threads, ui_threads = [], []
        window.bridge.progress.connect(lambda update: ui_threads.append(threading.get_ident()))

        def operation(app, cancel):
            worker_threads.append(threading.get_ident())
            for number in range(2000):
                app.status(f"Chunk {number}", number % 100)
            worker_entered.set()
            release.wait(3)
            return Result("failed", "Checked failure", "Sample details")

        window.start(operation)
        self.assertTrue(worker_entered.wait(2))
        try:
            self.assertFalse(window.app.run_operation(self.services.toggle))
            window.navigation.setCurrentRow(2)
            window.settings.appearance.setCurrentText("Dark")
            self.pump(lambda: bool(ui_threads) and self.theme.dark)
            self.assertEqual(window.pages.currentIndex(), 2)
            self.assertEqual(set(ui_threads), {threading.get_ident()})
            self.assertNotIn(threading.get_ident(), worker_threads)
            self.assertLess(len(ui_threads), 10)
        finally:
            release.set()
        self.pump(lambda: window.app.active is None and bool(window.dialogs.dialogs))
        self.assertEqual(window.status.text(), "Checked failure")
        self.assertTrue(window.home.run.isEnabled())
        next(iter(window.dialogs.dialogs)).reject()
        window.start(self.services.toggle)
        self.pump(lambda: window.app.active is None)
        self.assertTrue(self.services.current.running)

    def test_unchecked_return_and_exception_are_not_success(self):
        window = self.window()
        for operation in (lambda app, cancel: None, lambda app, cancel: 1 / 0):
            window.start(operation)
            self.pump(lambda: window.app.active is None and bool(window.dialogs.dialogs))
            self.assertIn("could not be completed", window.status.text())
            next(iter(window.dialogs.dialogs)).reject()
            self.application.processEvents()

    def test_cancel_waits_for_cleanup_and_never_publishes_success(self):
        window = self.window()
        entered, settle = threading.Event(), threading.Event()
        seen = []
        window.bridge.completed.connect(lambda completion: seen.append(completion.result.outcome))

        def operation(app, cancel):
            entered.set()
            cancel.wait(3)
            settle.wait(3)
            return Result("cancelled", "Cancelled after cleanup")

        window.start(operation)
        self.assertTrue(entered.wait(2))
        window.cancel.click()
        self.application.processEvents()
        try:
            self.assertIsNotNone(window.app.active)
            self.assertIn("Cancelling", window.status.text())
            self.assertFalse(window.install.continue_button.isEnabled())
        finally:
            settle.set()
        self.pump(lambda: window.app.active is None)
        self.assertEqual(seen, ["cancelled"])
        self.assertTrue(window.install.continue_button.isEnabled())

    def test_late_cancel_preserves_a_committed_result(self):
        window = self.window()
        entered, settle = threading.Event(), threading.Event()
        committed = self.root / "committed"

        def operation(app, cancel):
            committed.write_text("complete")
            entered.set()
            settle.wait(3)
            return Result("succeeded", "Committed before cancellation")

        window.start(operation)
        self.assertTrue(entered.wait(2))
        window.cancel.click()
        settle.set()
        self.pump(lambda: window.app.active is None)
        self.assertEqual(committed.read_text(), "complete")
        self.assertEqual(window.status.text(), "Committed before cancellation")

    def test_prompt_confirm_cancel_escape_x_and_parent_close_release_waiters(self):
        window = self.window()
        outcomes = []
        window.bridge.completed.connect(lambda completion: outcomes.append(completion.result.outcome))

        def operation(app, cancel):
            answer = app.ask("Sample choice", ["Continue", "Go back"])
            return Result("succeeded", answer)

        for action in ("confirm", "cancel", "escape", "x", "operation_cancel", "parent_close"):
            with self.subTest(action=action):
                window.start(operation)
                self.pump(lambda: bool(window.dialogs.pending))
                prompt, dialog = next(iter(window.dialogs.pending.items()))
                if action == "confirm":
                    dialog.findChild(QComboBox).setCurrentIndex(0)
                    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
                elif action == "cancel":
                    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
                elif action == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                elif action == "x":
                    dialog.close()
                elif action == "operation_cancel":
                    window.app.cancel_operation()
                else:
                    window.close()
                self.pump(lambda: prompt.done.is_set() and not window.app._prompts)
                self.pump(lambda: window.app.active is None or window._final_close)
                self.assertFalse(prompt.complete("second result"))
                self.assertEqual(prompt.answer, "Continue" if action == "confirm" else None)
                self.assertFalse(window.dialogs.pending)
                if action != "parent_close":
                    self.assertEqual(outcomes[-1], "succeeded" if action == "confirm" else "cancelled")

    def test_picker_rejection_and_draft_controls_do_not_write_configuration(self):
        window = self.window()
        initial = window.install.destination.text()
        window._browse()
        self.pump(lambda: any(isinstance(dialog, QFileDialog) for dialog in window.dialogs.dialogs))
        next(dialog for dialog in window.dialogs.dialogs if isinstance(dialog, QFileDialog)).reject()
        self.assertEqual(window.install.destination.text(), initial)
        for checked in (False, True):
            window.install.dependencies.setChecked(checked)
            window._preview()
            self.pump(lambda: window.app.active is None)
            self.assertEqual(self.services.requests[-1].install_dependencies, checked)
        window.install.destination.clear()
        window._preview()
        self.assertIsNone(window.app.active)
        self.assertEqual(len(self.services.requests), 2)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_path_prompt_rejection_does_not_resolve_to_current_directory(self):
        from ou_dedetai.constants import PROMPT_OPTION_DIRECTORY
        window = self.window()

        def operation(app, cancel):
            app.ask("Choose destination", [PROMPT_OPTION_DIRECTORY])
            raise AssertionError("Cancelled destination was accepted")

        window.start(operation)
        self.pump(lambda: bool(window.dialogs.pending))
        prompt, dialog = next(iter(window.dialogs.pending.items()))
        self.assertIsInstance(dialog, QFileDialog)
        dialog.reject()
        self.pump(lambda: window.app.active is None)
        self.assertIsNone(prompt.answer)
        self.assertIn("cancelled", window.status.text())

    def test_close_keeps_event_loop_and_worker_until_final_write_settles(self):
        window = self.window()
        entered, settle = threading.Event(), threading.Event()
        written = self.root / "settled"

        def operation(app, cancel):
            entered.set()
            settle.wait(3)
            written.write_text("safe boundary")
            return Result("succeeded", "Settled")

        window.start(operation)
        self.assertTrue(entered.wait(2))
        window.close()
        try:
            self.application.processEvents()
            self.assertTrue(window.isVisible())
            self.assertFalse(window._final_close)
            self.assertFalse(written.exists())
            self.assertFalse(window.app.run_operation(self.services.toggle))
        finally:
            settle.set()
        self.pump(lambda: window._final_close)
        self.assertEqual(written.read_text(), "safe boundary")

    def test_stale_events_and_repeated_dialog_cycles_leave_bounded_ownership(self):
        window = self.window()
        stale = Ticket(999, -1)
        original = window.status.text()
        window.bridge.post(progress=Progress(stale, "Old progress", 10))
        window.bridge.finish(Completion(stale, Result("failed", "Old failure")))
        self.application.processEvents()
        self.assertEqual(window.status.text(), original)
        for number in range(20):
            dialog = window.dialogs.failure("Sample error", "Sample failure", "Copyable details")
            dialog.close()
            window.start(self.services.toggle)
            self.pump(lambda: window.app.active is None)
            self.application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.pump(lambda: len(window.app._threads) == 1)
        self.assertFalse(window.dialogs.dialogs)
        self.assertFalse(window.app._prompts)
        self.assertEqual(len(window.app.config_updated_hooks), 1)
        self.assertEqual(window.home.activity.blockCount(), 20)

    def test_configuration_callback_is_queued_and_main_thread_questions_are_rejected(self):
        window = self.window()
        seen = []
        unsubscribe = window.app.subscribe_config(lambda: seen.append(threading.get_ident()))
        window.app._config_updated_event.set()
        self.pump(lambda: bool(seen))
        self.assertEqual(seen, [threading.get_ident()])
        unsubscribe()
        with self.assertRaisesRegex(RuntimeError, "outside the Qt"):
            window.app.ask("Invalid UI thread call", ["Yes"])

    def test_callback_failure_preserves_following_and_later_queued_work(self):
        window = self.window()
        seen = []

        def broken():
            raise ValueError("Intended queued callback failure")

        with patch("ou_dedetai.qt.bridge.logging.exception") as report:
            window.bridge.post(broken)
            window.bridge.post(lambda: seen.append("following"))
            self.pump(lambda: seen == ["following"])
            report.assert_called_once()
        window.bridge.post(lambda: seen.append("later"))
        self.pump(lambda: seen == ["following", "later"])


class TestNoQtCLI(unittest.TestCase):
    def test_frontend_selection_accepts_wayland_and_retains_terminal_and_legacy_aliases(self):
        from unittest.mock import Mock
        from ou_dedetai import main, maintenance, system
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "fixture-wayland"}, clear=True):
            self.assertEqual(system.get_dialog(), "qt")
        with patch.dict(os.environ, {"DIALOG": "tk"}, clear=True):
            self.assertEqual(system.get_dialog(), "qt")
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "__stdin__", Mock(isatty=lambda: True)):
            self.assertEqual(system.get_dialog(), "tui")
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "__stdin__", None):
            with self.assertRaises(maintenance.MaintenanceError):
                system.get_dialog()
        with patch.object(main, "start_graphical") as start:
            selected = Mock(dialog="tk")
            main.run_control_panel(selected)
            start.assert_called_once_with(selected)

    def test_explicit_gui_without_optional_dependency_fails_with_actionable_message(self):
        import tempfile
        program = '''
import importlib.abc,sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("PySide6"):
            raise ModuleNotFoundError("Qt omitted by this fixture", name="PySide6")
sys.meta_path.insert(0, NoQt())
from ou_dedetai import main
sys.argv = ["logos-desktop", "-c", sys.argv[1], "--dialog", "qt"]
main.main()
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected.json"
            selected.write_text("{}")
            env = dict(os.environ, HOME=str(root), XDG_STATE_HOME=str(root / "state"),
                       XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"))
            result = subprocess.run([sys.executable, "-B", "-c", program, str(selected)],
                                    capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("logos-desktop[gui]", result.stderr)
            self.assertIn("--dialog tui", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_cli_help_and_status_do_not_import_qt_or_initialize_a_display(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected.json"
            selected.write_text(json.dumps({"install_dir": str(root / "Logos"), "faithlife_product": "Logos"}))
            program = '''
import importlib.abc,sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("PySide6", "ou_dedetai.qt", "tkinter", "ou_dedetai.gui")):
            raise AssertionError("CLI imported Qt")
sys.meta_path.insert(0, NoQt())
from ou_dedetai import main
sys.argv = ["logos-desktop", "-c", sys.argv[1], sys.argv[2]]
main.main()
'''
            env = dict(os.environ, HOME=str(root), XDG_STATE_HOME=str(root / "state"),
                       XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"))
            for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
                env.pop(name, None)
            for flag in ("--help", "--status"):
                result = subprocess.run([sys.executable, "-B", "-c", program, str(selected), flag],
                                        capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
