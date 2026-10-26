"""Consume desktop launch tokens without publishing them to Python worker environments."""

from contextlib import contextmanager
import os

from PySide6.QtCore import Qt


TOKEN_VARIABLE = "XDG_ACTIVATION_TOKEN"


def take_startup_token():
    """Remove the inherited token before creating Qt or its operation workers."""
    token = os.environ.pop(TOKEN_VARIABLE, "")
    os.unsetenv(TOKEN_VARIABLE)
    return token


@contextmanager
def _token_environment(token):
    # Worker launches use explicit Python environment snapshots. Keep the new
    # token only in the C environment read by Qt, without dispatching events here.
    take_startup_token()
    try:
        if token:
            os.putenv(TOKEN_VARIABLE, token)
        yield
    finally:
        take_startup_token()


def show_initial_window(window, token):
    with _token_environment(token):
        window.show()


def activate_window(window, token):
    if window._closing or window.app.stop_requested.is_set():
        return

    attribute = Qt.WidgetAttribute.WA_ShowWithoutActivating
    previous = window.testAttribute(attribute)
    try:
        window.setAttribute(attribute, True)
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
    finally:
        window.setAttribute(attribute, previous)

    if window._closing or window.app.stop_requested.is_set():
        return

    handle = window.windowHandle()
    if handle is None:
        return

    with _token_environment(token):
        handle.requestActivate()
