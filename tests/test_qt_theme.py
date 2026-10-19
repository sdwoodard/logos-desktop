import unittest

try:
    import PySide6  # noqa: F401
except ModuleNotFoundError:
    QT_AVAILABLE = False
    QtCase = unittest.TestCase
else:
    QT_AVAILABLE = True
    from PySide6.QtGui import QPalette
    from ou_dedetai.qt.theme import explicit_palette
    from tests.qt_fixtures import QtCase


@unittest.skipUnless(QT_AVAILABLE, "Optional Qt frontend is not installed")
class TestQtTheme(QtCase):
    def test_portal_variants_and_live_system_fallback_do_not_override_explicit_choice(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from PySide6.QtCore import Qt
        from PySide6.QtDBus import QDBusVariant
        from ou_dedetai.qt.theme import DesktopAppearance

        desktop = SimpleNamespace(scheme=Qt.ColorScheme.Unknown, changed=Mock())
        self.theme.desktop = desktop
        try:
            DesktopAppearance._set(desktop, QDBusVariant(QDBusVariant(1)))
            self.theme._desktop_changed()
            self.pump(lambda: self.theme.dark)
            self.theme.set_preference("Light")
            self.pump(lambda: not self.theme.dark)
            self.theme._desktop_changed()
            self.application.processEvents()
            self.assertFalse(self.theme.dark)
            self.theme.set_preference("System")
            self.pump(lambda: self.theme.dark)
            DesktopAppearance._set(desktop, QDBusVariant(2))
            self.theme._desktop_changed()
            self.pump(lambda: not self.theme.dark)
            DesktopAppearance._set(desktop, QDBusVariant(["invalid"]))
            self.assertEqual(desktop.scheme, Qt.ColorScheme.Unknown)
        finally:
            self.theme.desktop = None

    def test_explicit_modes_persist_and_return_to_current_system_palette(self):
        for preference in ("Dark", "Light", "Dark"):
            self.theme.set_preference(preference)
            self.pump(lambda: self.theme.dark == (preference == "Dark"))
            self.assertEqual(self.theme._settings.value("appearance/theme"), preference)
            self.assertEqual(self.application.palette().color(QPalette.Window),
                             explicit_palette(preference == "Dark").color(QPalette.Window))
        self.theme.set_preference("System")
        self.pump(lambda: not self.theme.dark)
        self.assertFalse(self.theme.uses_application_palette)
        self.assertEqual(self.application.palette().resolveMask(), 0)
        self.application.setPalette(explicit_palette(True))
        self.pump(lambda: self.theme.dark)
        self.assertEqual(self.theme.preference, "System")

    def test_palette_followup_refresh_and_override_reapplication(self):
        self.theme.set_preference("System")
        changes = []
        self.theme.changed.connect(lambda: changes.append(self.theme.dark))
        self.application.setPalette(explicit_palette(True))
        self.pump(lambda: changes and changes[-1])
        self.application.setPalette(explicit_palette(False))
        self.pump(lambda: changes and not changes[-1])
        self.theme.set_preference("Dark")
        self.pump(lambda: self.theme.dark)
        self.application.setPalette(explicit_palette(False))
        self.pump(lambda: self.theme.dark)

    def test_preference_load_invalid_value_and_write_failure(self):
        from PySide6.QtCore import QSettings
        from ou_dedetai.qt.theme import Theme
        path = self.root / "reload.ini"
        settings = QSettings(str(path), QSettings.Format.IniFormat)
        settings.setValue("appearance/theme", "Dark")
        settings.sync()
        theme = Theme(self.application, path)
        try:
            self.assertEqual(theme.preference, "Dark")
        finally:
            theme.dispose()
            theme.deleteLater()
        settings.setValue("appearance/theme", "invalid")
        settings.sync()
        theme = Theme(self.application, path)
        try:
            self.assertEqual(theme.preference, "System")
        finally:
            theme.dispose()
            theme.deleteLater()
        blocked = self.root / "not-a-directory"
        blocked.write_text("sentinel")
        theme = Theme(self.application, blocked / "ui.ini")
        warnings = []
        theme.warning.connect(warnings.append)
        try:
            theme.set_preference("Dark")
            self.pump(lambda: theme.dark)
            self.assertTrue(warnings)
            self.assertIn("not saved", warnings[0])
            self.assertEqual(blocked.read_text(), "sentinel")
        finally:
            theme.dispose()
            theme.deleteLater()

    def test_disabled_text_and_selection_remain_distinct_in_both_palettes(self):
        for dark in (False, True):
            palette = explicit_palette(dark)
            for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
                for foreground, background in ((QPalette.Text, QPalette.Base),
                                                (QPalette.ButtonText, QPalette.Button),
                                                (QPalette.HighlightedText, QPalette.Highlight)):
                    self.assertNotEqual(palette.color(group, foreground), palette.color(group, background))
