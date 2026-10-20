"""Disposable profiles and actual Qt event dispatch for frontend regressions."""

from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from ou_dedetai import constants
from ou_dedetai.config import EphemeralConfiguration
from ou_dedetai.qt.bridge import Result
from ou_dedetai.qt.theme import Theme
from ou_dedetai.qt.window import Installation, MainWindow


class Services:
    def __init__(self, root):
        self.current = Installation("Logos", "sample", "Sample Wine", str(root / "Logos"))
        self.requests = []

    def snapshot(self):
        return self.current

    def toggle(self, app, cancel):
        self.current = replace(self.current, running=not self.current.running)
        return Result("succeeded", "Sample state changed")

    def preview(self, draft):
        def run(app, cancel):
            self.requests.append(draft)
            return Result("succeeded", "Preview finished")
        return run


class QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        cls.application = QApplication.instance() or QApplication(["frontend-tests"])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="logos-qt-test-")))
        self.application.styleHints().unsetColorScheme()
        self.application.setPalette(QPalette())
        self.application.processEvents()
        for name, path in (
            ("CACHE_DIR", self.root / "cache"), ("STATE_DIR", self.root / "state/app"),
            ("NETWORK_CACHE_PATH", self.root / "cache/network.json"),
        ):
            self.stack.enter_context(patch.object(constants, name, str(path)))
        self.config = self.root / "selected.json"
        self.config.write_text(json.dumps({"install_dir": str(self.root / "Logos"), "faithlife_product": "Logos"}))
        self.original = self.config.read_bytes()
        self.other = self.root / "unrelated.json"
        self.other.write_text('{"sentinel": "unchanged"}')
        self.theme = Theme(self.application, self.root / "gui/ui.ini")
        self.addCleanup(self.dispose_theme)

    def dispose_theme(self):
        self.theme.dispose()
        self.theme.deleteLater()
        self.application.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.styleHints().unsetColorScheme()
        self.application.setPalette(QPalette())

    def pump(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.assertTrue(predicate(), "Qt condition did not settle before the deadline")

    def window(self, services=None, configuration=None):
        self.services = services or Services(self.root)
        config = configuration or EphemeralConfiguration.project({}, str(self.config), environment={})
        window = MainWindow(config, self.theme, self.services)
        window.show()
        self.addCleanup(self.dispose_window, window)
        self.pump(lambda: window._unsubscribe is not None)
        return window

    def dispose_window(self, window):
        window.close()
        self.pump(lambda: window._final_close and not window.isVisible())
        self.assertFalse(window.app._threads)
        self.assertFalse(window.app._prompts)
        self.assertFalse(window.app.config_updated_hooks)
        window.deleteLater()
        self.application.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.other.read_text(), '{"sentinel": "unchanged"}')
