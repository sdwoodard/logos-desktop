"""Profile-bound desktop entries and reversible, serialized MIME integration."""

from contextlib import contextmanager
from dataclasses import dataclass
import configparser
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from . import adoption, config_store, constants, maintenance, runtime


SCHEMES = ('x-scheme-handler/logos4', 'x-scheme-handler/libronixdls')


@dataclass(frozen=True)
class FileChange:
    path: Path
    before: bytes | None
    after: bytes


@dataclass(frozen=True)
class Preview:
    config: Path
    profile_id: str
    journal: Path
    files: tuple[FileChange, ...]
    defaults: tuple[tuple[str, str | None], ...]
    issues: tuple[str, ...]
    checks: tuple[tuple[Path, bytes], ...]


@dataclass(frozen=True)
class Result:
    journal: Path | None
    state: str
    issues: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


@contextmanager
def integration_lock():
    """Never acquire controller or prefix locks from this shared integration lock."""
    root = Path(constants.COORDINATION_DIR)
    runtime._safe_target(root / 'integration.lock')
    root = maintenance.private_directory(root)
    with config_store.Target(root / 'integration.lock') as target:
        descriptor = os.open(target.path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=target.fd)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                raise adoption.AdoptionError('Integration lock must be private and owned')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


def _default(scheme):
    command = shutil.which('xdg-mime')
    if command is None:
        return None
    try:
        environment = {key: value for key, value in os.environ.items() if key != 'XDG_ACTIVATION_TOKEN'}
        result = subprocess.run([command, 'query', 'default', scheme], env=environment,
                                capture_output=True, timeout=5, check=True)
        if len(result.stdout) > 4096:
            return None
        value = result.stdout.decode('utf-8').strip()
        if value and (Path(value).name != value or not value.endswith('.desktop')):
            return None
        return value
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def _owned(content, profile_id):
    if content is None:
        return True
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(content.decode('utf-8'))
        return parser.get('Desktop Entry', 'X-LogosDesktop-Profile', fallback=None) == profile_id
    except (UnicodeError, configparser.Error):
        return False


def _entry(profile_id, name, argv, *, url=False, icon=None):
    from .installer import desktop_exec

    lines = ['[Desktop Entry]', 'Type=Application', f'Name={name}', 'Terminal=false',
             f'Exec={desktop_exec(argv, accepts_url=url)}', f'X-LogosDesktop-Profile={profile_id}']
    if icon is not None:
        value = str(icon)
        if any(ord(character) < 32 for character in value):
            raise ValueError('Icon path contains control characters')
        lines.append('Icon=' + value.replace('\\', '\\\\'))
    if url:
        lines.append('NoDisplay=true')
        lines.append('MimeType=' + ';'.join(SCHEMES) + ';')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def _historical_exec(arguments):
    """Reproduce M02/M12 Exec bytes for migration after current argv validation."""
    if '%' in arguments[0]:
        arguments = ['/usr/bin/env', '--', *arguments]

    quoted = []
    for argument in arguments:
        escaped = argument.replace('%', '%%')
        for character in ('\\', '"', '`', '$'):
            escaped = escaped.replace(character, '\\' + character)
        quoted.append('"' + escaped.replace('\\', '\\\\') + '"')
    return ' '.join([*quoted, '%u'])


def _legacy_handler(previous, data, config, launcher, product, windows_executable):
    """Recognize the old direct installed launcher only with matching profile evidence."""
    from .installer import desktop_exec

    if previous != f'{product}-url-handler.desktop':
        return None
    profile_bytes = adoption._read(config)
    if profile_bytes is None:
        return None
    document = config_store.canonical(config_store.decode(profile_bytes, config), config)
    install = document.get('install_dir')
    if not install or list(launcher) != [str(Path(install) / constants.BINARY_NAME)]:
        return None
    path = data / 'applications' / previous
    content = adoption._read(path)
    if content is None:
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(content.decode('utf-8'))
        arguments = [*launcher, '--wine', windows_executable]
        expected = desktop_exec(arguments, accepts_url=True)
        historical = _historical_exec(arguments)
        if (parser.get('Desktop Entry', 'Type', fallback=None) != 'Application'
                or parser.get('Desktop Entry', 'Name', fallback=None) != f'{product} URL Handler'
                or parser.get('Desktop Entry', 'Exec', fallback=None) not in (expected, historical)):
            return None
    except (UnicodeError, configparser.Error):
        return None
    return ((config, profile_bytes), (path, content))


