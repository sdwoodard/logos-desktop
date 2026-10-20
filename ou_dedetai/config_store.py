"""Decode configuration data and publish selected-profile snapshots."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import copy
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat

from . import artifacts, maintenance, runtime


ALIASES = {
    "faithlife_product": "FLPRODUCT",
    "faithlife_product_version": "TARGETVERSION",
    "faithlife_product_release": "TARGET_RELEASE_VERSION",
    "faithlife_product_logging": "LOGS",
    "install_dir": "INSTALLDIR",
    "wine_binary": "WINE_EXE",
    "wine_binary_code": "WINEBIN_CODE",
    "backup_dir": "BACKUPDIR",
    "curses_color_scheme": "curses_colors",
    "faithlife_product_release_channel": "logos_release_channel",
    "app_release_channel": "lli_release_channel",
}
BOOLEAN_FIELDS = {
    "faithlife_product_logging", "LOGS", "DEBUG", "VERBOSE", "CHECK_UPDATES",
    "SKIP_DEPENDENCIES", "DELETE_LOG", "use_python_dialog",
}
PATH_FIELDS = {
    "install_dir", "INSTALLDIR", "wine_binary", "WINE_EXE", "backup_dir", "BACKUPDIR",
    "APPDIR_BINDIR", "WINEPREFIX", "WINESERVER_EXE", "CUSTOMBINPATH", "LOGOS_LOG",
    "wine_log", "SELECTED_APPIMAGE_FILENAME",
}
PROCESS_FIELDS = {"CONFIG_FILE", "assume_yes", "agreed_to_faithlife_terms", "app_run_as_root_permitted"}


class ConfigurationError(maintenance.MaintenanceError, ValueError):
    """Configuration could not be decoded or saved without losing existing data."""


def absolute_path(value, *, base=None):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ConfigurationError("Configuration paths must be nonempty strings")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (Path(base) if base is not None else Path.cwd()) / path
    return Path(os.path.abspath(path))


def boolean(value, field, source):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "y", "yes"):
            return True
        if normalized in ("false", "0", "n", "no"):
            return False
    raise ConfigurationError(f"Boolean settings require true or false: {field} in {source}")


def decode(content, source):
    try:
        text = content.decode("utf-8-sig")
        if text.lstrip().startswith(("{", "[")) or str(source).endswith(".json"):
            document = json.loads(text)
            if not isinstance(document, dict):
                raise ValueError("Expected a JSON object")
            return document

        document = {}
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assignment = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            if assignment is None:
                raise ValueError(f"Unsupported assignment at line {number}")
            name, value = assignment.groups()
            if "`" in value or "$(" in value:
                raise ValueError(f"Shell expansion is unsupported at line {number}")
            parts = shlex.split(value, comments=True, posix=True)
            if len(parts) > 1:
                raise ValueError(f"Expected one value at line {number}")
            document[name] = parts[0] if parts else ""
        return document
    except (UnicodeError, ValueError) as error:
        raise ConfigurationError(f"Cannot read configuration {source}: {error}") from error


def read_document(path):
    path = absolute_path(path)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        raise ConfigurationError(f"Cannot read configuration {path}: {error}") from error
    return canonical(decode(content, path), path), content


def canonical(document, source):
    output = copy.deepcopy(document)
    for modern, legacy in ALIASES.items():
        if modern not in output and legacy in output:
            output[modern] = output[legacy]
        if modern in output:
            output[legacy] = output[modern]

    for name, value in output.items():
        if value is None:
            continue
        if name in BOOLEAN_FIELDS:
            output[name] = boolean(value, name, source)
        elif name in PATH_FIELDS:
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"Expected a nonempty path for {name} in {source}")
        elif name in ALIASES or name in ALIASES.values():
            if not isinstance(value, str):
                raise ConfigurationError(f"Expected a string for {name} in {source}")

    if output.get("install_dir") is not None:
        install = str(absolute_path(output["install_dir"], base=Path(source).parent))
        output["install_dir"] = output["INSTALLDIR"] = install
    if output.get("faithlife_product") not in (None, "Logos", "Verbum"):
        raise ConfigurationError(f"Unknown product in {source}")
    for name in ("faithlife_product_release_channel", "app_release_channel"):
        if name in output and output[name] not in ("stable", "beta", "dev"):
            raise ConfigurationError(f"Unknown release channel {name} in {source}")
    version = output.get("schema_version", 1)
    if type(version) is not int or version < 1:
        raise ConfigurationError(f"Invalid schema_version in {source}")
    for name in PROCESS_FIELDS:
        output.pop(name, None)
    return output


def apply_changes(document, changes, source):
    output = copy.deepcopy(document)
    reverse = {alias: name for name, alias in ALIASES.items()}
    for name, value in changes.items():
        modern = reverse.get(name, name)
        names = (modern, ALIASES[modern]) if modern in ALIASES else (modern,)
        for key in names:
            if value is None:
                output.pop(key, None)
            else:
                output[key] = copy.deepcopy(value)
    output.setdefault("schema_version", 1)
    return canonical(output, source)


def encode(document):
    return (json.dumps(document, indent=4, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


class Target:
    """Keep publication attached to the inspected parent and previous file."""

    def __init__(self, path):
        self.path = absolute_path(path)
        self.fd = None
        self.temporary = None
        runtime._safe_target(self.path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime._safe_target(self.path)
        self.fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.previous, self.identity = self.snapshot()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()

    def snapshot(self):
        try:
            info = os.stat(self.path.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None, None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ConfigurationError(f"Configuration target must be an owned, unlinked regular file: {self.path}")
        descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ConfigurationError(f"Configuration target changed: {self.path}")
            content = source.read()
        identity = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        return content, identity

    def validate(self):
        runtime._safe_target(self.path)
        parent = self.path.parent.stat()
        held = os.fstat(self.fd)
        if (parent.st_dev, parent.st_ino) != (held.st_dev, held.st_ino):
            raise ConfigurationError(f"Configuration parent changed: {self.path}")
        content, identity = self.snapshot()
        if identity != self.identity or content != self.previous:
            raise ConfigurationError(f"Configuration target changed before publication: {self.path}")

    def stage(self, content):
        self.temporary = f".config-{secrets.token_hex(16)}"
        descriptor = os.open(
            self.temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())

    def publish(self):
        self.validate()
        os.replace(self.temporary, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        self.temporary = None

    def close(self):
        if self.fd is not None:
            descriptor = self.fd
            self.fd = None
            try:
                if self.temporary is not None:
                    try:
                        os.unlink(self.temporary, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(descriptor)


@dataclass(frozen=True)
class Saved:
    document: dict
    warning: str = ""


def context(document, overrides=None):
    effective = apply_changes(document, overrides or {}, "configuration")
    install = effective.get("install_dir")
    if not install:
        return None
    install = absolute_path(install)
    prefix = absolute_path(effective.get("WINEPREFIX", "data/wine64_bottle"), base=install)
    return maintenance.Context(prefix.resolve(), install.resolve(), effective.get("faithlife_product"))


def require_ready(document, overrides=None):
    for overlay in ({}, overrides or {}):
        selected = context(document, overlay)
        if selected is None:
            continue
        if runtime.pending(selected.install) or selected.needs_recovery():
            raise maintenance.RecoveryRequired("Reconcile pending installation records before saving settings")


def selection_identity(document, overrides=None):
    selected = context(document, overrides)
    return selected, tuple(document.get(name) for name in ("wine_binary", "APPDIR_BINDIR", "WINESERVER_EXE"))


@dataclass
class Prepared:
    document: dict
    content: bytes
    targets: list
    backups: list
    source: Target | None

    def validate(self):
        if self.source is not None:
            self.source.validate()
        for target in self.targets:
            target.validate()

    def preserve_previous(self):
        self.validate()
        for target, content, immutable in self.backups:
            if immutable and target.previous is not None:
                if target.previous != content:
                    raise ConfigurationError(f"Migration original already exists with different bytes: {target.path}")
                target.validate()
                continue
            target.stage(content)
            target.publish()
            os.fsync(target.fd)


@contextmanager
def prepare(path, changes, *, source_path=None, source_bytes=None, overrides=None, expected=None, validator=None):
    """Share input validation and destination ownership with runtime activation."""
    path = absolute_path(path)
    source_path = absolute_path(source_path) if source_path is not None else path
    with ExitStack() as stack:
        primary = stack.enter_context(Target(path))
        source = None
        if source_path != path:
            source = stack.enter_context(Target(source_path))
            source.validate()
            if source.previous is None or source.previous != source_bytes or primary.previous is not None:
                raise ConfigurationError("Legacy migration source or destination changed. Reload before saving")
            document = canonical(decode(source.previous, source_path), source_path)
        else:
            document = canonical(decode(primary.previous, path), path) if primary.previous is not None else {}
        require_ready(document, overrides)
        if expected is not None and selection_identity(document) != selection_identity(expected):
            raise ConfigurationError("Selected installation changed on disk. Reload before saving")
        document = apply_changes(document, changes, path)
        require_ready(document, overrides)
        if validator is not None:
            validator(document)
        content = encode(document)
        targets = [primary]
        install = document.get("install_dir")
        if install:
            from . import constants

            portable = absolute_path(Path(install) / constants.PORTABLE_CONFIG_NAME)
            if portable != path:
                targets.append(stack.enter_context(Target(portable)))

        # Reserve every pathname before publishing even an auxiliary snapshot.
        roles = [target.path for target in targets]
        if source is not None:
            roles.append(source.path)
        backup_specs = []
        for target in targets:
            if target.previous is None:
                continue
            decode(target.previous, target.path)
            previous = target.path.with_name(f".{target.path.name}.previous")
            backup_specs.append((previous, target.previous, False))
            if not target.previous.lstrip().startswith((b"{", b"[")):
                original = target.path.with_name(f".{target.path.name}.legacy-original")
                backup_specs.append((original, target.previous, True))
        roles.extend(path for path, _, _ in backup_specs)
        if len(roles) != len(set(roles)):
            raise ConfigurationError("Configuration destinations conflict with source or recovery-copy paths")
        backups = [(stack.enter_context(Target(path)), value, immutable) for path, value, immutable in backup_specs]
        prepared = Prepared(document, content, targets, backups, source)
        for target in targets:
            target.stage(content)
        yield prepared


def save(path, changes, *, source_path=None, source_bytes=None, overrides=None, expected=None, validator=None):
    """Caller holds configuration_lock. A returned result means primary committed."""
    path = absolute_path(path)
    source_path = absolute_path(source_path) if source_path is not None else path
    committed = False
    warnings = []
    try:
        with prepare(
            path, changes, source_path=source_path, source_bytes=source_bytes, overrides=overrides, expected=expected,
            validator=validator,
        ) as prepared:
            document = prepared.document
            primary = prepared.targets[0]
            prepared.preserve_previous()
            primary.publish()
            committed = True
            try:
                os.fsync(primary.fd)
            except OSError as error:
                warnings.append(f"Saved {path}, but durability could not be confirmed: {error}")
            for target in prepared.targets[1:]:
                try:
                    target.publish()
                    os.fsync(target.fd)
                except (OSError, ValueError, maintenance.MaintenanceError, artifacts.ArtifactError) as error:
                    warnings.append(f"Saved {path}, but portable configuration {target.path} failed: {error}")
            return Saved(document, "\n".join(warnings))
    except maintenance.RecoveryRequired:
        raise
    except (OSError, TypeError, ValueError, maintenance.MaintenanceError, artifacts.ArtifactError) as error:
        if committed:
            return Saved(document, f"Saved {path}, but configuration cleanup failed: {error}")
        raise ConfigurationError(f"Configuration could not be saved: {path}: {error}") from error
