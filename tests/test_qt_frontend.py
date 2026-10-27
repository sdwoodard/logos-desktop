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
    from PySide6.QtWidgets import QComboBox, QDialogButtonBox, QFileDialog, QTextBrowser
    from logos_desktop.qt.bridge import Completion, Progress, Result, Ticket
    from tests.qt_fixtures import QtCase


@unittest.skipUnless(QT_AVAILABLE, "Optional Qt frontend is not installed")
class TestQtFrontend(QtCase):
    def test_numbered_shortcuts_follow_visible_pages_after_installation(self):
        from dataclasses import replace
        from logos_desktop.qt.services import Services

        services = Services()
        window = self.window(services)
        self.pump(lambda: window.app.active is None)
        window.activateWindow()
        window.navigation.setFocus()
        self.application.processEvents()

        for number, page in enumerate((window.home, window.install, window.maintenance,
                                       window.diagnostics, window.settings), start=1):
            QTest.keyClick(window.navigation, getattr(Qt.Key, f"Key_{number}"), Qt.KeyboardModifier.AltModifier)
            self.assertIs(window.pages.currentWidget().widget(), page)

        QTest.keyClick(window.navigation, Qt.Key.Key_2, Qt.KeyboardModifier.AltModifier)
        services._snapshot = replace(services.snapshot(), installed=True)
        window._render()
        self.assertIs(window.pages.currentWidget().widget(), window.home)
        self.assertTrue(window.navigation.item(window.install_page_index).isHidden())

        for number, page in enumerate((window.home, window.maintenance, window.diagnostics,
                                       window.settings), start=1):
            QTest.keyClick(window.navigation, getattr(Qt.Key, f"Key_{number}"), Qt.KeyboardModifier.AltModifier)
            self.assertIs(window.pages.currentWidget().widget(), page)
        QTest.keyClick(window.navigation, Qt.Key.Key_5, Qt.KeyboardModifier.AltModifier)
        self.assertIs(window.pages.currentWidget().widget(), window.settings)

    def test_first_run_recommends_location_without_writes_or_overwriting_edits(self):
        from logos_desktop import constants
        from logos_desktop.config import EphemeralConfiguration
        from logos_desktop.qt.services import Services

        install = self.root / "legacy-installation"
        (install / "data/wine64_bottle").mkdir(parents=True)
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"install_dir": str(install), "faithlife_product": "Logos",
                                      "faithlife_product_version": "10"}))
        original = legacy.read_bytes()
        missing = self.root / "new-default.json"
        self.stack.enter_context(patch.object(constants, "LEGACY_CONFIG_FILES", [str(legacy)]))
        self.stack.enter_context(patch.object(constants, "DATA_HOME", str(self.root / "recommended")))
        window = self.window(Services(), EphemeralConfiguration.project({}, str(missing), environment={}))
        self.pump(lambda: window.app.active is None)
        self.assertFalse(window.snapshot.selected)
        self.assertFalse(window.home.updates.buttons["check"].isEnabled())
        self.assertEqual(window.install.destination.text(), str(self.root / "recommended/installation"))
        self.assertIsNone(window.app.conf._raw.install_dir)
        self.assertFalse(missing.exists())

        window.home.run.click()
        self.assertIs(window.pages.currentWidget().widget(), window.install)
        window.install.destination.setFocus()
        QTest.keyClick(window.install.destination, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(window.install.destination, str(self.root / "chosen"))
        window._render()
        self.assertEqual(window.install.destination.text(), str(self.root / "chosen"))
        QTest.keyClick(window.install.destination, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(window.install.destination, Qt.Key.Key_Backspace)
        window._render()
        self.assertEqual(window.install.destination.text(), "")
        with patch.object(window.dialogs, "picker") as picker:
            window._browse()
            picker.call_args.args[2](str(self.root / "browsed"))
        window._render()
        self.assertEqual(window.install.destination.text(), str(self.root / "browsed"))
        self.assertEqual(legacy.read_bytes(), original)
        self.assertFalse((self.root / "recommended").exists())
        self.assertFalse(missing.exists())

    def test_delayed_install_form_hydration_honors_saved_and_invocation_paths(self):
        from logos_desktop.config import EphemeralConfiguration
        from logos_desktop.qt.services import Services

        window = self.window(Services())
        self.pump(lambda: window.app.active is None)
        self.assertEqual(window.install.destination.text(), str(self.root / "Logos"))
        self.assertFalse((self.root / "Logos").exists())

        override = self.root / "explicit location"
        config = EphemeralConfiguration.project({}, str(self.config), environment={},
                                                cli_overrides={"install_dir": str(override)})
        selected = self.window(Services(), config)
        self.pump(lambda: selected.app.active is None)
        self.assertEqual(selected.install.destination.text(), str(override))
        self.assertFalse(override.exists())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_information_preview_preserves_exact_multiline_snapshot_and_copy(self):
        from PySide6.QtWidgets import QPushButton
        window = self.window()
        snapshot = "\n".join(f"Sanitized member {number}" for number in range(450))
        window.bridge.information.emit("Support preview", snapshot)
        self.pump(lambda: bool(window.dialogs.dialogs))
        dialog = next(iter(window.dialogs.dialogs))
        self.assertEqual(dialog.details.toPlainText(), snapshot)
        copy = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Copy details")
        copy.click()
        self.assertEqual(self.application.clipboard().text(), snapshot)
        dialog.reject()

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

    def test_binary_confirmation_returns_canonical_answers_and_defaults_to_no(self):
        window = self.window()

        def operation(app, cancel):
            return Result("succeeded", app.ask("Install this product?", options))

        for options, action, expected in (
            (["Yes", "No"], "yes", "Yes"),
            (["Yes", "No"], "no", "No"),
            (["Yes", "No"], "enter", "No"),
            (["Yes", "No"], "x", None),
            (["Yes", "No"], "escape", None),
            (["No", "Yes"], "no", "No"),
        ):
            with self.subTest(action=action, options=options):
                window.start(operation)
                self.pump(lambda: bool(window.dialogs.pending))
                prompt, dialog = next(iter(window.dialogs.pending.items()))
                self.assertIsNone(dialog.findChild(QComboBox))
                buttons = dialog.findChild(QDialogButtonBox)
                self.assertEqual(buttons.standardButtons(),
                                 QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
                yes = buttons.button(QDialogButtonBox.StandardButton.Yes)
                no = buttons.button(QDialogButtonBox.StandardButton.No)
                self.assertFalse(yes.isDefault())
                self.assertFalse(yes.autoDefault())
                self.assertTrue(no.isDefault())
                self.assertFalse(prompt.done.is_set())
                self.assertIsNone(prompt.answer)

                if action == "yes":
                    yes.click()
                elif action == "no":
                    no.click()
                elif action == "enter":
                    text = dialog.findChild(QTextBrowser)
                    text.setFocus()
                    QTest.keyClick(text, Qt.Key.Key_Return)
                elif action == "x":
                    dialog.close()
                else:
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)

                self.pump(lambda: prompt.done.is_set() and window.app.active is None)
                self.assertEqual(prompt.answer, expected)
                self.assertFalse(prompt.complete("second result"))
                self.assertFalse(window.dialogs.pending)
                self.assertEqual(window.status.text(), expected if expected else "Operation cancelled")

    def test_review_actions_accept_ok_and_cancel_without_an_automatic_answer(self):
        window = self.window()

        def operation(app, cancel):
            return Result("succeeded", app.ask("Fixture report with no approval question", options))

        for options, action in (
            (["OK", "Cancel"], "ok"),
            (["OK", "Cancel"], "cancel"),
            (["OK", "Cancel"], "enter"),
            (["OK", "Cancel"], "x"),
            (["OK", "Cancel"], "escape"),
            (["Cancel", "OK"], "cancel"),
        ):
            with self.subTest(action=action, options=options):
                window.start(operation)
                self.pump(lambda: bool(window.dialogs.pending))
                prompt, dialog = next(iter(window.dialogs.pending.items()))
                self.assertIsNone(dialog.findChild(QComboBox))
                buttons = dialog.findChild(QDialogButtonBox)
                self.assertEqual(buttons.standardButtons(),
                                 QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
                cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
                self.assertFalse(ok.isDefault())
                self.assertFalse(ok.autoDefault())
                self.assertTrue(cancel.isDefault())
                self.assertFalse(prompt.done.is_set())

                if action == "ok":
                    ok.click()
                elif action == "cancel":
                    cancel.click()
                elif action == "enter":
                    text = dialog.findChild(QTextBrowser)
                    text.setFocus()
                    QTest.keyClick(text, Qt.Key.Key_Return)
                elif action == "x":
                    dialog.close()
                else:
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)

                self.pump(lambda: prompt.done.is_set() and window.app.active is None)
                self.assertEqual(prompt.answer, "OK" if action == "ok" else None)
                self.assertFalse(prompt.complete("second result"))
                self.assertFalse(window.dialogs.pending)
                self.assertEqual(window.status.text(), "OK" if action == "ok" else "Operation cancelled")

    def test_question_links_activate_only_safe_https_and_preserve_unicode_plain_text(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices, QTextCursor

        window = self.window()
        target = "https://example.test/terms"
        balanced = "https://example.test/policy_(current)"
        question = (
            f"\U0001f4d6 Read ({target}).\n"
            f"\U0001f600 Other terms: {balanced}.\n"
            "Path: /disposable/\u00e9tudes/\U0001f4da/Logos\n"
            '<b>Literal HTML</b> <a href="file:///tmp/private">not a link</a>\n'
            "file:///tmp/private javascript:alert(1) data:text/plain,hello custom://host\n"
            "http://example.test/insecure https://user:secret@example.test/private"
        )

        def operation(app, cancel):
            return Result("succeeded", app.ask(question, ["Yes", "No"]))

        with patch.object(QDesktopServices, "openUrl", return_value=True) as open_url:
            window.start(operation)
            self.pump(lambda: bool(window.dialogs.pending))
            prompt, dialog = next(iter(window.dialogs.pending.items()))
            text = dialog.findChild(QTextBrowser)
            self.assertEqual(text.toPlainText(), question)
            open_url.assert_not_called()

            anchors = []
            block = text.document().begin()
            while block.isValid():
                fragment = block.begin()
                while not fragment.atEnd():
                    part = fragment.fragment()
                    if part.charFormat().isAnchor():
                        anchors.append((part.text(), part.charFormat().anchorHref()))
                    fragment += 1
                block = block.next()
            self.assertEqual(anchors, [(target, target), (balanced, balanced)])

            start = len(question[:question.index(target)].encode("utf-16-le")) // 2
            cursor = QTextCursor(text.document())
            cursor.setPosition(start)
            cursor.setPosition(start + len(target), QTextCursor.MoveMode.KeepAnchor)
            self.assertEqual(cursor.selectedText(), target)
            self.assertEqual(cursor.charFormat().anchorHref(), target)
            cursor.setPosition(start + 3)
            text.setTextCursor(cursor)
            text.ensureCursorVisible()
            point = text.cursorRect().center()
            self.assertEqual(text.anchorAt(point), target)
            QTest.mouseClick(text.viewport(), Qt.MouseButton.LeftButton, pos=point)
            open_url.assert_called_once_with(QUrl(target))
            self.assertFalse(prompt.done.is_set())

            open_url.reset_mock()
            for forbidden in ("file:///tmp/private", "javascript:alert(1)", "data:text/plain,hello",
                              "custom://host", "http://example.test/", "https://user:secret@example.test/",
                              "https://@example.test/", "https:///missing-host"):
                text.anchorClicked.emit(QUrl(forbidden))
            open_url.assert_not_called()

            open_url.return_value = False
            with patch.object(window.dialogs, "failure") as failure:
                QTest.mouseClick(text.viewport(), Qt.MouseButton.LeftButton, pos=point)
                open_url.assert_called_once_with(QUrl(target))
                failure.assert_called_once()
                self.assertNotIn(target, str(failure.call_args))
            dialog.close()
            self.pump(lambda: prompt.done.is_set() and window.app.active is None)
            self.assertIsNone(prompt.answer)

    def test_long_confirmation_scope_is_keyboard_readable_without_automatic_answer(self):
        window = self.window()
        long_path = "/disposable/" + "long-installation-component/" * 30
        question = "\n".join([
            f"Config: {long_path}selected.json",
            f"Install: {long_path}Logos",
            f"Prefix: {long_path}Logos/data/wine64_bottle",
            *[f"Consequence {number}: review the selected installation scope." for number in range(60)],
            "Continue with this installation? <plain text, not a link>",
        ])

        def operation(app, cancel):
            return Result("succeeded", app.ask(question, ["Continue", "Go back"]))

        window.start(operation)
        self.pump(lambda: bool(window.dialogs.pending))
        prompt, dialog = next(iter(window.dialogs.pending.items()))
        text = dialog.findChild(QTextBrowser)
        self.assertIsNotNone(text)
        self.assertTrue(text.isReadOnly())
        self.assertEqual(text.toPlainText(), question)
        choice = dialog.findChild(QComboBox)
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertEqual(choice.currentIndex(), -1)
        self.assertFalse(buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())

        dialog.resize(420, 340)
        self.pump(lambda: text.verticalScrollBar().maximum() > 0)
        text.setFocus()
        QTest.keyClick(text, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(text.textCursor().selectedText().replace("\u2029", "\n"), question)
        QTest.keyClick(text, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(text, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
        self.pump(lambda: text.verticalScrollBar().value() > 0)
        self.assertTrue(text.textCursor().atEnd())
        self.assertTrue(text.viewport().rect().contains(text.cursorRect()))
        self.assertEqual(text.horizontalScrollBar().maximum(), 0)
        self.assertTrue(dialog.rect().contains(buttons.geometry()))
        self.assertTrue(dialog.rect().contains(choice.geometry()))
        self.assertFalse(prompt.done.is_set())
        self.assertIsNone(prompt.answer)

        dialog.close()
        self.pump(lambda: prompt.done.is_set() and window.app.active is None)
        self.assertIsNone(prompt.answer)
        self.assertFalse(prompt.complete("second result"))
        self.assertFalse(window.dialogs.pending)

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
        from logos_desktop.constants import PROMPT_OPTION_DIRECTORY
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

        with patch("logos_desktop.qt.bridge.logging.exception") as report:
            window.bridge.post(broken)
            window.bridge.post(lambda: seen.append("following"))
            self.pump(lambda: seen == ["following"])
            report.assert_called_once()
        window.bridge.post(lambda: seen.append("later"))
        self.pump(lambda: seen == ["following", "later"])


@unittest.skipUnless(QT_AVAILABLE, "Optional Qt frontend is not installed")
class TestQtActivation(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        from logos_desktop.qt import activation

        self.activation = activation
        mapping = patch.dict(os.environ, {"SYNTHETIC_WORKER_MARKER": "preserved"}, clear=True)
        mapping.start()
        self.addCleanup(mapping.stop)
        self.addCleanup(activation.take_startup_token)
        activation.take_startup_token()
        self.window = Mock()
        self.window._closing = False
        self.window.app.stop_requested = threading.Event()
        self.window.testAttribute.return_value = False
        self.window.isMinimized.return_value = False
        self.handle = self.window.windowHandle.return_value

    def c_token(self):
        from PySide6.QtCore import QProcessEnvironment
        return QProcessEnvironment.systemEnvironment().value(self.activation.TOKEN_VARIABLE)

    def assert_token_absent(self):
        self.assertEqual(self.c_token(), "")
        self.assertNotIn(self.activation.TOKEN_VARIABLE, os.environ)

    def test_forwarded_token_reaches_single_simulated_backend_only_after_restore(self):
        self.window.isMinimized.return_value = True
        self.window.testAttribute.return_value = True

        def restored():
            self.assert_token_absent()

        def backend():
            self.assertEqual(self.c_token(), "synthetic-fresh-token")
            self.assertNotIn(self.activation.TOKEN_VARIABLE, os.environ)
            self.window.showNormal.assert_called_once()
            self.window.setAttribute.assert_called_with(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            os.unsetenv(self.activation.TOKEN_VARIABLE)

        self.window.showNormal.side_effect = restored
        self.handle.requestActivate.side_effect = backend
        self.activation.activate_window(self.window, "synthetic-fresh-token")
        self.handle.requestActivate.assert_called_once_with()
        self.window.raise_.assert_not_called()
        self.window.activateWindow.assert_not_called()
        self.assert_token_absent()

    def test_initial_show_consumes_inherited_mapping_once_and_does_not_restore_it(self):
        os.environ[self.activation.TOKEN_VARIABLE] = "synthetic-startup-token"
        token = self.activation.take_startup_token()
        self.assert_token_absent()

        def backend():
            self.assertEqual(self.c_token(), "synthetic-startup-token")
            self.assertNotIn(self.activation.TOKEN_VARIABLE, os.environ)
            os.unsetenv(self.activation.TOKEN_VARIABLE)

        self.window.show.side_effect = backend
        self.activation.show_initial_window(self.window, token)
        self.assert_token_absent()
        self.window.show.side_effect = None
        self.activation.activate_window(self.window, "")
        self.handle.requestActivate.assert_called_once_with()
        self.assert_token_absent()

    def test_failure_cleanup_never_restores_stale_mapping_or_c_token(self):
        for stage in ("show", "request"):
            with self.subTest(stage=stage):
                os.environ[self.activation.TOKEN_VARIABLE] = "synthetic-stale-mapping"
                os.putenv(self.activation.TOKEN_VARIABLE, "synthetic-stale-c-value")
                if stage == "show":
                    self.window.show.side_effect = RuntimeError("synthetic show failure")
                    operation = self.activation.show_initial_window
                else:
                    self.handle.requestActivate.side_effect = RuntimeError("synthetic request failure")
                    operation = self.activation.activate_window
                with self.assertRaises(RuntimeError):
                    operation(self.window, "synthetic-new-token")
                self.assert_token_absent()
                self.window.show.side_effect = None

    def test_restore_failure_restores_attribute_and_never_publishes_token(self):
        self.window.show.side_effect = RuntimeError("synthetic restore failure")
        with self.assertRaises(RuntimeError):
            self.activation.activate_window(self.window, "synthetic-token")
        self.window.setAttribute.assert_called_with(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.handle.requestActivate.assert_not_called()
        self.assert_token_absent()

    def test_synchronous_close_during_show_skips_activation(self):
        self.window.show.side_effect = self.window.app.stop_requested.set
        self.activation.activate_window(self.window, "synthetic-token")
        self.handle.requestActivate.assert_not_called()
        self.assert_token_absent()

    def test_tokenless_missing_handle_and_closing_paths_do_not_force_focus(self):
        self.handle.requestActivate.side_effect = self.assert_token_absent
        self.activation.activate_window(self.window, "")
        self.handle.requestActivate.assert_called_once_with()
        self.window.windowHandle.return_value = None
        self.activation.activate_window(self.window, "synthetic-unused-token")
        self.assert_token_absent()
        self.window.reset_mock()
        self.window._closing = True
        self.activation.activate_window(self.window, "synthetic-token")
        self.window.show.assert_not_called()
        self.window._closing = False
        self.window.app.stop_requested.set()
        self.activation.activate_window(self.window, "synthetic-token")
        self.window.show.assert_not_called()

    def test_mime_worker_child_excludes_c_only_token_during_simulated_request(self):
        from logos_desktop import desktop

        run = subprocess.run
        observed = []
        failures = []

        def query(arguments, **kwargs):
            self.assertNotIn(self.activation.TOKEN_VARIABLE, kwargs["env"])
            code = "import os,sys\nsys.exit('XDG_ACTIVATION_TOKEN' in os.environ)\n"
            result = run([sys.executable, "-I", "-B", "-c", code], env=kwargs["env"],
                         capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(kwargs["env"]["SYNTHETIC_WORKER_MARKER"], "preserved")
            return subprocess.CompletedProcess(arguments, 0, b"fixture.desktop\n")

        def worker():
            try:
                observed.append(desktop._default("x-scheme-handler/synthetic"))
            except (AssertionError, OSError, subprocess.SubprocessError) as error:
                failures.append(type(error).__name__)

        def backend():
            self.assertEqual(self.c_token(), "synthetic-overlap-token")
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(10)
            self.assertFalse(thread.is_alive())

        self.handle.requestActivate.side_effect = backend
        with (patch.object(desktop.shutil, "which", return_value="/fixture/xdg-mime"),
              patch.object(desktop.subprocess, "run", side_effect=query)):
            self.activation.activate_window(self.window, "synthetic-overlap-token")
        self.assertEqual(failures, [])
        self.assertEqual(observed, ["fixture.desktop"])
        self.assert_token_absent()


class TestNoQtCLI(unittest.TestCase):
    def test_frontend_selection_accepts_wayland_and_retains_terminal_and_legacy_aliases(self):
        from unittest.mock import Mock
        from logos_desktop import main, maintenance, system
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
from logos_desktop import main
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
        if fullname.startswith(("PySide6", "logos_desktop.qt", "tkinter", "logos_desktop.gui")):
            raise AssertionError("CLI imported Qt")
sys.meta_path.insert(0, NoQt())
from logos_desktop import main
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
