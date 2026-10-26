"""Same-account controller leases and bounded desktop activation requests."""

from contextlib import contextmanager
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import socket
import stat
import struct
import threading
import time

import psutil

from . import constants, maintenance


MAX_REQUEST = 4096
MAX_FOCUS_REQUESTS = 16
IPC_TIMEOUT = 0.2


class InstanceBusy(maintenance.MaintenanceBusy):
    """The selected prefix already has a controller."""


def _directory(path):
    """Open each ancestor without following links, then retain the private root."""
    path = Path(path)
    if not path.is_absolute():
        raise maintenance.MaintenanceError("Coordination directory must be absolute")
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if (info.st_uid not in (0, os.getuid())
                    or (info.st_mode & 0o022 and not sticky_root)):
                raise maintenance.MaintenanceError("Unsafe coordination ancestor")
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise maintenance.MaintenanceError("Coordination directory must be private and owned")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _selection(conf):
    from .config import PersistentConfiguration

    overrides = getattr(conf, '_overrides', conf)
    raw = getattr(conf, '_raw', None)
    path = getattr(overrides, 'config_path', None) or getattr(conf, 'config_file_path', None)
    if raw is None:
        raw = PersistentConfiguration.load_from_path(path or constants.DEFAULT_CONFIG_PATH)
    install = getattr(overrides, 'install_dir', None) or raw.install_dir
    prefix = getattr(overrides, 'wine_prefix', None)
    if prefix is None and install:
        prefix = Path(install) / 'data/wine64_bottle'
    if prefix:
        prefix = Path(prefix).expanduser()
        if not prefix.is_absolute():
            raise maintenance.MaintenanceError("The selected Wine prefix must be absolute")
        return prefix.resolve(), None
    return None, Path(path or constants.DEFAULT_CONFIG_PATH).expanduser().resolve()


def select_previous_controller(path):
    """Remember an explicitly selected controller, never a bundled historical alias."""
    from . import adoption, config_store

    path = config_store.absolute_path(path)
    original = path
    files = {}
    scripts = []
    interpreter = None
    modules = []
    for _ in range(4):
        content = adoption._read(path)
        if content is None or not os.access(path, os.X_OK) or str(path) in files:
            raise InstanceBusy("Select an existing owned executable controller without a wrapper cycle")
        files[str(path)] = hashlib.sha256(content).hexdigest()
        scripts.append(str(path))
        if not content.startswith(b"#!"):
            break
        lines = content.decode().splitlines()
        if lines[0] in ("#!/bin/sh", "#!/bin/bash") and len(lines) == 2:
            arguments = shlex.split(lines[1])
            if len(arguments) != 3 or arguments[0] != "exec" or arguments[2] != "$@":
                raise InstanceBusy("Select the actual executable rather than an unsupported wrapper")
            path = Path(arguments[1])
            if not path.is_absolute():
                raise InstanceBusy("Selected wrapper must name an absolute executable")
            continue
        header = shlex.split(lines[0][2:])
        if (len(lines) >= 3 and lines[0] == "#!/bin/sh" and lines[1].startswith("'''exec' ")
                and lines[2] == "' '''"):
            arguments = shlex.split(lines[1][len("'''exec' "):])
            if len(arguments) != 3 or arguments[1:] != ["$0", "$@"]:
                raise InstanceBusy("Selected Python wrapper has unexpected arguments")
            header = [arguments[0]]
        if len(header) == 2 and header[0] == "/usr/bin/env":
            header = [shutil.which(header[1]) or ""]
        if len(header) != 1 or not Path(header[0]).is_absolute():
            raise InstanceBusy("Selected interpreter cannot be identified")
        interpreter = str(Path(header[0]).resolve(strict=True))
        try:
            tree = ast.parse(content.decode())
        except (SyntaxError, UnicodeError):
            raise InstanceBusy("Select the actual Python entry point rather than an unsupported script") from None
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and any(item.name == "main" for item in node.names):
                modules.append(node.module)
        break
    else:
        raise InstanceBusy("Selected wrapper chain exceeds the inspection limit")
    root = Path(constants.COORDINATION_DIR)
    descriptor = _directory(root)
    os.close(descriptor)
    target = root / "previous-controller.json"
    with config_store.Target(target) as selected:
        selected.stage(config_store.encode({"schema": 2, "path": str(original), "files": files,
                                           "scripts": scripts, "interpreter": interpreter, "modules": modules}))
        selected.publish()


