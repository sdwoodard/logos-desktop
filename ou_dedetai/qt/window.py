"""Thin pages consuming snapshots and explicitly supplied operation services."""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QStackedWidget, QStyle, QVBoxLayout, QWidget,
)

from ou_dedetai.constants import PROMPT_OPTION_DIRECTORY
from .app import QtApp
from .bridge import Bridge
from .dialogs import DialogHost


@dataclass(frozen=True)
class Installation:
    product: str
    version: str
    runtime: str
    path: str
    installed: bool = True
    running: bool = False


@dataclass(frozen=True)
class InstallDraft:
    product: str
    destination: str
    runtime: str
    install_dependencies: bool


def label(text, *, heading=False, secondary=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if heading:
        font = widget.font()
        font.setPointSizeF(font.pointSizeF() + 6)
        font.setBold(True)
        widget.setFont(font)
    if secondary:
        widget.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return widget


def value(text=""):
    widget = label(text)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class HomePage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(label("Your library, on Linux", heading=True))
        layout.addWidget(label("Run your managed installation and see what needs attention.", secondary=True))

        group = QGroupBox("Managed installation")
        form = QFormLayout(group)
        form.setContentsMargins(20, 24, 20, 20)
        form.setSpacing(16)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.product = value()
        self.state = value()
        self.runtime = value()
        self.path = value()
        form.addRow("Product", self.product)
        form.addRow("Status", self.state)
        form.addRow("Wine runtime", self.runtime)
        form.addRow("Location", self.path)
        self.run = QPushButton("Run Logos")
        self.run.setMinimumHeight(36)
        form.addRow("", self.run)
        layout.addWidget(group)

        self.health = label("Sample installation is ready. No personal library is connected.")
        layout.addWidget(self.health)
        layout.addWidget(label(
            "Controller: private development build, unversioned. Self-updates are disabled.", secondary=True,
        ))
        layout.addWidget(label("Recent activity"))
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setAccessibleName("Recent activity")
        self.activity.setMaximumBlockCount(100)
        self.activity.setMaximumHeight(130)
        layout.addWidget(self.activity)
        layout.addStretch()

    def render(self, snapshot):
        product = f"{snapshot.product} {snapshot.version}" if snapshot.installed else "No installation selected"
        self.product.setText(product)
        self.state.setText("Running" if snapshot.running else "Ready to run" if snapshot.installed else "Not installed")
        self.runtime.setText(snapshot.runtime)
        self.path.setText(snapshot.path)
        self.run.setText(f"Stop {snapshot.product}" if snapshot.running else f"Run {snapshot.product}")
        icon = QStyle.StandardPixmap.SP_MediaStop if snapshot.running else QStyle.StandardPixmap.SP_MediaPlay
        self.run.setIcon(self.style().standardIcon(icon))
        self.run.setEnabled(snapshot.installed)
        self.health.setText("Sample installation is ready. No personal library is connected." if snapshot.installed
                            else "Choose Install to preview a new setup with sample data.")


class InstallPage(QWidget):
    browse_requested = Signal()
    preview_requested = Signal()

    def __init__(self, snapshot):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(label("Set up an installation", heading=True))
        layout.addWidget(label("Step 1 of 4 · Product and location", secondary=True))
        layout.addWidget(label("Choose where to install and which Wine runtime to use. "
                               "This preview only checks sample choices."))
        group = QGroupBox("Installation options")
        form = QFormLayout(group)
        form.setContentsMargins(20, 24, 20, 20)
        form.setSpacing(16)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.product = QComboBox()
        self.product.addItems(["Logos", "Verbum"])
        self.product.setCurrentText(snapshot.product)
        product_label = QLabel("&Product")
        product_label.setBuddy(self.product)
        form.addRow(product_label, self.product)

        self.destination = QLineEdit(snapshot.path)
        self.destination.setAccessibleName("Installation location")
        self.browse = QPushButton("&Browse…")
        self.browse.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.browse.clicked.connect(self.browse_requested)
        location = QWidget()
        row = QHBoxLayout(location)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.destination, 1)
        row.addWidget(self.browse)
        destination_label = QLabel("&Location")
        destination_label.setBuddy(self.destination)
        form.addRow(destination_label, location)

        self.runtime = QComboBox()
        self.runtime.addItems([snapshot.runtime, "System Wine (sample)"])
        runtime_label = QLabel("&Wine runtime")
        runtime_label.setBuddy(self.runtime)
        form.addRow(runtime_label, self.runtime)
        self.dependencies = QCheckBox("Install system &dependencies")
        self.dependencies.setChecked(True)
        form.addRow("", self.dependencies)
        layout.addWidget(group)
        layout.addWidget(label("Changes stay in this form until you continue. "
                               "The preview does not download files or change your system.", secondary=True))
        self.validation = label("")
        layout.addWidget(self.validation)
        self.continue_button = QPushButton("&Continue preview")
        self.continue_button.setMinimumHeight(36)
        self.continue_button.clicked.connect(self.preview_requested)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self.continue_button)
        layout.addLayout(actions)
        layout.addStretch()

    def draft(self):
        return InstallDraft(self.product.currentText(), self.destination.text(),
                            self.runtime.currentText(), self.dependencies.isChecked())


