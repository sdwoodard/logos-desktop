"""Construct the reviewed private-XWayland offline execution boundary."""

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess

from . import artifacts, paths, processes, runtime_copies


REVISION = "private-xwayland-software-1"
SYSTEM_PATHS = ("/usr", "/etc/fonts", "/etc/ld.so.cache", "/etc/ssl/certs", "/etc/ssl/openssl.cnf",
                "/etc/ca-certificates", "/etc/alternatives")


class Unavailable(runtime_copies.CopyError):
    def __init__(self, message, reason="isolation-unavailable"):
        super().__init__(message, reason)


def expansion_budget(runner, *, checkpoint=lambda: None):
    helper = shutil.which("unsquashfs")
    if helper is None:
        raise Unavailable("Install squashfs-tools to measure private AppImage extraction space",
                          "extraction-inspection-unavailable")
    _, extent = artifacts._elf_extent(Path(runner))
    with open(runner, "rb") as stream:
        stream.seek(extent)
        padding = stream.read(4192)
    offset = padding.find(b"hsqs")
    if offset < 0 or offset > 4096:
        raise Unavailable("Cannot locate accepted AppImage filesystem")
    try:
        class Cancellation:
            def is_set(self):
                checkpoint()
                return False

        with processes.owned(Cancellation()):
            content, result = processes.capture(
                [helper, "-lln", "-o", str(extent + offset), str(runner)],
                env={"PATH": "/usr/bin", "LANG": "C.UTF-8"}, timeout=60,
                limit=64 * 1024 * 1024, label="AppImage space inspection",
            )
    except processes.CaptureError as error:
        raise Unavailable("Cannot measure accepted AppImage extraction space") from error
    if result:
        raise Unavailable("Accepted AppImage filesystem inspection failed")
    total = count = 0
    for line in content.splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and len(fields[0]) == 10 and fields[0][:1] in (b"-", b"d", b"l"):
            if not fields[2].isdigit():
                raise Unavailable("Unrecognized AppImage extraction listing")
            total += int(fields[2]) + 4096
            count += 1
    if not count:
        raise Unavailable("AppImage extraction listing is empty")
    return {"bytes": total, "entries": count, "helper_sha256": artifacts.regular_hash(helper)}


@dataclass(frozen=True)
class Boundary:
    bwrap: str
    wayland: Path
    dependencies: tuple[tuple[str, str], ...]


def validate_host_binds(source_roots, mounted, *, checkpoint=lambda: None):
    mounts = []
    with open("/proc/self/mountinfo", "rb") as stream:
        for line in stream:
            fields = line.split()

            def decode(value):
                for code, replacement in ((b"\\040", b" "), (b"\\011", b"\t"),
                                          (b"\\012", b"\n"), (b"\\134", b"\\")):
                    value = value.replace(code, replacement)
                return Path(os.fsdecode(value))

            mounts.append((decode(fields[4]), fields[2], decode(fields[3])))

    def physical(path):
        matching = [entry for entry in mounts if path.is_relative_to(entry[0])]
        target, device, root = max(matching, key=lambda entry: len(entry[0].parts))
        return device, root / path.relative_to(target)

    sources = [physical(Path(source)) for source in source_roots]
    count = 0
    for exposed in mounted:
        device, physical_root = physical(exposed)
        for source_device, source_root in sources:
            if device == source_device and not paths.disjoint(physical_root, source_root):
                raise Unavailable("A system mount aliases original data", "unsupported-system-overlap")
        if any(target != exposed and target.is_relative_to(exposed) for target, _, _ in mounts):
            raise Unavailable("A system dependency contains an unmodeled nested mount")
        pending = [exposed]
        while pending:
            checkpoint()
            path = pending.pop()
            info = path.lstat()
            count += 1
            if count > 2_000_000:
                raise Unavailable("System dependency inspection exceeded its bound")
            if stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode):
                raise Unavailable("A system dependency exposes an unexpected IPC endpoint")
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)


def inspect(source_roots, *, checkpoint=lambda: None):
    bwrap = shutil.which("bwrap")
    if not bwrap or not shutil.which("Xwayland") or not Path("/usr/bin/python3").is_file():
        raise Unavailable("Install Bubblewrap, XWayland and system Python before trying an offline copy")
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if not Path(runtime).is_absolute() or not display or Path(display).name != display:
        raise Unavailable("A usable local Wayland display is required", "display-unavailable")
    wayland = Path(runtime) / display
    try:
        info = wayland.lstat()
    except OSError as error:
        raise Unavailable("The selected Wayland display is unavailable. Recheck the desktop session",
                          "display-unavailable") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise Unavailable("The selected Wayland socket is unavailable", "display-unavailable")
    mounted = [Path(name).resolve() for name in SYSTEM_PATHS if Path(name).exists()]
    for source in source_roots:
        if any(not paths.disjoint(Path(source), target) for target in mounted):
            raise Unavailable("Installation overlaps a required system mount", "unsupported-system-overlap")
    try:
        validate_host_binds(source_roots, mounted, checkpoint=checkpoint)
    except OSError as error:
        raise Unavailable("Cannot inspect required read-only system dependencies. Check their permissions",
                          "dependency-inspection-unavailable") from error
    dependencies = [Path("/usr/share/glvnd/egl_vendor.d/50_mesa.json")]
    for name in ("libEGL_mesa.so.0", "dri/swrast_dri.so"):
        candidates = [Path("/usr/lib") / name, Path("/usr/lib/x86_64-linux-gnu") / name]
        selected = next((candidate for candidate in candidates if candidate.is_file()), None)
        if selected is None:
            raise Unavailable("Mesa software EGL is required for the private display", "display-unavailable")
        dependencies.append(selected.resolve(strict=True))
    try:
        observed = tuple((str(path), artifacts.regular_hash(path)) for path in dependencies)
    except OSError as error:
        raise Unavailable("Cannot inspect private display dependencies", "display-unavailable") from error
    return Boundary(bwrap, wayland, observed)