def preview(config, launcher, *, product='Logos', windows_executable=None, data_home=None, config_home=None):
    """Inspect exact profile-bound entries and optional MIME file changes without writes."""
    if product not in constants.FAITHLIFE_PRODUCTS:
        raise ValueError('Unknown desktop product')
    config = config_store.absolute_path(config).resolve()
    profile_id = hashlib.sha256(os.fsencode(config)).hexdigest()[:24]
    stem = f'logos-desktop-{profile_id}'
    data = config_store.absolute_path(data_home or constants.XDG_DATA_HOME)
    config_root = config_store.absolute_path(config_home or constants.CONFIG_BASE)
    journal = Path(constants.COORDINATION_DIR) / 'desktop' / f'{profile_id}.json'
    base = [*launcher, '-c', str(config)]
    icon = constants.APP_IMAGE_DIR / 'icon.png'
    entries = [(f'{stem}.desktop', _entry(profile_id, constants.APP_NAME, base, icon=icon)),
               (f'{stem}-app.desktop', _entry(profile_id, product, [*base, '--run-installed-app'], icon=icon))]
    handler = f'{stem}-url.desktop'
    if windows_executable:
        entries.append((handler, _entry(profile_id, f'{product} URL Handler',
                                       [*base, '--wine', windows_executable], url=True, icon=icon)))
    files = []
    issues = []
    checks = {}
    for filename, content in entries:
        path = data / 'applications' / filename
        before = adoption._read(path)
        if not _owned(before, profile_id):
            raise adoption.AdoptionError(f'Desktop ID is not owned by this profile: {path}')
        files.append(FileChange(path, before, content))

    defaults = tuple((scheme, _default(scheme)) for scheme in SCHEMES) if windows_executable else ()
    if not defaults:
        return Preview(config, profile_id, journal, tuple(files), (), (), ())
    mime_path = config_root / 'mimeapps.list'
    before = adoption._read(mime_path)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    if before is not None:
        try:
            parser.read_string(before.decode('utf-8'))
        except (UnicodeError, configparser.Error) as error:
            raise adoption.AdoptionError('Cannot safely parse the existing MIME associations') from error
    changed = False
    for scheme, previous in defaults:
        if previous is None:
            issues.append(f'MIME default could not be inspected: {scheme}')
            continue
        legacy = _legacy_handler(previous, data, config, launcher, product, windows_executable)
        if legacy:
            checks.update(legacy)
        if previous and previous != handler and not legacy:
            # Another profile also owns its own routing. Do not steal its default.
            issues.append(f'Existing MIME default preserved: {scheme}: {previous}')
            continue
        if not parser.has_section('Default Applications'):
            parser.add_section('Default Applications')
        parser.set('Default Applications', scheme, handler + ';')
        changed = True
    if changed:
        output = io.StringIO()
        parser.write(output, space_around_delimiters=False)
        content = output.getvalue().encode('utf-8')
        if len(content) > adoption.MAX_CONFIG_BYTES:
            raise adoption.AdoptionError('MIME associations exceed the publication limit')
        files.append(FileChange(mime_path, before, content))
    return Preview(config, profile_id, journal, tuple(files), defaults, tuple(issues), tuple(checks.items()))


