"""Read-only, bounded diagnostics shared by desktop and terminal callers."""

import os
from pathlib import Path
import platform
import stat

import psutil

from . import maintenance, runtime


def stored(conf, name, default=None):
    """Read stored fields without invoking Config properties."""
    for source in (conf._overrides, conf._raw):
        value = vars(source).get(name)
        if value is not None:
            return value
    return default


def selected_paths(conf):
    install = stored(conf, "install_dir")
    install = Path(install) if isinstance(install, (str, Path)) and str(install) else None
    prefix = stored(conf, "wine_prefix")
    prefix = Path(prefix) if isinstance(prefix, (str, Path)) and str(prefix) else None
    if prefix is None and install is not None:
        prefix = install / "data/wine64_bottle"
    backup = stored(conf, "backup_dir")
    backup = Path(backup) if isinstance(backup, (str, Path)) and str(backup) else None
    if backup is not None and not backup.is_absolute():
        backup = install / backup if install is not None else None
    return install, prefix, backup


def _directory_state(path):
    if path is None:
        return "unknown"
    if not path.is_absolute() or ".." in path.parts:
        return "invalid"
    try:
        return "available" if path.is_dir() else "missing"
    except (OSError, ValueError, RuntimeError):
        return "unknown"


def command_preview(conf):
    """Offer only a vetted system Wine version command, without running it."""
    selected = stored(conf, "wine_binary")
    if selected not in ("/usr/bin/wine", "/usr/bin/wine64"):
        return None
    try:
        path = Path(selected).resolve(strict=True)
        if not path.is_relative_to("/usr"):
            return None
        for entry in (path, *path.parents):
            info = entry.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            return None
        with path.open("rb") as source:
            header = source.read(20)
        if header[:5] != b"\x7fELF\x02" or header[18:20] != b"\x3e\x00":
            return None
        return [selected, "--version"]
    except OSError:
        return None


def _runtime_check(conf, install):
    selected = stored(conf, "wine_appimage_path") or stored(conf, "wine_binary")
    if not isinstance(selected, (str, Path)) or not str(selected):
        return "unknown", "No runtime selection is stored."
    path = Path(selected)
    if not path.is_absolute():
        if install is None or not install.is_absolute():
            return "unknown", "Runtime selection cannot be resolved without an installation."
        path = install / path
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return "warning", "Selected runtime is not a regular file. Select a supported Wine runtime."
            header = source.read(20)
        if header[:4] != b"\x7fELF":
            return "unknown", "Selected runtime is not an ELF binary. Compatibility is unverified."
        if len(header) < 20 or header[4:6] != b"\x02\x01" or header[18:20] != b"\x3e\x00":
            return "error", "Selected runtime is not a 64-bit x86 ELF. Select a supported Wine runtime."
        return "unknown", "Runtime has a 64-bit x86 ELF header. Wine version compatibility is unverified."
    except FileNotFoundError:
        return "warning", "Selected runtime file is missing. Select an available Wine runtime."
    except (OSError, ValueError, RuntimeError):
        return "unknown", "Selected runtime cannot be safely inspected. Compatibility is unverified."


def inspect(conf):
    """Return serializable facts and fixed guidance without repairing anything."""
    checks = []

    def add(name, status, summary):
        checks.append({"name": name, "status": status, "summary": summary})

    machine = platform.machine()
    supported = machine in ("x86_64", "amd64", "AMD64")
    add("architecture", "ok" if supported else "error",
        "64-bit x86 host detected." if supported else "This host is not supported. Use a 64-bit x86 host.")

    install, prefix, backup = selected_paths(conf)
    for name, path in (("installation", install), ("prefix", prefix), ("backup", backup)):
        state = _directory_state(path)
        add(name, "ok" if state == "available" else "warning",
            f"{name.capitalize()} directory is {state}.")

    preview = command_preview(conf)
    runtime_status, runtime_summary = _runtime_check(conf, install)
    add("runtime", runtime_status, runtime_summary)
    add("tools", "ok" if preview else "unknown",
        "A vetted system Wine command is available." if preview else "No vetted system Wine command is available.")

    free = None
    if _directory_state(install) == "available":
        try:
            space = os.statvfs(install)
            free = space.f_bavail * space.f_frsize
        except OSError:
            pass
    add("space", "unknown" if free is None else "warning" if free < 1024**3 else "ok",
        "Installation free space is unknown." if free is None else
        "Less than 1 GiB is free. Free space before maintenance." if free < 1024**3 else
        "At least 1 GiB is free. Installation and backup needs vary.")

    recovery = "unknown"
    if install is not None and prefix is not None and install.is_absolute() and prefix.is_absolute():
        try:
            context = maintenance.Context(prefix.resolve(), install.resolve())
            recovery = "pending" if context.needs_recovery() or runtime.pending(install) else "clear"
        except (OSError, ValueError, RuntimeError):
            pass
    add("recovery", "warning" if recovery == "pending" else "ok" if recovery == "clear" else "unknown",
        "Pending recovery. Use explicit repair before mutation." if recovery == "pending" else
        "No pending recovery record found." if recovery == "clear" else "Recovery state is unknown.")

    # Process environments are inspected only for prefix equality and never retained.
    process_state = "unknown"
    if prefix is not None and prefix.is_absolute():
        try:
            uncertain = False
            process_state = "idle"
            for count, process in enumerate(psutil.process_iter()):
                if count >= 2048:
                    uncertain = True
                    break
                try:
                    if process.uids().real != os.geteuid():
                        continue
                    value = process.environ().get("WINEPREFIX")
                    if value and Path(value).is_absolute() and Path(value).resolve() == prefix.resolve():
                        process_state = "active"
                        break
                except (psutil.AccessDenied, OSError, ValueError, RuntimeError):
                    uncertain = True
                except psutil.NoSuchProcess:
                    continue
            if uncertain and process_state != "active":
                process_state = "unknown"
        except (psutil.Error, OSError):
            process_state = "unknown"
    add("processes", "warning" if process_state == "active" else "unknown" if process_state == "unknown" else "ok",
        {"active": "A process uses the selected prefix.", "idle": "No selected-prefix process found.",
         "unknown": "Selected-prefix process state is unknown."}[process_state])

    return {"schema": 1, "checks": checks, "command_preview": preview,
            "summary": "\n".join(f"{item['name']}: {item['status']} - {item['summary']}" for item in checks)}
