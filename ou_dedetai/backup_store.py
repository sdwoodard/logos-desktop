"""Checked data copying and restore recovery for the three user-data roots."""

from contextlib import AbstractContextManager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

from ou_dedetai import maintenance, paths


ROOTS = ("Data", "Documents", "Users")
SCHEMA = 1
MARKER = ".logos-desktop-backups"


class BackupError(maintenance.MaintenanceError):
    """A backup or restore did not complete."""


class CopyCancelled(BackupError):
    """The operation stopped before publication."""


def checkpoint(stop):
    if stop is not None and stop.is_set():
        raise CopyCancelled("Operation cancelled before publication")


def identity(info):
    return [info.st_dev, info.st_ino]


def entry_identity(path):
    try:
        return identity(path.lstat())
    except FileNotFoundError:
        return None


def mount_identity(path: Path):
    resolved = path.resolve(strict=True)
    selected = None
    with open("/proc/self/mountinfo") as source:
        for line in source:
            fields = line.split()
            mount = fields[4]
            for encoded, character in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
                mount = mount.replace(encoded, character)
            if resolved.is_relative_to(mount) and (selected is None or len(mount) > selected[0]):
                selected = (len(mount), fields[0], fields[2], fields[3], mount)
    if selected is None:
        raise BackupError(f"Cannot identify the filesystem for {path}")
    return selected[1:]


