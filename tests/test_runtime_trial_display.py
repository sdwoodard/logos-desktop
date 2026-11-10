"""Check actual X11 close recipients on a disposable server."""

import ctypes
import os
import select
import shutil
import subprocess
import unittest

from logos_desktop.runtime_trial_worker import Display, Event


@unittest.skipUnless(shutil.which("Xvfb"), "Xvfb is required for actual X11 recipient checks")
class DisplayTests(unittest.TestCase):
    def setUp(self):
        read_fd, write_fd = os.pipe()
        self.server = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "640x480x24", "-nolisten", "tcp",
             "-extension", "GLX"],
            pass_fds=(write_fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_server)
        self.addCleanup(self.server.stderr.close)
        os.close(write_fd)
        try:
            self.assertTrue(select.select([read_fd], [], [], 5)[0], "Disposable X server did not start")
            number = os.read(read_fd, 32).strip()
        finally:
            os.close(read_fd)
        try:
            self.display = Display(b":" + number)
        except RuntimeError:
            self.stop_server()
            self.fail("Disposable X display refused connection: " + self.server.stderr.read().decode())
        self.addCleanup(self.display.close)
        self.lib = self.display.library
        self.lib.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                                ctypes.c_ulong, ctypes.c_ulong]
        self.lib.XCreateSimpleWindow.restype = ctypes.c_ulong
        self.lib.XCreateWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        self.lib.XCreateWindow.restype = ctypes.c_ulong
        self.lib.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.XPending.argtypes = [ctypes.c_void_p]
        self.lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(Event)]

    def stop_server(self):
        if self.server.poll() is None:
            self.server.terminate()
            self.server.wait(timeout=5)

    def property(self, window, atom, kind, values, bits=32):
        data = (ctypes.c_ulong * len(values))(*values)
        self.lib.XChangeProperty(self.display.connection, window, atom, kind, bits, 0, data, len(values))

    def window(self, *, parent=None, mapped=True, pid=None, protocol=True):
        window = self.lib.XCreateSimpleWindow(self.display.connection, parent or self.display.root,
                                             0, 0, 100, 100, 0, 0, 0)
        if pid is not None:
            self.property(window, self.display.pid_atom, 6, [pid])
        if protocol:
            self.property(window, self.display.protocol, 4, [self.display.delete])
        if mapped:
            self.lib.XMapWindow(self.display.connection, window)
        self.lib.XSync(self.display.connection, 0)
        return window

    def test_only_mapped_application_toplevel_with_close_protocol_receives_message(self):
        main = self.window(pid=os.getpid())
        self.window(parent=main, pid=os.getpid())
        self.window(mapped=False, pid=os.getpid())
        self.window(pid=os.getpid() + 1)
        self.window(pid=os.getpid(), protocol=False)
        dialog = self.window(pid=os.getpid())
        self.property(dialog, self.display.transient_atom, 33, [main])
        self.assertTrue(self.display.viewable({os.getpid()}))
        self.assertEqual(self.display.close_windows({os.getpid()}), (main,))
        self.lib.XSync(self.display.connection, 0)
        recipients = []
        while self.lib.XPending(self.display.connection):
            event = Event()
            self.lib.XNextEvent(self.display.connection, ctypes.byref(event))
            if event.client.type == 33:
                recipients.append(event.client.window)
                self.assertEqual(event.client.data[0], self.display.delete)
        self.assertEqual(recipients, [main])

    def test_invalid_pid_properties_do_not_establish_readiness_or_receive_close(self):
        self.window()
        for kind, values, bits in ((6, [], 32), (6, [os.getpid(), os.getpid()], 32),
                                   (4, [os.getpid()], 32), (6, [os.getpid()], 8)):
            window = self.window()
            self.property(window, self.display.pid_atom, kind, values, bits)
        removed = self.window(pid=os.getpid())
        self.lib.XDestroyWindow(self.display.connection, removed)
        self.lib.XSync(self.display.connection, 0)
        self.assertFalse(self.display.viewable({os.getpid()}))
        self.assertEqual(self.display.close_windows({os.getpid()}), ())

    def test_override_redirect_and_input_only_windows_are_not_application_clients(self):
        class WindowAttributes(ctypes.Structure):
            _fields_ = [("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
                        ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                        ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
                        ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
                        ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
                        ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                        ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong),
                        ("cursor", ctypes.c_ulong)]

        attributes = WindowAttributes()
        attributes.override_redirect = 1
        popup = self.lib.XCreateWindow(self.display.connection, self.display.root, 0, 0, 100, 100,
                                       0, 0, 1, None, 1 << 9, ctypes.byref(attributes))
        input_only = self.lib.XCreateWindow(self.display.connection, self.display.root, 0, 0, 100, 100,
                                            0, 0, 2, None, 0, None)
        for window in (popup, input_only):
            self.property(window, self.display.pid_atom, 6, [os.getpid()])
            self.property(window, self.display.protocol, 4, [self.display.delete])
            self.lib.XMapWindow(self.display.connection, window)
        self.lib.XSync(self.display.connection, 0)
        self.assertFalse(self.display.viewable({os.getpid()}))
        self.assertEqual(self.display.close_windows({os.getpid()}), ())

    def test_invalid_protocol_properties_do_not_receive_close(self):
        for kind, values, bits in ((4, [], 32), (6, [self.display.delete], 32),
                                   (4, [self.display.delete], 8), (4, [self.display.delete] * 257, 32)):
            window = self.window(pid=os.getpid(), protocol=False)
            self.property(window, self.display.protocol, kind, values, bits)
        self.assertTrue(self.display.viewable({os.getpid()}))
        self.assertEqual(self.display.close_windows({os.getpid()}), ())

    def test_uncertain_transient_properties_do_not_receive_close(self):
        main = self.window(pid=os.getpid())
        for kind, values, bits in ((33, [], 32), (6, [main], 32),
                                   (33, [main], 8), (33, [main, main], 32)):
            window = self.window(pid=os.getpid())
            self.property(window, self.display.transient_atom, kind, values, bits)
        self.assertEqual(self.display.close_windows({os.getpid()}), (main,))
