"""Application appearance preferences and effective palette changes."""

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSettings, QTimer, Qt, Signal, Slot, SLOT
from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher, QDBusVariant
from PySide6.QtGui import QColor, QPalette


def preference_path():
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return base / "logos-desktop/ui.ini"


def explicit_palette(dark):
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#191e26" if dark else "#f4f6f8",
        QPalette.ColorRole.WindowText: "#e8edf4" if dark else "#202731",
        QPalette.ColorRole.Base: "#11161d" if dark else "#ffffff",
        QPalette.ColorRole.AlternateBase: "#222a35" if dark else "#eaf0f5",
        QPalette.ColorRole.Text: "#e8edf4" if dark else "#202731",
        QPalette.ColorRole.Button: "#293341" if dark else "#e7edf3",
        QPalette.ColorRole.ButtonText: "#e8edf4" if dark else "#202731",
        QPalette.ColorRole.ToolTipBase: "#293341" if dark else "#ffffff",
        QPalette.ColorRole.ToolTipText: "#e8edf4" if dark else "#202731",
        QPalette.ColorRole.Highlight: "#3777b5" if dark else "#2563a0",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#89bfff" if dark else "#225e9a",
        QPalette.ColorRole.LinkVisited: "#c5a5eb" if dark else "#704799",
        QPalette.ColorRole.PlaceholderText: "#a5b0bf" if dark else "#647181",
        QPalette.ColorRole.Light: "#46566a" if dark else "#ffffff",
        QPalette.ColorRole.Midlight: "#354355" if dark else "#edf1f6",
        QPalette.ColorRole.Mid: "#566479" if dark else "#a4afbb",
        QPalette.ColorRole.Dark: "#0d1117" if dark else "#677587",
        QPalette.ColorRole.Shadow: "#080b10" if dark else "#46515f",
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Accent: "#89bfff" if dark else "#225e9a",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#939faf" if dark else "#677586"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.Highlight, QColor("#414f61" if dark else "#d8e0e9"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.HighlightedText, QColor("#e8edf4" if dark else "#435063"))
    return palette


class DesktopAppearance(QObject):
    """Follow the desktop portal when an isolated platform theme lacks preferences."""

    changed = Signal()
    service = "org.freedesktop.portal.Desktop"
    path = "/org/freedesktop/portal/desktop"
    interface = "org.freedesktop.portal.Settings"

    def __init__(self, parent):
        super().__init__(parent)
        self.scheme = Qt.ColorScheme.Unknown
        self._bus = QDBusConnection.sessionBus()
        self._slot = SLOT("_setting_changed(QString,QString,QDBusVariant)")
        self._bus.connect(self.service, self.path, self.interface, "SettingChanged", self, self._slot)
        message = QDBusMessage.createMethodCall(self.service, self.path, self.interface, "Read")
        message.setArguments(["org.freedesktop.appearance", "color-scheme"])
        self._watcher = QDBusPendingCallWatcher(self._bus.asyncCall(message, 2000), self)
        self._watcher.finished.connect(self._read)

    @Slot(QDBusPendingCallWatcher)
    def _read(self, watcher):
        try:
            reply = watcher.reply()
            if reply.type() != QDBusMessage.MessageType.ErrorMessage and reply.arguments():
                self._set(reply.arguments()[0])
        finally:
            watcher.deleteLater()
            self._watcher = None

    @Slot(str, str, QDBusVariant)
    def _setting_changed(self, namespace, key, value):
        if namespace == "org.freedesktop.appearance" and key == "color-scheme":
            self._set(value)

    def _set(self, value):
        while isinstance(value, QDBusVariant):
            value = value.variant()
        schemes = {1: Qt.ColorScheme.Dark, 2: Qt.ColorScheme.Light}
        scheme = schemes.get(value, Qt.ColorScheme.Unknown) if type(value) is int else Qt.ColorScheme.Unknown
        if scheme != self.scheme:
            self.scheme = scheme
            self.changed.emit()

    def dispose(self):
        self._bus.disconnect(self.service, self.path, self.interface, "SettingChanged", self, self._slot)
        if self._watcher is not None:
            self._watcher.finished.disconnect(self._read)
            self._watcher.deleteLater()
            self._watcher = None


