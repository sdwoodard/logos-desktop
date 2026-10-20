"""Lazy graphical entry point for production and explicit repair workflows."""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from ou_dedetai import processes, updates
from .services import Services
from .theme import Theme
from .window import MainWindow


def start(config, function=None, *, install_only=False):
    processes.resume()
    application = QApplication.instance() or QApplication([sys.argv[0]])
    application.setApplicationName("LogosDesktop")
    application.setOrganizationName("LogosDesktop")
    theme = Theme(application)
    services = Services()
    window = MainWindow(config, theme, services)
    if install_only:
        window.navigation.setCurrentRow(4)
    window.closed.connect(application.quit)
    window.show()

    def begin():
        if window._closing:
            return
        if not window.app.ready.is_set():
            QTimer.singleShot(50, begin)
            return
        if window.app.initialization_error:
            return
        if window._unsubscribe is None or window.app.active is not None:
            QTimer.singleShot(50, begin)
            return
        updates.confirm_startup()
        if function is not None:
            from ou_dedetai.repair import repair_installation
            if function is not repair_installation:
                window.dialogs.failure("Unsupported startup action", "Choose the requested action in the control panel")
                return
            window.start(services.operation("repair"))

    QTimer.singleShot(0, begin)
    try:
        return application.exec()
    finally:
        window.app.request_close()
        window.dialogs.close()
        while not window.app.close(timeout=.05):
            application.processEvents()
        if services.prepared is not None:
            services.prepared.cancel()
        theme.dispose()
        processes.shutdown()
        window.deleteLater()
