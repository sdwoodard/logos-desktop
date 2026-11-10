"""Own the private display and reap every child inside one offline namespace."""

import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


OPERATION_ID = None
SHUTDOWN_TIMEOUT = 30


def file_hash(stream):
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def write(value):
    value["operation"] = OPERATION_ID
    temporary = Path("/trial/worker-status.new")
    with temporary.open("w") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace("/trial/worker-status.json")


def terminal(value):
    write(value)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if take_request() == {"action": "settle"}:
            return
        time.sleep(0.1)
    raise RuntimeError("Supervisor did not acknowledge settled worker identity")


def children():
    processes = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal() and int(path.name) != os.getpid():
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] != "Z":
                    processes[int(path.name)] = int(fields[1])
            except FileNotFoundError:
                pass
    owned = {os.getpid()}
    while True:
        expanded = owned | {pid for pid, parent in processes.items() if parent in owned}
        if expanded == owned:
            return sorted(owned - {os.getpid()})
        owned = expanded


class CloseEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class Event(ctypes.Union):
    _fields_ = [("client", CloseEvent), ("padding", ctypes.c_long * 24)]


class Attributes(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int) for name in ("x", "y", "width", "height", "border_width", "depth")]
    _fields_ += [("visual", ctypes.c_void_p), ("root", ctypes.c_ulong)]
    _fields_ += [(name, ctypes.c_int) for name in ("window_class", "bit_gravity", "win_gravity", "backing_store")]
    _fields_ += [("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
                ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long), ("your_event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
                ("screen", ctypes.c_void_p)]


class Display:
    def __init__(self, name=b":77"):
        self.library = ctypes.CDLL("libX11.so.6")
        library = self.library
        library.XOpenDisplay.argtypes = [ctypes.c_char_p]
        library.XOpenDisplay.restype = ctypes.c_void_p
        self.connection = library.XOpenDisplay(name)
        if not self.connection:
            raise RuntimeError("Private X display is unusable")
        library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        library.XDefaultRootWindow.restype = ctypes.c_ulong
        library.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        library.XInternAtom.restype = ctypes.c_ulong
        library.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                      ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                                      ctypes.POINTER(ctypes.c_uint)]
        library.XFree.argtypes = [ctypes.c_void_p]
        library.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                                      ctypes.POINTER(Event)]
        library.XFlush.argtypes = [ctypes.c_void_p]
        library.XCloseDisplay.argtypes = [ctypes.c_void_p]
        library.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Attributes)]
        library.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                              ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                                              ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                                              ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                              ctypes.POINTER(ctypes.c_void_p)]
        handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self.error_handler = handler(lambda display, event: 0)
        library.XSetErrorHandler.argtypes = [handler]
        library.XSetErrorHandler(self.error_handler)
        self.root = library.XDefaultRootWindow(self.connection)
        self.protocol = library.XInternAtom(self.connection, b"WM_PROTOCOLS", 0)
        self.delete = library.XInternAtom(self.connection, b"WM_DELETE_WINDOW", 0)
        self.pid_atom = library.XInternAtom(self.connection, b"_NET_WM_PID", 0)
        self.transient_atom = library.XInternAtom(self.connection, b"WM_TRANSIENT_FOR", 0)

    def property_values(self, window, atom, expected_type, maximum):
        actual, count, remaining = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        bits, data = ctypes.c_int(), ctypes.c_void_p()
        status = self.library.XGetWindowProperty(
            self.connection, window, atom, 0, maximum, 0, expected_type,
            ctypes.byref(actual), ctypes.byref(bits), ctypes.byref(count), ctypes.byref(remaining),
            ctypes.byref(data),
        )
        try:
            if not status and actual.value == 0:
                return ()
            if (status or actual.value != expected_type or bits.value != 32 or remaining.value
                    or not data.value or not 0 < count.value <= maximum):
                return None
            values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
            return tuple(int(values[index]) for index in range(count.value))
        finally:
            if data.value:
                self.library.XFree(data)

    def client_windows(self, application_pids):
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        count = ctypes.c_uint()
        children = ctypes.POINTER(ctypes.c_ulong)()
        if not self.library.XQueryTree(self.connection, self.root, ctypes.byref(root), ctypes.byref(parent),
                                      ctypes.byref(children), ctypes.byref(count)):
            return ()
        try:
            if count.value > 8192:
                raise RuntimeError("Private window tree exceeds its bound")
            clients = []
            for index in range(count.value):
                window = int(children[index])
                attributes = Attributes()
                if not self.library.XGetWindowAttributes(self.connection, window, ctypes.byref(attributes)):
                    continue
                if attributes.map_state != 2 or attributes.window_class != 1 or attributes.override_redirect:
                    continue
                pids = self.property_values(window, self.pid_atom, 6, 1)
                if pids and len(pids) == 1 and pids[0] in application_pids:
                    clients.append(window)
            return tuple(clients)
        finally:
            if children:
                self.library.XFree(children)

    def viewable(self, application_pids):
        return bool(self.client_windows(application_pids))

    def close_windows(self, application_pids):
        targets = []
        for window in self.client_windows(application_pids):
            # A shutdown dialog needs user interaction rather than another automatic close.
            if self.property_values(window, self.transient_atom, 33, 1) != ():
                continue
            if self.delete not in (self.property_values(window, self.protocol, 4, 256) or ()):
                continue
            event = Event()
            event.client.type = 33
            event.client.display = self.connection
            event.client.window = window
            event.client.message_type = self.protocol
            event.client.format = 32
            event.client.data[0] = self.delete
            self.library.XSendEvent(self.connection, window, 0, 0, ctypes.byref(event))
            targets.append(window)
        self.library.XFlush(self.connection)
        return tuple(targets)

    def close(self):
        self.library.XCloseDisplay(self.connection)