def adopt(plan):
    """Commit the preview under the shared lock, recording every previous byte."""
    with integration_lock():
        if any(adoption._read(path) != content for path, content in plan.checks):
            raise adoption.AdoptionError('Legacy desktop ownership evidence changed after preview')
        if any(_default(scheme) != previous for scheme, previous in plan.defaults):
            raise adoption.AdoptionError('MIME defaults changed after preview')
        prior = adoption._read(plan.journal, limit=8 * adoption.MAX_CONFIG_BYTES)
        if prior is not None:
            previous = json.loads(prior)
            if previous.get('state') == 'committed':
                if all(adoption._read(item.path) == item.after for item in plan.files):
                    return Result(plan.journal, 'incomplete' if plan.issues else 'committed', plan.issues)
                raise adoption.AdoptionError('Desktop publication changed, rollback or resolve it first')
            if previous.get('state') != 'rolled-back':
                raise adoption.AdoptionError('Desktop integration needs rollback before retry')
        record = {'schema_version': 1, 'kind': 'desktop', 'state': 'prepared', 'profile': plan.profile_id,
                  'config': str(plan.config), 'defaults': list(plan.defaults),
                  'files': [{'path': str(item.path), 'before': adoption._encoded(item.before),
                             'after': adoption._encoded(item.after)} for item in plan.files]}
        adoption._publish_files(plan.journal, record)
        issues = list(plan.issues)
        for scheme, previous in plan.defaults:
            expected = f'logos-desktop-{plan.profile_id}-url.desktop'
            if any(item.path.name == 'mimeapps.list' for item in plan.files) and _default(scheme) != expected:
                issues.append(f'MIME publication could not be confirmed: {scheme}')
        record['observed_defaults'] = [[scheme, _default(scheme)] for scheme, _ in plan.defaults]
        adoption._write_record(plan.journal, record)
        return Result(plan.journal, 'incomplete' if issues else 'committed', tuple(issues))


def rollback(journal):
    """Restore exact owned publications, preserving conflicting later edits."""
    journal = config_store.absolute_path(journal)
    with integration_lock():
        content = adoption._read(journal, limit=8 * adoption.MAX_CONFIG_BYTES)
        if content is None:
            raise adoption.AdoptionError('Desktop integration journal is missing')
        record = json.loads(content)
        profile = hashlib.sha256(os.fsencode(record['config'])).hexdigest()[:24]
        expected = Path(constants.COORDINATION_DIR) / 'desktop' / f'{profile}.json'
        if (record.get('schema_version') != 1 or record.get('kind') != 'desktop'
                or record.get('profile') != profile or journal != expected
                or not 2 <= len(record['files']) <= 4):
            raise adoption.AdoptionError('Invalid desktop integration journal')
        preserve = []
        dependents = {}
        handlers = tuple(item['path'] for item in record['files']
                         if Path(item['path']).name == f'logos-desktop-{profile}-url.desktop')
        for item in record['files']:
            path = Path(item['path'])
            if path.name == 'mimeapps.list':
                dependents[str(path)] = handlers
                if adoption._read(path) == adoption._decoded(item['before']):
                    if any(_default(scheme) == Path(handler).name for scheme in SCHEMES for handler in handlers):
                        preserve.extend(handlers)
                    continue
                observed = record.get('observed_defaults')
                if observed is None:
                    observed = [(scheme, f'logos-desktop-{profile}-url.desktop') for scheme in SCHEMES]
                if any(_default(scheme) != value for scheme, value in observed):
                    preserve.append(str(path))
            elif (path.parent.name != 'applications'
                  or path.name not in {f'logos-desktop-{profile}{suffix}.desktop' for suffix in ('', '-app', '-url')}
                  or not _owned(adoption._decoded(item['after']), profile)):
                raise adoption.AdoptionError('Invalid desktop journal target')
        if not dependents and any(_default(scheme) == Path(handler).name
                                  for scheme in SCHEMES for handler in handlers):
            preserve.extend(handlers)
        conflicts = adoption._rollback_files(journal, record, preserve=preserve, dependents=dependents)
        return Result(journal, record['state'], conflicts=conflicts)
