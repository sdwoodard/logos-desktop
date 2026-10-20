"""Admission and resource ownership for one managed Wine prefix."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import stat
import threading

import psutil

from ou_dedetai import constants


class MaintenanceError(RuntimeError):
    """An operation cannot safely use the selected installation."""


class MaintenanceBusy(MaintenanceError):
    """Another operation or process still owns the installation."""


class RecoveryRequired(MaintenanceError):
    """Preserved restore data needs recovery before another writer starts."""


def private_directory(path: Path) -> Path:
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise MaintenanceError(f"State directory must be private and owned: {path}")
    for created in reversed(missing):
        for directory in (created, created.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return path


@dataclass(frozen=True)
class Context:
    prefix: Path
    install: Path | None = None
    product: str | None = None

    @classmethod
    def from_app(cls, app, *, prompt=True):
        raw = app.conf._raw
        overrides = app.conf._overrides
        install = getattr(overrides, "install_dir", None) or raw.install_dir
        if (not isinstance(install, (str, Path)) or not str(install)) and prompt:
            install = app.conf.install_dir
        if not isinstance(install, (str, Path)) or not Path(install).is_absolute():
            raise MaintenanceError("Select an absolute installation directory first")
        prefix = getattr(overrides, "wine_prefix", None) or Path(install) / "data/wine64_bottle"
        if not isinstance(prefix, (str, Path)) or not Path(prefix).is_absolute():
            raise MaintenanceError("The selected Wine prefix must be absolute")
        return cls(Path(prefix).resolve(), Path(install).resolve(), raw.faithlife_product)

    @property
    def state(self) -> Path:
        return private_directory(self.state_path)

    @property
    def state_path(self) -> Path:
        base = Path(constants.STATE_DIR).expanduser().parent / "logos-desktop/maintenance"
        key = hashlib.sha256(os.fsencode(self.prefix)).hexdigest()
        return base / key

    @property
    def pending(self) -> Path:
        return self.state / "restore.pending"

    def needs_recovery(self):
        return os.path.lexists(self.state_path / "restore.pending") or os.path.lexists(self.state_path / "restore.json")


_local = threading.local()
_admission = threading.RLock()
_reservations: dict[Path, int] = {}
_configuration_users: dict[int, int] = {}


@contextmanager
def artifact_use(*, exclusive=False):
    """Protect immutable stores from removal, including shared cross-prefix cache."""
    root = private_directory(Path(constants.STATE_DIR).expanduser().parent / "logos-desktop/artifact-locks")
    descriptor = os.open(root / "usage.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise MaintenanceError("Unsafe artifact usage lock")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceBusy("Artifacts are in use by another operation") from error
        yield
    finally:
        os.close(descriptor)


@contextmanager
def configured(app):
    with _admission:
        context = Context.from_app(app)
        key = id(app.conf)
        _configuration_users[key] = _configuration_users.get(key, 0) + 1
    try:
        yield context
    finally:
        with _admission:
            _configuration_users[key] -= 1
            if not _configuration_users[key]:
                del _configuration_users[key]


@contextmanager
def change_selection(configuration, install=None, *, document=None):
    """Exclude selection changes until all users of this Config have finished."""
    selecting = getattr(_local, "selecting", set())
    if id(configuration) in selecting:
        yield
        return
    leases = []
    with _admission:
        if _configuration_users.get(id(configuration)):
            raise MaintenanceBusy("Wait for the current operation before changing installation settings")
        old = getattr(configuration._overrides, "install_dir", None) or configuration._raw.install_dir
        selected = install or old
        prefixes = set()
        if hasattr(configuration._overrides, "environment"):
            from . import config_store

            previous = configuration._raw._as_dict()
            proposed = dict(previous if document is None else document)
            if install is not None:
                proposed["install_dir"] = install
            for values in (previous, proposed):
                context = config_store.context(values, configuration._overrides.environment)
                if context is not None:
                    prefixes.add(context.prefix)
        else:
            for value in (old, selected):
                if value:
                    prefix = getattr(configuration._overrides, "wine_prefix", None)
                    prefix = prefix or Path(value) / "data/wine64_bottle"
                    prefixes.add(Path(prefix).resolve())
        try:
            for prefix in sorted(prefixes):
                leases.append(Lease(Context(prefix), exclusive=True))
            _local.selecting = selecting | {id(configuration)}
            yield
        finally:
            _local.selecting = selecting
            for lease in reversed(leases):
                lease.close()


def current_context() -> Context | None:
    active = getattr(_local, "active", None)
    return active[0] if active else None


def _operation_directory(context):
    from .instance import _directory

    key = hashlib.sha256(os.fsencode(context.prefix.resolve())).hexdigest()
    return _directory(Path(constants.COORDINATION_DIR) / 'operations' / key)


def _operation_lock(directory, name, *, exclusive):
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise MaintenanceError('Unsafe stable operation lock')
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceBusy('Another controller is using this installation') from error
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _pending_records(directory, names):
    for name in names:
        try:
            (directory / name).lstat()
        except FileNotFoundError:
            continue
        return True
    return False


def _known_recovery_locations(directory, context):
    """Remember state locations without moving or rewriting recovery records."""
    from . import runtime

    current = {'schema_version': 1, 'state_dir': str(Path(constants.STATE_DIR).expanduser().absolute()),
               'install': str(context.install.resolve()) if context.install is not None else None,
               'prefix': str(context.prefix)}
    content = json.dumps(current, sort_keys=True).encode()
    name = 'state-' + hashlib.sha256(content).hexdigest() + '.json'
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        temporary = '.state-' + secrets.token_hex(12)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    locations = []
    with os.scandir(directory) as entries:
        for number, entry in enumerate(entries):
            if number >= 128:
                raise RecoveryRequired('Too many coordination entries. Inspect recovery location records')
            if not entry.name.startswith('state-') or not entry.name.endswith('.json'):
                continue
            if len(locations) >= 64:
                raise RecoveryRequired('Too many known recovery locations. Inspect coordination records')
            descriptor = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                    raise RecoveryRequired('Unsafe recovery location record')
                record = source.read(16385)
            try:
                if (len(record) > 16384
                        or entry.name != 'state-' + hashlib.sha256(record).hexdigest() + '.json'):
                    raise ValueError
                location = json.loads(record)
                if (not isinstance(location, dict)
                        or set(location) != {'schema_version', 'state_dir', 'install', 'prefix'}
                        or location['schema_version'] != 1 or not isinstance(location['state_dir'], str)
                        or not Path(location['state_dir']).is_absolute()
                        or not isinstance(location['prefix'], str) or not Path(location['prefix']).is_absolute()
                        or Path(location['prefix']).resolve() != context.prefix.resolve()
                        or (location['install'] is not None and
                            (not isinstance(location['install'], str) or not Path(location['install']).is_absolute()))):
                    raise ValueError
            except (ValueError, UnicodeError) as error:
                raise RecoveryRequired('Invalid recovery location record') from error
            locations.append(location)

    # Existing default-root journals remain detectable before any new lease has
    # registered them. Arbitrary historical XDG roots cannot be enumerated.
    defaults = {Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/logos-desktop',
                Path.home() / '.local/state/logos-desktop', Path(constants.RECOVERY_STATE_DIR)}
    locations.extend({'state_dir': str(root.expanduser().absolute()), 'install': current['install'], 'prefix': prefix}
                     for root in defaults for prefix in {str(context.prefix), str(context.prefix.resolve())})
    current_restore = context.state_path.resolve()
    current_runtime = runtime.selection_state(context.install, create=False).resolve() if context.install else None
    for location in locations:
        base = Path(location['state_dir']).parent / 'logos-desktop'
        prefix_key = hashlib.sha256(os.fsencode(location['prefix'])).hexdigest()
        restore = base / 'maintenance' / prefix_key
        if restore.resolve() != current_restore and _pending_records(restore, ('restore.pending', 'restore.json')):
            raise RecoveryRequired(f'Restore recovery is required in another state location: {restore}')
        install = location['install']
        if install is not None:
            key = hashlib.sha256(os.fsencode(Path(install).resolve())).hexdigest()
            selection = base / 'runtime-selection' / key
            if selection.resolve() != current_runtime and _pending_records(selection, ('runtime.json',)):
                raise RecoveryRequired(f'Runtime recovery is required in another state location: {selection}')


class Lease:
    def __init__(self, context: Context, *, exclusive=False, recovery=False, role=None):
        self.context = context
        self.exclusive = exclusive
        self.descriptor = None
        self.artifacts = None
        self.role_descriptor = None
        self.stable_descriptor = None
        self.stable_role_descriptor = None
        with _admission:
            owner = _reservations.get(context.prefix)
            if owner is not None and owner != threading.get_ident():
                raise MaintenanceBusy("Maintenance is preparing this installation")
            directory = _operation_directory(context)
            try:
                # Stable prefix admission comes before state-local compatibility
                # locks. Every flock here is nonblocking, including role locks.
                self.stable_descriptor = _operation_lock(directory, 'usage.lock', exclusive=exclusive)
                self._acquire_local(context, exclusive=exclusive, recovery=recovery, role=role, directory=directory)
            except BaseException:
                self.close()
                raise
            finally:
                os.close(directory)

    def _acquire_local(self, context, *, exclusive, recovery, role, directory):
        lock = context.state / "usage.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise MaintenanceError(f"Unsafe operation lock: {lock}")
            try:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise MaintenanceBusy("Another controller is using this installation") from error
            _known_recovery_locations(directory, context)
            if context.needs_recovery() and not recovery:
                raise RecoveryRequired(f"Restore recovery is required. Preserved state: {context.state}")
            if context.install is not None and not recovery:
                from . import runtime

                if runtime.pending(context.install):
                    raise RecoveryRequired("Runtime selection needs recovery. Use --repair before installation use")
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        if not exclusive:
            self.artifacts = artifact_use()
            try:
                self.artifacts.__enter__()
            except BaseException:
                os.close(descriptor)
                self.descriptor = None
                self.artifacts = None
                raise

        if role is not None:
            if role not in ("logos", "index"):
                raise ValueError("Unknown managed program role")
            self.stable_role_descriptor = _operation_lock(directory, f'{role}.lock', exclusive=True)
            self.role_descriptor = os.open(
                context.state / f"{role}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
            )
            info = os.fstat(self.role_descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise MaintenanceError("Unsafe program launch lock")
            try:
                fcntl.flock(self.role_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise MaintenanceBusy("Another controller owns this program run") from error

    def close(self):
        if self.role_descriptor is not None:
            os.close(self.role_descriptor)
            self.role_descriptor = None
        if self.stable_role_descriptor is not None:
            os.close(self.stable_role_descriptor)
            self.stable_role_descriptor = None
        if self.artifacts is not None:
            self.artifacts.__exit__(None, None, None)
            self.artifacts = None
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        if self.stable_descriptor is not None:
            os.close(self.stable_descriptor)
            self.stable_descriptor = None

    @contextmanager
    def entered(self):
        previous = getattr(_local, "active", None)
        _local.active = (self.context, self.exclusive)
        try:
            yield self.context
        finally:
            _local.active = previous
            self.close()


@contextmanager
def use(context: Context, *, exclusive=False, recovery=False):
    active = getattr(_local, "active", None)
    if active:
        same_context = (
            active[0].prefix == context.prefix
            and (context.install is None or active[0].install == context.install)
            and (context.product is None or active[0].product == context.product)
        )
        installing = getattr(_local, "installing", None) == active[0]
        borrowed = installing and active[1] and not exclusive
        if not same_context or (exclusive != active[1] and not borrowed):
            raise MaintenanceBusy("Conflicting nested installation operation")
        yield active[0]
        return
    lease = Lease(context, exclusive=exclusive, recovery=recovery)
    with lease.entered():
        yield context


def uses_installation(function):
    """Keep preparation and final writes inside the same normal-use lease."""
    @wraps(function)
    def admitted(app, *args, **kwargs):
        with configured(app) as context, use(context):
            return function(app, *args, **kwargs)
    return admitted


def uses_owner(function):
    @wraps(function)
    def admitted(owner, *args, **kwargs):
        with configured(owner.app) as context, use(context):
            return function(owner, *args, **kwargs)
    return admitted


def uses_prefix(function):
    @wraps(function)
    def admitted(source, prefix, *args, **kwargs):
        with use(Context(Path(prefix).resolve())):
            return function(source, prefix, *args, **kwargs)
    return admitted


def spawn(app, task, *args, **kwargs):
    """Transfer an already acquired lease to a new application worker."""
    context = current_context() or Context.from_app(app)
    lease = Lease(context)

    def owned_task():
        with lease.entered():
            return task(*args, **kwargs)

    try:
        return app.start_thread(owned_task)
    except BaseException:
        lease.close()
        raise


def hold_process(app, process, on_exit):
    """Hand a live process lease to its waiter before launch admission ends."""
    context = current_context() or Context.from_app(app)
    lease = Lease(context)

    def wait():
        try:
            returncode = process.wait()
        finally:
            lease.close()
        on_exit(returncode)

    try:
        return app.start_thread(wait)
    except BaseException:
        lease.close()
        raise


def assert_quiescent(context: Context):
    """Inspect metadata only, without Wine queries or cached process handles."""
    for process in psutil.process_iter(["pid", "name", "cmdline", "uids"]):
        if process.pid == os.getpid():
            continue
        info = process.info
        name = (info.get("name") or "").lower()
        command = info.get("cmdline") or []
        candidate = (
            "wine" in name or "logos" in name or "verbum" in name or name.endswith(".exe")
            or any(str(context.prefix) in argument for argument in command)
        )
        try:
            uids = info.get("uids")
            if uids is not None and uids.real != os.geteuid():
                continue
            prefix = process.environ().get("WINEPREFIX")
            matches_prefix = bool(prefix and Path(prefix).is_absolute() and Path(prefix).resolve() == context.prefix)
            if prefix and Path(prefix).is_absolute() and not matches_prefix:
                continue
            if not candidate and not matches_prefix:
                continue
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                continue
            raise MaintenanceBusy("Close Logos, its indexer and other programs in this Wine prefix, then retry")
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied as error:
            if candidate:
                raise MaintenanceBusy("Cannot verify a potentially relevant Wine process") from error


@contextmanager
def maintain(app, context: Context | None = None, *, recovery=False):
    if context is None:
        with configured(app) as selected, maintain(app, selected, recovery=recovery):
            yield selected
        return
    with _admission:
        if context.prefix in _reservations:
            raise MaintenanceBusy("Maintenance is already preparing this installation")
        _reservations[context.prefix] = threading.get_ident()
    try:
        manager = getattr(app, "logos", None)
        assert_quiescent(context)
        if manager is not None:
            states = (getattr(manager, "logos_state", None), getattr(manager, "indexing_state", None))
            if any(getattr(state, "name", None) in ("STARTING", "STOPPING") for state in states):
                raise MaintenanceBusy("Wait for Logos and indexing transitions to finish, then retry")
            manager.stop_watchers()
        with use(context, exclusive=True, recovery=recovery):
            assert_quiescent(context)
            yield context
    finally:
        with _admission:
            _reservations.pop(context.prefix, None)


def maintains_installation(function):
    @wraps(function)
    def admitted(app, *args, **kwargs):
        with maintain(app):
            return function(app, *args, **kwargs)
    return admitted


@contextmanager
def installing(app):
    """Own an idle prefix exclusively while permitting this owner's Wine commands."""
    if getattr(_local, "installing", None) is None:
        reconcile(app)
    with configured(app) as context:
        active = getattr(_local, "installing", None)
        if active is not None:
            if active != context:
                raise MaintenanceBusy("Installation selection changed during the operation")
            yield context
            return

        # Lock order is prefix admission, artifact use, runtime selection, config,
        # then controller destination. Ordinary restore still rejects Wine calls.
        with maintain(app, context), artifact_use():
            _local.installing = context
            try:
                yield context
            finally:
                _local.installing = None


def changes_installation(function):
    @wraps(function)
    def admitted(app, *args, **kwargs):
        with installing(app):
            return function(app, *args, **kwargs)
    return admitted


def installation_owned():
    return getattr(_local, "installing", None) is not None


def reconcile(app):
    """Reconcile journals only for an explicitly requested mutating operation."""
    from . import backup, runtime

    context = Context.from_app(app)
    backup.recover_pending_restore(context, app)
    if runtime.pending(context.install):
        runtime.recover(app)
    return Context.from_app(app)
