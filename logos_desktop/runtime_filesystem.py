"""Stable local filesystem observations for managed runner storage."""

import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

from . import maintenance


def identity(path):
    path = Path(path)
    try:
        result = subprocess.run(
            ["findmnt", "--json", "--target", str(path), "--output", "UUID,FSTYPE,FSROOT,TARGET,ID"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        if len(result.stdout) > 65536:
            raise ValueError("Mount facts exceeded their budget")
        mounts = json.loads(result.stdout)["filesystems"]
        if len(mounts) != 1:
            raise ValueError("Ambiguous mount identity")
        mount = mounts[0]
        filesystem_uuid = str(uuid.UUID(mount["uuid"]))
        if mount["fstype"] not in ("ext4", "btrfs"):
            raise ValueError("Runner storage supports ext4 and Btrfs")
        root = Path(mount["fsroot"]) / path.relative_to(Path(mount["target"]))
        info = path.stat()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        uuid.UUID(boot)
        return {
            "canonical_path": str(path), "filesystem_uuid": filesystem_uuid, "filesystem_root": str(root),
            "boot_id": boot, "device": info.st_dev, "inode": info.st_ino, "mount_id": mount["id"],
            "owner_uid": os.geteuid(),
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        raise maintenance.MaintenanceError("Cannot establish supported runner filesystem identity") from error


def same_retained_root(saved, current):
    stable = ("canonical_path", "filesystem_uuid", "filesystem_root", "inode", "owner_uid")
    return all(saved.get(key) == current.get(key) for key in stable)


def live_identity(path):
    """Read boot-scoped kernel facts without spawning a filesystem discovery helper."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        fields = Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines()
        mount_id = next(int(line.split()[1]) for line in fields if line.startswith("mnt_id:"))
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        uuid.UUID(boot)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Unowned live root")
        return {"canonical_path": str(path), "boot_id": boot, "device": info.st_dev,
                "inode": info.st_ino, "mount_id": mount_id, "owner_uid": info.st_uid}
    except (OSError, ValueError, StopIteration) as error:
        raise maintenance.MaintenanceError("Cannot verify the live copied filesystem root") from error
    finally:
        os.close(descriptor)


def same_live_root(saved, path):
    current = live_identity(path)
    keys = ("canonical_path", "boot_id", "device", "inode", "mount_id", "owner_uid")
    return all(saved.get(key) == current[key] for key in keys)
