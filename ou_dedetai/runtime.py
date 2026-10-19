"""Pinned AppImage execution and recoverable activation of accepted runtimes."""

from base64 import b64decode, b64encode
from contextlib import contextmanager
import copy
from functools import wraps
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from . import artifacts, constants, maintenance, paths


_writers = threading.RLock()
_local = threading.local()


@contextmanager
def configuration_lock():
    """Serialize existing writes with activation snapshots and rollback."""
    with _writers:
        if getattr(_local, "writing", False):
            yield
            return
        root = maintenance.private_directory(
            Path(constants.STATE_DIR).expanduser().parent / "logos-desktop/artifact-locks",
        )
        descriptor = os.open(root / "configuration.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _local.writing = True
            yield
        finally:
            _local.writing = False
            os.close(descriptor)


def configuration_writer(function):
    @wraps(function)
    def write(*args, **kwargs):
        try:
            with configuration_lock():
                install = args[0].install_dir
                if install and os.path.lexists(selection_state(Path(install)) / "runtime.json"):
                    raise maintenance.RecoveryRequired("Recover the pending runtime selection before saving settings")
                return function(*args, **kwargs)
        except OSError as error:
            if kwargs.get("checked"):
                raise maintenance.MaintenanceError("Configuration write admission failed") from error
            raise
    return write


def selection_state(install):
    key = hashlib.sha256(os.fsencode(install.resolve())).hexdigest()
    base = Path(constants.STATE_DIR).expanduser().parent / "logos-desktop/runtime-selection"
    return maintenance.private_directory(base / key)


@contextmanager
def activation_guard(app, *, exclusive=False):
    context = maintenance.Context.from_app(app)
    active = getattr(_local, "activation", None)
    if active is not None:
        if active != context.prefix:
            raise maintenance.MaintenanceBusy("Conflicting runtime selection")
        yield context
        return
    state = selection_state(context.install)
    descriptor = os.open(state / "runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise maintenance.MaintenanceBusy("Runtime selection is changing") from error
        _local.activation = context.prefix
        if os.path.lexists(state / "runtime.json") and not exclusive:
            raise maintenance.RecoveryRequired("Runtime activation needs recovery before use")
        yield context
    finally:
        _local.activation = None
        os.close(descriptor)


def _replace_bytes(path, content, mode=0o600):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _snapshot(path):
    if path.is_symlink():
        return {"path": str(path), "link": os.readlink(path)}
    if not path.exists():
        return {"path": str(path), "absent": True}
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise artifacts.ArtifactError("Runtime activation target is not an owned regular file or link")
    return {"path": str(path), "content": b64encode(path.read_bytes()).decode(), "mode": stat.S_IMODE(info.st_mode)}


def _replace_link(path, target):
    with tempfile.TemporaryDirectory(prefix=".runtime-link-", dir=path.parent) as directory:
        temporary = Path(directory) / "link"
        temporary.symlink_to(target)
        os.replace(temporary, path)


def _restore(snapshots):
    for saved in reversed(snapshots):
        path = Path(saved["path"])
        _safe_target(path)
        if saved.get("absent"):
            path.unlink(missing_ok=True)
        elif "link" in saved:
            _replace_link(path, saved["link"])
        else:
            _replace_bytes(path, b64decode(saved["content"], validate=True), saved["mode"])


def _targets(app):
    conf = app.conf
    bindir = Path(conf.installer_binary_dir)
    configs = [Path(conf.install_dir) / f"{constants.BINARY_NAME}.json", Path(conf._overrides.config_path)]
    link = conf.wine_appimage_link_file_name
    if Path(link).name != link or link in artifacts.COMPANIONS or link in (".", "..", ""):
        raise artifacts.ArtifactError("Invalid selected runtime link name")
    output = [bindir / link, *(bindir / name for name in artifacts.COMPANIONS)]
    if set(output).intersection(configs):
        raise artifacts.ArtifactError("Runtime links and configuration targets overlap")
    return output, list(dict.fromkeys(configs))


def _safe_target(path):
    if not path.is_absolute() or ".." in path.parts or any(parent.is_symlink() for parent in path.parents):
        raise artifacts.ArtifactError("Ambiguous runtime activation target")
    parent = path.parent
    if parent.exists():
        info = parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise artifacts.ArtifactError("Runtime activation needs an owned nonshared parent")


@maintenance.uses_installation
def recover(app):
    """Restore a previous activation before admitting another runtime consumer."""
    with activation_guard(app, exclusive=True) as context, configuration_lock():
        _recover_locked(app, context)


def _recover_locked(app, context):
    journal = selection_state(context.install) / "runtime.json"
    if not os.path.lexists(journal):
        return
    if journal.is_symlink() or not journal.is_file():
        raise maintenance.RecoveryRequired("Invalid runtime recovery record")
    try:
        record = json.loads(journal.read_bytes())
        links, configs = _targets(app)
        allowed = {str(path) for path in (*links, *configs)}
        snapshots = record["previous"]
        if record["install"] != str(context.install) or len(snapshots) != len(allowed) or {
            saved["path"] for saved in snapshots
        } != allowed:
            raise ValueError("Recovery context mismatch")
        _restore(snapshots)
        from .config import PersistentConfiguration

        app.conf._raw = PersistentConfiguration.load_from_path(app.conf._overrides.config_path)
        app.conf._overrides.wine_appimage_path = record.get("override")
        for name in ("_wine64_path", "_wine64_identity", "_wine_binary_files", "_wine_appimage_files"):
            setattr(app.conf, name, None)
        journal.unlink()
    except (OSError, ValueError, KeyError, TypeError, artifacts.ArtifactError) as error:
        raise maintenance.RecoveryRequired(f"Runtime recovery failed. Preserved record: {journal}") from error


def recover_startup(overrides, persistent):
    """Recover selection files without constructing an App or fetching metadata."""
    from types import SimpleNamespace

    install = getattr(overrides, "install_dir", None) or persistent.install_dir
    if not install:
        return persistent
    install = Path(install)
    if not os.path.lexists(selection_state(install) / "runtime.json"):
        return persistent
    conf = SimpleNamespace(
        _raw=persistent, _overrides=overrides, install_dir=str(install),
        installer_binary_dir=getattr(overrides, "installer_binary_dir", None) or str(install / "data/bin"),
        wine_appimage_link_file_name=(
            getattr(overrides, "wine_appimage_link_file_name", None) or "selected_wine.AppImage"
        ),
    )
    recover(SimpleNamespace(conf=conf))
    return conf._raw


@maintenance.changes_installation
def activate(app, accepted):
    """Select a prepared immutable runtime without replacing old runtime bytes."""
    if accepted.spec.kind != "appimage":
        raise artifacts.ArtifactError("Runtime activation requires an accepted AppImage")
    accepted.check()
    conf = app.conf
    from . import wine

    store = Path(conf.install_dir) / "data/bin/.artifacts"
    if not accepted.path.is_relative_to(store):
        accepted = artifacts.import_local(accepted.path, store.parent, accepted.spec)
    wine.require_compatible(app, accepted.path)
    with activation_guard(app, exclusive=True) as context, configuration_lock():
        _recover_locked(app, context)
        links, configs = _targets(app)
        for path in (*links, *configs):
            _safe_target(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _safe_target(path)
        snapshots = [_snapshot(path) for path in (*links, *configs)]
        journal = selection_state(context.install) / "runtime.json"
        override = conf._overrides.wine_appimage_path
        record = {"install": str(context.install), "previous": snapshots,
                  "override": str(override) if override is not None else None}
        old_binary, old_code = conf._raw.wine_binary, conf._raw.wine_binary_code
        fields = ("_wine64_path", "_wine64_identity", "_wine_binary_files", "_wine_appimage_files")
        old_cache = {name: getattr(conf, name, None) for name in fields}
        old_override = conf._overrides.wine_appimage_path
        from .config import LegacyConfiguration

        proposed_raw = copy.deepcopy(conf._raw)
        proposed_raw.wine_binary = paths.relative_to_install(accepted.path, conf.install_dir)
        proposed_raw.wine_binary_code = "AppImage"
        legacy = proposed_raw._legacy or LegacyConfiguration()
        legacy.WINE_EXE = proposed_raw.wine_binary
        legacy.WINEBIN_CODE = "AppImage"
        legacy.SELECTED_APPIMAGE_FILENAME = None
        legacy.WINEPREFIX = str(context.prefix)
        legacy.APPDIR_BINDIR = str(conf.installer_binary_dir)
        proposed_raw._legacy = legacy
        proposed = proposed_raw._as_dict()
        content = (json.dumps(proposed, indent=4, sort_keys=True) + "\n").encode()
        _replace_bytes(journal, json.dumps(record).encode())
        try:
            selected = links[0]
            _replace_link(selected, str(accepted.path))
            for alias in links[1:]:
                _replace_link(alias, selected.name)
            for config in configs:
                _replace_bytes(config, content)
            journal.unlink()
        except BaseException as error:
            try:
                _restore(snapshots)
                journal.unlink(missing_ok=True)
            except (OSError, ValueError) as rollback_error:
                raise maintenance.RecoveryRequired(
                    f"Runtime activation failed. Recovery record retained at {journal}"
                ) from rollback_error
            raise artifacts.ArtifactError("Runtime activation failed. Previous selection restored") from error
        finally:
            conf._raw.wine_binary, conf._raw.wine_binary_code = old_binary, old_code
            conf._overrides.wine_appimage_path = old_override
            for name, value in old_cache.items():
                setattr(conf, name, value)
        conf._raw = proposed_raw
        conf._overrides.wine_appimage_path = None
        for name in fields:
            setattr(conf, name, None)
        app._config_updated_event.set()
    return accepted


def executable(binary, app=None, *, destination=None):
    """Resolve mutable aliases once, preserving companion argv0 within a generation."""
    if app is not None:
        with activation_guard(app):
            return executable(binary, destination=Path(app.conf.install_dir) / "data/bin")
    requested = Path(binary)
    if not requested.is_absolute():
        raise ValueError("Runtime executable must be absolute")
    if not requested.is_file():
        raise ValueError("Runtime executable is missing or is not a regular file")
    concrete = requested.resolve(strict=True)
    if concrete.name.lower().endswith(".appimage"):
        artifacts.validate_format(concrete, "appimage")
        if concrete.parent.parent.parent.name == ".artifacts":
            accepted = artifacts.installed(concrete, "appimage")
            if requested.name in artifacts.COMPANIONS:
                companion = concrete.parent / requested.name
                if not companion.is_symlink() or companion.resolve() != concrete:
                    raise artifacts.ArtifactError("Runtime companion does not match the accepted generation")
            if destination is not None and not concrete.is_relative_to(destination / ".artifacts"):
                accepted = artifacts.import_local(concrete, destination, accepted.spec)
                concrete = accepted.path
            if requested.name in artifacts.COMPANIONS:
                return str(concrete.parent / requested.name)
            return str(concrete)
        if destination is None:
            destination = paths.ensure_cache_directory(Path(constants.CACHE_DIR), constants.CACHE_BASE) / "runtimes"
        accepted = artifacts.import_local(
            concrete, destination, artifacts.ArtifactSpec("local:" + str(concrete), concrete.name, "appimage"),
        )
        if requested.name in artifacts.COMPANIONS:
            return str(accepted.path.parent / requested.name)
        return str(accepted.path)
    paths.executable(str(requested))
    if concrete.parent.parent.parent.name == ".artifacts":
        artifacts.installed(concrete, "winetricks" if concrete.name == "winetricks" else "elf")
    return str(concrete)
