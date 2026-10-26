"""Path identity and ownership checks shared by filesystem consumers."""

import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import shutil
import stat


CACHE_MARKER = ".logos-desktop-cache"
CACHE_MARKER_CONTENT = "logos-desktop cache schema 1\n"


def xdg_base(value: str | None, fallback: Path) -> Path:
    return Path(value) if value and Path(value).is_absolute() else fallback


def relative_to_install(path: str | Path, install_dir: str | Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return str(candidate)

    try:
        return str(candidate.relative_to(install_dir))
    except ValueError:
        return str(candidate)


def absolute_from_install(path: str | Path, install_dir: str | Path) -> Path:
    if not str(path) or not Path(install_dir).is_absolute():
        raise ValueError("An absolute installation directory and nonempty path are required")
    candidate = Path(path)
    return candidate if candidate.is_absolute() else Path(install_dir) / candidate


def disjoint(first: Path, second: Path) -> bool:
    first = first.resolve()
    second = second.resolve()
    return not first.is_relative_to(second) and not second.is_relative_to(first)


def rename_no_replace(source: Path, destination: Path) -> None:
    """Preserve an unexpected destination entry during publication or recovery."""
    import ctypes

    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise RuntimeError("No-replace rename is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def validate_removal_path(path: Path, protected: tuple[Path, ...] = ()) -> os.stat_result:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Refusing an ambiguous removal path: {path}")

    resolved = path.resolve()
    home = Path.home().resolve()
    system_roots = (
        "/", "/etc", "/usr", "/var", "/opt", "/tmp", "/home", "/root",
        "/bin", "/sbin", "/lib", "/lib64", "/run", "/boot", "/dev",
        "/proc", "/sys", "/srv", "/mnt", "/media",
    )
    forbidden = {Path(root).resolve() for root in system_roots}
    forbidden.update(root.expanduser().resolve() for root in protected)
    if any(root.is_relative_to(resolved) for root in forbidden) or home.is_relative_to(resolved):
        raise ValueError(f"Refusing to remove a protected directory: {path}")

    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError(f"Removal requires an owned, unlinked path: {path}")
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Refusing to remove a special file: {path}")
    return info


@dataclass
class RemovalTarget:
    path: Path
    info: os.stat_result
    parent_info: os.stat_result


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _restore_claim(parent_fd: int, quarantine_fd: int, name: str) -> bool:
    """Restore a claimed entry only if its old name is still vacant."""
    import ctypes

    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        return False
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    return rename(quarantine_fd, b"target", parent_fd, os.fsencode(name), 1) == 0


def _recovery_location(quarantine_fd: int) -> str:
    info = os.fstat(quarantine_fd)
    try:
        directory = Path(os.readlink(f"/proc/self/fd/{quarantine_fd}"))
        if _same_file(directory.stat(), info):
            return str(directory / "target")
    except OSError:
        pass
    return f"directory device {info.st_dev}, inode {info.st_ino} (current path unavailable)"


def remove_targets(targets: list[RemovalTarget], protected: tuple[Path, ...]) -> None:
    """Claim approved identities through held parents before any destructive work."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("Safe recursive removal is unavailable on this platform")

    opened = []
    claims = []
    quarantines = []
    try:
        for target in targets:
            validate_removal_path(target.path, protected)
            parent_fd = os.open(target.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            opened.append(parent_fd)
            current = os.stat(target.path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_file(os.fstat(parent_fd), target.parent_info) or not _same_file(current, target.info):
                raise ValueError(f"Uninstall target changed after confirmation: {target.path}")

            parent = os.fstat(parent_fd)
            shared_writes = bool(parent.st_mode & 0o022)
            if parent.st_uid not in (0, os.geteuid()) or (shared_writes and not parent.st_mode & stat.S_ISVTX):
                raise ValueError(f"Uninstall target has an untrusted parent directory: {target.path}")

            quarantine_name = f".logos-desktop-uninstall-{secrets.token_hex(12)}"
            os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
            quarantine_info = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
            quarantines.append((parent_fd, quarantine_name, quarantine_info))
            quarantine_fd = os.open(
                quarantine_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd,
            )
            opened.append(quarantine_fd)
            held = os.fstat(quarantine_fd)
            if (
                not _same_file(held, quarantine_info)
                or held.st_uid != os.geteuid()
                or stat.S_IMODE(held.st_mode) != 0o700
                or os.listdir(quarantine_fd)
            ):
                raise ValueError("Uninstall quarantine is not the private directory created by this operation")
            claim = (target, parent_fd, quarantine_name, quarantine_fd)
            claims.append(claim)
            os.rename(target.path.name, "target", src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd)
            claimed = os.stat("target", dir_fd=quarantine_fd, follow_symlinks=False)
            if not _same_file(claimed, target.info):
                raise ValueError(f"Uninstall target was replaced while claiming it: {target.path}")

        for target, parent_fd, quarantine_name, quarantine_fd in claims:
            if stat.S_ISDIR(target.info.st_mode):
                shutil.rmtree("target", dir_fd=quarantine_fd)
            else:
                os.unlink("target", dir_fd=quarantine_fd)
    except BaseException as error:
        recovery = []
        for target, parent_fd, quarantine_name, quarantine_fd in reversed(claims):
            try:
                os.stat("target", dir_fd=quarantine_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not _restore_claim(parent_fd, quarantine_fd, target.path.name):
                recovery.append(_recovery_location(quarantine_fd))
        if recovery:
            message = "Uninstall stopped. Preserved entries need recovery at: " + ", ".join(recovery)
            raise RuntimeError(message) from error
        raise
    finally:
        for parent_fd, quarantine_name, quarantine_info in quarantines:
            try:
                current = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    _same_file(current, quarantine_info)
                    and current.st_uid == os.geteuid()
                    and stat.S_IMODE(current.st_mode) == 0o700
                ):
                    os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError:
                pass
        for descriptor in reversed(opened):
            os.close(descriptor)


def validate_cache_directory(cache: Path, base: Path, *, require_marker: bool = False) -> None:
    if not base.is_absolute() or cache != base / "logos-desktop":
        raise ValueError("Cache must be the logos-desktop child of its absolute base")
    if cache.resolve().parent != base.resolve():
        raise ValueError(f"Cache escapes its base: {cache}")

    info = validate_removal_path(cache, (base,))
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Cache is not a directory: {cache}")
    if require_marker:
        marker = cache / CACHE_MARKER
        marker_info = marker.lstat()
        if (
            not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_uid != os.geteuid()
            or marker.read_text() != CACHE_MARKER_CONTENT
        ):
            raise ValueError(f"Cache ownership is not established: {cache}")


def ensure_cache_directory(cache: Path, base: Path) -> Path:
    if not base.is_absolute() or cache != base / "logos-desktop":
        raise ValueError("Invalid application cache location")
    base.mkdir(parents=True, exist_ok=True)
    try:
        cache.mkdir(mode=0o700)
    except FileExistsError:
        validate_cache_directory(cache, base)
    else:
        validate_cache_directory(cache, base)
        with (cache / CACHE_MARKER).open("x") as marker:
            marker.write(CACHE_MARKER_CONTENT)
    return cache


def executable(path: str | Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(f"Configured executable is missing or not executable: {candidate}")
    return str(candidate)
