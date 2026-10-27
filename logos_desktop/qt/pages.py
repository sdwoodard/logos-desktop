"""Maintenance and diagnostics controls with window-owned action delivery."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QPalette
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from logos_desktop import releases


def heading(text, size=17):
    result = QLabel(text)
    font = result.font()
    font.setPointSizeF(size)
    font.setBold(True)
    result.setFont(font)
    result.setWordWrap(True)
    return result


def description(text):
    result = QLabel(text)
    result.setWordWrap(True)
    return result


class Card(QFrame):
    def __init__(self, title, text):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setBackgroundRole(QPalette.ColorRole.Base)
        self.setAutoFillBackground(True)
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(20, 20, 20, 20)
        self.content.setSpacing(12)
        self.content.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.title = heading(title, 13)
        self.content.addWidget(self.title)
        self.detail = description(text)
        self.content.addWidget(self.detail)


class UpdateCards(QWidget):
    requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(20)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.buttons = {}
        self.updates = Card("Logos updates", "Keep your library software up to date.")
        self.updates.content.addWidget(self.summary)
        row = QHBoxLayout()
        row.addWidget(self.button("check", "Check for updates"))
        row.addWidget(self.button("product_update", "Install update"))
        self.updates.content.addLayout(row)
        self.controller = Card("LogosDesktop updates", "Identifying this controller installation…")
        self.controller.content.insertWidget(1, description("Update the Linux desktop app."))
        self.controller.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.controller.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addWidget(self.button("controller_check", "Check controller updates"))
        row.addWidget(self.button("controller_update", "Update environment…"))
        self.buttons["controller_check"].setEnabled(False)
        self.buttons["controller_update"].setVisible(False)
        self.controller.content.addLayout(row)
        self.grid.addWidget(self.updates, 0, 0)
        self.grid.addWidget(self.controller, 1, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.grid.addWidget(self.controller, 0 if self.width() >= 760 else 1,
                            1 if self.width() >= 760 else 0)

    def button(self, key, text):
        button = QPushButton(text)
        button.setMinimumHeight(32)
        button.clicked.connect(lambda: self.requested.emit(key))
        self.buttons[key] = button
        return button


class CarePage(QWidget):
    requested = Signal(str)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(heading("Backups & tools"))
        layout.addWidget(description("Protect your library and keep it running well."))
        self.buttons = {}
        self.grid = QGridLayout()
        self.grid.setSpacing(20)

        self.backups = Card("Back up & restore", "Save a copy of your Logos data before making changes.")
        row = QHBoxLayout()
        row.addWidget(self.button("backup", "Create backup"))
        row.addWidget(self.button("restore", "Restore…"))
        self.backups.content.addLayout(row)
        self.backups.content.addWidget(self.button("open_backup", "Open backup folder"))

        repair = Card("Repair & shortcuts", "Recover an interrupted installation or add desktop launchers.")
        repair.content.addWidget(self.button("repair", "Repair installation…"))
        repair.content.addWidget(self.button("desktop", "Set up desktop launchers…"))
        repair.content.addWidget(self.button("open_install", "Open installation folder"))

        indexing = Card("Library indexing", "Manage the index Logos uses to search your library.")
        self.index_status = description("")
        indexing.content.addWidget(self.index_status)
        row = QHBoxLayout()
        row.addWidget(self.button("index", "Start indexing"))
        row.addWidget(self.button("stop_index", "Stop indexing"))
        indexing.content.addLayout(row)

        wine = Card("Wine runtime", "Manage the compatibility tools used by Logos.")
        self.wine_status = description("")
        self.wine_status.setTextFormat(Qt.TextFormat.PlainText)
        wine.content.addWidget(self.wine_status)
        wine.content.addWidget(self.button("runtime_update", "Update recommended Wine…"))
        wine.content.addWidget(self.button("dependencies", "Install system dependencies…"))
        self.cards = (self.backups, repair, indexing, wine)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index, 0)
        layout.addLayout(self.grid)

        self.tools = QToolButton()
        self.tools.setText("More tools")
        self.tools.setMinimumHeight(32)
        self.tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.tools)
        menu.setToolTipsVisible(True)
        for key, text in (("edit_config", "Open configuration file"),
                          ("logging", "Toggle Logos logging"), ("icu", "Reinstall ICU data…")):
            action = QAction(text, menu)
            action.triggered.connect(lambda checked=False, key=key: self.requested.emit(key))
            self.buttons[key] = action
            menu.addAction(action)
        self.tools.setMenu(menu)
        layout.addWidget(self.tools, alignment=Qt.AlignmentFlag.AlignLeft)

        self.cleanup_toggle = QToolButton()
        self.cleanup_toggle.setText("Cleanup & removal")
        self.cleanup_toggle.setCheckable(True)
        self.cleanup_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.cleanup_toggle.setArrowType(Qt.ArrowType.RightArrow)
        cleanup = Card("Cleanup & removal", "These actions remove selected files. Review the scope before continuing.")
        for key, text in (("catalog", "Remove library catalog…"), ("indexes", "Remove index files…"),
                          ("uninstall", "Uninstall Logos…")):
            cleanup.content.addWidget(self.button(key, text))
        cleanup.hide()
        self.cleanup_toggle.toggled.connect(cleanup.setVisible)
        self.cleanup_toggle.toggled.connect(lambda opened: self.cleanup_toggle.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
        layout.addWidget(self.cleanup_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(cleanup)
        layout.addStretch()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = 2 if self.width() >= 760 else 1
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)

    def button(self, key, text):
        button = QPushButton(text)
        button.setMinimumHeight(32)
        button.clicked.connect(lambda: self.requested.emit(key))
        self.buttons[key] = button
        return button


class HelpPage(QWidget):
    requested = Signal(str)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(heading("Help & logs"))
        layout.addWidget(description("See what needs attention and collect the details needed to troubleshoot."))
        self.health = Card("Installation status", "Reading the selected installation…")
        layout.addWidget(self.health)
        self.summary = self.health.detail
        support = Card("Get help", "Preview the exact sanitized support snapshot before exporting. "
                       "Raw configuration and logs are omitted. Keep the archive private.")
        actions = QHBoxLayout()
        self.buttons = {}
        for key, text in (("support", "Create support archive…"), ("open_log", "Open log"),
                          ("open_logs", "Open log folder"), ("doctor", "Run diagnostics"),
                          ("refresh", "Refresh status")):
            button = QPushButton(text)
            button.setMinimumHeight(32)
            button.clicked.connect(lambda checked=False, key=key: self.requested.emit(key))
            self.buttons[key] = button
            actions.addWidget(button)
        support.content.addLayout(actions)
        layout.addWidget(support)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Technical details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Installation technical details")
        self.details.hide()
        self.details_toggle.toggled.connect(self.details.setVisible)
        self.details_toggle.toggled.connect(lambda opened: self.details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
        layout.addWidget(self.details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.details)
        self.doctor = QPlainTextEdit()
        self.doctor.setReadOnly(True)
        self.doctor.setAccessibleName("Read-only diagnostic report")
        self.doctor.hide()
        layout.addWidget(self.doctor)
        layout.addStretch()


class ProfileSettings(Card):
    save_requested = Signal()
    profile_requested = Signal()
    runtime_requested = Signal()
    rollback_requested = Signal()

    def __init__(self):
        super().__init__("Installation", "Your library stays in its existing location.")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.location = description("")
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Location", self.location)
        self.backup = QLineEdit()
        self.backup.setAccessibleName("Backup folder")
        row = QHBoxLayout()
        row.addWidget(self.backup, 1)
        save = QPushButton("Save")
        save.setAccessibleName("Save backup folder")
        save.setMinimumHeight(32)
        save.clicked.connect(self.save_requested)
        row.addWidget(save)
        form.addRow("Backup folder", row)
        self.content.addLayout(form)
        self.content.addWidget(description("Logos and LogosDesktop updates use the latest stable releases. "
                                           + releases.WINE_RECOMMENDATION_POLICY))

        self.advanced = QToolButton()
        self.advanced.setText("Advanced installation settings")
        self.advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced.setCheckable(True)
        self.content.addWidget(self.advanced, alignment=Qt.AlignmentFlag.AlignLeft)
        details = QWidget()
        fields = QFormLayout(details)
        fields.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.profile = description("")
        self.profile.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fields.addRow("Configuration", self.profile)
        self.runtime = description("")
        self.runtime.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fields.addRow("Wine runtime", self.runtime)
        self.wine_status = description("")
        self.wine_status.setTextFormat(Qt.TextFormat.PlainText)
        fields.addRow("Wine recommendation", self.wine_status)
        self.choose_profile = QPushButton("Choose installation profile…")
        self.choose_runtime = QPushButton("Change Wine AppImage…")
        for button, signal in ((self.choose_profile, self.profile_requested),
                               (self.choose_runtime, self.runtime_requested)):
            button.clicked.connect(signal)
            fields.addRow(button)
        self.undo_desktop = QPushButton("Undo desktop integration…")
        self.undo_desktop.clicked.connect(self.rollback_requested)
        fields.addRow(self.undo_desktop)
        self.content.addWidget(details)
        details.hide()
        self.advanced.toggled.connect(details.setVisible)
        self.advanced.toggled.connect(lambda opened: self.advanced.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))

    def load(self, snapshot):
        self.detail.setText("Your library stays in its existing location." if snapshot.selected
                            else "Set up Logos from Home, or choose an existing profile below.")
        self.profile.setText(snapshot.profile)
        self.location.setText(snapshot.path)
        self.runtime.setText(snapshot.runtime)
        self.wine_status.setText(snapshot.wine_status)
        self.backup.setText(snapshot.backup)