class Directory(AbstractContextManager):
    """Hold a directory so renamed ancestors cannot redirect transaction writes."""

    def __init__(self, path: Path):
        self.original = Path(path)
        self.virtual = self.original.parent == Path("/proc/self/fd") and self.original.name.isdigit()
        self.fd = (
            os.dup(int(self.original.name)) if self.virtual
            else os.open(self.original, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        )
        try:
            self.info = os.fstat(self.fd)
            if self.info.st_uid != os.geteuid():
                raise BackupError(f"Directory is not owned by the current user: {path}")
            self.mount = mount_identity(self.path)
            self.validate()
        except BaseException:
            os.close(self.fd)
            raise

    @property
    def path(self):
        return Path(f"/proc/self/fd/{self.fd}")

    def validate(self):
        if identity(self.original.stat()) != identity(self.info) or (not self.virtual and self.original.is_symlink()):
            raise BackupError(f"Directory changed during the operation: {self.original}")
        if mount_identity(self.original) != self.mount:
            raise BackupError(f"Filesystem changed during the operation: {self.original}")

    def __exit__(self, *args):
        os.close(self.fd)


def read_json(path: Path, stop=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise BackupError(f"Metadata is not an owned regular file: {path}")
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise BackupError("Duplicate metadata key")
                result[key] = value
            return result
        chunks = []
        while True:
            checkpoint(stop)
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        result = json.loads("".join(chunks), object_pairs_hook=unique_keys)
        checkpoint(stop)
        return result


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(parent: Path, name: str, value):
    temporary = parent / (".metadata-" + secrets.token_hex(12))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as target:
            json.dump(value, target, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, parent / name)
        sync_directory(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _hash_file(path, stop=None, target=None, progress=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise BackupError(f"Unsupported data entry: {path}")
        size = 0
        while True:
            if stop is not None and stop.is_set():
                raise CopyCancelled("Copy cancelled. Current installation is unchanged")
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
            if target is not None:
                target.write(block)
            if progress is not None:
                progress(len(block))
        after = os.fstat(source.fileno())
        if (
            identity(before) != identity(after) or before.st_size != size
            or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise BackupError(f"Source changed while reading: {path}")
        return {"kind": "file", "size": size, "sha256": digest.hexdigest()}


def inventory(root: Path, stop=None):
    entries = {}
    states = {}
    with Directory(root) as directory:
        def visit(parent, relative):
            checkpoint(stop)
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_dev != directory.info.st_dev:
                raise BackupError(f"Linked or mounted data entry is unsupported: {relative}")
            if stat.S_ISDIR(info.st_mode):
                if mount_identity(parent) != directory.mount:
                    raise BackupError(f"Nested data mount is unsupported: {relative}")
                entries[relative] = {"kind": "directory"}
                with Directory(parent) as held:
                    for name in sorted(os.listdir(held.fd)):
                        visit(held.path / name, relative + "/" + name)
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = _hash_file(parent, stop)
            else:
                raise BackupError(f"Unsupported data entry: {relative}")

        for name in ROOTS:
            checkpoint(stop)
            child = directory.path / name
            states[name] = os.path.lexists(child)
            if states[name]:
                if not stat.S_ISDIR(child.lstat().st_mode):
                    raise BackupError(f"Data root must be an unlinked directory: {name}")
                visit(child, name)
        directory.validate()
    return {"roots": states, "entries": entries}


def validate_manifest(manifest, product, stop=None):
    if (
        not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
        or manifest.get("product") != product or not isinstance(manifest.get("id"), str)
        or not isinstance(manifest.get("created"), str)
    ):
        raise BackupError("Backup metadata is unsupported or belongs to another product")
    roots = manifest.get("roots")
    entries = manifest.get("entries")
    if not isinstance(roots, dict) or set(roots) != set(ROOTS) or not isinstance(entries, dict):
        raise BackupError("Backup has incomplete root metadata")
    if not any(roots.values()) or any(type(value) is not bool for value in roots.values()):
        raise BackupError("Backup has invalid root states")
    for name, record in entries.items():
        checkpoint(stop)
        if not isinstance(name, str):
            raise BackupError("Backup inventory has an invalid path")
        parts = name.split("/")
        if (
            not parts or parts[0] not in ROOTS or not roots[parts[0]]
            or any(part in ("", ".", "..") for part in parts) or "\0" in name
            or not isinstance(record, dict)
        ):
            raise BackupError("Backup inventory escapes its data roots")
        if len(parts) > 1 and entries.get("/".join(parts[:-1])) != {"kind": "directory"}:
            raise BackupError("Backup inventory has a missing parent")
        if record == {"kind": "directory"}:
            continue
        if (
            set(record) != {"kind", "size", "sha256"} or record["kind"] != "file"
            or type(record["size"]) is not int or record["size"] < 0
            or not isinstance(record["sha256"], str) or len(record["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in record["sha256"])
        ):
            raise BackupError("Backup inventory has an invalid file record")
    for name, present in roots.items():
        if present and entries.get(name) != {"kind": "directory"}:
            raise BackupError("Backup is missing a declared data root")


def verify_payload(path, manifest, stop=None):
    actual = inventory(path, stop)
    if actual != {"roots": manifest["roots"], "entries": manifest["entries"]}:
        raise BackupError("Backup files do not match their completed manifest")


def verify_capacity(directory: Directory, manifest):
    usage = os.fstatvfs(directory.fd)
    required = sum(
        ((entry.get("size", 0) + 4095) // 4096 + 1) * 4096
        for entry in manifest["entries"].values()
    )
    required += max(1024 * 1024, len(json.dumps(manifest).encode()) * 2)
    if usage.f_flag & os.ST_RDONLY or usage.f_bavail * usage.f_frsize < required:
        raise BackupError("Destination is read-only or has insufficient space for the complete staged copy")


def copy_payload(source: Path, destination: Path, manifest, stop=None, progress=None, phase=None):
    with Directory(source) as incoming, Directory(destination) as target:
        for name, expected in sorted(manifest["entries"].items()):
            if stop is not None and stop.is_set():
                raise CopyCancelled("Copy cancelled")
            parent_parts = name.split("/")[:-1]
            source_parent = incoming.path
            target_parent = target.path
            held = []
            try:
                for part in parent_parts:
                    source_directory = Directory(source_parent / part)
                    held.append(source_directory)
                    target_directory = Directory(target_parent / part)
                    held.append(target_directory)
                    source_parent = source_directory.path
                    target_parent = target_directory.path
                basename = name.split("/")[-1]
                if expected["kind"] == "directory":
                    os.mkdir(target_parent / basename, mode=0o700)
                else:
                    descriptor = os.open(target_parent / basename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        actual = _hash_file(source_parent / basename, stop, output, progress)
                        output.flush()
                        os.fsync(output.fileno())
                    if actual != expected:
                        raise BackupError(f"Source no longer matches the snapshot: {name}")
                sync_directory(target_parent)
            finally:
                for directory in reversed(held):
                    directory.__exit__()
        incoming.validate()
        target.validate()
    if phase:
        phase("Verifying staged data…", None)
    verify_payload(destination, manifest, stop)


def new_manifest(source, product, stop=None, *, source_identity=None):
    manifest = {
        "schema": SCHEMA, "id": secrets.token_hex(16), "product": product,
        "created": datetime.now(UTC).isoformat(), **inventory(source, stop),
    }
    if source_identity is not None:
        manifest["source"] = str(source_identity)
    validate_manifest(manifest, product, stop)
    return manifest


def bind_root(directory: Directory, context, app):
    key = hashlib.sha256(os.fsencode(directory.original.resolve())).hexdigest()
    bindings = maintenance.private_directory(context.state / "backup-roots")
    record = bindings / (key + ".json")
    marker = directory.path / MARKER
    if os.path.lexists(record):
        binding = read_json(record)
        if not os.path.lexists(marker) or read_json(marker) != binding:
            raise BackupError(
                "Backup disk or folder binding is missing. Reconnect the original disk or select another folder"
            )
        return
    if getattr(app.conf._overrides, "assume_yes", False):
        raise BackupError("Confirm a new backup folder interactively before using assume-yes")
    context_text = f"Folder: {directory.original}\nFilesystem: {directory.mount[-1]}"
    if not app.approve("Bind this backup folder on its currently mounted filesystem?", context_text):
        raise CopyCancelled("Backup folder selection cancelled")
    directory.validate()
    if os.path.lexists(marker):
        binding = read_json(marker)
        if not isinstance(binding, dict) or set(binding) != {"schema", "token"} or binding["schema"] != SCHEMA:
            raise BackupError("Backup folder has an unsupported ownership marker")
    else:
        binding = {"schema": SCHEMA, "token": secrets.token_hex(24)}
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(binding, output)
            output.flush()
            os.fsync(output.fileno())
        sync_directory(directory.path)
    write_json(bindings, record.name, binding)


def _move(source, destination):
    """Move only into vacant private entries, never replace an unexpected tree."""
    if os.path.lexists(destination):
        raise maintenance.RecoveryRequired(f"Recovery destination is occupied: {destination}")
    paths.rename_no_replace(source, destination)
    sync_directory(source.parent)
    sync_directory(destination.parent)


def _journal(context, record):
    write_json(context.state, "restore.json", record)


def _check_record(context, record):
    if (
        not isinstance(record, dict) or record.get("schema") != SCHEMA
        or record.get("prefix") != str(context.prefix)
        or record.get("phase") not in ("prepared", "committed", "rolled-back")
    ):
        raise maintenance.RecoveryRequired("Restore journal is invalid. Keep the preserved data for manual recovery")
    live = Path(record["live"])
    transaction = Path(record["transaction"])
    if (
        not live.is_absolute() or not transaction.is_absolute() or live.parent != transaction.parent
        or not transaction.name.startswith(".logos-desktop-restore-")
        or not live.is_relative_to(context.prefix) or not paths.disjoint(live, transaction)
        or set(record["old"]) != set(ROOTS) or set(record["new"]) != set(ROOTS)
    ):
        raise maintenance.RecoveryRequired("Restore journal paths are inconsistent")
    return live, transaction


def _recover(context, record):
    live_path, transaction_path = _check_record(context, record)
    expected = record["new"] if record["phase"] == "committed" else record["old"]
    if not os.path.lexists(transaction_path):
        with Directory(live_path) as live:
            if identity(live.info) != record["live_identity"] or any(
                entry_identity(live.path / name) != expected[name] for name in ROOTS
            ):
                raise maintenance.RecoveryRequired("Active roots conflict with completed recovery")
        context.pending.unlink(missing_ok=True)
        sync_directory(context.state)
        (context.state / "restore.json").unlink(missing_ok=True)
        sync_directory(context.state)
        return
    with Directory(live_path) as live, Directory(transaction_path) as transaction:
        if (
            identity(live.info) != record["live_identity"]
            or identity(transaction.info) != record["transaction_identity"]
        ):
            raise maintenance.RecoveryRequired("Restore directories changed. Original data is retained")
        if record["phase"] == "prepared":
            with Directory(transaction.path / "old") as old, Directory(transaction.path / "new") as new:
                if (
                    identity(old.info) != record["old_directory_identity"]
                    or identity(new.info) != record["new_directory_identity"]
                ):
                    raise maintenance.RecoveryRequired("Restore storage identities changed")
                for name in reversed(ROOTS):
                    original = record["old"][name]
                    replacement = record["new"][name]
                    current = entry_identity(live.path / name)
                    saved = entry_identity(old.path / name)
                    if current == replacement and replacement is not None:
                        _move(live.path / name, new.path / name)
                        current = None
                    if saved is not None:
                        if saved != original or current is not None:
                            raise maintenance.RecoveryRequired(
                                f"Conflicting {name} entry. Original data is retained at {transaction_path}"
                            )
                        _move(old.path / name, live.path / name)
                    elif current != original:
                        raise maintenance.RecoveryRequired(
                            f"Cannot prove the original {name} location. Keep {transaction_path}"
                        )
                record["phase"] = "rolled-back"
                _journal(context, record)
        if any(entry_identity(live.path / name) != expected[name] for name in ROOTS):
            raise maintenance.RecoveryRequired(f"Active roots conflict with recovery state. Keep {transaction_path}")
        live.validate()
        transaction.validate()
        for name in ("old", "new"):
            child = transaction.path / name
            if os.path.lexists(child):
                with Directory(child) as held:
                    if identity(held.info) != record[name + "_directory_identity"]:
                        raise maintenance.RecoveryRequired("Restore cleanup storage changed")
                shutil.rmtree(child)
    transaction_path.rmdir()
    sync_directory(transaction_path.parent)
    context.pending.unlink(missing_ok=True)
    sync_directory(context.state)
    (context.state / "restore.json").unlink(missing_ok=True)
    sync_directory(context.state)


def recover_pending_restore(context, app=None):
    if not context.needs_recovery():
        return
    with maintenance.maintain(app, context, recovery=True):
        try:
            record = read_json(context.state / "restore.json")
            _journal(context, record)
            _recover(context, record)
        except maintenance.RecoveryRequired:
            raise
        except Exception as error:
            raise maintenance.RecoveryRequired(
                f"Restore recovery needs attention. Preserved state: {context.state}"
            ) from error


def restore_payload(context, live_path, payload, manifest, stop=None, progress=None, precommit=None, phase=None):
    with Directory(live_path) as live, Directory(live_path.parent) as parent:
        verify_capacity(parent, manifest)
        name = ".logos-desktop-restore-" + secrets.token_hex(16)
        os.mkdir(parent.path / name, mode=0o700)
        transaction_path = live_path.parent / name
        pending = False
        record = None
        try:
            with Directory(parent.path / name) as transaction:
                for child in ("new", "old"):
                    os.mkdir(transaction.path / child, mode=0o700)
                    sync_directory(transaction.path / child)
                sync_directory(transaction.path)
                sync_directory(parent.path)
                copy_payload(payload, transaction.path / "new", manifest, stop, progress, phase=phase)
                if phase:
                    phase("Verifying restore source and current data…", None)
                verify_payload(payload, manifest, stop)
                inventory(live.path, stop)
                live.validate()
                parent.validate()
                maintenance.assert_quiescent(context)
                if precommit is not None:
                    precommit()
                checkpoint(stop)
                if phase:
                    phase("Replacing verified data roots…", None)
                record = {
                    "schema": SCHEMA, "prefix": str(context.prefix), "live": str(live_path),
                    "transaction": str(transaction_path), "live_identity": identity(live.info),
                    "transaction_identity": identity(transaction.info), "phase": "prepared",
                    "old_directory_identity": entry_identity(transaction.path / "old"),
                    "new_directory_identity": entry_identity(transaction.path / "new"),
                    "old": {root: entry_identity(live.path / root) for root in ROOTS},
                    "new": {root: entry_identity(transaction.path / "new" / root) for root in ROOTS},
                }
                _journal(context, record)
                descriptor = os.open(context.pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                pending = True
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                sync_directory(context.state)
                for root in ROOTS:
                    live.validate()
                    parent.validate()
                    if entry_identity(live.path / root) != record["old"][root]:
                        raise BackupError("Live data changed before replacement")
                    if record["old"][root] is not None:
                        _move(live.path / root, transaction.path / "old" / root)
                    if record["new"][root] is not None:
                        _move(transaction.path / "new" / root, live.path / root)
                # Finish or roll back the replacement once the journal is durable.
                verify_payload(live.path, manifest)
                record["phase"] = "committed"
                _journal(context, record)
        except BaseException:
            if pending:
                try:
                    persisted = read_json(context.state / "restore.json")
                    if persisted["phase"] == "committed":
                        raise maintenance.RecoveryRequired(
                            f"Restore commit persistence is uncertain. Preserved recovery data: {transaction_path}"
                        )
                    _recover(context, persisted)
                except BaseException as recovery_error:
                    raise maintenance.RecoveryRequired(
                        f"Restore interrupted. Original data remains at {transaction_path}"
                    ) from recovery_error
            else:
                shutil.rmtree(parent.path / name)
            raise
        try:
            _recover(context, record)
        except Exception:
            return f"Restore committed. Cleanup remains at {transaction_path}. Recovery will retry on next startup"
    return None
