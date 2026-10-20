"""Read existing installations and explicitly publish a separate controller profile."""

from base64 import b64decode, b64encode
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

from . import config_store, constants, maintenance, runtime


MAX_CONFIG_BYTES = 1024 * 1024
MAX_CANDIDATES = 64
MAX_USERS = 64
MAX_DATA_PROFILES = 64


class AdoptionError(config_store.ConfigurationError):
    """An adoption needs a new selection or recovery before continuing."""


@dataclass(frozen=True)
class Candidate:
    source: Path | None
    install: Path
    prefix: Path
    product: str
    content: bytes | None
    document: bytes


@dataclass(frozen=True)
class Discovery:
    candidates: tuple[Candidate, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class Preview:
    candidate: Candidate
    destination: Path
    content: bytes
    journal: Path
    overrides: bytes


@dataclass(frozen=True)
class Result:
    destination: Path
    journal: Path
    state: str
    conflicts: tuple[str, ...] = ()


def _read(path, *, limit=MAX_CONFIG_BYTES):
    """Read an existing owned file without creating directories or following links."""
    path = config_store.absolute_path(path)
    runtime._safe_target(path)
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise AdoptionError(f"Expected an owned regular file: {path}")
            content = source.read(limit + 1)
            if len(content) > limit:
                raise AdoptionError(f"File exceeds the inspection limit: {path}")
            return content
    finally:
        os.close(parent)


def _wine_users(prefix):
    """Refuse user layouts that the controller's first-entry selection cannot represent."""
    users = prefix / 'drive_c/users'
    runtime._safe_target(users / '.inspection')
    selected = []
    if users.is_dir():
        with os.scandir(users) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_USERS:
                    raise AdoptionError(f'Too many Wine user entries: {users}')
                if entry.name == 'Public':
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    raise AdoptionError(f'Cannot select a linked or non-directory Wine user: {entry.path}')
                selected.append(Path(entry.path))
    if len(selected) > 1:
        raise AdoptionError(f'Cannot represent multiple Wine users in one controller profile: {users}')
    return tuple(selected)


def _installation(document, source, content):
    document = config_store.canonical(document, source)
    if document.get('schema_version', 1) != 1:
        raise AdoptionError(f"Unsupported configuration schema: {source}")
    install = document.get('install_dir')
    if not install:
        raise AdoptionError(f"Configuration has no installation directory: {source}")
    install = Path(install)
    prefix = config_store.absolute_path(document.get('WINEPREFIX', 'data/wine64_bottle'), base=install)
    if not install.is_dir() or not prefix.is_dir():
        raise AdoptionError(f"Installation or prefix is missing: {install}")
    for directory in (install, prefix):
        runtime._safe_target(directory / '.inspection')
    _wine_users(prefix)
    product = document.get('faithlife_product')
    if product not in constants.FAITHLIFE_PRODUCTS:
        raise AdoptionError(f"Select a configuration with a known product: {source}")

    # Rebase saved path values before moving the profile to another directory.
    for name in config_store.PATH_FIELDS - {'install_dir', 'INSTALLDIR'}:
        value = document.get(name)
        if value and value not in (constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL):
            document[name] = str(config_store.absolute_path(value, base=install))
    document['schema_version'] = 1
    document['WINEPREFIX'] = str(prefix)
    return Candidate(source if content is not None else None, install, prefix, product, content,
                     config_store.encode(document))


def _reconstruct(install):
    prefix = install / 'data/wine64_bottle'
    matches = []
    for user in _wine_users(prefix):
        for product in constants.FAITHLIFE_PRODUCTS:
            executable = user / f'AppData/Local/{product}/{product}.exe'
            if executable.is_file() and not executable.is_symlink():
                runtime._safe_target(executable)
                matches.append((product, executable.parent))
    if len(matches) != 1:
        raise AdoptionError(f"Cannot identify one conventional installed product and Wine user: {install}")
    product, appdata = matches[0]
    data = appdata / 'Data'
    runtime._safe_target(data / '.inspection')
    profiles = []
    if data.is_dir():
        with os.scandir(data) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_DATA_PROFILES:
                    raise AdoptionError(f'Too many data profile entries: {data}')
                if entry.is_symlink():
                    raise AdoptionError(f'Cannot inspect a linked data profile: {entry.path}')
                if entry.is_dir(follow_symlinks=False):
                    profiles.append(entry.name)
    if len(profiles) > 1:
        raise AdoptionError(f'Select a configuration that identifies one data profile: {data}')
    document = {'install_dir': str(install), 'faithlife_product': product,
                'faithlife_product_version': '10', 'WINEPREFIX': str(prefix)}
    return _installation(document, install / constants.PORTABLE_CONFIG_NAME, None)


def discover(paths=None, *, installation=None):
    """Inspect bounded explicit paths or known legacy names. No writes or execution."""
    selected = list(dict.fromkeys(paths if paths is not None else constants.LEGACY_CONFIG_FILES))
    if installation is not None:
        install = config_store.absolute_path(installation)
        selected.extend([install / constants.PORTABLE_CONFIG_NAME, install / f'{constants.BINARY_NAME}.json'])
    if len(selected) > MAX_CANDIDATES:
        raise AdoptionError('Too many configuration candidates')
    candidates = []
    issues = []
    for selected_path in dict.fromkeys(selected):
        path = config_store.absolute_path(selected_path)
        try:
            content = _read(path)
            if content is not None:
                candidates.append(_installation(config_store.decode(content, path), path, content))
        except (OSError, ValueError, maintenance.MaintenanceError) as error:
            issues.append(str(error))
    if installation is not None and not candidates and not issues:
        try:
            candidates.append(_reconstruct(install))
        except (OSError, ValueError, maintenance.MaintenanceError) as error:
            issues.append(str(error))
    return Discovery(tuple(candidates), tuple(issues))


def _journal_path(destination):
    key = hashlib.sha256(os.fsencode(destination)).hexdigest()
    return Path(constants.STATE_DIR) / 'adoption' / f'{key}.json'


@contextmanager
def _destination_lock(destination):
    """Reserve a canonical config name across prefixes and XDG state overrides."""
    from .instance import _directory

    canonical = config_store.absolute_path(destination).resolve()
    key = hashlib.sha256(os.fsencode(canonical)).hexdigest()
    directory = _directory(constants.COORDINATION_DIR)
    descriptor = None
    try:
        descriptor = os.open(f'destination-{key}.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise AdoptionError('Destination lock must be private and owned')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise maintenance.MaintenanceBusy(f'Configuration destination is in use: {canonical}') from error
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def preview(selection, destination=None, *, overrides=None):
    """Require an explicit candidate when discovery found more than one profile."""
    if isinstance(selection, Discovery):
        if len(selection.candidates) != 1:
            raise AdoptionError('Select exactly one discovered configuration')
        selection = selection.candidates[0]
    destination = config_store.absolute_path(destination or constants.DEFAULT_CONFIG_PATH)
    if selection.source is None:
        reconstructed = _reconstruct(selection.install)
        if (reconstructed.prefix, reconstructed.product) != (selection.prefix, selection.product):
            raise AdoptionError('Reconstructed installation changed after discovery')
    if selection.source == destination:
        raise AdoptionError('Adoption must preserve the source at a separate path')
    journal = _journal_path(destination)
    if any(path.resolve().is_relative_to(selection.prefix.resolve()) for path in (destination, journal)):
        raise AdoptionError('Adoption configuration and journal must be outside the Wine prefix')
    document = config_store.apply_changes(json.loads(selection.document), overrides or {}, destination)
    checked = _installation(document, destination, selection.content)
    if (checked.install, checked.prefix, checked.product) != (selection.install, selection.prefix, selection.product):
        raise AdoptionError('Overrides cannot change the selected installation')
    config_store.require_ready(document)
    existing = _read(destination)
    content = config_store.encode(document)
    if len(content) > MAX_CONFIG_BYTES:
        raise AdoptionError('Adopted configuration exceeds the publication limit')
    if existing is not None and existing != content:
        raise AdoptionError(f'Destination already contains another profile: {destination}')
    return Preview(selection, destination, content, journal, config_store.encode(overrides or {}))


def _encoded(content):
    return None if content is None else b64encode(content).decode('ascii')


def _decoded(content):
    return None if content is None else b64decode(content, validate=True)


def _write_record(path, record):
    content = config_store.encode(record)
    if len(content) > 8 * MAX_CONFIG_BYTES:
        raise AdoptionError('Recovery journal exceeds the publication limit')
    with config_store.Target(path) as target:
        target.stage(content)
        target.publish()
        os.fsync(target.fd)
        if _read(path, limit=8 * MAX_CONFIG_BYTES) != content:
            raise AdoptionError(f'Recovery journal readback changed: {path}')


def _publish_absent(target):
    """Claim an absent name atomically through its held parent descriptor."""
    target.validate()
    rename = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if rename is None:
        raise AdoptionError('Atomic no-clobber configuration publication is unavailable')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(target.fd, os.fsencode(target.temporary), target.fd, os.fsencode(target.path.name), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target.path))
    target.temporary = None


def _publish_files(journal, record, *, validator=None, no_replace=False):
    """Write intent durably before any publication, including absent-file states."""
    with ExitStack() as stack:
        targets = [stack.enter_context(config_store.Target(item['path'])) for item in record['files']]
        for target, item in zip(targets, record['files']):
            if target.previous != _decoded(item['before']):
                raise AdoptionError(f'Target changed since preview: {target.path}')
            target.stage(_decoded(item['after']))
            if no_replace:
                if target.previous is not None:
                    raise AdoptionError(f'No-clobber publication requires an absent target: {target.path}')
                staged = os.stat(target.temporary, dir_fd=target.fd, follow_symlinks=False)
                item['publication_identity'] = [staged.st_dev, staged.st_ino]
        _write_record(journal, record)
        if validator is not None:
            validator()
        for target, item in zip(targets, record['files']):
            if no_replace:
                _publish_absent(target)
            else:
                target.publish()
            os.fsync(target.fd)
            if _read(target.path) != _decoded(item['after']):
                raise AdoptionError(f'Publication readback changed: {target.path}')
        record['state'] = 'committed'
        _write_record(journal, record)


def adopt(plan):
    """Publish the new config after the caller reserves controller ownership.

    Failure leaves a durable rollback record. This service never activates Wine.
    """
    with _destination_lock(plan.destination), runtime.configuration_lock():
        prior = _read(plan.journal, limit=8 * MAX_CONFIG_BYTES)
        if prior is not None:
            record = json.loads(prior)
            if record.get('state') == 'committed' and _read(plan.destination) == plan.content:
                return Result(plan.destination, plan.journal, 'committed')
            if record.get('state') != 'rolled-back':
                raise AdoptionError(f'Reconcile the existing adoption journal: {plan.journal}')
        current = preview(plan.candidate, plan.destination, overrides=json.loads(plan.overrides))
        if current.content != plan.content:
            raise AdoptionError('Adoption preview changed')
        with ExitStack() as stack:
            if plan.candidate.source is not None:
                source = stack.enter_context(config_store.Target(plan.candidate.source))
                if source.previous != plan.candidate.content:
                    raise AdoptionError('Source changed after preview')
                source.validate()
            record = {'schema_version': 1, 'kind': 'adoption', 'state': 'prepared',
                      'destination': str(plan.destination), 'source': str(plan.candidate.source),
                      'original': _encoded(plan.candidate.content),
                      'files': [{'path': str(plan.destination), 'before': None, 'after': _encoded(plan.content)}]}
            if _read(plan.destination) is not None:
                raise AdoptionError('Destination exists without a matching adoption journal')
            validator = source.validate if plan.candidate.source is not None else None
            _publish_files(plan.journal, record, validator=validator, no_replace=True)
        return Result(plan.destination, plan.journal, 'committed')


def _rollback_files(journal, record, *, preserve=(), dependents=None):
    dependents = dependents or {}
    conflicts = []

    def retain(path):
        for name in (path, *dependents.get(path, ())):
            if name not in conflicts:
                conflicts.append(name)

    for path in preserve:
        retain(path)
    for item in reversed(record['files']):
        if item['path'] in conflicts:
            continue
        with config_store.Target(item['path']) as target:
            before, after = _decoded(item['before']), _decoded(item['after'])
            if target.previous == before:
                continue
            identity = item.get('publication_identity')
            same_publication = identity is None or (
                target.identity is not None and list(target.identity[:2]) == identity
            )
            if target.previous != after or not same_publication:
                retain(str(target.path))
                continue
            target.validate()
            if before is None:
                os.unlink(target.path.name, dir_fd=target.fd)
            else:
                target.stage(before)
                target.publish()
            os.fsync(target.fd)
    record['state'] = 'conflict' if conflicts else 'rolled-back'
    record['conflicts'] = conflicts
    _write_record(journal, record)
    return tuple(conflicts)


def rollback(journal):
    """Restore recorded publications after the caller settles owned profile work."""
    journal = config_store.absolute_path(journal)
    initial = _read(journal, limit=8 * MAX_CONFIG_BYTES)
    if initial is None:
        raise AdoptionError('Adoption journal is missing')
    destination = config_store.absolute_path(json.loads(initial)['destination'])
    with _destination_lock(destination), runtime.configuration_lock():
        content = _read(journal, limit=8 * MAX_CONFIG_BYTES)
        if content is None:
            raise AdoptionError('Adoption journal is missing')
        record = json.loads(content)
        if (record.get('schema_version') != 1 or record.get('kind') != 'adoption'
                or config_store.absolute_path(record['destination']) != destination
                or journal != _journal_path(destination) or len(record['files']) != 1
                or record['files'][0]['path'] != str(destination)):
            raise AdoptionError('Invalid adoption journal')
        conflicts = _rollback_files(journal, record)
        return Result(destination, journal, record['state'], conflicts)