def identity():
    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    prefix = Path(os.environ["WINEPREFIX"]).stat()
    return {"pid": os.getpid(), "start_ticks": int(fields[19]),
            "namespaces": {name: os.readlink("/proc/self/ns/" + name) for name in ("mnt", "net", "pid", "ipc")},
            "prefix_device": prefix.st_dev, "prefix_inode": prefix.st_ino}


def reap(excluded=()):
    pending = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    for value in pending:
        pid = int(value)
        if pid in excluded:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def registry_value(path, name, kind):
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(["/runner/wine", "reg.exe", "query", path, "/v", name],
                                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                close_fds=True, timeout=60, check=False)
        output.seek(0)
        text = output.read(16385).decode("utf-8", errors="strict")
    if result.returncode == 1:
        return None
    if result.returncode or len(text) > 16384:
        raise RuntimeError("Copied recipe observation failed")
    pattern = r"\s{4}" + re.escape(name) + r"\s{4}" + kind + r"(?:\s{4}(.*))?"
    values = [match.group(1) or "" for line in text.splitlines() if (match := re.fullmatch(pattern, line))]
    if len(values) != 1:
        raise RuntimeError("Copied recipe observation has unexpected type")
    return int(values[0], 0) if kind == "REG_DWORD" else values[0]


class PreparationCancelled(RuntimeError):
    pass


def take_request():
    claimed = Path("/trial/worker-request.json")
    try:
        Path("/trial/request.json").rename(claimed)
    except FileNotFoundError:
        return None
    value = json.loads(claimed.read_text())
    claimed.unlink()
    return value


def checkpoint():
    command = take_request()
    if command in ({"action": "cancel"}, {"action": "stop"}):
        raise PreparationCancelled()
    return command


def application_pids(pids, name):
    result = set()
    for pid in pids:
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip().lower() == name:
                result.add(pid)
        except FileNotFoundError:
            pass
    return result