def account_name(value=None):
    value = pwd.getpwuid(os.getuid()).pw_name if value is None else value
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
        raise Unavailable("The copied Wine account name is unsupported", "unsupported-wine-account")
    return value


def prepare_private(directory, *, wine_user=None):
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    for name in ("home", "config", "share", "cache", "state", "runtime", "tmp"):
        (directory / name).mkdir(mode=0o700)
    user = account_name(wine_user)
    (directory / "passwd").write_text(f"{user}:x:{os.getuid()}:{os.getgid()}:Offline trial:/trial/home:/bin/sh\n")
    (directory / "group").write_text(f"{user}:x:{os.getgid()}:\n")
    denial = directory / "deny-url"
    denial.write_text("#!/bin/sh\nprintf '%s\\n' denied >> /trial/url-denials\nexit 63\n")
    denial.chmod(0o500)
    applications = directory / "share/applications"
    applications.mkdir()
    (applications / "offline.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Offline trial URL denial\n"
        "Exec=/trial/deny-url %u\nNoDisplay=true\n"
    )
    (directory / "config/mimeapps.list").write_text(
        "[Default Applications]\nx-scheme-handler/http=offline.desktop\n"
        "x-scheme-handler/https=offline.desktop\nx-scheme-handler/logos4=offline.desktop\n"
    )


def command(boundary, *, source, copied, private, runner, worker, dependencies=(), wine_user=None, check=False):
    source, copied, private, runner, worker = map(Path, (source, copied, private, runner, worker))
    for original in (source,):
        for exposed in (copied, private, runner, worker):
            if not paths.disjoint(original, exposed):
                raise Unavailable("Trial mount overlaps original data", "unsupported-system-overlap")
    for path, digest in boundary.dependencies:
        if artifacts.regular_hash(path) != digest:
            raise Unavailable("Private display dependency changed", "display-unavailable")
    lib64 = "usr/lib64" if Path("/usr/lib64/ld-linux-x86-64.so.2").exists() else "usr/lib"
    arguments = [boundary.bwrap, "--unshare-user", "--unshare-net", "--unshare-ipc",
                 "--unshare-pid", "--unshare-uts", "--die-with-parent", "--new-session",
                 "--cap-drop", "ALL", "--clearenv", "--tmpfs", "/",
                 "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                 "--symlink", "usr/lib", "/lib", "--symlink", lib64, "/lib64",
                 "--proc", "/proc", "--dev", "/dev", "--bind", str(private / "tmp"), "/tmp",
                 "--dir", "/tmp/.X11-unix", "--dir", "/etc",
                 "--bind", str(private), "/trial", "--bind", str(copied), str(source),
                 "--ro-bind", str(private / "passwd"), "/etc/passwd",
                 "--ro-bind", str(private / "group"), "/etc/group",
                 "--ro-bind", str(runner), "/runner/image.AppImage",
                 "--ro-bind", str(worker), "/worker.py",
                 "--ro-bind", str(boundary.wayland), "/trial/runtime/host-display"]
    for role in ("wine", "wine64", "wineserver"):
        arguments.extend(["--symlink", "image.AppImage", "/runner/" + role])
    for name in ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos"):
        empty = Path.home() / name
        if not paths.disjoint(source, empty) or not paths.disjoint(Path("/usr"), empty):
            raise Unavailable("Private home folder mapping overlaps an execution root")
        arguments.extend(["--dir", str(empty)])
    for item in dependencies:
        path = Path(item["path"])
        if not paths.disjoint(source, path) or artifacts.regular_hash(path) != item["sha256"]:
            raise Unavailable("Trial dependency does not bind independent accepted bytes")
        arguments.extend(["--ro-bind", str(path), "/inputs/" + item["sha256"]])
    for name in SYSTEM_PATHS[1:]:
        if Path(name).exists():
            arguments.extend(["--ro-bind", name, name])
    environment = {
        "PATH": "/runner:/usr/bin", "HOME": "/trial/home", "LANG": "C.UTF-8",
        "USER": account_name(wine_user), "LOGNAME": account_name(wine_user),
        "XDG_CONFIG_HOME": "/trial/config", "XDG_DATA_HOME": "/trial/share",
        "XDG_CACHE_HOME": "/trial/cache", "XDG_STATE_HOME": "/trial/state",
        "XDG_RUNTIME_DIR": "/trial/runtime", "WAYLAND_DISPLAY": "host-display",
        "TMPDIR": "/tmp",
        "WINEPREFIX": str(source), "WINEDEBUG": "-all", "BROWSER": "/trial/deny-url",
        "WINEDLLOVERRIDES": "winemenubuilder.exe=d;mscoree,mshtml=",
        "APPIMAGE_EXTRACT_AND_RUN": "1", "LIBGL_ALWAYS_SOFTWARE": "1",
        "__GLX_VENDOR_LIBRARY_NAME": "mesa",
        "__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/50_mesa.json",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name, value in environment.items():
        arguments.extend(["--setenv", name, value])
    arguments.extend(["--chdir", "/trial", "--", "/usr/bin/python3", "-I", "/worker.py"])
    if check:
        arguments.append("--check")
    return arguments


def probe(arguments):
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unavailable("Offline display preflight failed. Check user namespaces and Wayland access") from error
    if result.returncode:
        raise Unavailable("Offline display preflight failed. Check user namespaces and Wayland access")
