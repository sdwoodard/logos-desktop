"""Lazy graphical entry point for production and explicit repair workflows."""

import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from logos_desktop import controller_install, instance, processes, updates
from .activation import activate_window, show_initial_window, take_startup_token
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
    startup_token = take_startup_token()
    processes.resume()
    application = QApplication.instance() or QApplication([sys.argv[0]])
    application.setApplicationName("LogosDesktop")
    application.setOrganizationName("LogosDesktop")
    theme = Theme(application)
    services = Services()
    window = MainWindow(config, theme, services)
    if install_only:
        window.navigation.setCurrentRow(window.install_page_index)
    window.closed.connect(application.quit)
    try:
        show_initial_window(window, startup_token)
    finally:
        del startup_token

    def activate(token):
        activate_window(window, token)

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
            if os.environ.get(controller_install.TOKEN):
                application.exit(1)
            return
        if window._unsubscribe is None or window.app.active is not None:
            QTimer.singleShot(50, begin)
            return
        updates.confirm_startup()
        window.app.start_thread(controller_install.confirm_startup, daemon_bool=False)
        if function is not None:
            from logos_desktop.repair import repair_installation
            if function is not repair_installation:
                window.dialogs.failure("Unsupported startup action", "Choose the requested action in the control panel")
                return
            window.start(services.operation("repair"))

    QTimer.singleShot(0, begin)
    try:
        result = application.exec()
        if window.app.initialization_error:
            raise SystemExit(result or 1)
        return result
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
