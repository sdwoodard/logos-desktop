"""Owned asynchronous dialogs and exactly-once prompt resolution."""

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QLabel,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from ou_dedetai.constants import PROMPT_OPTION_DIRECTORY, PROMPT_OPTION_FILE, PROMPT_OPTION_NEW_FILE


PATH_CHOICES = (PROMPT_OPTION_DIRECTORY, PROMPT_OPTION_FILE, PROMPT_OPTION_NEW_FILE)


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

        dialog = self._own(QDialog(self.window))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("Choose an option")
        layout = QVBoxLayout(dialog)
        label = QLabel(prompt.question)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        choice = QComboBox()
        choice.setAccessibleName("Answer")
        choice.addItems(prompt.options)
        choice.setCurrentIndex(-1)
        layout.addWidget(choice)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        choice.currentIndexChanged.connect(lambda index: buttons.button(QDialogButtonBox.StandardButton.Ok)
                                          .setEnabled(index >= 0))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self.pending[prompt] = dialog

        def finished(code):
            self.pending.pop(prompt, None)
            answer = choice.currentText() if code == QDialog.DialogCode.Accepted else None
            if answer in PATH_CHOICES and not prompt.done.is_set() and not self.closing:
                self._path_prompt(prompt, answer)
            else:
                prompt.complete(answer)

        dialog.finished.connect(finished)
        dialog.open()

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
