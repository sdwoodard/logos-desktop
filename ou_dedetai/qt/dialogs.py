"""Owned asynchronous dialogs and exactly-once prompt resolution."""

from pathlib import Path
from itertools import islice
import re

from PySide6.QtCore import QObject, Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QLabel,
    QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout,
)

from ou_dedetai.constants import PROMPT_OPTION_DIRECTORY, PROMPT_OPTION_FILE, PROMPT_OPTION_NEW_FILE


PATH_CHOICES = (PROMPT_OPTION_DIRECTORY, PROMPT_OPTION_FILE, PROMPT_OPTION_NEW_FILE)


def _is_https_url(url):
    return (url.isValid() and not url.isRelative() and url.scheme() == "https"
            and bool(url.host()) and "@" not in url.authority())


class FailureDialog(QDialog):
    def __init__(self, title, summary, details="", *, parent=None, retry=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.resize(560, 340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)
        label = QLabel(summary)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Failure details")
        self.details.setPlainText(details or summary)
        self.details.document().setMaximumBlockCount(300)
        layout.addWidget(self.details)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = QPushButton("Copy details")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.details.toPlainText()))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        if retry is not None:
            button = buttons.addButton("Retry", QDialogButtonBox.ButtonRole.AcceptRole)
            button.clicked.connect(retry)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class DialogHost(QObject):
    def __init__(self, window, bridge):
        super().__init__(window)
        self.window = window
        self.pending = {}
        self.dialogs = set()
        self.closing = False
        bridge.prompt.connect(self.present, Qt.ConnectionType.QueuedConnection)
        bridge.cancel_prompt.connect(self.cancel, Qt.ConnectionType.QueuedConnection)
        bridge.information.connect(self.information, Qt.ConnectionType.QueuedConnection)

    def _own(self, dialog):
        self.dialogs.add(dialog)
        dialog.finished.connect(lambda: self.dialogs.discard(dialog))
        return dialog

    def failure(self, title, summary, details="", retry=None):
        if self.closing:
            return None
        dialog = self._own(FailureDialog(title, summary, details, parent=self.window, retry=retry))
        dialog.open()
        return dialog

    @Slot(str, str)
    def information(self, title, message):
        if self.closing:
            return
        dialog = self._own(FailureDialog(title, "Review the details below.", parent=self.window))
        dialog.details.document().setMaximumBlockCount(0)
        dialog.details.setAccessibleName("Information details")
        dialog.details.setPlainText(message)
        dialog.open()

    def picker(self, title, choice, callback, initial=None):
        dialog = self._own(QFileDialog(self.window, title, initial or str(Path.home())))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if choice == PROMPT_OPTION_DIRECTORY:
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            dialog.setOption(QFileDialog.Option.ShowDirsOnly)
        elif choice == PROMPT_OPTION_NEW_FILE:
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        else:
            dialog.setFileMode(QFileDialog.FileMode.ExistingFile)

        def finished(code):
            selected = dialog.selectedFiles() if code == QDialog.DialogCode.Accepted else []
            callback(selected[0] if selected and selected[0].strip() else None)

        dialog.finished.connect(finished)
        dialog.open()
        return dialog

    @Slot(object)
    def present(self, prompt):
        if self.closing or prompt.done.is_set():
            prompt.complete()
            return
        if isinstance(prompt.options, str):
            self._path_prompt(prompt, prompt.options)
            return

        binary = len(prompt.options) == 2 and set(prompt.options) == {"Yes", "No"}
        review = len(prompt.options) == 2 and set(prompt.options) == {"OK", "Cancel"}
        dialog = self._own(QDialog(self.window))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        title = "Choose an option"
        if binary:
            title = "Confirm"
        elif review:
            title = "Review"
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(420)
        dialog.resize(640, 420)
        layout = QVBoxLayout(dialog)
        question = QTextBrowser(dialog)
        question.setReadOnly(True)
        question.setOpenLinks(False)
        question.setOpenExternalLinks(False)
        question.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        question.setAccessibleName("Question and scope")
        question.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        question.setPlainText(prompt.question)

        # Keep untrusted question text literal and format only bounded HTTPS ranges.
        matches = re.finditer(r"(?<![\w:/])https://[^\s<>\"']+", prompt.question)
        for match in islice(matches, 100):
            target = match.group()
            if len(target) > 4096:
                continue
            target = target.rstrip(".,;:!?")
            for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
                while target.endswith(closing) and target.count(closing) > target.count(opening):
                    target = target[:-1]
            url = QUrl(target, QUrl.ParsingMode.StrictMode)
            if not _is_https_url(url):
                continue

            # QTextCursor positions count UTF-16 units, including surrogate pairs.
            start = len(prompt.question[:match.start()].encode("utf-16-le")) // 2
            length = len(target.encode("utf-16-le")) // 2
            cursor = QTextCursor(question.document())
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            link = QTextCharFormat()
            link.setAnchor(True)
            link.setAnchorHref(url.toString())
            link.setFontUnderline(True)
            cursor.mergeCharFormat(link)

        def open_link(url):
            if _is_https_url(url) and not QDesktopServices.openUrl(url):
                self.failure("Browser could not open", "The browser could not open this link.",
                             "Copy the link from the question and open it in your browser.")

        question.anchorClicked.connect(open_link)
        layout.addWidget(question, 1)

        selected_answer = None
        if binary or review:
            positive = QDialogButtonBox.StandardButton.Yes if binary else QDialogButtonBox.StandardButton.Ok
            negative = QDialogButtonBox.StandardButton.No if binary else QDialogButtonBox.StandardButton.Cancel
            buttons = QDialogButtonBox(positive | negative)
            positive_button = buttons.button(positive)
            negative_button = buttons.button(negative)
            positive_button.setAutoDefault(False)
            positive_button.setDefault(False)
            negative_button.setAutoDefault(False)
            negative_button.setDefault(True)

            def direct_clicked(button):
                nonlocal selected_answer
                if button is positive_button:
                    selected_answer = "Yes" if binary else "OK"
                elif binary:
                    selected_answer = "No"
                else:
                    dialog.reject()
                    return
                dialog.accept()

            buttons.clicked.connect(direct_clicked)
        else:
            choice = QComboBox()
            choice.setAccessibleName("Answer")
            choice.addItems(prompt.options)
            choice.setCurrentIndex(-1)
            layout.addWidget(choice)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

            def choice_changed(index):
                nonlocal selected_answer
                selected_answer = choice.currentText() if index >= 0 else None
                buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(index >= 0)

            choice.currentIndexChanged.connect(choice_changed)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)

        layout.addWidget(buttons)
        self.pending[prompt] = dialog

        def finished(code):
            self.pending.pop(prompt, None)
            answer = selected_answer if code == QDialog.DialogCode.Accepted else None
            if answer in PATH_CHOICES and not prompt.done.is_set() and not self.closing:
                self._path_prompt(prompt, answer)
            else:
                prompt.complete(answer)

        dialog.finished.connect(finished)
        dialog.open()
        if binary or review:
            negative_button.setFocus()

    def _path_prompt(self, prompt, choice):
        def finished(answer):
            self.pending.pop(prompt, None)
            prompt.complete(answer)
        self.pending[prompt] = self.picker(prompt.question, choice, finished)

    @Slot(object)
    def cancel(self, prompt):
        prompt.complete()
        dialog = self.pending.get(prompt)
        if dialog is not None:
            dialog.reject()

    def close(self):
        self.closing = True
        for prompt in tuple(self.pending):
            self.cancel(prompt)
        for dialog in tuple(self.dialogs):
            dialog.reject()