class Theme(QObject):
    changed = Signal()
    warning = Signal(str)
    choices = ("System", "Light", "Dark")

    def __init__(self, application, path=None):
        super().__init__(application)
        self.application = application
        self.path = Path(path) if path is not None else preference_path()
        if not self.path.is_absolute():
            raise ValueError("Appearance settings need an absolute path")
        self.preference = "System"
        self.uses_application_palette = False
        self._applying = False
        self._refresh_pending = False
        self.desktop = DesktopAppearance(self)
        if self.desktop is not None:
            self.desktop.changed.connect(self._desktop_changed)
        self._settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        self._settings.setFallbacksEnabled(False)
        value = self._settings.value("appearance/theme", "System")
        self.preference = value if value in self.choices else "System"
        self.application.installEventFilter(self)
        self.application.styleHints().colorSchemeChanged.connect(self._schedule_refresh)
        self.set_preference(self.preference, persist=False)

    @property
    def system_dark(self):
        """Report the desktop preference independently of our palette override."""
        scheme = self.desktop.scheme if self.desktop is not None else Qt.ColorScheme.Unknown
        if scheme == Qt.ColorScheme.Unknown:
            return None
        return scheme == Qt.ColorScheme.Dark

    @property
    def dark(self):
        palette = self.application.palette()
        return palette.color(QPalette.Window).lightness() < palette.color(QPalette.WindowText).lightness()

    def set_preference(self, preference, *, persist=True):
        if preference not in self.choices:
            raise ValueError("Unknown appearance preference")
        self.preference = preference
        self._applying = True
        try:
            self.application.setPalette(QPalette())
            self.uses_application_palette = False
            hints = self.application.styleHints()
            if preference == "System":
                hints.unsetColorScheme()
            else:
                hints.setColorScheme(Qt.ColorScheme.Dark if preference == "Dark" else Qt.ColorScheme.Light)
                # Keep explicit modes stable even when a platform applies its hint later.
                self.application.setPalette(explicit_palette(preference == "Dark"))
                self.uses_application_palette = True
        finally:
            self._applying = False
        self._schedule_refresh()
        if persist:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._settings.setValue("appearance/theme", preference)
                self._settings.sync()
                if self._settings.status() != QSettings.Status.NoError:
                    raise OSError("The appearance settings file could not be written")
            except OSError as error:
                self.warning.emit(f"Appearance changed for this session, but was not saved. {error}")

    def _schedule_refresh(self, *args):
        if not self._refresh_pending and not self._applying:
            self._refresh_pending = True
            QTimer.singleShot(0, self._refresh)

    def _desktop_changed(self):
        if self.preference == "System":
            self._applying = True
            try:
                self.application.setPalette(QPalette())
                self.uses_application_palette = False
            finally:
                self._applying = False
        self._schedule_refresh()

    def _refresh(self):
        self._refresh_pending = False
        if self.preference == "System":
            desired = self.desktop.scheme if self.desktop is not None else Qt.ColorScheme.Unknown
            if desired == Qt.ColorScheme.Unknown:
                desired = self.application.styleHints().colorScheme()
        else:
            desired = Qt.ColorScheme.Dark if self.preference == "Dark" else Qt.ColorScheme.Light
        if desired != Qt.ColorScheme.Unknown and self.dark != (desired == Qt.ColorScheme.Dark):
            self._applying = True
            try:
                self.application.setPalette(explicit_palette(desired == Qt.ColorScheme.Dark))
                self.uses_application_palette = True
            finally:
                self._applying = False
        self.changed.emit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.ApplicationPaletteChange, QEvent.Type.PaletteChange):
            self._schedule_refresh()
        return False

    def dispose(self):
        self.application.removeEventFilter(self)
        self.application.styleHints().colorSchemeChanged.disconnect(self._schedule_refresh)
        if self.desktop is not None:
            self.desktop.dispose()
