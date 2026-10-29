"""Thin pages consuming snapshots and explicitly supplied operation services."""

from dataclasses import dataclass
from os.path import expanduser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from logos_desktop.constants import APP_IMAGE_DIR, PROMPT_OPTION_DIRECTORY, PROMPT_OPTION_FILE
from logos_desktop import constants, releases
from .app import QtApp
from .bridge import Bridge
from .dialogs import DialogHost
from .pages import Card, CarePage, HelpPage, ProfileSettings, UpdateCards


@dataclass(frozen=True)
class Installation:
    product: str
    version: str
    runtime: str
    path: str
    installed: bool = True
    running: bool = False
    state: str = ""
    indexing: str = "Stopped"
    health: str = ""
    profile: str = ""
    backup: str = ""
    product_channel: str = "stable"
    controller_channel: str = "stable"
    logging: bool = False
    product_update: str = "Not checked"
    runtimes: tuple[str, ...] = ()
    selected: bool = False
    wine_status: str = releases.WINE_RECOMMENDATION_POLICY + " Availability has not been checked."
    controller_owner: str = "unsupported"
    controller_status: str = "unchecked"
    controller_message: str = "Controller installation has not been identified."
    controller_standalone: bool = False


@dataclass(frozen=True)
class InstallDraft:
    product: str
    destination: str
    runtime: str
    install_dependencies: bool
    release: str = ""


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


def primary_text(background):
    channels = [background.redF(), background.greenF(), background.blueF()]
    linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
              for channel in channels]
    luminance = sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))
    return QColor(Qt.GlobalColor.black if luminance > .179 else Qt.GlobalColor.white)