class SettingsPage(QWidget):
    def __init__(self, theme, snapshot):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(label("Settings", heading=True))
        layout.addWidget(label("Make the controller fit your desktop.", secondary=True))
        group = QGroupBox("Appearance")
        form = QFormLayout(group)
        form.setContentsMargins(20, 24, 20, 20)
        form.setSpacing(16)
        self.appearance = QComboBox()
        self.appearance.addItems(theme.choices)
        self.appearance.setCurrentText(theme.preference)
        appearance_label = QLabel("&Color scheme")
        appearance_label.setBuddy(self.appearance)
        form.addRow(appearance_label, self.appearance)
        self.effective = label("")
        form.addRow("", self.effective)
        layout.addWidget(group)
        layout.addWidget(label("System follows your desktop while LogosDesktop stays open.", secondary=True))
        self.warning = label("")
        layout.addWidget(self.warning)
        details = QGroupBox("Preview installation")
        paths = QFormLayout(details)
        paths.setContentsMargins(20, 24, 20, 20)
        paths.setSpacing(16)
        paths.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        paths.addRow("Location", value(snapshot.path))
        paths.addRow("Wine runtime", value(snapshot.runtime))
        paths.addRow("Controller updates", label("Disabled during private development"))
        layout.addWidget(details)
        layout.addStretch()