def prepare(job):
    prefix = Path(os.environ["WINEPREFIX"])
    for item in job["dependencies"]:
        checkpoint()
        asset = Path("/inputs") / item["sha256"]
        with asset.open("rb") as stream:
            if file_hash(stream) != item["sha256"]:
                raise RuntimeError("Pinned trial dependency changed")
        if item["role"] != "icu":
            continue
        expected = dict(item["files"])
        with tempfile.TemporaryDirectory(dir="/trial") as temporary:
            staged = Path(temporary)
            observed = set()
            with tarfile.open(asset, "r:gz") as archive:
                for member in archive:
                    checkpoint()
                    canonical_name = str(PurePosixPath(member.name))
                    base = next((base for base in ("windows/", "icu-win/windows/")
                                 if canonical_name.startswith(base)), None)
                    if base is None or not member.isfile():
                        continue
                    name = canonical_name[len(base):]
                    if name not in expected or name in observed:
                        raise RuntimeError("Unexpected pinned ICU member")
                    observed.add(name)
                    source = archive.extractfile(member)
                    destination = staged / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    with destination.open("rb") as stream:
                        if file_hash(stream) != expected[name]:
                            raise RuntimeError("Pinned ICU file changed")
            if observed != set(expected):
                raise RuntimeError("Pinned ICU files are missing")
            for name in expected:
                destination = prefix / "drive_c/windows" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.parent.resolve().is_relative_to(prefix.resolve()):
                    raise RuntimeError("Copied ICU target escapes prefix")
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output, (staged / name).open("rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
    for path, name, kind, expected in job["registry"]:
        checkpoint()
        if registry_value(path, name, kind) == expected:
            continue
        arguments = (["delete", path, "/v", name, "/f"] if expected is None else
                     ["add", path, "/v", name, "/t", kind, "/d", str(expected), "/f"])
        result = subprocess.run(["/runner/wine", "reg.exe", *arguments], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, timeout=60, check=False)
        if result.returncode or registry_value(path, name, kind) != expected:
            raise RuntimeError("Copied recipe write failed verification")


def main():
    global OPERATION_ID
    os.umask(0o077)
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0):
        raise RuntimeError("Cannot own application descendants")
    with open("/trial/display.log", "wb") as log:
        server = subprocess.Popen(
            ["Xwayland", ":77", "-geometry", "1280x800", "-decorate", "-shm",
             "-noreset", "-nolisten", "tcp", "-extension", "MIT-SHM"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
        )
        deadline = time.monotonic() + 15
        while not Path("/tmp/.X11-unix/X77").is_socket():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Private display unavailable")
            time.sleep(0.1)
        os.environ["DISPLAY"] = ":77"
        display = Display()
        if "--check" in sys.argv:
            display.close()
            server.terminate()
            server.wait(timeout=5)
            return
        job = json.loads(Path("/trial/job.json").read_text())
        OPERATION_ID = job["id"]
        worker = identity()
        write({"schema": 1, "phase": "preparing", "identity": worker, "reason": "", "returncode": None})
        try:
            prepare(job)
            # Addon bootstrap suppression applies to preparation, not native .NET launch.
            os.environ["WINEDLLOVERRIDES"] = "winemenubuilder.exe=d"
            checkpoint()
            write({"schema": 1, "phase": "prepared", "identity": worker, "reason": "", "returncode": None})
            while True:
                if checkpoint() == {"action": "launch"}:
                    break
                time.sleep(0.1)
        except PreparationCancelled:
            display.close()
            server.terminate()
            server.wait(timeout=5)
            terminal({"schema": 1, "phase": "ended", "identity": worker,
                      "reason": "cancelled-before-launch", "returncode": 0})
            return
        arguments = ["/runner/wine", job["executable"]]
        if job.get("resource_uri") is not None:
            arguments.append(job["resource_uri"])
        launcher = subprocess.Popen(arguments, close_fds=True,
                                    stdin=subprocess.DEVNULL)
        ready = False
        failure_reason = ""
        application_name = job["executable"].split("\\")[-1].lower()
        shutdown_deadline = None
        closing = False
        stop_requested = False
        close_sent = False
        session_helper = None

        def end_session():
            # End only this namespace's Windows session, with no force or kill option.
            return subprocess.Popen(["/runner/wine", "wineboot.exe", "-e", "-s"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True)

        while True:
            launcher.poll()
            server.poll()
            if session_helper is not None:
                session_helper.poll()
            excluded = (server.pid, launcher.pid) + (() if session_helper is None else (session_helper.pid,))
            reap(excluded)
            alive = [pid for pid in children() if pid != server.pid]
            applications = application_pids(alive, application_name)
            if server.poll() is not None:
                failure_reason = failure_reason or "display-lost"
            helper_failed = session_helper is not None and session_helper.poll() not in (None, 0)
            if helper_failed:
                failure_reason = failure_reason or "normal-session-shutdown-failed"
            if not alive:
                launcher.poll()
                if server.poll() is None:
                    display.close()
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=5)
                reap()
                terminal({"schema": 1, "phase": "ended", "identity": worker,
                          "reason": failure_reason or
                          ("processes-settled" if ready else "application-exited-before-readiness"),
                          "returncode": launcher.returncode})
                return
            # A lost display does not release the source while Wine remains alive.
            if server.returncode is not None:
                write({"schema": 1, "phase": "unknown", "identity": worker,
                       "reason": "display-lost", "returncode": None})
            command = take_request()
            if command is not None:
                if command in ({"action": "stop"}, {"action": "cancel"}) and server.returncode is None:
                    stop_requested = True
                    closing = True
                    if shutdown_deadline is None:
                        shutdown_deadline = time.monotonic() + SHUTDOWN_TIMEOUT
                        close_sent = False
                        if session_helper is not None and session_helper.poll() is not None:
                            if session_helper.returncode:
                                failure_reason = failure_reason or "normal-session-shutdown-failed"
                            session_helper = None
            if server.returncode is None and not ready and not stop_requested:
                if display.viewable(applications):
                    ready = True
                    write({"schema": 1, "phase": "running", "identity": worker, "reason": "", "returncode": None})
            if (ready or stop_requested) and not applications and session_helper is None:
                closing = True
                if shutdown_deadline is None:
                    shutdown_deadline = time.monotonic() + SHUTDOWN_TIMEOUT
                session_helper = end_session()
            if helper_failed:
                write({"schema": 1, "phase": "unknown", "identity": worker,
                       "reason": failure_reason, "returncode": None})
            if closing and server.returncode is None and not close_sent:
                close_sent = bool(display.close_windows(applications))
            if shutdown_deadline and time.monotonic() >= shutdown_deadline:
                failure_reason = failure_reason or "shutdown-timeout"
                write({"schema": 1, "phase": "unknown", "identity": worker,
                       "reason": "shutdown-timeout", "returncode": None})
                shutdown_deadline = None
                closing = False
            time.sleep(0.2)


if __name__ == "__main__":
    main()
