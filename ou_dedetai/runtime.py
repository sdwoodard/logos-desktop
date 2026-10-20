"""Pinned AppImage execution and recoverable activation of accepted runtimes."""

from base64 import b64decode, b64encode
from contextlib import contextmanager
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
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise maintenance.MaintenanceError("Unsafe configuration lock")
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
                return function(*args, **kwargs)
        except OSError as error:
            if kwargs.get("checked"):
                raise maintenance.MaintenanceError("Configuration write admission failed") from error
            raise
    return write


def selection_state(install, *, create=True):
    key = hashlib.sha256(os.fsencode(install.resolve())).hexdigest()
    base = Path(constants.STATE_DIR).expanduser().parent / "logos-desktop/runtime-selection"
    path = base / key
    return maintenance.private_directory(path) if create else path


def pending(install):
    return os.path.lexists(selection_state(Path(install), create=False) / "runtime.json")


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
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
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


def _restore(snapshots, proposed):
    expected = {saved["path"]: saved for saved in proposed}
    for saved in reversed(snapshots):
        path = Path(saved["path"])
        _safe_target(path)
        if _snapshot(path) not in (saved, expected[saved["path"]]):
            raise maintenance.RecoveryRequired(f"Runtime recovery target changed. Preserved without overwrite: {path}")
        if saved.get("absent"):
            path.unlink(missing_ok=True)
        elif "link" in saved:
            _replace_link(path, saved["link"])
        else:
            _replace_bytes(path, b64decode(saved["content"], validate=True), saved["mode"])


def _targets(app, *, legacy_portable=False):
    conf = app.conf
    bindir = Path(conf.installer_binary_dir)
    destination = getattr(conf._overrides, "config_destination", None) or conf._overrides.config_path
    portable = f'{constants.BINARY_NAME}.json' if legacy_portable else constants.PORTABLE_CONFIG_NAME
    configs = [Path(conf.install_dir) / portable, Path(destination)]
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


