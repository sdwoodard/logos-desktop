"""Read-only disk accounting for registered runtime copies belonging to one source."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from . import artifacts, maintenance, runtime_copies, runtime_filesystem, runtime_recovery, runtime_trials


@dataclass(frozen=True)
class Retained:
    purpose: str
    location: str
    logical: int | None
    allocated: int | None


def _raise(error):
    raise error


def measure(purpose, root, expected, checkpoint):
    if not expected:
        return Retained(purpose, str(root), None, None)
    logical = allocated = count = 0
    linked = set()
    try:
        with runtime_recovery.export_directory(root) as (descriptor, identity):
            if not runtime_filesystem.same_retained_root(expected, identity):
                raise maintenance.RecoveryRequired("Retained copy identity changed")
            allocated += os.fstat(descriptor).st_blocks * 512
            for _, dirs, files, directory in os.fwalk(".", dir_fd=descriptor, onerror=_raise):
                checkpoint()
                if runtime_recovery.export_mount_id(directory) != identity["mount_id"]:
                    raise maintenance.RecoveryRequired("Retained copy contains a separate mount")
                for name in (*dirs, *files):
                    count += 1
                    if count > runtime_copies.MAX_ENTRIES:
                        raise maintenance.RecoveryRequired("Retained copy exceeds the measurement budget")
                    item = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISREG(item.st_mode):
                        logical += item.st_size
                    key = (item.st_dev, item.st_ino)
                    if item.st_nlink > 1 and not stat.S_ISDIR(item.st_mode):
                        if key in linked:
                            continue
                        linked.add(key)
                    allocated += item.st_blocks * 512
            runtime_recovery.check_export_directory(root, descriptor, identity)
    except (OSError, maintenance.MaintenanceError, RecursionError):
        return Retained(purpose, str(root), None, None)
    return Retained(purpose, str(root), logical, allocated)


def retained(context, *, checkpoint):
    items = []
    lookups = runtime_trials._lookup_root(context)
    if os.path.lexists(lookups):
        runtime_copies.checked_directory(lookups)
        for index, registration in enumerate(lookups.iterdir()):
            checkpoint()
            if index >= 4096:
                raise maintenance.RecoveryRequired("Too many retained trial registrations to measure")
            if registration.name == "latest.json" or registration.suffix != ".json":
                continue
            operation = runtime_recovery.identifier(registration.stem)
            entry = runtime_trials.read(registration)
            directory = runtime_copies.checked_directory(entry["directory"])
            record = runtime_trials.status(directory)
            if (entry["id"] != operation or directory.name != operation or record["id"] != operation
                    or record["source"] != str(context.prefix) or record["job_sha256"] != entry["job_sha256"]):
                raise maintenance.RecoveryRequired("Retained trial registration changed")
            purpose = "Trial copy (" + record["result"] + ")"
            if record["copy"]:
                items.append(measure(purpose, directory / "prefix", record["copy"]["copy"], checkpoint))
            elif os.path.lexists(directory / "prefix.partial") or os.path.lexists(directory / "prefix"):
                items.append(Retained(purpose, str(directory), None, None))

    recovery = runtime_recovery.Recovery(context)
    if not recovery.registered:
        return tuple(items)
    journal = recovery.status()
    journals = [journal]
    points = recovery.point_index()["points"]
    for point, binding in points.items():
        checkpoint()
        root = recovery.root / ("point-" + point) / "prefix"
        if binding["cleanup"]:
            items.append(Retained("Point cleanup pending", str(root), None, None))
            continue
        detail = recovery._point_record(point)
        items.append(measure("Recovery point " + detail["created"], root, detail["copy"], checkpoint))
        if binding["history"]:
            history = recovery.root / ("history-" + point + ".json")
            if artifacts.regular_hash(history) != binding["history"]:
                raise maintenance.RecoveryRequired("Point history changed during disk accounting")
            previous = runtime_recovery.read(history)
            runtime_recovery.validate_journal(previous, recovery.registered)
            journals.append(previous)
    for record in journals:
        if record["newer"] and os.path.lexists(record["newer"]):
            root = Path(record["newer"])
            saved = recovery._moved_identity(record["newer_identity"], root)
            items.append(measure("Retained newer installation", root, saved, checkpoint))
    for index, directory in enumerate(recovery.root.iterdir()):
        if index >= 4096:
            raise maintenance.RecoveryRequired("Too many recovery entries to measure")
        checkpoint()
        if directory.name.startswith("inspection-"):
            runtime_copies.checked_directory(directory)
            operation = runtime_recovery.identifier(directory.name.removeprefix("inspection-"))
            record = recovery.inspection_record(operation)
            items.append(measure("Offline inspection (" + record["result"] + ")", directory / "prefix",
                                 record["copy"], checkpoint))
        elif directory.name.startswith("point-") and directory.name[6:] not in points and directory.is_dir():
            items.append(Retained("Unverified recovery copy", str(directory), None, None))
    return tuple(items)
