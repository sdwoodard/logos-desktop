"""Lazy graphical entry point for production and explicit repair workflows."""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from ou_dedetai import instance, processes, updates
from .services import Services
from .theme import Theme
from .window import MainWindow


def start(config, function=None, *, install_only=False):
    session = getattr(config, '_instance_session', None)
    if session is None:
        session = instance.Session.for_config(config)
    session.acquire()
    config._instance_session = session
    try:
        session.listen()
        return _start(config, function, install_only=install_only)
    finally:
        owner = getattr(session, '_application', None)
        if owner is not None:
            while not owner.close(timeout=.05):
                QApplication.instance().processEvents()
        session.close()


def _start(config, function=None, *, install_only=False):
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

    def activate(_token):
        if window._closing or window.app.stop_requested.is_set():
            return
        handle = window.windowHandle()
        # PySide's Wayland interface has no activation-token setter. Let Qt
        # request activation through the compositor rather than changing env.
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()
        if handle is not None:
            handle.requestActivate()

    def poll_activation():
        if window._closing or window.app.stop_requested.is_set():
            activation.stop()
            return
        if not window.app.ready.is_set() or window.app.initialization_error:
            return
        if not window.app._profile_lock.acquire(blocking=False):
            return
        try:
            current = window.app._instance_session
            if current is not None:
                current.listen()
                current.drain_focus(activate)
        finally:
            window.app._profile_lock.release()

    activation = QTimer(window)
    activation.setInterval(100)
    activation.timeout.connect(poll_activation)
    activation.start()

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
        activation.stop()
        window.app.request_close()
        window.dialogs.close()
        while not window.app.close(timeout=.05):
            application.processEvents()
        if services.prepared is not None:
            services.prepared.cancel()
        theme.dispose()
        processes.shutdown()
        window.deleteLater()