class HomePage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(label("Your Logos library", heading=True))
        layout.addWidget(label("Open Logos, check its status, and pick up where you left off.", secondary=True))

        group = Card("Logos", "")
        group.title.hide()
        group.detail.hide()
        hero = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QIcon(str(APP_IMAGE_DIR / "logos-desktop-icon.png")).pixmap(48, 48))
        hero.addWidget(icon)
        product_text = QVBoxLayout()
        self.product = label("Logos", heading=True)
        self.state = value()
        product_text.addWidget(self.product)
        product_text.addWidget(self.state)
        hero.addLayout(product_text, 1)
        self.run = QPushButton("Open Logos")
        self.run.setMinimumSize(160, 42)
        hero.addWidget(self.run)
        group.content.addLayout(hero)
        self.health = label("")
        group.content.addWidget(self.health)
        layout.addWidget(group)

        self.updates = UpdateCards()
        layout.addWidget(self.updates)

        details_toggle = QToolButton()
        details_toggle.setText("Installation details")
        details_toggle.setCheckable(True)
        details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        details = QWidget()
        form = QFormLayout(details)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.runtime = value()
        self.path = value()
        form.addRow("Wine runtime", self.runtime)
        form.addRow("Location", self.path)
        details.hide()
        details_toggle.toggled.connect(details.setVisible)
        details_toggle.toggled.connect(lambda opened: details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
        layout.addWidget(details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(details)
        self.activity_title = label("Recent activity")
        layout.addWidget(self.activity_title)
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
        state = {"Stopped": "Ready to open", "Starting": "Opening Logos…", "Stopping": "Stopping Logos…"}.get(
            snapshot.state, snapshot.state or ("Running" if snapshot.running else "Ready to open"))
        if snapshot.indexing != "Stopped":
            state += f" · Indexer: {snapshot.indexing}"
        self.state.setText(state if snapshot.installed else "Not installed")
        self.runtime.setText(snapshot.runtime)
        self.path.setText(snapshot.path)
        self.run.setText("Stop Logos" if snapshot.running else "Open Logos")
        icon = QStyle.StandardPixmap.SP_MediaStop if snapshot.running else QStyle.StandardPixmap.SP_MediaPlay
        self.run.setIcon(self.style().standardIcon(icon))
        self.run.setEnabled(snapshot.installed and snapshot.state not in ("Starting", "Stopping"))
        self.health.setText(snapshot.health or ("Sample installation is ready. No personal library is connected."
                            if snapshot.installed else "Choose Install to preview a new setup with sample data."))


class InstallPage(QWidget):
    browse_requested = Signal()
    preview_requested = Signal()

    def __init__(self, snapshot):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(label("Set up an installation", heading=True))
        self.step_label = label("Step 1 of 4 · Product and location", secondary=True)
        layout.addWidget(self.step_label)
        self.description = label("Choose where to install and which Wine runtime to use. "
                                 "This preview only checks sample choices.")
        layout.addWidget(self.description)
        group = QGroupBox("Installation options")
        form = QFormLayout(group)
        form.setContentsMargins(20, 24, 20, 20)
        form.setSpacing(16)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.product = QComboBox()
        self.product.addItem("Logos")
        self.product.setCurrentText(snapshot.product)
        product_label = QLabel("&Product")
        product_label.setBuddy(self.product)
        form.addRow(product_label, self.product)

        self.recommended_location = str(Path(constants.DATA_HOME) / "installation")
        self.destination = QLineEdit(self.display_location(snapshot.path or self.recommended_location))
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
        self.location_hint = label("Recommended for your account. You can choose another folder. "
                                   "~ means your home folder.", secondary=True)
        form.addRow("", self.location_hint)

        self.runtime = QComboBox()
        self.runtime.addItems([snapshot.runtime, "System Wine (sample)"])
        runtime_label = QLabel("&Wine runtime")
        runtime_label.setBuddy(self.runtime)
        form.addRow(runtime_label, self.runtime)
        self.release = QLineEdit()
        self.release.setPlaceholderText("Leave blank to choose from available releases")
        self.release.setAccessibleName("Product release")
        form.addRow("Release", self.release)
        self.dependencies = QCheckBox("Install system &dependencies")
        self.dependencies.setChecked(True)
        form.addRow("", self.dependencies)
        layout.addWidget(group)
        self.consequences = label("Changes stay in this form until you continue. "
                                  "The preview does not download files or change your system.", secondary=True)
        layout.addWidget(self.consequences)
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
        return InstallDraft(self.product.currentText(), expanduser(self.destination.text()),
                             self.runtime.currentText(), self.dependencies.isChecked(), self.release.text().strip())

    @staticmethod
    def display_location(path):
        location = Path(path)
        if location.is_relative_to(Path.home()):
            return str(Path("~") / location.relative_to(Path.home()))
        return str(location)

    def load_destination(self, snapshot):
        """Hydrate saved paths without replacing a user's typed, cleared or browsed choice."""
        if self.destination.isModified():
            return
        selected = self.display_location(snapshot.path or self.recommended_location)
        if self.destination.text() != selected:
            self.destination.setText(selected)
        self.location_hint.setText(
            "Selected location. You can edit it before installing. ~ means your home folder." if snapshot.path
            else "Recommended for your account. You can choose another folder. ~ means your home folder."
        )


class SettingsPage(QWidget):
    def __init__(self, theme, snapshot):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.addWidget(label("Settings", heading=True))
        layout.addWidget(label("Appearance and installation preferences.", secondary=True))
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
        self.preview_details = details
        paths = QFormLayout(details)
        paths.setContentsMargins(20, 24, 20, 20)
        paths.setSpacing(16)
        paths.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        paths.addRow("Location", value(snapshot.path))
        paths.addRow("Wine runtime", value(snapshot.runtime))
        paths.addRow("Controller updates", label("Not available in sample mode"))
        layout.addWidget(details)
        layout.addStretch()


class MainWindow(QMainWindow):
    closed = Signal()

    def __init__(self, config, theme, services):
        super().__init__()
        self.setWindowTitle("LogosDesktop · Design preview")
        icon = QIcon(str(APP_IMAGE_DIR / "logos-desktop-icon.png"))
        QApplication.instance().setWindowIcon(icon)
        self.setWindowIcon(icon)
        self.resize(1100, 760)
        self.setMinimumSize(640, 480)
        self.theme = theme
        self.services = services
        self.production = getattr(services, "production", False)
        self.snapshot = services.snapshot()
        self._ticket = None
        self._retry = None
        self._closing = False
        self._final_close = False
        self._unsubscribe = None
        self._last_activity = ""
        self._refresh_pending = False
        self._refreshing = False
        self._queued_operation = None
        self._settings_profile = None
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
        version = constants.APP_RELEASE_VERSION
        identity = "Private development" if version is None else f"v{version}"
        header.addWidget(label(identity if self.production else "DESIGN PREVIEW · Sample data",
                               secondary=True))
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
        self.home.updates.setVisible(self.production)
        self.install = InstallPage(self.snapshot)
        self.settings = SettingsPage(theme, self.snapshot)
        page_widgets = [self.home, self.install, self.settings]
        if self.production:
            self.setWindowTitle("LogosDesktop")
            self.settings.preview_details.hide()
            self.install.step_label.setText("Latest stable release")
            self.install.description.setText(
                "Choose where to keep Logos. Review the installation before anything changes.")
            self.install.consequences.setText(
                "Earlier completed installation steps remain if you cancel. Terms acceptance requires your choice.")
            self.install.continue_button.setText("Review and install")
            self.install.product.clear()
            self.install.product.addItem("Logos")
            self.install.product.hide()
            options_form = self.install.runtime.parent().layout()
            options_form.labelForField(self.install.product).hide()
            self.install.release.hide()
            options_form.labelForField(self.install.release).hide()
            self.install.runtime.setEditable(True)
            self.install.runtime.clear()
            self.install.runtime.addItem(constants.WINE_RECOMMENDED_SIGIL)
            discover = QPushButton("Discover installed Wine runtimes")
            discover.clicked.connect(lambda: self.start(self.services.discover))
            self.install.layout().insertWidget(4, discover)
            self.install.discover = discover
            advanced = QToolButton()
            advanced.setText("Advanced setup")
            advanced.setCheckable(True)
            advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            advanced.setArrowType(Qt.ArrowType.RightArrow)
            options_form.addRow(advanced)
            for widget in (self.install.runtime, options_form.labelForField(self.install.runtime),
                           self.install.dependencies, discover):
                widget.hide()
                advanced.toggled.connect(widget.setVisible)
            advanced.toggled.connect(lambda opened: advanced.setArrowType(
                Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
            self.profile_settings = ProfileSettings()
            self.settings.layout().insertWidget(self.settings.layout().count() - 1, self.profile_settings)
            self.profile_settings.save_requested.connect(self._save_settings)
            self.profile_settings.profile_requested.connect(self._select_profile)
            self.profile_settings.runtime_requested.connect(self._select_runtime)
            self.profile_settings.rollback_requested.connect(self._rollback_desktop)
            self.maintenance = CarePage()
            self.diagnostics = HelpPage()
            for page in (self.home.updates, self.maintenance, self.diagnostics):
                page.requested.connect(self._action)
            self.navigation.clear()
            self.navigation.addItems(["Home", "Install Logos", "Backups & tools", "Help & logs", "Settings"])
            self.navigation.setCurrentRow(0)
            page_widgets = [self.home, self.install, self.maintenance, self.diagnostics, self.settings]
        self.install_page_index = page_widgets.index(self.install)
        for page in page_widgets:
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
        self.cancel.setVisible(not self.production)
        footer.addWidget(self.cancel)
        outer.addLayout(footer)

        self.settings.appearance.currentTextChanged.connect(theme.set_preference)
        theme.changed.connect(self._theme_changed)
        theme.warning.connect(self.settings.warning.setText)
        self.home.run.clicked.connect(self._home_action)
        self.install.browse_requested.connect(self._browse)
        self.install.preview_requested.connect(self._preview)
        self.cancel.clicked.connect(self._cancel)
        self.bridge.started.connect(self._started)
        self.bridge.progress.connect(self._progress)
        self.bridge.initialized.connect(self._initialized, Qt.ConnectionType.QueuedConnection)
        self.bridge.close_requested.connect(self._exit_requested, Qt.ConnectionType.QueuedConnection)
        self.bridge.observation.connect(self._observation, Qt.ConnectionType.QueuedConnection)
        for index in range(len(page_widgets)):
            shortcut = QShortcut(QKeySequence(f"Alt+{index + 1}"), self)
            shortcut.activated.connect(lambda index=index: self._navigate_visible_page(index))
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
            self.status.setText("Controller could not be opened")
            self.dialogs.failure("Cannot open controller", self.app.initialization_error)
            return
        self._unsubscribe = self.app.subscribe_config(self._configuration_changed)
        if self.production:
            self.app.logos.start_monitoring()
        self._configuration_changed()
        self.install.continue_button.setEnabled(self.app.active is None)
        if not self.production:
            self.status.setText("Ready · Preview actions use sample data")

    def _configuration_changed(self):
        if self.production and not self._closing:
            if self.app.active is None:
                self.start(self.services.refresh)
            else:
                self._refresh_pending = True
        self._render()

    def _render(self):
        if not self._closing:
            self.snapshot = self.services.snapshot()
            self.home.render(self.snapshot)
            if self.app.active is not None:
                self.home.run.setEnabled(False)
            if self.production:
                self.install.load_destination(self.snapshot)
                self.navigation.item(self.install_page_index).setHidden(self.snapshot.installed)
                if self.snapshot.installed and self.navigation.currentRow() == self.install_page_index:
                    self.navigation.setCurrentRow(0)
                if not self.snapshot.selected:
                    self.home.run.setText("Set up Logos")
                    self.home.run.setEnabled(self.app.active is None)
                show_activity = bool(self.home.activity.toPlainText())
                self.home.activity.setVisible(show_activity)
                self.home.activity_title.setVisible(show_activity)
                self.home.updates.summary.setText(
                    self.snapshot.product_update)
                self.diagnostics.summary.setText(self.snapshot.health)
                report = self.services.doctor_report
                self.diagnostics.doctor.setPlainText(report)
                self.diagnostics.doctor.setVisible(bool(report))
                self.diagnostics.details.setPlainText(
                    f"Configuration: {self.snapshot.profile}\nLocation: {self.snapshot.path}\n"
                    f"Wine: {self.snapshot.runtime}\nLogos: {self.snapshot.version}\n"
                    f"Process: {self.snapshot.state}\nIndexer: {self.snapshot.indexing}\n"
                    f"Logos/LogosDesktop updates: stable\n{self.snapshot.wine_status}\n"
                    f"Controller: {constants.APP_RELEASE_VERSION or 'unversioned'}")
                self.maintenance.backups.detail.setText(
                    "Save a verified copy of your Logos data.\n"
                    + (f"Folder: {Path(self.snapshot.backup).name}" if self.snapshot.backup
                       else "You will choose a backup folder when you create one."))
                self.maintenance.backups.detail.setToolTip(self.snapshot.backup)
                if self.snapshot.profile and self.snapshot.profile != self._settings_profile:
                    self.profile_settings.load(self.snapshot)
                    self._settings_profile = self.snapshot.profile
                self.profile_settings.runtime.setText(self.snapshot.runtime)
                self.profile_settings.wine_status.setText(self.snapshot.wine_status)
                self.maintenance.wine_status.setText(self.snapshot.wine_status)
                self.maintenance.index_status.setText(f"Indexer: {self.snapshot.indexing}")
                self.home.updates.controller.detail.setText(self.snapshot.controller_message)
                for page in (self.home.updates, self.maintenance, self.diagnostics):
                    for name, button in page.buttons.items():
                        allowed = name in ("refresh", "open_log", "support", "edit_config", "dependencies",
                                            "controller_check", "controller_update", "doctor", "open_logs")
                        allowed = allowed or (name == "repair" and self.snapshot.selected)
                        button.setEnabled(self.app.active is None and (allowed or self.snapshot.installed))
                        tooltip = self.snapshot.wine_status if name == "runtime_update" else ""
                        button.setToolTip("Wait for the current operation" if self.app.active is not None else tooltip)
                running_states = ("Starting", "Running", "Stopping")
                process_busy = self.snapshot.state in running_states or self.snapshot.indexing in running_states
                self.profile_settings.choose_profile.setEnabled(self.app.active is None and not process_busy)
                self.profile_settings.undo_desktop.setEnabled(self.app.active is None and not process_busy)
                self.profile_settings.choose_runtime.setEnabled(
                    self.app.active is None and self.snapshot.selected and not process_busy)
                idle_actions = ("backup", "restore", "product_update", "runtime_update",
                                "repair", "dependencies", "icu", "catalog", "indexes", "uninstall")
                for name in idle_actions:
                    button = self.home.updates.buttons.get(name) or self.maintenance.buttons.get(name)
                    if button is not None and process_busy:
                        button.setEnabled(False)
                        button.setToolTip("Close Logos and its indexer before this operation")
                if self.snapshot.indexing in running_states:
                    self.maintenance.buttons["index"].setEnabled(False)
                else:
                    self.maintenance.buttons["stop_index"].setEnabled(False)
                update = self.home.updates.buttons["product_update"]
                update.setVisible(self.snapshot.product_update.startswith("Update available"))
                managed = self.snapshot.controller_owner == "venv"
                controller_update = self.home.updates.buttons["controller_update"]
                controller_update.setVisible(managed or self.snapshot.controller_standalone)
                controller_update.setText("Update LogosDesktop…")
                controller_update.setEnabled(self.app.active is None and (
                    self.snapshot.controller_status == "available" if managed else self.snapshot.controller_standalone))
                self.home.updates.buttons["controller_check"].setEnabled(
                    self.app.active is None and (managed or self.snapshot.controller_standalone))
                self.home.updates.buttons["controller_check"].setText(
                    "Check controller updates" if self.snapshot.controller_status == "unchecked" else "Check again")

    def start(self, operation):
        if self.production:
            if self.app.active is not None and self._refreshing and operation != self.services.refresh:
                if self._queued_operation is None:
                    self._queued_operation = operation
                return
            if self.app.active is not None:
                return
            self._refreshing = operation == self.services.refresh
        if self.app.run_operation(operation):
            self._retry = operation

    def _home_action(self):
        if self.production and not self.snapshot.selected:
            self.navigation.setCurrentRow(self.install_page_index)
        else:
            self.start(self.services.toggle)

    def _navigate_visible_page(self, index):
        rows = [row for row in range(self.navigation.count()) if not self.navigation.item(row).isHidden()]
        if index < len(rows):
            self.navigation.setCurrentRow(rows[index])

    def _preview(self):
        draft = self.install.draft()
        if not draft.destination.strip() or not Path(draft.destination).is_absolute():
            self.install.validation.setText("Choose an absolute installation location before continuing.")
            self.install.destination.setFocus()
            return
        self.install.validation.clear()
        self.start(self.services.preview(draft))

    def _action(self, name):
        if name == "check" and self.app.active is None:
            self.home.updates.summary.setText("Checking stable Logos releases…")
        operation = (getattr(self.services, name) if name in ("refresh", "open_log")
                     else self.services.operation(name))
        self.start(operation)

    @Slot(object, str)
    def _observation(self, manager, message):
        if self._closing or manager is not self.app.logos:
            return
        if message:
            self.home.activity.appendPlainText(f"Process observation: {message}")
        self._configuration_changed()

    def _save_settings(self):
        form = self.profile_settings
        self.start(self.services.settings(form.backup.text().strip(), "stable", "stable",
                                          profile=form.profile.text()))

    def _select_profile(self):
        def chosen(path):
            if path and not self._closing and self.app.active is None:
                self.start(self.services.select_profile(path))
        self.dialogs.picker("Use an existing installation profile", PROMPT_OPTION_FILE, chosen)

    def _select_runtime(self):
        def chosen(path):
            if path and not self._closing and self.app.active is None:
                self.start(self.services.select_runtime(path))
        self.dialogs.picker("Select a Wine AppImage", PROMPT_OPTION_FILE, chosen)

    def _rollback_desktop(self):
        def chosen(path):
            if path and not self._closing and self.app.active is None:
                self.start(self.services.rollback_desktop(path))
        self.dialogs.picker("Select desktop journal", PROMPT_OPTION_FILE, chosen)

    def _browse(self):
        def chosen(path):
            if path is not None and not self._closing:
                self.install.destination.setText(self.install.display_location(path))
                self.install.destination.setModified(True)
                self.install.destination.setFocus()
        self.dialogs.picker("Choose installation location", PROMPT_OPTION_DIRECTORY, chosen,
                             expanduser(self.install.destination.text()))

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
        if self.production:
            self.install.setEnabled(False)
            self.profile_settings.setEnabled(False)
            self._render()
        if self._refreshing:
            self.cancel.setEnabled(False)
            if self.production:
                self.cancel.hide()
            return
        self.cancel.show()
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
        if self.production and progress.message == "Checking stable Logos releases…":
            self.home.updates.summary.setText(progress.message)
        if self.production and progress.message == "Checking the controller installation provider":
            self.home.updates.controller.detail.setText(self.services.snapshot().controller_message)
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
        if self.production:
            self.cancel.hide()
        self.progress.hide()
        self.install.continue_button.setEnabled(True)
        self.install.browse.setEnabled(True)
        if self.production:
            self.install.setEnabled(True)
            self.profile_settings.setEnabled(True)
            current_runtime = self.install.runtime.currentText()
            for item in self.services.snapshot().runtimes:
                if self.install.runtime.findText(item) < 0:
                    self.install.runtime.addItem(item)
            self.install.runtime.setCurrentText(current_runtime)
        self._render()
        result = completion.result
        if self.production and self.services.controller_handed_off:
            self.status.setText(result.message)
            self.close()
            return
        if not self._refreshing or result.outcome != "succeeded":
            self.status.setText(result.message)
            self.home.activity.appendPlainText(f"{result.outcome.title()}: {result.message}")
            if self.production:
                self.home.activity.show()
                self.home.activity_title.show()
        elif not self.home.activity.toPlainText():
            self.status.setText("Ready")
        if result.outcome in ("failed", "unknown"):
            operation = self._retry
            retry = (lambda: self.start(operation)) if operation is not None and not self.production else None
            self.dialogs.failure("Operation could not finish", result.message, result.details, retry)
        if self.production and self.services.prepared is not None:
            prepared, self.services.prepared = self.services.prepared, None
            try:
                from logos_desktop import updates
                if self.app._cancel.is_set():
                    self.status.setText("Controller restart cancelled. The installed controller was not changed")
                else:
                    prepared.restart(updates.resume_arguments(self.app.conf.config_file_path, "qt"))
            except Exception as error:
                self.dialogs.failure("Controller restart failed", str(error))
            finally:
                prepared.cancel()
        self._refreshing = False
        if self._queued_operation is not None:
            operation, self._queued_operation = self._queued_operation, None
            self.start(operation)
        elif self._refresh_pending:
            self._refresh_pending = False
            self._configuration_changed()

    @Slot()
    def _theme_changed(self):
        combo = self.settings.appearance
        combo.blockSignals(True)
        combo.setCurrentText(self.theme.preference)
        combo.blockSignals(False)
        mode = "Dark" if self.theme.dark else "Light"
        self.settings.effective.setText(f"Effective appearance: {mode}")
        palette = QApplication.instance().palette()
        accent = QPalette(palette)
        accent.setColor(QPalette.ColorRole.Button, palette.color(QPalette.ColorRole.Highlight))
        accent.setColor(QPalette.ColorRole.ButtonText, primary_text(palette.color(QPalette.ColorRole.Highlight)))
        accent.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button,
                        palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button))
        accent.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
                        palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText))
        self.home.run.setPalette(accent)
        background = palette.color(QPalette.ColorRole.Highlight)
        foreground = primary_text(background)
        hover = background.darker(110) if foreground.lightness() > 127 else background.lighter(110)
        disabled = QPalette.ColorGroup.Disabled
        style = (
            f"QPushButton {{ background: {background.name()}; color: {foreground.name()}; "
            f"border: 2px solid {background.name()}; border-radius: 4px; padding: 6px 14px; }}"
            f"QPushButton:hover {{ background: {hover.name()}; }}"
            f"QPushButton:focus {{ border-color: {palette.color(QPalette.ColorRole.WindowText).name()}; }}"
            f"QPushButton:disabled {{ background: {palette.color(disabled, QPalette.ColorRole.Button).name()}; "
            f"color: {palette.color(disabled, QPalette.ColorRole.ButtonText).name()}; "
            f"border-color: {palette.color(disabled, QPalette.ColorRole.Button).name()}; }}"
        )
        if self.home.run.styleSheet() != style:
            self.home.run.setStyleSheet(style)

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
        self._queued_operation = None
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