class MainWindow(QMainWindow):
    closed = Signal()

    def __init__(self, config, theme, services):
        super().__init__()
        self.setWindowTitle("LogosDesktop · Design preview")
        self.resize(1100, 760)
        self.setMinimumSize(640, 480)
        self.theme = theme
        self.services = services
        self.snapshot = services.snapshot()
        self._ticket = None
        self._retry = None
        self._closing = False
        self._final_close = False
        self._unsubscribe = None
        self._last_activity = ""
        self.bridge = Bridge(self)
        self.dialogs = DialogHost(self, self.bridge)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(16)
        header = QHBoxLayout()
        header.addWidget(label("LogosDesktop", heading=True))
        header.addStretch()
        header.addWidget(label("DESIGN PREVIEW · Sample data", secondary=True))
        outer.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(24)
        self.navigation = QListWidget()
        self.navigation.setAccessibleName("Navigation")
        self.navigation.addItems(["Home", "Install", "Settings"])
        self.navigation.setFixedWidth(164)
        self.navigation.setSpacing(6)
        self.navigation.setCurrentRow(0)
        body.addWidget(self.navigation)
        self.pages = QStackedWidget()
        self.home = HomePage()
        self.install = InstallPage(self.snapshot)
        self.settings = SettingsPage(theme, self.snapshot)
        for page in (self.home, self.install, self.settings):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)

        footer = QHBoxLayout()
        self.status = label("Opening the preview…")
        self.status.setAccessibleName("Operation status")
        footer.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Operation progress")
        self.progress.setMaximumWidth(180)
        self.progress.setRange(0, 0)
        self.progress.hide()
        footer.addWidget(self.progress)
        self.cancel = QPushButton("Cancel operation")
        self.cancel.setEnabled(False)
        footer.addWidget(self.cancel)
        outer.addLayout(footer)

        self.settings.appearance.currentTextChanged.connect(theme.set_preference)
        theme.changed.connect(self._theme_changed)
        theme.warning.connect(self.settings.warning.setText)
        self.home.run.clicked.connect(lambda: self.start(self.services.toggle))
        self.install.browse_requested.connect(self._browse)
        self.install.preview_requested.connect(self._preview)
        self.cancel.clicked.connect(self._cancel)
        self.bridge.started.connect(self._started)
        self.bridge.progress.connect(self._progress)
        self.bridge.initialized.connect(self._initialized, Qt.ConnectionType.QueuedConnection)
        self.bridge.close_requested.connect(self._exit_requested, Qt.ConnectionType.QueuedConnection)
        for key, index in (("Alt+1", 0), ("Alt+2", 1), ("Alt+3", 2)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda index=index: self.navigation.setCurrentRow(index))
        quit_shortcut = QShortcut(QKeySequence.StandardKey.Quit, self)
        quit_shortcut.activated.connect(self.close)
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(50)
        self._close_timer.timeout.connect(self._finish_close)
        self.app = QtApp(config, self.bridge)
        self.bridge.completed.connect(self._finished)
        self.home.run.setEnabled(False)
        self.install.continue_button.setEnabled(False)
        self._theme_changed()

    @Slot()
    def _initialized(self):
        if self._closing:
            return
        if self.app.initialization_error:
            self.status.setText("Preview could not be opened")
            self.dialogs.failure("Cannot open preview", self.app.initialization_error)
            return
        self._unsubscribe = self.app.subscribe_config(self._configuration_changed)
        self._configuration_changed()
        self.install.continue_button.setEnabled(True)
        self.status.setText("Ready · Preview actions use sample data")

    def _configuration_changed(self):
        if not self._closing:
            self.snapshot = self.services.snapshot()
            self.home.render(self.snapshot)
            if self.app.active is not None:
                self.home.run.setEnabled(False)

    def start(self, operation):
        if self.app.run_operation(operation):
            self._retry = operation

    def _preview(self):
        draft = self.install.draft()
        if not draft.destination.strip() or not Path(draft.destination).is_absolute():
            self.install.validation.setText("Choose an absolute installation location before continuing.")
            self.install.destination.setFocus()
            return
        self.install.validation.clear()
        self.start(self.services.preview(draft))

    def _browse(self):
        def chosen(path):
            if path is not None and not self._closing:
                self.install.destination.setText(path)
                self.install.destination.setFocus()
        self.dialogs.picker("Choose installation location", PROMPT_OPTION_DIRECTORY, chosen,
                            self.install.destination.text())

    def _cancel(self):
        if self.app.active is not None:
            self.app.cancel_operation()
            self.cancel.setEnabled(False)
            self.status.setText("Cancelling · Waiting for work to settle")

    @Slot(object)
    def _started(self, ticket):
        self._ticket = ticket
        self.home.run.setEnabled(False)
        self.install.continue_button.setEnabled(False)
        self.install.browse.setEnabled(False)
        self.cancel.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText("Working…")

    @Slot(object)
    def _progress(self, progress):
        if self._closing or progress.ticket != self._ticket:
            return
        if not self.app._cancel.is_set():
            self.status.setText(progress.message)
        if progress.percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, progress.percent)))
        if progress.message != self._last_activity:
            self.home.activity.appendPlainText(progress.message)
            self._last_activity = progress.message

    @Slot(object)
    def _finished(self, completion):
        if self._closing or completion.ticket != self._ticket:
            return
        self._ticket = None
        self.cancel.setEnabled(False)
        self.progress.hide()
        self.install.continue_button.setEnabled(True)
        self.install.browse.setEnabled(True)
        self._configuration_changed()
        result = completion.result
        self.status.setText(result.message)
        self.home.activity.appendPlainText(f"{result.outcome.title()}: {result.message}")
        if result.outcome in ("failed", "unknown"):
            operation = self._retry
            retry = (lambda: self.start(operation)) if operation is not None else None
            self.dialogs.failure("Preview could not finish", result.message, result.details, retry)

    @Slot()
    def _theme_changed(self):
        combo = self.settings.appearance
        combo.blockSignals(True)
        combo.setCurrentText(self.theme.preference)
        combo.blockSignals(False)
        mode = "Dark" if self.theme.dark else "Light"
        self.settings.effective.setText(f"Effective appearance: {mode}")

    @Slot(str, bool)
    def _exit_requested(self, reason, intended):
        if not intended:
            self.home.activity.appendPlainText(reason)
        self.close()

    def closeEvent(self, event):
        if self._final_close:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self.app.request_close()
        self.dialogs.close()
        self.status.setText("Closing · Waiting for owned work to settle")
        self.centralWidget().setEnabled(False)
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._close_timer.start()
        QTimer.singleShot(0, self._finish_close)

    def _finish_close(self):
        if self.app.close(timeout=0):
            self._close_timer.stop()
            self.bridge.dispose()
            self._final_close = True
            self.closed.emit()
            self.close()
