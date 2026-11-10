"""Independent, verified directory copies for offline runtime operations."""

import base64
from contextlib import ExitStack
from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import heapq
import os
from pathlib import Path
import stat

from . import maintenance, paths, runtime_filesystem, runtime_stacks


MAX_ENTRIES = 10_000_000
METADATA_RESERVE = 64 * 1024 * 1024
FICLONE = 0x40049409
GETFLAGS = 0x80086601


class CopyError(maintenance.MaintenanceError):
    def __init__(self, message, reason="unsupported-layout"):
        super().__init__(message)
        self.reason = reason


class Cancelled(CopyError):
    def __init__(self):
        super().__init__("Temporary copy cancelled", "cancelled")


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def checked_directory(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise CopyError("Directory must have an absolute, unlinked path")
    for component in (path, *path.parents):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise CopyError("Directory ancestry contains a link or special file")
        shared_temporary = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in (0, os.geteuid()) or (stat.S_IMODE(info.st_mode) & 0o022 and not shared_temporary):
            raise CopyError("Directory ancestry is writable by another account")
    if path.stat().st_uid != os.geteuid():
        raise CopyError("Directory must belong to this account")
    return path


def _mount_paths():
    # mountinfo distinguishes bind mounts even when device numbers agree.
    with open("/proc/self/mountinfo", "rb") as source:
        for line in source:
            fields = line.split()
            value = fields[4]
            for code, replacement in ((b"\\040", b" "), (b"\\011", b"\t"),
                                      (b"\\012", b"\n"), (b"\\134", b"\\")):
                value = value.replace(code, replacement)
            yield os.fsdecode(value)


def layout(path):
    path = checked_directory(path)
    if any(Path(mount) == path or Path(mount).is_relative_to(path) for mount in _mount_paths()):
        raise CopyError("Prefix contains a mount boundary")
    return runtime_filesystem.identity(path)


def physical_disjoint(first, second):
    if first["filesystem_uuid"] != second["filesystem_uuid"]:
        return True
    return paths.disjoint(Path(first["filesystem_root"]), Path(second["filesystem_root"]))


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _open_root(root):
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _open_parent(root, relative):
    descriptor = os.dup(root)
    try:
        for component in relative.split(b"/")[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _metadata(descriptor, info):
    if info.st_uid != os.geteuid() or info.st_gid != os.getegid() or info.st_mode & 0o7000:
        raise CopyError("Unsupported ownership or privileged file mode")
    flags = bytearray(4)
    fcntl.ioctl(descriptor, GETFLAGS, flags)
    if int.from_bytes(flags, "little") & (0x10 | 0x20):
        raise CopyError("Immutable and append-only files are unsupported")
    attributes = []
    for name in sorted(os.listxattr(descriptor)):
        if not name.startswith("user."):
            raise CopyError("Unsupported ACL or security metadata")
        attributes.append([encoded(os.fsencode(name)), encoded(os.getxattr(descriptor, name))])
    return attributes


def _link_policy(relative, target, root):
    if b"\0" in target:
        raise CopyError("Invalid symlink target")
    if relative == b"dosdevices/z:" and target == b"/":
        return
    logical = Path(os.fsdecode(root)) / os.fsdecode(relative)
    resolved = Path(os.path.normpath(logical.parent / os.fsdecode(target)))
    if resolved.is_relative_to(Path(os.fsdecode(root))):
        return
    parts = relative.split(b"/")
    folders = (b"Desktop", b"Documents", b"Downloads", b"Music", b"Pictures", b"Videos")
    if (len(parts) == 4 and parts[:2] == [b"drive_c", b"users"] and parts[3] in folders
            and resolved == Path.home() / os.fsdecode(parts[3])):
        return
    raise CopyError("External mutable symlink target is unsupported")


def entries(root, *, checkpoint=lambda: None, logical_root=None, observe=False):
    """Yield canonical entries without following any directory or file link."""
    root = os.fsencode(root)
    descriptor = _open_root(root)
    root_info = os.fstat(descriptor)
    pending = [b""]
    count = 0
    try:
        while pending:
            checkpoint()
            relative = heapq.heappop(pending)
            count += 1
            if count > MAX_ENTRIES:
                raise CopyError("Manifest entry limit exceeded")
            parent = _open_parent(descriptor, relative) if relative else os.dup(descriptor)
            name = relative.rsplit(b"/", 1)[-1] if relative else b"."
            try:
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if before.st_dev != root_info.st_dev:
                    raise CopyError("Filesystem changed during traversal")
                mode = stat.S_IMODE(before.st_mode)
                item = {"path": encoded(relative), "mode": mode, "uid": before.st_uid,
                        "gid": before.st_gid, "mtime_ns": before.st_mtime_ns,
                        "size": None, "sha256": None, "target": None, "xattrs": []}
                if stat.S_ISLNK(before.st_mode):
                    target = os.readlink(name, dir_fd=parent)
                    _link_policy(relative, target, os.fsencode(logical_root) if logical_root else root)
                    if before.st_uid != os.geteuid() or before.st_gid != os.getegid():
                        raise CopyError("Unsupported symlink ownership")
                    if os.listxattr(f"/proc/self/fd/{parent}/" + os.fsdecode(name), follow_symlinks=False):
                        raise CopyError("Symlink extended metadata is unsupported")
                    item.update(kind="symlink", target=encoded(target))
                elif stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if stat.S_ISDIR(before.st_mode):
                        flags |= os.O_DIRECTORY
                    child = os.open(name, flags, dir_fd=parent)
                    try:
                        if _signature(os.fstat(child)) != _signature(before):
                            raise CopyError("Source changed while opening", "source-changed")
                        item["xattrs"] = _metadata(child, before)
                        if stat.S_ISDIR(before.st_mode):
                            item["kind"] = "directory"
                            for entry in os.listdir(child):
                                component = os.fsencode(entry)
                                heapq.heappush(pending, relative + b"/" + component if relative else component)
                        else:
                            if before.st_nlink != 1:
                                raise CopyError("Hardlinked writable files are unsupported")
                            digest = hashlib.sha256()
                            while chunk := os.read(child, 1024 * 1024):
                                checkpoint()
                                digest.update(chunk)
                            item.update(kind="file", size=before.st_size, sha256=digest.hexdigest())
                        if _signature(os.fstat(child)) != _signature(before):
                            raise CopyError("Source changed during inspection", "source-changed")
                    finally:
                        os.close(child)
                else:
                    raise CopyError("Special files are unsupported")
                if _signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _signature(before):
                    raise CopyError("Source changed during traversal", "source-changed")
                if observe:
                    item["observation"] = _signature(before)
                    item["allocated"] = before.st_blocks * 512
                yield item
            finally:
                os.close(parent)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class Manifest:
    sha256: str
    entries: int
    logical_bytes: int
    allocated_bytes: int = field(compare=False)
    observation_sha256: str = field(compare=False)
    schema: int = 1
    format: str = "posix-base64-jsonl-v1"


def manifest(root, output=None, *, checkpoint=lambda: None, logical_root=None):
    digest = hashlib.sha256()
    observations = hashlib.sha256()
    count = logical = allocated = 0
    for item in entries(root, checkpoint=checkpoint, logical_root=logical_root, observe=True):
        observations.update(runtime_stacks.canonical(item.pop("observation")))
        allocated += item.pop("allocated")
        content = runtime_stacks.canonical(item) + b"\n"
        digest.update(content)
        count += 1
        logical += item["size"] or 0
        if output is not None:
            output.write(content)
    return Manifest(digest.hexdigest(), count, logical, allocated, observations.hexdigest())


def require_capacity(destination, logical_bytes, count=0):
    facts = os.statvfs(destination)
    required = logical_bytes + count * 4096 + METADATA_RESERVE
    available = facts.f_bavail * facts.f_frsize
    if available < required or (facts.f_files and facts.f_favail < count + 16):
        raise CopyError("Insufficient space for an independent full copy", "insufficient-space")
    return {"required": required, "available": available}


def _restore_metadata(descriptor, item):
    os.fchmod(descriptor, item["mode"])
    for name, value in item["xattrs"]:
        os.setxattr(descriptor, base64.b64decode(name), base64.b64decode(value))
    os.utime(descriptor, ns=(item["mtime_ns"], item["mtime_ns"]))


def copy(source, destination, *, checkpoint=lambda: None, status=lambda value: None, reflink=True):
    """Publish only a verified independent copy, preserving failures for owned cleanup."""
    source = checked_directory(source)
    if not Path(destination).is_absolute() or ".." in Path(destination).parts:
        raise CopyError("Copy destination must be an unambiguous absolute path")
    parent = checked_directory(Path(destination).parent)
    destination = parent / Path(destination).name
    if not paths.disjoint(source, destination):
        raise CopyError("Copy paths overlap")
    source_identity = layout(source)
    parent_identity = layout(parent)
    prospective = dict(parent_identity,
                       filesystem_root=str(Path(parent_identity["filesystem_root"]) / destination.name))
    if not physical_disjoint(source_identity, prospective):
        raise CopyError("Physical copy paths overlap through a filesystem alias")
    before = manifest(source, checkpoint=checkpoint)
    require_capacity(parent, before.logical_bytes, before.entries)
    manifest_staging = destination.with_name(destination.name + ".manifest.partial")
    staging = destination.with_name(destination.name + ".partial")
    directories = []
    copied_bytes = 0
    methods = {"reflink": 0, "full": 0}
    with ExitStack() as owned:
        publication_fd = _open_root(parent)
        owned.callback(os.close, publication_fd)
        publication_info = os.fstat(publication_fd)
        if (publication_info.st_dev, publication_info.st_ino) != (parent_identity["device"], parent_identity["inode"]):
            raise CopyError("Copy parent changed before staging", "destination-changed")
        manifest_fd = os.open(manifest_staging.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=publication_fd)
        with os.fdopen(manifest_fd, "wb") as output:
            recorded = manifest(source, output, checkpoint=checkpoint)
            output.flush()
            os.fsync(output.fileno())
        if recorded != before or recorded.observation_sha256 != before.observation_sha256:
            raise CopyError("Source changed before copying", "source-changed")
        os.mkdir(staging.name, 0o700, dir_fd=publication_fd)
        target_fd = os.open(staging.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=publication_fd)
        owned.callback(os.close, target_fd)
        target_info = os.fstat(target_fd)
        source_fd = _open_root(source)
        owned.callback(os.close, source_fd)
        for item in entries(source, checkpoint=checkpoint, observe=True):
            expected = item.pop("observation")
            item.pop("allocated")
            checkpoint()
            relative = base64.b64decode(item["path"])
            name = relative.rsplit(b"/", 1)[-1]
            if item["kind"] == "directory":
                if relative:
                    descriptor = _open_parent(target_fd, relative)
                    try:
                        os.mkdir(name, 0o700, dir_fd=descriptor)
                    finally:
                        os.close(descriptor)
                directories.append((relative, item))
                continue
            if item["kind"] == "symlink":
                descriptor = _open_parent(target_fd, relative)
                try:
                    os.symlink(base64.b64decode(item["target"]), name, dir_fd=descriptor)
                    os.utime(name, ns=(item["mtime_ns"], item["mtime_ns"]),
                             dir_fd=descriptor, follow_symlinks=False)
                finally:
                    os.close(descriptor)
                continue
            require_capacity(parent, before.logical_bytes - copied_bytes, before.entries)
            parent_fd = _open_parent(source_fd, relative)
            try:
                input_fd = os.open(relative.rsplit(b"/", 1)[-1],
                                   os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            try:
                if _signature(os.fstat(input_fd)) != expected or not stat.S_ISREG(os.fstat(input_fd).st_mode):
                    raise CopyError("Source changed before copying", "source-changed")
                descriptor = _open_parent(target_fd, relative)
                try:
                    output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600, dir_fd=descriptor)
                finally:
                    os.close(descriptor)
                try:
                    cloned = False
                    if reflink:
                        try:
                            fcntl.ioctl(output_fd, FICLONE, input_fd)
                            cloned = True
                        except OSError as error:
                            if error.errno not in (errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.EINVAL):
                                raise
                    if not cloned:
                        while chunk := os.read(input_fd, 1024 * 1024):
                            checkpoint()
                            view = memoryview(chunk)
                            while view:
                                written = os.write(output_fd, view)
                                if written <= 0:
                                    raise OSError(errno.EIO, "Copy write made no progress")
                                view = view[written:]
                    first, second = os.fstat(input_fd), os.fstat(output_fd)
                    if (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino):
                        raise CopyError("Copy is not independent")
                    _restore_metadata(output_fd, item)
                    os.fsync(output_fd)
                    if _signature(os.fstat(input_fd)) != expected:
                        raise CopyError("Source changed while copying", "source-changed")
                    methods["reflink" if cloned else "full"] += 1
                finally:
                    os.close(output_fd)
            finally:
                os.close(input_fd)
            copied_bytes += item["size"]
            status({"phase": "copying", "bytes": copied_bytes, "total": before.logical_bytes})
        for relative, item in reversed(directories):
            descriptor = _open_parent(target_fd, relative + b"/.") if relative else os.dup(target_fd)
            try:
                _restore_metadata(descriptor, item)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        checkpoint()
        staged_info = staging.lstat()
        parent_info = parent.lstat()
        if ((staged_info.st_dev, staged_info.st_ino) != (target_info.st_dev, target_info.st_ino)
                or (parent_info.st_dev, parent_info.st_ino) != (publication_info.st_dev, publication_info.st_ino)):
            raise CopyError("Copy destination identity changed", "destination-changed")
        status({"phase": "verifying", "bytes": copied_bytes, "total": before.logical_bytes})
        copied = manifest(staging, checkpoint=checkpoint, logical_root=source)
        after = manifest(source, checkpoint=checkpoint)
        if (before != copied or before != after or before.observation_sha256 != after.observation_sha256
                or not runtime_filesystem.same_retained_root(source_identity, layout(source))):
            raise CopyError("Source or copied contents changed", "source-changed")
        checkpoint()
        paths.rename_no_replace(staging.name, destination.name,
                                source_dir_fd=publication_fd, destination_dir_fd=publication_fd)
        paths.rename_no_replace(manifest_staging.name, destination.name + ".manifest",
                                source_dir_fd=publication_fd, destination_dir_fd=publication_fd)
        os.fsync(publication_fd)
        published = destination.lstat()
        if (published.st_dev, published.st_ino) != (target_info.st_dev, target_info.st_ino):
            raise CopyError("Published copy identity changed", "destination-changed")
        return {"manifest": before.__dict__, "source": source_identity,
                "copy": runtime_filesystem.identity(destination), "methods": methods}
