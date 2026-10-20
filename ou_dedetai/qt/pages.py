"""Maintenance and diagnostics controls with window-owned action delivery."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QPalette
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ou_dedetai import constants


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
        self.title = heading(title, 13)
        self.content.addWidget(self.title)
        self.detail = description(text)
        self.content.addWidget(self.detail)


class CarePage(QWidget):
    requested = Signal(str)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(heading("Backups & tools"))
        layout.addWidget(description("Back up your work and keep Logos up to date."))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.buttons = {}
        grid = QGridLayout()
        self.grid = grid
        grid.setSpacing(20)
        self.backups = Card("Backups", "Save a verified copy of your Logos data before making changes.")
        row = QHBoxLayout()
        row.addWidget(self.button("backup", "Create backup"))
        row.addWidget(self.button("restore", "Restore…"))
        self.backups.content.addLayout(row)
        self.updates = Card("Logos updates", "Check for the latest stable release when you are ready.")
        self.updates.content.addWidget(self.summary)
        row = QHBoxLayout()
        row.addWidget(self.button("check", "Check for updates"))
        row.addWidget(self.button("product_update", "Install update"))
        self.updates.content.addLayout(row)
        grid.addWidget(self.backups, 0, 0)
        grid.addWidget(self.updates, 1, 0)
        layout.addLayout(grid)

        tools = Card("Need to fix something?", "Repair, indexing and runtime tools are here when needed. "
                     "System dependency installation changes host packages. Cleanup actions ask for confirmation.")
        self.tools = QToolButton()
        self.tools.setText("Advanced tools")
        self.tools.setMinimumHeight(32)
        self.tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.tools)
        menu.setToolTipsVisible(True)
        for group in (
            [("index", "Start indexing"), ("stop_index", "Stop indexing")],
            [("repair", "Repair installation…"), ("runtime_update", "Update recommended Wine…"),
             ("controller_update", "Update LogosDesktop…"),
             ("dependencies", "Install system dependencies…"), ("icu", "Reinstall ICU data…")],
            [("edit_config", "Open configuration file"), ("logging", "Toggle Logos logging")],
            [("catalog", "Remove library catalog…"), ("indexes", "Remove index files…"),
             ("uninstall", "Uninstall Logos…")],
        ):
            if not menu.isEmpty():
                menu.addSeparator()
            for key, text in group:
                if key == "controller_update" and not constants.APP_SELF_UPDATES_ENABLED:
                    continue
                action = QAction(text, menu)
                action.triggered.connect(lambda checked=False, key=key: self.requested.emit(key))
                self.buttons[key] = action
                menu.addAction(action)
        self.tools.setMenu(menu)
        tools.content.addWidget(self.tools, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(tools)
        layout.addStretch()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.grid.addWidget(self.updates, 0 if self.width() >= 700 else 1, 1 if self.width() >= 700 else 0)

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
        support = Card("Get help", "Create a local support archive with configuration and logs. "
                       "Review it privately before sharing.")
        actions = QHBoxLayout()
        self.buttons = {}
        for key, text in (("support", "Create support archive…"), ("open_log", "Open log"),
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
        layout.addStretch()


class ProfileSettings(Card):
    save_requested = Signal()
    profile_requested = Signal()
    runtime_requested = Signal()

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
        self.content.addWidget(description("Updates use the latest stable releases."))

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
        self.choose_profile = QPushButton("Choose installation profile…")
        self.choose_runtime = QPushButton("Change Wine AppImage…")
        for button, signal in ((self.choose_profile, self.profile_requested),
                               (self.choose_runtime, self.runtime_requested)):
            button.clicked.connect(signal)
            fields.addRow(button)
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
        self.backup.setText(snapshot.backup)
