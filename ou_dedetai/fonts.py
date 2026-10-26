"""Bounded Arial installation with pinned inputs and prefix-local destinations."""

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess

import requests

from . import artifacts, maintenance


FONT_KEYS = (
    r"HKEY_LOCAL_MACHINE\Software\Microsoft\Windows NT\CurrentVersion\Fonts",
    r"HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Fonts",
)
FACES = {
    "arial.ttf": "Arial",
    "arialbd.ttf": "Arial Bold",
    "arialbi.ttf": "Arial Bold Italic",
    "ariali.ttf": "Arial Italic",
    "ariblk.ttf": "Arial Black",
}
# Public member identity recorded with the pinned arial32.exe source.
ARIAL_SHA256 = "35c0f3559d8db569e36c31095b8a60d441643d95f59139de40e23fada819b833"
# Winetricks 20250102-next, load_arial. Extract these archives, never execute them.
ARCHIVES = (
    ("arial32.exe", "85297a4d146e9c87ac6f74822734bdee5f4b2a722d7eaa584b7f2cbf76f478f6",
     ("Arial.TTF", "Arialbd.TTF", "Arialbi.TTF", "Ariali.TTF")),
    ("arialb32.exe", "a425f0ffb6a1a5ede5b979ed6177f4f4f4fdef6ae7c302a7b7720ef332fec0a8", ("AriBlk.TTF",)),
)
ARCHIVE_URL = "https://github.com/pushcx/corefonts/raw/master/"
FONT_TERMS = "https://corefonts.sourceforge.net/eula.htm"
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_directory(path, *, mapping=False):
    """Walk from root without following any directory symlinks."""
    if not path.is_absolute() or (not mapping and ".." in path.parts):
        raise maintenance.MaintenanceError("Arial requires an absolute, safe prefix path")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _case_name(descriptor, expected):
    matches = [name for name in os.listdir(descriptor) if name.casefold() == expected.casefold()]
    if len(matches) > 1:
        raise maintenance.MaintenanceError("Ambiguous case variants in the Arial font destination")
    return matches[0] if matches else expected