def _previous_controller():
    from . import adoption

    path = Path(constants.COORDINATION_DIR) / "previous-controller.json"
    if not path.exists() and not path.is_symlink():
        return None
    record = json.loads(adoption._read(path))
    if (record.get("schema") != 2 or not isinstance(record.get("files"), dict)
            or not 1 <= len(record["files"]) <= 4
            or not isinstance(record.get("modules"), list)
            or not all(isinstance(value, str) for value in record["modules"])):
        raise InstanceBusy("Invalid previous controller selection")
    for name, digest in record["files"].items():
        selected = Path(name)
        content = adoption._read(selected)
        if not selected.is_absolute() or content is None or hashlib.sha256(content).hexdigest() != digest:
            raise InstanceBusy("Selected previous controller changed. Select its current executable again")
    return record


def check_legacy(prefix):
    """Refuse selected nonparticipating controllers without signaling any process."""
    from . import config_store
    from .controller_install import python_target

    selection = _previous_controller()
    selected_paths = {Path(path) for path in selection["files"]} if selection else set()
    selected_modules = selection["modules"] if selection else []
    legacy_names = {'logos-desktop', 'Logos_on_Linux.sh'}
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        command = []
        relevant = False
        try:
            if process.uids().real != os.getuid():
                continue
            created = process.create_time()
            command = process.cmdline()
            # Command identity is readable for many protected unrelated processes
            # whose executable or environment is not available to this account.
            executable = Path(command[0]).name if command else ''
            script = None
            module = None
            recognized_interpreter = executable.startswith(('python', 'bash', 'sh'))
            if (recognized_interpreter or selection) and len(command) > 1:
                script, module = python_target(command)
            selected_script = None
            if script:
                selected_script = Path(script)
                if not selected_script.is_absolute():
                    relevant = any(path.name == selected_script.name for path in selected_paths)
                    selected_script = Path(process.cwd()) / selected_script
                selected_script = selected_script.resolve()
            relevant = (executable in legacy_names or (script and Path(script).name in legacy_names)
                        or module in ('logos_desktop', 'logos_desktop.main')
                        or selected_script in selected_paths
                        or command and Path(command[0]) in selected_paths)
            if selection and selection["interpreter"] is None:
                if command:
                    invoked = Path(command[0])
                    relevant = relevant or any(path.name == invoked.name for path in selected_paths)
                    if not invoked.is_absolute() and '/' in command[0]:
                        invoked = Path(process.cwd()) / invoked
                    if invoked.is_absolute():
                        relevant = relevant or invoked.resolve() in selected_paths
                relevant = relevant or Path(process.exe()).resolve() in selected_paths
            if relevant and selection and not recognized_interpreter and selected_script in selected_paths:
                relevant = Path(process.exe()).resolve() == Path(selection["interpreter"] or selected_script)
            if not relevant and module in selected_modules:
                relevant = True
                relevant = Path(process.exe()).resolve() == Path(selection["interpreter"])
            if not relevant:
                continue

            root = Path(constants.COORDINATION_DIR)
            participating = any(Path(item.path).parent == root and Path(item.path).suffix == '.lock'
                                and len(Path(item.path).stem) == 64 for item in process.open_files())
            if participating:
                continue

            environment = process.environ()
            selected = environment.get('WINEPREFIX')
            if not selected and environment.get('INSTALLDIR'):
                selected = Path(environment['INSTALLDIR']) / 'data/wine64_bottle'
            config_path = environment.get('CONFIG_FILE')
            for index, argument in enumerate(command[1:], 1):
                if argument in ('-c', '--config') and index + 1 < len(command):
                    config_path = command[index + 1]
                elif argument.startswith('--config='):
                    config_path = argument.split('=', 1)[1]
            if config_path and not selected:
                config_path = Path(config_path)
                if not config_path.is_absolute():
                    config_path = Path(process.cwd()) / config_path
                try:
                    document, _ = config_store.read_document(config_path)
                    context = config_store.context(document, {})
                    selected = context.prefix if context is not None else None
                except (OSError, ValueError):
                    raise InstanceBusy("Close the legacy controller. Its profile cannot be inspected") from None
            if process.create_time() != created:
                raise InstanceBusy("Controller identity changed during admission. Try again")
            if not selected or not Path(selected).is_absolute():
                raise InstanceBusy("Close the selected previous controller before continuing. Its prefix is unknown")
            if prefix is None or Path(selected).resolve() == prefix:
                raise InstanceBusy("Close the selected previous controller for this prefix before continuing")
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            # Only an already identified legacy entry point is relevant here.
            if relevant or command and (Path(command[0]).name in legacy_names
                    or any(Path(arg).name in legacy_names for arg in command[1:])
                    or '-m' in command and any(arg in ('logos_desktop', 'logos_desktop.main') for arg in command)):
                raise InstanceBusy("Close the legacy controller. Its prefix cannot be inspected") from None


