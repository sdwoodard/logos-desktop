"""Allowlisted support snapshots and explicit private archive publication."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from zipfile import ZipFile

from . import backup_store, constants, doctor, maintenance


class SupportCancelled(maintenance.MaintenanceError):
    """Support preparation or publication was cancelled."""


@dataclass(frozen=True)
class Snapshot:
    entries: tuple[tuple[str, str], ...]
    protected: tuple[Path, ...] = ()

    @property
    def manifest(self):
        return json.loads(dict(self.entries)["manifest.json"])


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise SupportCancelled("Support export cancelled")


def _json(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def prepare(app, cancel=None):
    """Freeze only structured diagnostics, never raw source files or payloads."""
    _check_cancel(cancel)
    report = doctor.inspect(app.conf)
    _check_cancel(cancel)
    # All report strings are fixed vocabulary. No configured values are copied.
    entries = (("doctor.json", _json(report)), ("summary.txt", report["summary"] + "\n"))
    manifest = {
        "schema": 1,
        "kind": "logos-desktop-sanitized-support",
        "entries": [{"name": name, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                     "bytes": len(text.encode())} for name, text in entries],
        "omitted": ["Raw configuration", "Logs and crash reports with unknown free-form content",
                    "Environment and network cache", "Library databases, notes and user paths"],
    }
    protected = []
    for source, names in ((app.conf._overrides, ("config_path", "config_destination", "app_log_path",
                                                "app_wine_log_path")),
                          (app.conf, ("config_file_path", "app_log_path", "app_wine_log_path",
                                      "app_wine_log_previous_path", "_faithlife_crash_log"))):
        for name in names:
            value = vars(source).get(name)
            if isinstance(value, (str, Path)) and str(value):
                protected.append(Path(value).expanduser().absolute())
    for path in doctor.selected_paths(app.conf):
        if path is not None and path.is_absolute():
            protected.append(path)
    selected_runtime = doctor.stored(app.conf, "wine_appimage_path") or doctor.stored(app.conf, "wine_binary")
    if isinstance(selected_runtime, (str, Path)) and str(selected_runtime):
        selected_runtime = Path(selected_runtime)
        install, _, _ = doctor.selected_paths(app.conf)
        if not selected_runtime.is_absolute() and install is not None:
            selected_runtime = install / selected_runtime
        if selected_runtime.is_absolute():
            protected.append(selected_runtime)
    protected.extend(Path(value).expanduser().absolute() for value in
                     (constants.CACHE_DIR, constants.STATE_DIR))
    return Snapshot((("manifest.json", _json(manifest)), *entries), tuple(protected))


def preview(snapshot):
    """Render every member name and its exact UTF-8 text before export."""
    return "".join(f"=== {name} ===\n{text}" for name, text in snapshot.entries)


def _target_info(parent, name):
    try:
        info = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise maintenance.MaintenanceError("Support output must be an owned regular file without aliases")
    return info


def export(snapshot, output, overwrite=False, cancel=None):
    """Validate a closed archive and atomically publish only approved snapshot bytes."""
    _check_cancel(cancel)
    output = Path(output).expanduser().absolute()
    if output.suffix.lower() != ".zip" or ".." in output.parts:
        raise maintenance.MaintenanceError("Support output must have a .zip filename and an unambiguous path")
    resolved = output.resolve()
    for source in snapshot.protected:
        if resolved == source.resolve() or resolved.is_relative_to(source.resolve()):
            raise maintenance.MaintenanceError("Support output overlaps protected application data")
    if any(parent.is_symlink() for parent in output.parents):
        raise maintenance.MaintenanceError("Support output cannot use symbolic-link directories")

    with backup_store.Directory(output.parent) as parent:
        if parent.info.st_mode & 0o022:
            raise maintenance.MaintenanceError("Support output directory must not be writable by other users")
        original = _target_info(parent, output.name)
        if original is not None and not overwrite:
            raise FileExistsError(output)
        temporary = ".support-" + secrets.token_hex(16)
        try:
            descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent.fd)
            with os.fdopen(descriptor, "w+b") as target:
                with ZipFile(target, "w") as archive:
                    for name, text in snapshot.entries:
                        _check_cancel(cancel)
                        archive.writestr(name, text.encode("utf-8"))
                target.flush()
                os.fsync(target.fileno())
                target.seek(0)
                with ZipFile(target) as archive:
                    if archive.namelist() != [name for name, _ in snapshot.entries]:
                        raise maintenance.MaintenanceError("Support archive member validation failed")
                    for name, text in snapshot.entries:
                        _check_cancel(cancel)
                        if archive.read(name) != text.encode("utf-8"):
                            raise maintenance.MaintenanceError("Support archive content validation failed")
            _check_cancel(cancel)
            parent.validate()
            current = _target_info(parent, output.name)
            if current != original:
                raise maintenance.MaintenanceError("Support output changed after confirmation")
            if overwrite:
                os.replace(temporary, output.name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
            else:
                # A hard link gives atomic no-clobber publication if a target appears late.
                os.link(temporary, output.name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
                os.unlink(temporary, dir_fd=parent.fd)
            os.fsync(parent.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent.fd)
            except FileNotFoundError:
                pass
    return output