@contextmanager
def _font_directory(prefix):
    with ExitStack() as stack:
        prefix_fd = _open_directory(prefix)
        stack.callback(os.close, prefix_fd)
        devices = os.open("dosdevices", DIRECTORY_FLAGS, dir_fd=prefix_fd)
        stack.callback(os.close, devices)
        target = os.readlink("c:", dir_fd=devices)
        raw_drive = prefix / "dosdevices" / target
        # Traverse every raw component before normalizing. A symlink or missing
        # directory followed by '..' must not disappear from the safety check.
        descriptor = _open_directory(raw_drive, mapping=True)
        stack.callback(os.close, descriptor)
        drive = Path(os.path.abspath(raw_drive))
        if not drive.is_relative_to(prefix) or drive == prefix:
            raise maintenance.MaintenanceError("Arial requires a prefix-local C: mapping")
        for component in ("windows", "fonts"):
            descriptor = os.open(_case_name(descriptor, component), DIRECTORY_FLAGS, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        yield descriptor


def _destinations(descriptor):
    entries = {}
    for expected in FACES:
        name = _case_name(descriptor, expected)
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            identity = None
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise maintenance.MaintenanceError("Arial font destinations must be regular files with one hard link")
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        entries[expected] = (name, identity)
    return entries


def _check_destination(prefix, descriptor, entries):
    with _font_directory(prefix) as current:
        before, after = os.fstat(descriptor), os.fstat(current)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise maintenance.MaintenanceError("Arial font directory mapping changed during preparation")
        if _destinations(current) != entries:
            raise maintenance.MaintenanceError("Arial font destinations changed during preparation")


def _read_font(descriptor, name):
    try:
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        return b""
    with os.fdopen(file, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_ARCHIVE_BYTES:
            raise maintenance.MaintenanceError("Arial must be a readable regular font file")
        return source.read(MAX_ARCHIVE_BYTES + 1)


def _local_registration(value, filename):
    if not isinstance(value, str):
        return False
    return value.casefold() in (filename.casefold(), "c:\\windows\\fonts\\" + filename.casefold())


def _ready(app, descriptor, filename):
    from . import wine

    value = wine.wine_reg_query(app, FONT_KEYS[0], "Arial (TrueType)")
    return (_local_registration(value, filename)
            and hashlib.sha256(_read_font(descriptor, filename)).hexdigest() == ARIAL_SHA256)


@contextmanager
def _sealed_archive(name, expected, checkpoint):
    """Extractors receive the same sealed bytes whose digest was accepted."""
    descriptor = os.memfd_create(name, os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(os.dup(descriptor), "wb") as output:
            with requests.get(
                ARCHIVE_URL + name, stream=True, timeout=artifacts.NETWORK_TIMEOUT,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                artifacts.require_response(response, ARCHIVE_URL + name)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise maintenance.MaintenanceError("Encoded Arial archives are unsupported")
                for chunk in response.iter_content(64 * 1024):
                    checkpoint()
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise maintenance.MaintenanceError("Arial archive exceeds the expected size limit")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != expected:
            raise maintenance.MaintenanceError("Arial archive checksum mismatch")
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        os.lseek(descriptor, 0, os.SEEK_SET)
        checkpoint()
        yield descriptor
    finally:
        os.close(descriptor)


def _acquire_fonts(app):
    extractor = shutil.which("cabextract")
    if extractor is None:
        raise maintenance.MaintenanceError("Installing Arial requires cabextract")
    fonts = {}
    with ExitStack() as stack:
        archives = [(stack.enter_context(_sealed_archive(name, digest, app.checkpoint)), members)
                    for name, digest, members in ARCHIVES]
        for descriptor, members in archives:
            for member in members:
                app.checkpoint()
                result = subprocess.run(
                    [extractor, "-p", "-F", member, f"/proc/self/fd/{descriptor}"],
                    env={key: value for key, value in os.environ.items() if key != "XDG_ACTIVATION_TOKEN"},
                    pass_fds=(descriptor,), capture_output=True, timeout=30, check=True,
                )
                if not result.stdout or len(result.stdout) > MAX_ARCHIVE_BYTES:
                    raise maintenance.MaintenanceError("Arial archive has an invalid font member")
                fonts[member.lower()] = result.stdout
    if hashlib.sha256(fonts["arial.ttf"]).hexdigest() != ARIAL_SHA256:
        raise maintenance.MaintenanceError("Arial archive did not yield the required Arial identity")
    return fonts


def _publish_fonts(prefix, descriptor, entries, fonts, checkpoint):
    temporary = {}
    try:
        for expected, data in fonts.items():
            checkpoint()
            name = ".arial-" + secrets.token_hex(12)
            file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            temporary[expected] = name
            with os.fdopen(file, "wb") as output:
                output.write(data)
                output.flush()
                os.fchmod(output.fileno(), 0o644)
                os.fsync(output.fileno())

        checkpoint()
        _check_destination(prefix, descriptor, entries)
        # Once publication starts, finish the five-file set before observing cancel.
        for expected, name in temporary.items():
            os.replace(name, entries[expected][0], src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
        for expected, data in fonts.items():
            if _read_font(descriptor, entries[expected][0]) != data:
                raise maintenance.MaintenanceError("Published Arial font bytes changed")
    finally:
        for name in temporary.values():
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass


def _register(app, entries, names):
    from . import wine

    for key in FONT_KEYS:
        for name in names:
            app.checkpoint()
            wine.run_wine_during_install(
                app, app.conf.wine64_binary, exe="reg.exe",
                exe_args=["add", key, "/v", FACES[name] + " (TrueType)",
                          "/t", "REG_SZ", "/d", entries[name][0], "/f"], timeout=60,
            )


def ensure_arial(app):
    """Establish the regular Arial prerequisite inside installer maintenance admission."""
    try:
        app.checkpoint()
        prefix = Path(app.conf.wine_prefix)
        with _font_directory(prefix) as descriptor:
            entries = _destinations(descriptor)
            regular = entries["arial.ttf"][0]
            if _ready(app, descriptor, regular):
                _check_destination(prefix, descriptor, entries)
                return

            app.status("Configuring the Arial font prerequisite…")
            local = _read_font(descriptor, regular)
            names = ("arial.ttf",)
            if hashlib.sha256(local).hexdigest() != ARIAL_SHA256:
                if app.conf._overrides.assume_yes:
                    raise maintenance.MaintenanceError("Arial download requires interactive font-terms authorization")
                app.approve_or_exit(
                    "Do you accept the Microsoft core-font terms and authorize downloading Arial and Arial Black?",
                    FONT_TERMS,
                )
                app.checkpoint()
                fonts = _acquire_fonts(app)
                _publish_fonts(prefix, descriptor, entries, fonts, app.checkpoint)
                entries = _destinations(descriptor)
                names = tuple(FACES)

            app.checkpoint()
            _check_destination(prefix, descriptor, entries)
            _register(app, entries, names)
            if not _ready(app, descriptor, regular):
                raise maintenance.MaintenanceError("Arial prerequisite is still unmet after repair")
            _check_destination(prefix, descriptor, entries)
    except (OSError, requests.RequestException, artifacts.ArtifactError, subprocess.SubprocessError) as error:
        raise maintenance.MaintenanceError(f"Arial prerequisite failed ({type(error).__name__})") from None