class Session:
    """Own one canonical prefix, or one not-yet-configured profile.

    acquire() is nonblocking and returns self. A competing owner raises
    InstanceBusy. close() is idempotent and never signals another process.
    """

    def __init__(self, prefix=None, *, profile=None):
        self.prefix = Path(prefix).resolve() if prefix is not None else None
        identity = f'prefix:{self.prefix}' if self.prefix is not None else f'profile:{Path(profile).resolve()}'
        self.key = hashlib.sha256(os.fsencode(identity)).hexdigest()
        self.root = Path(constants.COORDINATION_DIR)
        self.endpoint = self.root / f'{self.key[:32]}.sock'
        self._directory_fd = None
        self._descriptor = None
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._requests = queue.Queue(maxsize=MAX_FOCUS_REQUESTS)

    @classmethod
    def for_config(cls, conf):
        prefix, profile = _selection(conf)
        return cls(prefix, profile=profile)

    def acquire(self):
        if self._descriptor is not None:
            return self
        from . import runtime

        if getattr(runtime._local, 'writing', False):
            raise maintenance.MaintenanceError("Reserve controller ownership before taking the configuration lock")
        directory = _directory(self.root)
        descriptor = None
        try:
            descriptor = os.open(self.key + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=directory)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                raise maintenance.MaintenanceError("Unsafe controller lease")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstanceBusy("Another LogosDesktop controller owns this installation") from None
            check_legacy(self.prefix)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)
            raise
        self._descriptor = descriptor
        self._directory_fd = directory
        return self

    @property
    def listening(self):
        return self._socket is not None

    def listen(self, *, activation_source=None):
        """Queue GUI focus, optionally sharing the graphical owner's bounded queue."""
        if self._descriptor is None:
            raise RuntimeError("Controller ownership is required")
        if self._socket is not None:
            return
        if activation_source is not None:
            if not activation_source.listening:
                raise RuntimeError("The graphical activation source must be listening")
            self._requests = activation_source._requests
        try:
            info = os.stat(self.endpoint.name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise maintenance.MaintenanceError("Unsafe activation endpoint")
            os.unlink(self.endpoint.name, dir_fd=self._directory_fd)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            listener.bind(str(self.endpoint))
            bound = True
            os.chmod(self.endpoint, 0o600)
            listener.listen(8)
            listener.settimeout(IPC_TIMEOUT)
            self._socket = listener
            self._stop.clear()
            self._thread = threading.Thread(target=self._receive, name='Controller activation', daemon=True)
            self._thread.start()
        except BaseException:
            listener.close()
            self._socket = None
            self._thread = None
            if bound:
                try:
                    os.unlink(self.endpoint.name, dir_fd=self._directory_fd)
                except FileNotFoundError:
                    pass
            raise

    def _receive(self):
        while not self._stop.is_set():
            try:
                peer, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with peer:
                peer.settimeout(IPC_TIMEOUT)
                try:
                    credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    if struct.unpack('3i', credentials)[1] != os.getuid():
                        continue
                    data = bytearray()
                    deadline = time.monotonic() + IPC_TIMEOUT
                    while len(data) <= MAX_REQUEST:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        peer.settimeout(remaining)
                        chunk = peer.recv(MAX_REQUEST + 1 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if len(data) > MAX_REQUEST:
                        continue
                    request = json.loads(data)
                    if (not isinstance(request, dict) or set(request) != {'action', 'token'}
                            or request['action'] != 'focus' or not isinstance(request['token'], str)
                            or len(request['token']) > 2048
                            or any(ord(char) < 32 for char in request['token'])):
                        continue
                    self._requests.put_nowait(request['token'])
                    peer.sendall(b'ok')
                except (OSError, ValueError, UnicodeError, queue.Full):
                    continue

    def focus(self, token=''):
        """Request focus, with no command execution or token logging."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(IPC_TIMEOUT)
                peer.connect(str(self.endpoint))
                credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                if struct.unpack('3i', credentials)[1] != os.getuid():
                    return False
                content = json.dumps({'action': 'focus', 'token': token}).encode()
                if len(content) > MAX_REQUEST:
                    return False
                peer.sendall(content)
                peer.shutdown(socket.SHUT_WR)
                return peer.recv(16) == b'ok'
        except OSError:
            return False

    def drain_focus(self, callback):
        for _ in range(MAX_FOCUS_REQUESTS):
            try:
                token = self._requests.get_nowait()
            except queue.Empty:
                break
            callback(token)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2 * IPC_TIMEOUT + 0.1)
            if self._thread.is_alive():
                raise RuntimeError("Activation receiver has not settled")
            self._thread = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
            try:
                os.unlink(self.endpoint.name, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None
        # A transferred graphical owner retains the shared queue. A closed
        # Session must not reuse that queue if it is later acquired again.
        self._requests = queue.Queue(maxsize=MAX_FOCUS_REQUESTS)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.close()


@contextmanager
def ownership(conf):
    """Convenience admission for explicit early CLI commands."""
    with Session.for_config(conf) as session:
        yield session
