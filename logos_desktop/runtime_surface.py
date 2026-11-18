"""Present the entire private XWayland surface inside a purpose-labelled Wayland window."""

import os
import sys


SCENE = b"""
import QtQuick
import QtWayland.Compositor
import QtWayland.Compositor.XdgShell

WaylandCompositor {
    id: compositor
    socketName: "offline-display"
    XdgShell {
        onToplevelCreated: function(toplevel, surface) {
            client.shellSurface = surface
            client.forceActiveFocus()
        }
    }
    WaylandOutput {
        sizeFollowsWindow: true
        window: Window {
            id: frame
            title: purpose
            visible: true
            width: Math.min(1120, Screen.desktopAvailableWidth * 0.9)
            height: Math.min(740, Screen.desktopAvailableHeight * 0.9)
            color: palette.window
            onClosing: function(event) {
                event.accepted = false
                if (canRequestClose) {
                    closeControl.request()
                    banner.text = "Ending offline copy. Check for a save dialog. Close again to retry."
                } else {
                    banner.text = "Applying Wine. Keep this window open until the change settles."
                }
            }
            Text {
                id: banner
                height: Math.max(32, implicitHeight + 8)
                anchors.horizontalCenter: parent.horizontalCenter
                verticalAlignment: Text.AlignVCenter
                text: purpose
                color: frame.palette.windowText
            }
            Item {
                id: area
                anchors { top: banner.bottom; bottom: parent.bottom; left: parent.left; right: parent.right }
                ShellSurfaceItem {
                    id: client
                    anchors.centerIn: parent
                    focus: true
                    scale: width && height ? Math.min(area.width / width, area.height / height) : 1
                }
            }
        }
    }
}
"""


def main():
    # The private namespace exposes only system Qt, independent of the controller environment.
    from PyQt6.QtCore import QObject, QUrl, pyqtSlot
    from PyQt6.QtGui import QGuiApplication, QIcon
    from PyQt6.QtQml import QQmlApplicationEngine

    app = QGuiApplication(["logos-desktop-offline"])
    app.setDesktopFileName("logos-desktop-offline")
    app.setApplicationDisplayName("Logos offline copy")
    app.setWindowIcon(QIcon("/offline-icon.png"))
    close_fd = int(sys.argv[2])

    class CloseControl(QObject):
        @pyqtSlot()
        def request(self):
            try:
                os.write(close_fd, b"C")
            except BlockingIOError:
                pass

    control = CloseControl()
    engine = QQmlApplicationEngine()
    purpose = "Logos · " + {"check": "Checking offline display", "trial": "Offline trial copy",
                            "inspection": "Offline recovery inspection",
                            "apply": "Applying Wine to the original installation"}[sys.argv[1]]
    engine.rootContext().setContextProperty("purpose", purpose)
    engine.rootContext().setContextProperty("closeControl", control)
    engine.rootContext().setContextProperty("canRequestClose", sys.argv[1] != "apply")
    engine.loadData(SCENE, QUrl("qrc:/offline-window.qml"))
    if not engine.rootObjects():
        raise SystemExit("Install the Qt 6 Wayland compositor and Qt Quick modules for offline inspection")
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