def recover(app):
    """Restore a previous activation before admitting another runtime consumer."""
    with maintenance.configured(app) as selected, maintenance.use(selected, recovery=True), activation_guard(
        app, exclusive=True,
    ) as context, configuration_lock():
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
        recorded_paths = {saved.get('path') for saved in record['previous']}
        if recorded_paths != {str(path) for path in (*links, *configs)}:
            links, configs = _targets(app, legacy_portable=True)
        allowed = {str(path) for path in (*links, *configs)}
        snapshots = record["previous"]
        if record["install"] != str(context.install) or len(snapshots) != len(allowed) or {
            saved["path"] for saved in snapshots
        } != allowed:
            raise ValueError("Recovery context mismatch")
        source = Path(record.get("source", app.conf._overrides.config_path))
        destination = Path(getattr(app.conf._overrides, "config_destination", None) or app.conf._overrides.config_path)
        if source != destination:
            allowed_sources = {Path(app.conf._overrides.config_path)}
            if destination == Path(constants.DEFAULT_CONFIG_PATH):
                allowed_sources.update(Path(path) for path in constants.LEGACY_CONFIG_FILES)
            unchanged = hashlib.sha256(source.read_bytes()).hexdigest() == record["source_hash"]
            if source not in allowed_sources or not unchanged:
                raise ValueError("Migration source changed during runtime recovery")
        proposed = record["next"]
        if {saved["path"] for saved in proposed} != allowed or len(proposed) != len(allowed):
            raise ValueError("Recovery publication mismatch")
        _restore(snapshots, proposed)
        from .config import PersistentConfiguration

        app.conf._raw = PersistentConfiguration.load_from_path(
            str(source), environment=getattr(app.conf._overrides, "environment", True),
        )
        app.conf._overrides.config_path = str(source)
        if callable(getattr(app.conf, "rebind", None)):
            app.conf.rebind(app.conf._raw)
        app.conf._overrides.wine_appimage_path = record.get("override")
        for name in ("_wine64_path", "_wine64_identity", "_wine_binary_files", "_wine_appimage_files"):
            setattr(app.conf, name, None)
        if callable(getattr(app.conf, "invalidate_context", None)):
            app.conf.invalidate_context()
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
    if not pending(install):
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
        from .config import PersistentConfiguration, environment_document
        from . import config_store

        source = conf._overrides.config_path
        destination = getattr(conf._overrides, "config_destination", None) or source
        changes = {
            "install_dir": str(context.install), "faithlife_product": context.product,
            "faithlife_product_version": conf.faithlife_product_version,
            "wine_binary": paths.relative_to_install(accepted.path, conf.install_dir), "wine_binary_code": "AppImage",
            "SELECTED_APPIMAGE_FILENAME": None, "WINEPREFIX": str(context.prefix),
            "APPDIR_BINDIR": str(conf.installer_binary_dir),
        }
        with config_store.prepare(
            destination, changes, source_path=source, source_bytes=getattr(conf._raw, "_file_bytes", None),
            expected=getattr(conf._raw, "_saved", None),
            validator=getattr(conf, "write_validator", None),
        ) as prepared:
            auxiliary = {target.path for target, _, _ in prepared.backups}
            if auxiliary.intersection(links):
                raise artifacts.ArtifactError("Runtime links conflict with configuration recovery copies")
            prepared.preserve_previous()
            snapshots = [_snapshot(path) for path in (*links, *configs)]
            journal = selection_state(context.install) / "runtime.json"
            override = conf._overrides.wine_appimage_path
            record = {
                "install": str(context.install), "previous": snapshots,
                "override": str(override) if override is not None else None, "source": source,
                "next": [
                    {"path": str(links[0]), "link": str(accepted.path)},
                    *({"path": str(alias), "link": links[0].name} for alias in links[1:]),
                    *({"path": str(path), "content": b64encode(prepared.content).decode(), "mode": 0o600}
                      for path in configs),
                ],
            }
            if source != destination:
                record["source_hash"] = hashlib.sha256(Path(source).read_bytes()).hexdigest()
            _replace_bytes(journal, json.dumps(record).encode())
            try:
                prepared.validate()
                selected = links[0]
                _replace_link(selected, str(accepted.path))
                for alias in links[1:]:
                    _replace_link(alias, selected.name)
                for config in configs:
                    if prepared.source is not None:
                        prepared.source.validate()
                    next(target for target in prepared.targets if target.path == config).validate()
                    _replace_bytes(config, prepared.content)
                journal.unlink()
            except BaseException as error:
                try:
                    _restore(snapshots, record["next"])
                    journal.unlink(missing_ok=True)
                except (OSError, ValueError, maintenance.MaintenanceError) as rollback_error:
                    raise maintenance.RecoveryRequired(
                        f"Runtime activation failed. Recovery record retained at {journal}"
                    ) from rollback_error
                raise artifacts.ArtifactError("Runtime activation failed. Previous selection restored") from error
        conf._overrides.config_path = destination
        if hasattr(conf._overrides, "config_destination"):
            conf._overrides.config_destination = destination
        if hasattr(conf._overrides, "cli_overrides"):
            conf._overrides.cli_overrides.pop("wine_appimage_path", None)
        if hasattr(conf._overrides, "runtime_selection"):
            conf._overrides.runtime_selection = {
                "wine_binary": changes["wine_binary"], "wine_binary_code": "AppImage",
                "SELECTED_APPIMAGE_FILENAME": None,
            }
        environment = getattr(conf._overrides, "environment", None)
        if not isinstance(environment, dict):
            environment = {} if environment is False else environment_document()
        conf._raw = PersistentConfiguration.from_document(prepared.document, environment)
        conf._raw._file_path = str(config_store.absolute_path(destination))
        conf._raw._file_bytes = prepared.content
        if callable(getattr(conf, "rebind", None)):
            conf.rebind(conf._raw)
        conf._overrides.wine_appimage_path = None
        for name in ("_wine64_path", "_wine64_identity", "_wine_binary_files", "_wine_appimage_files"):
            setattr(conf, name, None)
        if callable(getattr(conf, "invalidate_context", None)):
            conf.invalidate_context()
        app._config_updated_event.set()
    return accepted


def executable(binary, app=None, *, destination=None):
    """Pin accepted generations while preserving native wrapper invocation paths."""
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
    return str(requested)
