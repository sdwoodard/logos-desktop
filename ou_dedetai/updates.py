"""Destination-side controller replacement with an independently runnable backup."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading

from . import artifacts, constants, maintenance, processes, releases


class UpdateError(artifacts.ArtifactError):
    """Controller replacement failed without accepting a partial installation."""


class ManagedInstallation(UpdateError):
    """This controller must be updated through its installation method."""


_startup = None
_handoff = False
TOKEN_VARIABLE = "LOGOS_DESKTOP_UPDATE_TOKEN"


def fresh_environment():
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
        env.pop("LD_LIBRARY_PATH_ORIG", None)
    bundle = str(getattr(sys, "_MEIPASS", ""))
    for key in ("TCL_LIBRARY", "TK_LIBRARY", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        if bundle and bundle in env.get(key, ""):
            env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.pop(TOKEN_VARIABLE, None)
    return env


def _safe_parents(path):
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or parent.is_symlink():
            raise UpdateError("Controller parents must be real directories")
        if info.st_uid not in (0, os.geteuid()):
            raise UpdateError("Controller directory has an unexpected owner")
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise UpdateError("Controller directory is writable by another user")


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)):
        raise UpdateError("Controller must be an owned, unlinked regular executable")
    if "security.capability" in os.listxattr(path, follow_symlinks=False):
        raise UpdateError("Controller file capabilities prevent self-replacement")
    return info


def _package_owned(path):
    queries = (
        ("pacman", ["-Qqo", "--", str(path)], "No package owns"),
        ("dpkg-query", ["-S", str(path)], "no path found matching pattern"),
        ("rpm", ["-qf", "--", str(path)], "is not owned by any package"),
    )
    for program, arguments, absent in queries:
        executable = shutil.which(program)
        if executable is None:
            continue
        env = fresh_environment()
        env["LC_ALL"] = "C"
        try:
            result = subprocess.run([executable, *arguments], env=env, timeout=5,
                                    capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise ManagedInstallation("Cannot verify controller package ownership") from error
        if result.returncode == 0:
            return True
        if result.returncode != 1 or absent not in result.stdout + result.stderr:
            raise ManagedInstallation("Controller package ownership is unknown")
    return False


def _running_matches(target):
    running, installed = os.stat("/proc/self/exe"), target.stat()
    return (running.st_dev, running.st_ino) == (installed.st_dev, installed.st_ino)


def eligible(*, require_enabled=True):
    """Admission does not load profiles or create application state."""
    if require_enabled and not constants.APP_SELF_UPDATES_ENABLED:
        raise ManagedInstallation(constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
    if (constants.RUNMODE != "binary" or constants.CONTROLLER_DISTRIBUTION != "standalone-onefile"
            or not getattr(sys, "frozen", False) or not hasattr(sys, "_MEIPASS")):
        raise ManagedInstallation("Use the installation method to update this controller")
    invocation = Path(sys.executable)
    if not invocation.is_absolute():
        raise ManagedInstallation("Controller executable identity is not absolute")
    target = invocation.resolve(strict=True)
    home = Path.home().resolve()
    if not target.is_relative_to(home) or Path(sys._MEIPASS).resolve().is_relative_to(target.parent):
        raise ManagedInstallation("Only a standalone executable in your home can self-update")
    _safe_parents(target)
    info = _regular(target)
    if not info.st_mode & stat.S_IXUSR or _package_owned(target):
        raise ManagedInstallation("Use the package manager to update this controller")
    if not _running_matches(target):
        raise UpdateError("Controller pathname no longer identifies the running executable")
    artifacts.validate_format(target, "elf")
    return target


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(path):
    info = _regular(path)
    return info.st_dev, info.st_ino, artifacts.regular_hash(path), stat.S_IMODE(info.st_mode)


class Destination:
    def __init__(self, target):
        self.target = Path(target)
        _safe_parents(self.target)
        info = self.target.parent.stat()
        self.parent = info.st_dev, info.st_ino
        self.state = maintenance.private_directory(self.target.parent / ".logos-desktop-controller")
        key = hashlib.sha256(os.fsencode(self.target.name)).hexdigest()
        self.record = self.state / f"{key}.json"
        self.descriptor = os.open(self.state / f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise UpdateError("Unsafe controller lock")
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.descriptor)
            self.descriptor = None
            raise

    def check_parent(self):
        _safe_parents(self.target)
        info = self.target.parent.stat()
        if (info.st_dev, info.st_ino) != self.parent:
            raise UpdateError("Controller directory changed during replacement")

    def write(self, record):
        self.check_parent()
        temporary = self.state / f".record-{secrets.token_hex(12)}"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(record, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.record)
            _sync(self.state)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def _copy(source, target, mode):
    """Copy into a new private sibling, never into an installed pathname."""
    temporary = target.parent / f".logos-desktop-stage-{secrets.token_hex(12)}"
    try:
        expected = artifacts.regular_hash(source)
        with source.open("rb") as incoming, temporary.open("xb") as output:
            shutil.copyfileobj(incoming, output)
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        if artifacts.regular_hash(temporary) != expected:
            raise UpdateError("Controller changed during sibling copying")
        artifacts.validate_format(temporary, "elf")
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def probe(path):
    """Bound output and reap the process group created only for this probe."""
    try:
        output, status = processes.capture([str(path), "--version"], env=fresh_environment(), timeout=15,
                                           limit=16384, label="Controller version probe")
        if status != 0:
            raise UpdateError("Controller version probe failed")
        text = output.decode("utf-8", errors="strict").strip()
        match = re.fullmatch(
            r"LogosDesktop, (\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?(?:\+[\w.-]+)?)(?: by [^\n]+)?", text,
        )
        if match is None:
            raise UpdateError("Controller did not report an exact LogosDesktop version")
        return match[1]
    except processes.CaptureError as error:
        raise UpdateError(str(error)) from error


@contextmanager
def idle(app):
    raw = app.conf._raw
    if not (getattr(app.conf._overrides, "install_dir", None) or raw.install_dir):
        yield
        return
    with maintenance.configured(app) as context, maintenance.use(context, exclusive=True):
        maintenance.assert_quiescent(context)
        yield


def resume_arguments(config_path=None, dialog=None):
    arguments = []
    if config_path:
        arguments.extend(["-c", str(Path(config_path).expanduser().absolute())])
    if dialog:
        dialog = {"curses": "tui", "cli": "tui"}.get(dialog, dialog)
        if dialog not in ("tk", "tui"):
            raise UpdateError("Unknown controller frontend for restart")
        arguments.extend(["--dialog", dialog])
    return arguments


@dataclass
class PreparedUpdate:
    destination: Destination
    stage: Path
    previous: tuple
    release: releases.SoftwareReleaseInfo
    app: object

    def cancel(self):
        self.stage.unlink(missing_ok=True)
        self.destination.close()

    def restart(self, arguments):
        if threading.current_thread() is not threading.main_thread():
            raise UpdateError("Controller restart must run on the main thread")
        destination, target = self.destination, self.destination.target
        backup = None
        record = None
        stopped = False
        try:
            if eligible() != target:
                raise UpdateError("Running controller target changed")
            with idle(self.app):
                destination.check_parent()
                if _identity(target) != self.previous:
                    raise UpdateError("Installed controller changed during preparation")
                artifacts._verify(self.stage, self.release.spec("elf"))
                processes.shutdown()
                stopped = True
                backup = _copy(target, target, self.previous[3])
                backup_name = f".logos-desktop-previous-{secrets.token_hex(12)}"
                os.rename(backup, target.parent / backup_name)
                backup = target.parent / backup_name
                record = {"schema": 1, "target": target.name, "parent": list(destination.parent),
                          "backup": backup.name, "old_hash": self.previous[2],
                          "new_hash": artifacts.regular_hash(self.stage),
                          "mode": self.previous[3], "version": self.release.version, "token": secrets.token_hex(24)}
                destination.write(record)
                destination.check_parent()
                if _identity(target) != self.previous:
                    raise UpdateError("Installed controller changed before commit")
                os.replace(self.stage, target)
                _sync(target.parent)
                env = fresh_environment()
                env[TOKEN_VARIABLE] = record["token"]
                self.app.status(f"Restarting LogosDesktop. Previous executable: {backup}", 0)
                os.execve(target, [str(target), *arguments], env)
                raise UpdateError("Controller restart returned unexpectedly")
        except BaseException as error:
            if record is not None:
                try:
                    _rollback(destination, record)
                except BaseException as rollback_error:
                    raise UpdateError(f"Controller recovery is required. Run {backup}. "
                                      f"Preserved record: {destination.record}") from rollback_error
            elif backup is not None:
                backup.unlink(missing_ok=True)
            raise error
        finally:
            self.cancel()
            if stopped:
                processes.resume()


def prepare(app):
    target = eligible()
    if constants.APP_RELEASE_VERSION is None:
        raise UpdateError("This private build has no LogosDesktop release version")
    destination = Destination(target)
    stage = None
    try:
        if os.path.lexists(destination.record):
            raise UpdateError("Controller replacement needs recovery before another update")
        with idle(app), maintenance.artifact_use():
            lookup = app.conf._network.controller_release(app.conf.app_release_channel, force=True)
            release = lookup.release
            if lookup.status != "ok" or lookup.stale or release is None or release.error:
                raise UpdateError(lookup.message)
            if releases.version_key(release.version) <= releases.version_key(constants.APP_RELEASE_VERSION):
                raise UpdateError("No newer LogosDesktop release is available")
            spec = release.spec("elf")
            if release.repository != constants.REPOSITORY_SLUG or spec.digest is None:
                raise UpdateError("Controller release lineage or digest is missing")
            previous = _identity(target)
            root = Path(app.conf.download_dir)
            accepted = artifacts.acquire(spec, root, [root / release.name], app.status)
            accepted.check()
            stage = _copy(accepted.path, target, previous[3])
            artifacts._verify(stage, spec)
            if probe(stage) != release.version:
                raise UpdateError("Controller artifact has the wrong version")
            artifacts._verify(stage, spec)
            return PreparedUpdate(destination, stage, previous, release, app)
    except BaseException:
        if stage is not None:
            stage.unlink(missing_ok=True)
        destination.close()
        raise


def _read_record(destination):
    path = destination.record
    info = _regular(path)
    if info.st_size > 8192 or stat.S_IMODE(info.st_mode) != 0o600:
        raise UpdateError("Unsafe controller recovery record")
    record = json.loads(path.read_text())
    if (not isinstance(record, dict) or record.get("schema") != 1 or record.get("target") != destination.target.name
            or record.get("parent") != list(destination.parent)
            or re.fullmatch(r"\.logos-desktop-previous-[0-9a-f]{24}", record.get("backup", "")) is None
            or any(re.fullmatch(r"[0-9a-f]{64}", record.get(key, "")) is None for key in ("old_hash", "new_hash"))
            or re.fullmatch(r"[0-9a-f]{48}", record.get("token", "")) is None
            or type(record.get("mode")) is not int or record["mode"] & ~0o755
            or releases.SEMVER.fullmatch(record.get("version", "")) is None):
        raise UpdateError("Invalid controller recovery identity")
    backup = destination.target.parent / record["backup"]
    _regular(backup)
    if artifacts.regular_hash(backup) != record["old_hash"]:
        raise UpdateError("Previous controller backup changed")
    return record


def _rollback(destination, record):
    destination.check_parent()
    current = artifacts.regular_hash(destination.target)
    if current not in (record["old_hash"], record["new_hash"]):
        raise UpdateError("Refusing to overwrite a changed controller target")
    if current == record["new_hash"]:
        backup = destination.target.parent / record["backup"]
        if artifacts.regular_hash(backup) != record["old_hash"]:
            raise UpdateError("Previous controller backup changed")
        temporary = _copy(backup, destination.target, record["mode"])
        try:
            os.replace(temporary, destination.target)
            _sync(destination.target.parent)
        finally:
            temporary.unlink(missing_ok=True)
    destination.record.unlink(missing_ok=True)
    _sync(destination.state)


def recover_startup(arguments):
    """Reconcile only this executable before profile or library initialization."""
    global _startup, _handoff
    if constants.CONTROLLER_DISTRIBUTION != "standalone-onefile":
        return
    target = Path(sys.executable).resolve(strict=True)
    state = target.parent / ".logos-desktop-controller"
    key = hashlib.sha256(os.fsencode(target.name)).hexdigest()
    if not os.path.lexists(state / f"{key}.json"):
        return
    if eligible(require_enabled=False) != target:
        raise UpdateError("Pending controller recovery target changed")
    destination = Destination(target)
    try:
        record = _read_record(destination)
        current = artifacts.regular_hash(target)
        if (os.environ.pop(TOKEN_VARIABLE, None) == record["token"] and current == record["new_hash"]
                and constants.APP_RELEASE_VERSION == record["version"]):
            _startup = destination, record
            _handoff = True
            return
        _rollback(destination, record)
        if current == record["new_hash"]:
            os.execve(target, [str(target), *arguments], fresh_environment())
            raise UpdateError("Recovered controller restart returned unexpectedly")
    finally:
        if _startup is None:
            destination.close()


def confirm_startup():
    global _startup
    if _startup is None:
        return
    destination, record = _startup
    try:
        destination.check_parent()
        if artifacts.regular_hash(destination.target) != record["new_hash"]:
            raise UpdateError("Updated controller changed before startup confirmation")
        destination.record.unlink()
        _sync(destination.state)
    finally:
        destination.close()
        _startup = None


def startup_handoff():
    return _handoff


def copy_launcher(source, target):
    try:
        running = eligible(require_enabled=False)
    except ManagedInstallation as error:
        logging.info("Skipping unmanaged launcher copy: %s", error)
        return None
    source, target = Path(source).resolve(strict=True), Path(target).absolute()
    if source != running:
        raise UpdateError("Launcher source is not the running standalone controller")
    if target.exists() and os.path.samefile(source, target):
        return target
    _safe_parents(target)
    if not target.is_relative_to(Path.home().resolve()) or _package_owned(target):
        raise ManagedInstallation("Cannot create an unmanaged launcher at this destination")
    destination = Destination(target)
    temporary = None
    backup = None
    record = None
    try:
        if os.path.lexists(destination.record):
            raise UpdateError("Launcher needs controller recovery first")
        before = _identity(target) if target.exists() else None
        temporary = _copy(source, target, stat.S_IMODE(_regular(source).st_mode))
        destination.check_parent()
        if (_identity(target) if target.exists() else None) != before:
            raise UpdateError("Launcher destination changed during copying")
        if before is not None:
            backup = _copy(target, target, before[3])
            backup_name = f".logos-desktop-previous-{secrets.token_hex(12)}"
            os.rename(backup, target.parent / backup_name)
            backup = target.parent / backup_name
            record = {"schema": 1, "target": target.name, "parent": list(destination.parent),
                      "backup": backup.name, "old_hash": before[2], "new_hash": artifacts.regular_hash(temporary),
                      "mode": before[3], "version": constants.APP_RELEASE_VERSION or constants.LLI_CURRENT_VERSION,
                      "token": secrets.token_hex(24)}
            destination.write(record)
        os.replace(temporary, target)
        _sync(target.parent)
        destination.record.unlink(missing_ok=True)
        _sync(destination.state)
        return target
    except BaseException as error:
        if record is not None:
            try:
                _rollback(destination, record)
            except BaseException as rollback_error:
                raise UpdateError(f"Launcher recovery is required. Run {backup}. "
                                  f"Preserved record: {destination.record}") from rollback_error
        elif backup is not None:
            backup.unlink(missing_ok=True)
        raise error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        destination.close()
