"""The versioned user environment and Arch source package controller owners.

Provider configuration belongs to one installation, not a profile or environment
variable. Helpers own transactions independently of the application's process scope.
"""

import argparse
import base64
import configparser
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from email.parser import BytesParser
import fcntl
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile


PACKAGE = "logos-desktop"
TOKEN = "LOGOS_DESKTOP_OWNER_STARTUP"
STABLE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
INSTALLED_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?"
VERSION_PATH = re.compile(r"versions/" + INSTALLED_VERSION + r"-[0-9a-f]{64}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_WHEEL = 64 * 1024 * 1024
RELEASE_MANIFEST = "https://github.com/sdwoodard/logos-desktop/releases/latest/download/controller-release.json"


class InstallError(RuntimeError):
    """An owner or transaction could not be verified."""


@dataclass(frozen=True)
class Identity:
    owner: str
    running_version: str = ""
    installed_version: str = ""
    root: Path | None = None
    launcher: Path | None = None
    provider_path: Path | None = None
    message: str = ""


@dataclass(frozen=True)
class Result:
    status: str
    message: str
    version: str = ""
    artifact: dict | None = None
    command: tuple[str, ...] = ()


def _environment():
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", TOKEN, "VIRTUAL_ENV"):
        env.pop(key, None)
    env["LC_ALL"] = "C"
    return env


def _run(command, *, cwd=None, timeout=30):
    try:
        result = subprocess.run(command, cwd=cwd, env=_environment(), timeout=timeout,
                                capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError(f"Command could not complete: {command[0]}") from error
    if result.returncode:
        raise InstallError(f"Command failed ({result.returncode}): {command[0]}\n{result.stderr[-2000:]}")
    if len(result.stdout) > 1024 * 1024:
        raise InstallError("Command output exceeded the metadata limit")
    return result.stdout.strip()


def _private(path, *, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise InstallError(f"Expected an owned private {'directory' if directory else 'file'}: {path}")
    return info


def _root(path, *, create=False):
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.geteuid() == 0:
        raise InstallError("Controller installation requires an ordinary Linux x86_64 user")
    path = Path(path).expanduser().absolute()
    if (path == path.parent or path != path.resolve(strict=False)
            or not path.is_relative_to(Path.home().resolve())):
        raise InstallError("The installation root must be inside your home directory")
    for parent in reversed((path.parent, *path.parent.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if not create:
                raise
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise InstallError(f"Unsafe installation parent: {parent}")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    _private(path, directory=True)
    return path


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, value):
    temporary = path.parent / (".write-" + secrets.token_hex(16))
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    if _private(path).st_size > 65536:
        raise InstallError("Installation record is too large")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise InstallError("Installation record must be an object")
    return value


@contextmanager
def _locked(root, *, wait=False):
    fd = os.open(root / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _private(root / "operation.lock")
        for attempt in range(100 if wait else 1):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if not wait or attempt == 99:
                    raise InstallError("Another controller helper is active") from error
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _owner(path):
    queries = (
        ("pacman", ["-Qqo", "--", str(path)], "No package owns"),
        ("dpkg-query", ["-S", str(path)], "no path found matching pattern"),
        ("rpm", ["-qf", "--", str(path)], "is not owned by any package"),
    )
    owners = []
    for tool, args, missing in queries:
        executable = shutil.which(tool)
        if not executable:
            continue
        try:
            result = subprocess.run([executable, *args], env=_environment(), timeout=5,
                                    capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallError("Package database ownership is unavailable") from error
        if result.returncode == 0:
            owners.append((tool, result.stdout.strip()))
        elif result.returncode != 1 or missing not in result.stdout + result.stderr:
            raise InstallError("Package database ownership is ambiguous")
    return owners


PROBE = '''
import importlib.metadata as m
import json
from pathlib import Path
import sys
import logos_desktop
from logos_desktop import constants
d = m.distribution("logos-desktop")
module = Path(logos_desktop.__file__).resolve()
files = {Path(d.locate_file(f)).resolve() for f in d.files or ()}
assert module in files
assert any(e.name == "logos-desktop" and e.value == "logos_desktop.main:main"
           and e.group == "console_scripts" for e in d.entry_points)
assert constants.APP_VERSION == d.version
assert module.is_relative_to(Path(sys.prefix).resolve())
for resource in ("assets/LogosStubFailOK.mst", "img/logos-desktop.svg"):
    assert (module.parent / resource).is_file()
import PySide6.QtCore
direct = json.loads(d.read_text("direct_url.json") or "{}")
digest = direct.get("archive_info", {}).get("hashes", {}).get("sha256")
print(json.dumps({"version": d.version, "prefix": sys.prefix, "module": str(module), "digest": digest}))
'''


def _system_alias(prefix, candidate, expected, *, package=None):
    """Accept package-owned Arch binusr aliases, not foreign paths or venv links."""
    if (prefix / "pyvenv.cfg").exists() or prefix.parent.name == "versions":
        return False
    candidate = Path(candidate)
    directories = {prefix / "bin", prefix / "sbin", prefix.parent / "bin", prefix.parent / "sbin"}
    if not candidate.is_absolute() or candidate.parent not in directories:
        return False
    try:
        canonical = expected.resolve(strict=True)
        if (candidate.parent.resolve(strict=True) != expected.parent.resolve(strict=True)
                or candidate.resolve(strict=True) != canonical or not candidate.samefile(expected)):
            return False
        owners = _owner(canonical)
        if len(owners) != 1 or owners[0][0] != "pacman":
            return False
        if package is not None and owners != [("pacman", package)]:
            return False
        if _owner(expected) != owners:
            return False
        alias_owners = _owner(candidate)
        # An alternate directory spelling need not be a separate database key.
        # The actual file is owned above, and conflicting alias ownership refuses.
        return not alias_owners or alias_owners == owners
    except OSError:
        return False


def _probe(prefix, *, system_aliases=False):
    prefix = Path(prefix)
    data = json.loads(_run([str(prefix / "bin/python"), "-I", "-c", PROBE]))
    if Path(data["prefix"]).resolve() != prefix.resolve():
        raise InstallError("Interpreter reported a different installation")
    if prefix.parent.name == "versions":
        if prefix.name != f'{data["version"]}-{data.get("digest")}':
            raise InstallError("Installed wheel provenance differs from the environment path")
    entry = prefix / "bin/logos-desktop"
    if not entry.is_file() or entry.is_symlink():
        raise InstallError("Controller entry point is not a regular installed script")
    lines = entry.read_text().splitlines()
    python = str(prefix / "bin/python")
    direct = bool(lines) and lines[0] == f"#!{python}"
    wrapped = (len(lines) >= 3 and lines[0] == "#!/bin/sh" and lines[1].startswith("'''exec' ")
               and lines[2] == "' '''" and shlex.split(lines[1][len("'''exec' "):]) == [python, "$0", "$@"])
    if not direct and not wrapped and system_aliases:
        candidate = lines[0][2:] if lines and lines[0].startswith("#!") else None
        if (len(lines) >= 3 and lines[0] == "#!/bin/sh" and lines[1].startswith("'''exec' ")
                and lines[2] == "' '''"):
            arguments = shlex.split(lines[1][len("'''exec' "):])
            candidate = arguments[0] if len(arguments) == 3 and arguments[1:] == ["$0", "$@"] else None
        if candidate is not None:
            direct = _system_alias(prefix, candidate, Path(python))
    if not direct and not wrapped:
        raise InstallError("Controller entry point does not bind this interpreter")
    return data["version"]


def _target(root):
    pointer = root / "current"
    if not pointer.is_symlink():
        raise InstallError("The canonical current pointer is missing")
    relative = os.readlink(pointer)
    if VERSION_PATH.fullmatch(relative) is None:
        raise InstallError("The canonical current pointer is invalid")
    target = root / relative
    _private(root / "versions", directory=True)
    _private(target, directory=True)
    return relative


def _launcher(root):
    return f'#!/bin/sh\nexec {shlex.quote(str(root / "current/bin/logos-desktop"))} "$@"\n'


def identify():
    """Identify the loaded package and actual installed interpreter before markers."""
    try:
        import logos_desktop
        from . import constants

        module = Path(logos_desktop.__file__).resolve()
        distribution = importlib.metadata.distribution(PACKAGE)
        files = {Path(distribution.locate_file(f)).resolve() for f in distribution.files or ()}
        if module not in files:
            raise InstallError("Loaded source does not match the installed distribution")
        metadata = [path for path in files if path.name == "METADATA" and path.parent.name.endswith(".dist-info")]
        if len(metadata) != 1:
            raise InstallError("Installed distribution metadata is missing or ambiguous")
        prefix = Path(sys.prefix).absolute()
        entry = prefix / "bin/logos-desktop"
        owners = _owner(module)
        entry_owners = _owner(entry)
        metadata_owners = _owner(metadata[0])
        if owners or entry_owners or metadata_owners:
            if (owners != [("pacman", PACKAGE)] or entry_owners != owners or metadata_owners != owners
                    or entry != Path("/usr/bin/logos-desktop")):
                raise InstallError("This controller belongs to a different or mixed package owner")
            if _probe(prefix, system_aliases=True) != distribution.version:
                raise InstallError("Installed package metadata and interpreter disagree")
            installed = _run(["pacman", "-Q", PACKAGE]).split()
            if len(installed) != 2 or installed[0] != PACKAGE:
                raise InstallError("Unexpected installed Arch package identity")
            from packaging.version import Version

            package_version = installed[1].split(":")[-1].rsplit("-", 1)[0]
            if Version(package_version) != Version(distribution.version):
                raise InstallError("Arch package version and Python distribution disagree")
            path_entry = shutil.which("logos-desktop")
            if (path_entry and Path(path_entry).absolute() != entry
                    and not _system_alias(prefix, path_entry, entry, package=PACKAGE)):
                raise InstallError("PATH selects a different controller")
            return Identity("aur", constants.APP_VERSION, installed[1], launcher=entry,
                            provider_path=Path.home() / ".local/state/logos-desktop/aur/provider.json")

        root = _root(prefix.parent.parent)
        if distribution.version != constants.APP_VERSION:
            raise InstallError("Running package version differs from its immutable environment")
        if (prefix.parent != root / "versions"
                or prefix.name != f"{distribution.version}-{prefix.name.rsplit('-', 1)[-1]}"):
            raise InstallError("This is not the canonical versioned environment")
        if VERSION_PATH.fullmatch("versions/" + prefix.name) is None:
            raise InstallError("The running environment has no canonical artifact identity")
        if Path(sys.executable).absolute() != prefix / "bin/python":
            raise InstallError("The running interpreter is not the canonical interpreter")
        _private(prefix, directory=True)
        if "include-system-site-packages = false" not in (prefix / "pyvenv.cfg").read_text():
            raise InstallError("The environment includes foreign system packages")
        _private(root / "logos-desktop")
        if (root / "logos-desktop").read_text() != _launcher(root):
            raise InstallError("The stable launcher has changed")
        installed_version = _probe(root / _target(root))
        if _probe(prefix) != distribution.version:
            raise InstallError("The running and installed package identities disagree")
        path_entry = shutil.which("logos-desktop")
        launchers = (root / "logos-desktop", root / "current/bin/logos-desktop", entry)
        if path_entry and Path(path_entry).absolute() not in launchers:
            raise InstallError("PATH selects a different controller")
        return Identity("venv", distribution.version, installed_version, root, root / "logos-desktop",
                        root / "provider.json")
    except (InstallError, OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError) as error:
        return Identity("unsupported", message=str(error))


def _provider(identity):
    if identity.provider_path is None or not identity.provider_path.exists():
        raise InstallError("No release provider is configured for this installation")
    _private(identity.provider_path.parent, directory=True)
    data = _read(identity.provider_path)
    scope = str(identity.root if identity.owner == "venv" else identity.launcher)
    if data.get("schema") != 1 or data.get("installation") != scope:
        raise InstallError("Provider configuration belongs to another installation")
    return data


def configure_provider(identity):
    """Create the public provider for an identified installation without replacement."""
    if identity.owner == "venv" and identity.root is not None:
        directory = _root(identity.root)
        scope = str(directory)
        data = {"schema": 1, "installation": scope, "manifest_url": RELEASE_MANIFEST}
    elif identity.owner == "aur" and identity.launcher == Path("/usr/bin/logos-desktop"):
        directory = Path.home() / ".local/state/logos-desktop/aur"
        data = {"schema": 1, "installation": str(identity.launcher),
                "rpc_url": "https://aur.archlinux.org/rpc/v5/info?arg[]=logos-desktop",
                "git_url": "https://aur.archlinux.org/logos-desktop.git",
                "release_manifest_url": RELEASE_MANIFEST}
    else:
        raise InstallError("Provider setup requires an identified managed environment or Arch package")
    if identity.provider_path != directory / "provider.json":
        raise InstallError("Provider path does not match the identified installation")
    if os.geteuid() == 0:
        raise InstallError("Configure the provider as the user who runs the controller")

    # Walk from the user's home without following directory aliases. Existing
    # shared user directories stay unchanged, while the final parent is private.
    home = Path.home().resolve()
    relative = directory.relative_to(home)
    for parent in home.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise InstallError("Unsafe ancestor of the provider home directory")
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".provider-" + secrets.token_hex(16)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise InstallError("Provider home must be owned and not writable by other users")
        for part in relative.parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise InstallError("Provider parent must be owned and not writable by other users")
        if os.fstat(fd).st_mode & 0o077:
            raise InstallError("Provider directory must be private")

        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=fd)
        with os.fdopen(output_fd, "w") as output:
            json.dump(data, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, "provider.json", src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError as error:
            raise InstallError(
                "Provider already exists and was preserved. Keep it or review it before removal"
            ) from error
        os.unlink(temporary, dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)
    return Result("configured", "Public update provider configured for this installation")


def _url(url, config):
    if not isinstance(url, str):
        raise InstallError("Provider URL must be a string")
    parsed = urllib.parse.urlsplit(url)
    loopback = parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if (parsed.username or parsed.password or parsed.fragment or not parsed.hostname
            or not (parsed.scheme == "https" or
                    (parsed.scheme == "http" and loopback and config.get("allow_loopback") is True))):
        raise InstallError("Provider URL requires HTTPS or explicitly configured private loopback")
    return url


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, config):
        self.config = config

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl, self.config)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, config, *, limit, destination=None):
    opener = urllib.request.build_opener(_Redirects(config))
    request = urllib.request.Request(_url(url, config), headers={"User-Agent": "LogosDesktop-owner-helper"})
    started = time.monotonic()
    size = 0
    digest = hashlib.sha256()
    chunks = []
    with opener.open(request, timeout=10) as response:
        while True:
            block = response.read1(min(65536, limit + 1 - size))
            if not block:
                break
            size += len(block)
            if size > limit or time.monotonic() - started > 60:
                raise InstallError("Provider transfer exceeded its size or time limit")
            digest.update(block)
            if destination is None:
                chunks.append(block)
            else:
                destination.write(block)
    return (b"".join(chunks) if destination is None else size), digest.hexdigest()


def _wheel(data, *, stable=True):
    if not isinstance(data, dict):
        raise InstallError("Wheel manifest must be an object")
    version = data.get("version", "")
    digest = data.get("sha256", "")
    filename = data.get("filename", "")
    version_pattern = STABLE if stable else re.compile(INSTALLED_VERSION)
    if (not version_pattern.fullmatch(version) or not DIGEST.fullmatch(digest)
            or filename != f"logos_desktop-{version}-py3-none-any.whl"
            or type(data.get("size")) is not int or not 0 < data["size"] <= MAX_WHEEL):
        raise InstallError("Release must identify an exact allowed wheel version, size and SHA-256")
    return data


def check(identity):
    """Return provider availability without modifying installed files."""
    if identity.owner not in ("venv", "aur"):
        return Result("refused", identity.message or "Unsupported controller installation")
    try:
        if identity.owner == "venv" and (identity.root / "operation.json").exists():
            record = _receipt(identity.root)
            if record["status"] not in ("ready", "rolled-back", "aborted"):
                command = (sys.executable, "-I", "-m", "logos_desktop.controller_install", "recover")
                return Result("unknown", "Activation has not confirmed readiness. Reconcile safely with: "
                              + shlex.join(command), command=command)
        config = _provider(identity)
        if identity.owner == "venv":
            payload, _ = _download(config["manifest_url"], config, limit=65536)
            data = json.loads(payload)
            if data.get("status") in ("unavailable", "provider-lag"):
                return Result(data["status"], "The installation provider has no usable release")
            data = _wheel(data)
            _url(data["url"], config)
            from packaging.version import Version

            newer = Version(data["version"]) > Version(identity.installed_version)
        else:
            payload, _ = _download(config["rpc_url"], config, limit=65536)
            rpc = json.loads(payload)
            results = rpc.get("results", [])
            if rpc.get("type") != "multiinfo" or rpc.get("resultcount") != len(results):
                raise InstallError("Invalid AUR RPC response")
            if results and (len(results) != 1 or results[0].get("Name") != PACKAGE
                            or results[0].get("PackageBase") != PACKAGE):
                raise InstallError("AUR returned a different package")
            version = results[0]["Version"] if results else None
            newer = False
            if version is not None:
                if not re.fullmatch(r"(?:[0-9]+:)?[0-9]+\.[0-9]+\.[0-9]+-[0-9]+(?:\.[0-9]+)?", version):
                    raise InstallError("AUR has no supported stable package version")
                newer = int(_run(["vercmp", version, identity.installed_version])) > 0

            # Source metadata can explain a missing AUR update, never replace its recipe.
            if not newer and "release_manifest_url" in config:
                payload, _ = _download(config["release_manifest_url"], config, limit=65536)
                source = _wheel(json.loads(payload))
                _url(source["url"], config)
                from packaging.version import Version

                upstream = version.split(":")[-1].rsplit("-", 1)[0] if version is not None else None
                if upstream is None or Version(source["version"]) > Version(upstream):
                    return Result("provider-lag", f"Source release {source['version']} is not yet available from AUR",
                                  source["version"])
            if version is None:
                return Result("unavailable", "AUR has no logos-desktop source package")

            data = {"version": version}
            if newer:
                git_url = _url(config["git_url"], config)
                command = ["git", "-c", "http.followRedirects=false", "ls-remote", "--", git_url, "HEAD"]
                commit = _run(command).split()[0]
                if not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise InstallError("AUR recipe has no pinned Git revision")
                data.update(commit=commit, git_url=git_url)
        if newer:
            return Result("available", "An owner-managed update is available", data["version"], data)
        installed_python = identity.installed_version
        if identity.owner == "aur":
            installed_python = installed_python.split(":")[-1].rsplit("-", 1)[0]
        from packaging.version import Version

        if Version(identity.running_version) != Version(installed_python):
            return Result("restart-required", "The installed controller differs from this running process",
                          identity.installed_version)
        if identity.owner == "aur":
            message = "No newer version is available from AUR"
            if "release_manifest_url" not in config:
                message += ". Source release availability was not checked"
            else:
                message += ". The configured source release is not ahead of AUR"
            return Result("current", message, data["version"])
        return Result("current", "No newer version is available from the installation provider", data["version"])
    except urllib.error.HTTPError as error:
        error.close()
        return Result("unavailable", f"The installation provider returned HTTP {error.code}")
    except (OSError, TimeoutError) as error:
        return Result("offline", f"The installation provider could not be reached: {error}")
    except (InstallError, ValueError, KeyError, TypeError, IndexError) as error:
        return Result("unavailable", str(error))


def handoff(identity, config_path, frontend="qt", checkpoint=None):
    """Check cancellation during preparation, then transfer ownership at Popen."""
    if checkpoint is not None:
        checkpoint()
    if frontend not in ("qt", "tui"):
        return Result("refused", "Unknown restart frontend")
    current = identify()
    if checkpoint is not None:
        checkpoint()
    if identity != current:
        return Result("refused", "Controller identity changed before handoff")
    if identity.owner == "venv":
        recovery = recover(identity)
        if checkpoint is not None:
            checkpoint()
        if recovery.status != "recovered":
            return recovery
    available = check(identity)
    if checkpoint is not None:
        checkpoint()
    if available.status != "available":
        return available
    interpreter = str(Path(sys.executable).absolute())
    helper_arguments = ["handoff", "--frontend", frontend]
    if config_path:
        helper_arguments.extend(["--config", str(Path(config_path).expanduser().absolute())])
    command = [interpreter, "-I", "-m", "logos_desktop.controller_install", *helper_arguments]
    terminal = None
    for name, flag in (("konsole", "-e"), ("xterm", "-e"), ("gnome-terminal", "--")):
        if name == "xterm" and not os.environ.get("DISPLAY"):
            continue
        executable = shutil.which(name)
        if executable:
            terminal = [executable, flag]
            break
    if checkpoint is not None:
        checkpoint()
    if terminal is None:
        return Result("manual", "Run this command in a terminal: " + shlex.join(command), command=tuple(command))
    # KDE can consume helper options even after -e. Keep every helper argument
    # inside one Python literal, never as a terminal parser token or shell command.
    source = ("from logos_desktop.controller_install import main\n"
              f"raise SystemExit(main({helper_arguments!r}))\n")
    launch_command = [*terminal, interpreter, "-I", "-c", source]
    env = _environment()
    # Callback exceptions must escape, including OSError, before accepting handoff.
    if checkpoint is not None:
        checkpoint()
    try:
        subprocess.Popen(launch_command, env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    except OSError as error:
        return Result("manual", f"Terminal could not open ({error}). Run: {shlex.join(command)}",
                      command=tuple(command))
    return Result("handed-off", "The terminal owns this update. Check again after it finishes.", command=tuple(command))


def _publish(root, target):
    temporary = root / (".current-" + secrets.token_hex(16))
    try:
        temporary.symlink_to(target)
        os.replace(temporary, root / "current")
        _sync(root)
    finally:
        temporary.unlink(missing_ok=True)


def _receipt(root):
    record = _read(root / "operation.json")
    if (record.get("schema") != 1 or record.get("installation") != str(root)
            or not re.fullmatch(r"[0-9a-f]{48}", record.get("token", ""))):
        raise InstallError("Invalid activation receipt")
    for name in ("previous", "target"):
        value = record.get(name)
        if value is None and name == "previous":
            continue
        if not isinstance(value, str) or VERSION_PATH.fullmatch(value) is None:
            raise InstallError("Invalid activation path")
    return record


def _recover(root, expected, *, explicit=False):
    record = _receipt(root)
    if record != expected or _target(root) != record["target"]:
        raise InstallError("Activation changed. Recovery will not overwrite a later publication")
    if record["status"] == "ready" and not explicit:
        return False
    previous = record["previous"]
    if previous is None:
        raise InstallError("No previous environment exists. The failed environment is retained")
    if "previous_files" in record:
        if installed_evidence(root / previous) != record["previous_files"]:
            raise InstallError("Previous installation changed. Recovery evidence was preserved")
    else:
        _probe(root / previous)
    if _receipt(root) != expected or _target(root) != record["target"]:
        raise InstallError("Activation changed during recovery. Later edits were preserved")
    _publish(root, previous)
    record["status"] = "rolled-back"
    _write(root / "operation.json", record)
    return True


def _reconcile(root):
    if not (root / "operation.json").exists():
        return
    record = _receipt(root)
    if record["status"] in ("ready", "rolled-back", "aborted"):
        return
    current = _target(root) if (root / "current").is_symlink() else None
    if record["status"] == "prepared" and current == record["previous"]:
        record["status"] = "aborted"
        _write(root / "operation.json", record)
        return
    raise InstallError("An interrupted activation has an unknown successor. Check it before another update")


def recover(identity):
    """Reconcile an unpublished operation without launching or guessing about B."""
    if identity.owner != "venv" or identity != identify():
        return Result("refused", "Recovery requires the current canonical installation identity")
    try:
        root = _root(identity.root)
        with _locked(root):
            _reconcile(root)
        return Result("recovered", "No unresolved publication remains. No successor was launched")
    except (InstallError, OSError, ValueError, KeyError) as error:
        return Result("unknown", str(error))


def _install(root, wheel, data):
    """Create at the final path. Never relocate a venv or modify the running one."""
    _wheel(data, stable=False)
    if wheel.stat().st_size != data["size"] or hashlib.sha256(wheel.read_bytes()).hexdigest() != data["sha256"]:
        raise InstallError("Wheel bytes do not match the accepted size and SHA-256")
    with zipfile.ZipFile(wheel) as archive:
        if "logos_desktop/controller_install.py" not in archive.namelist():
            raise InstallError("Wheel is missing its packaged installation helper")
    versions = root / "versions"
    versions.mkdir(mode=0o700, exist_ok=True)
    _private(versions, directory=True)
    target = versions / f'{data["version"]}-{data["sha256"]}'
    if target.exists() or target.is_symlink():
        record = _receipt(root) if (root / "operation.json").exists() else {}
        if (record.get("status") != "aborted" or record.get("target") != str(target.relative_to(root))
                or record.get("previous") != _target(root)):
            raise InstallError("The final environment path already exists. It was retained for inspection")
        _private(target, directory=True)
    else:
        target.mkdir(mode=0o700)
        _run([sys.executable, "-I", "-m", "venv", str(target)], timeout=120)
        _run([str(target / "bin/python"), "-I", "-m", "pip", "--isolated", "install",
              "--disable-pip-version-check", "--no-input", "--timeout", "15", "--retries", "1",
              f"{wheel}[gui]"], timeout=600)

    # A prepared but unpublished B may be reused only after read-only validation.
    python = str(target / "bin/python")
    _run([python, "-I", "-m", "pip", "--isolated", "check"], timeout=60)
    if _probe(target) != data["version"]:
        raise InstallError("Installed wheel has the wrong package version")
    return str(target.relative_to(root))


def _activate(root, target, version, *, expected_previous=None, previous_files=None):
    previous = _target(root) if (root / "current").is_symlink() else None
    if expected_previous is not None and previous != expected_previous:
        raise InstallError("The current pointer changed during preparation")
    record = {"schema": 1, "installation": str(root), "token": secrets.token_hex(24),
              "previous": previous, "target": target, "version": version, "status": "prepared"}
    if previous_files is not None:
        record["previous_files"] = previous_files
    # All later states must remain readable by the same bounded receipt reader.
    if len(json.dumps(dict(record, status="rolled-back")).encode()) > 65536:
        raise InstallError("Activation evidence exceeds the receipt size limit")
    _write(root / "operation.json", record)
    if (_target(root) if (root / "current").is_symlink() else None) != previous:
        raise InstallError("The current pointer changed before publication")
    _publish(root, target)
    record["status"] = "published"
    _write(root / "operation.json", record)
    return record


def _tui_terminal():
    if not all(os.isatty(fd) for fd in (0, 1, 2)):
        raise InstallError("TUI restart requires one exclusive terminal for stdin, stdout and stderr")
    if len({os.fstat(fd).st_rdev for fd in (0, 1, 2)}) != 1:
        raise InstallError("TUI restart descriptors belong to different terminals")
    if os.tcgetpgrp(0) != os.getpgrp():
        raise InstallError("TUI restart requires the foreground terminal process group")


def _supervise(root, record, config_path, frontend, notify, *, detach=False, observation_seconds=30):
    """Observe until readiness or confirmed exit, even after an initial unknown."""
    command = [str(root / record["target"] / "bin/logos-desktop"), "--dialog", frontend]
    if config_path:
        command.extend(["-c", str(Path(config_path).expanduser().absolute())])
    env = _environment()
    env[TOKEN] = json.dumps({"root": str(root), "token": record["token"]})
    descriptors = {}
    if frontend == "qt":
        descriptors = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        child = subprocess.Popen(command, env=env, start_new_session=frontend == "qt",
                                 close_fds=True, **descriptors)
    except OSError:
        with _locked(root, wait=True):
            _recover(root, record)
        return Result("failed", "Successor could not start. Previous pointer restored")

    if detach:
        # The TUI inherits its original terminal session before its observer leaves.
        # Only the successor reads that terminal while the helper waits on a pipe.
        os.setsid()
        fd = os.open(os.devnull, os.O_RDWR)
        try:
            for target in (0, 1, 2):
                os.dup2(fd, target)
        finally:
            if fd > 2:
                os.close(fd)

    deadline = time.monotonic() + observation_seconds
    reported_unknown = False
    while True:
        outcome = None
        try:
            with _locked(root, wait=True):
                current = _receipt(root)
                owns_publication = current["token"] == record["token"] and _target(root) == record["target"]
                status = child.poll()
                if not owns_publication:
                    outcome = Result("unknown", "A later publication changed the activation. It was preserved")
                elif current["status"] == "ready":
                    outcome = Result("updated", "The new controller confirmed readiness", record["version"])
                elif status is not None:
                    if status:
                        _recover(root, record)
                        outcome = Result("failed", "Startup failed before readiness. Previous pointer restored")
                    else:
                        outcome = Result("unknown", "Successor exited without confirming readiness. Check again")
        except (InstallError, OSError, ValueError, KeyError) as error:
            status = child.poll()
            outcome = Result("unknown", f"Successor readiness or recovery could not be verified: {error}")

        # A TUI keeps exclusive ownership until it exits, including after readiness.
        if outcome is not None and (frontend == "qt" or status is not None):
            return outcome
        if not reported_unknown and time.monotonic() >= deadline and frontend == "qt":
            notify(Result("unknown", "Readiness is unknown. Independent supervision continues without killing B"))
            reported_unknown = True
        time.sleep(0.1)


def _launch(root, record, config_path, frontend, *, observation_seconds=30):
    """Give the observer its own process lifetime, independent of terminal closure."""
    if frontend == "tui":
        _tui_terminal()
    reading, writing = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(reading)
        os.close(writing)
        with _locked(root, wait=True):
            _recover(root, record)
        return Result("failed", "Supervisor could not start. Previous pointer restored")

    if pid == 0:
        os.close(reading)
        sent = False

        def notify(result):
            nonlocal sent
            if not sent:
                try:
                    os.write(writing, (json.dumps(result.__dict__) + "\n").encode())
                except BrokenPipeError:
                    pass
                sent = True

        try:
            outcome = _supervise(root, record, config_path, frontend, notify, detach=True,
                                 observation_seconds=observation_seconds)
            _write(root / f"supervision-{record['token']}.json", outcome.__dict__)
            notify(outcome)
        except BaseException as error:
            notify(Result("unknown", f"Independent supervision could not complete: {error}"))
        finally:
            os.close(writing)
            os._exit(0)

    os.close(writing)
    # Normal helper exit waits for observation. A terminal HUP cannot kill the
    # detached observer, which remains B's parent until readiness or confirmed exit.
    threading.Thread(target=os.waitpid, args=(pid, 0), name="controller-supervisor", daemon=False).start()
    with os.fdopen(reading, "r") as incoming:
        response = incoming.readline(8192)
    if not response:
        return Result("unknown", "Independent supervisor exited without an observable result")
    return Result(**json.loads(response))


def confirm_startup():
    """Consume the operation token only when this exact installed successor is ready."""
    token = os.environ.pop(TOKEN, None)
    if token is None:
        return False
    claim = json.loads(token)
    root = _root(Path(claim["root"]))
    identity = identify()
    with _locked(root, wait=True):
        record = _receipt(root)
        if (identity.owner != "venv" or identity.root != root or claim["token"] != record["token"]
                or record["status"] != "published" or _target(root) != record["target"]
                or Path(sys.prefix) != root / record["target"] or identity.running_version != record["version"]):
            raise InstallError("Startup confirmation does not match the published operation")
        record["status"] = "ready"
        _write(root / "operation.json", record)
    return True


def _wheel_update(identity, release, config_path, frontend, *, before_tui_launch=None):
    if frontend == "tui":
        _tui_terminal()
    root = _root(identity.root)
    with _locked(root):
        _reconcile(root)
        if identity != identify():
            raise InstallError("Installed identity changed before the transaction")
        previous = _target(root)
        config = _provider(identity)
        cache = root / (".download-" + secrets.token_hex(12))
        cache.mkdir(mode=0o700)
        wheel = cache / release["filename"]
        try:
            with wheel.open("xb") as output:
                size, digest = _download(release["url"], config, limit=release["size"], destination=output)
            if size != release["size"] or digest != release["sha256"]:
                raise InstallError("Downloaded wheel failed size or SHA-256 validation")
            target = _install(root, wheel, release)
            record = _activate(root, target, release["version"], expected_previous=previous)
        finally:
            wheel.unlink(missing_ok=True)
            cache.rmdir()
    if frontend == "tui" and before_tui_launch is not None:
        before_tui_launch()
    return _launch(root, record, config_path, frontend)


def _aur_update(identity, release, config_path=None, frontend="qt"):
    if (identity.owner != "aur" or identity.launcher != Path("/usr/bin/logos-desktop")
            or frontend not in ("qt", "tui")):
        raise InstallError("AUR restart requires its admitted launcher and a known frontend")
    if os.geteuid() == 0:
        raise InstallError("AUR builds must run as an ordinary user")
    state = identity.provider_path.parent
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    _root(state)
    with _locked(state):
        if identify() != identity:
            raise InstallError("Installed package changed before AUR handoff")

        build = state / ("build-" + secrets.token_hex(12))
        _run(["git", "-c", "http.followRedirects=false", "clone", "--no-checkout", "--",
              release["git_url"], str(build)], timeout=120)
        _run(["git", "checkout", "--detach", release["commit"]], cwd=build)
        if _run(["git", "rev-parse", "HEAD"], cwd=build) != release["commit"]:
            raise InstallError("AUR checkout differs from the accepted revision")

        files = _recipe_files(build, release["commit"])
        fields = _srcinfo(files[".SRCINFO"])
        version = fields.get("pkgver", [""])[0] + "-" + fields.get("pkgrel", [""])[0]
        if fields.get("epoch", ["0"])[0] != "0":
            version = fields["epoch"][0] + ":" + version
        if (fields.get("pkgname") != [PACKAGE] or fields.get("pkgbase") != [PACKAGE]
                or fields.get("arch") != ["x86_64"] or version != release["version"]):
            raise InstallError("Pinned recipe does not match the AUR RPC package/version")

        print(f"Pinned recipe: {release['commit']}\nRetained build directory: {build}")
        review = _run(["git", "--no-pager", "show", "--stat", "--patch", release["commit"]], cwd=build)
        print(_terminal_text(review))
        for name, text in sorted(files.items()):
            print(f"\n===== {name} =====\n{_terminal_text(text)}")
        print("makepkg -si builds this recipe and may install dependencies through pacman authorization.")
        print("It does not upgrade the whole system. Use a fully upgraded Arch system before continuing.")
        if input("Type REVIEWED to build and install this pinned recipe: ") != "REVIEWED":
            return Result("cancelled", "Recipe build was declined")

        # Do not refresh package databases here, which would risk a partial upgrade.
        pending = subprocess.run(["pacman", "-Qu"], env=_environment(), timeout=15,
                                 capture_output=True, text=True, check=False)
        if pending.returncode not in (0, 1) or (pending.returncode == 1 and pending.stderr):
            raise InstallError("Cannot check pending system upgrades")
        if pending.stdout.strip():
            raise InstallError("System upgrades are pending. Complete a normal full upgrade before rebuilding")

        # This sources PKGBUILD, so every admitted file must already be reviewed.
        if _recipe_files(build, release["commit"]) != files:
            raise InstallError("Reviewed recipe files changed before execution")
        generated = _run(["makepkg", "--printsrcinfo"], cwd=build)
        if _srcinfo(generated) != fields:
            raise InstallError("Reviewed recipe generates different source metadata. Installation was refused")

        if _recipe_files(build, release["commit"]) != files:
            raise InstallError("Recipe files changed during metadata generation")
        result = subprocess.run(["makepkg", "-si"], cwd=build, env=_environment(), check=False)
        if result.returncode:
            return Result("failed", "makepkg or package authorization failed. Inspect the retained terminal output")

        installed = _run(["pacman", "-Q", PACKAGE]).split()
        if (installed != [PACKAGE, release["version"]]
                or _owner(Path("/usr/bin/logos-desktop")) != [("pacman", PACKAGE)]):
            return Result("unknown", "Transaction returned but the installed package identity was not verified")
        command = [str(identity.launcher)]
        if config_path:
            command.extend(["-c", str(Path(config_path).expanduser().absolute())])
        command.extend(["--dialog", frontend])
        return Result("restart-required", "The package is installed. Use the restart command below. "
                      "The restarted runtime has not been verified.", release["version"], command=tuple(command))


def _recipe_files(build, commit):
    """Admit only a small flat recipe whose complete tracked contents can be shown."""
    tree = _run(["git", "ls-tree", "-rz", "--full-tree", commit], cwd=build)
    entries = tree.rstrip("\0").split("\0")
    if not 2 <= len(entries) <= 32:
        raise InstallError("Recipe must contain between two and 32 top-level ordinary files")

    files = {}
    total = 0
    for entry in entries:
        header, name = entry.split("\t", 1)
        mode, kind, digest = header.split()
        if (mode not in ("100644", "100755") or kind != "blob"
                or re.fullmatch(r"[A-Za-z0-9._+-]+", name) is None
                or name in (".", "..", ".git") or name in files):
            raise InstallError("Recipe contains nested content, symlinks or unsupported tracked files")
        path = build / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise InstallError("Recipe review requires bounded ordinary files without symlinks")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as incoming:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise InstallError("Recipe review requires ordinary files")
            content = incoming.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise InstallError("Recipe file grew past the review size limit")
        total += len(content)
        if total > 4 * 1024 * 1024:
            raise InstallError("Recipe review exceeds the total size limit")
        actual = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if actual != digest:
            raise InstallError("Recipe working files differ from the pinned Git contents")
        files[name] = content.decode("utf-8", errors="strict")

    if not {"PKGBUILD", ".SRCINFO"}.issubset(files):
        raise InstallError("Recipe requires ordinary tracked PKGBUILD and .SRCINFO files")
    if {path.name for path in build.iterdir()} - {".git"} != set(files):
        raise InstallError("Recipe contains undisplayed directories or untracked files")
    return files


def _terminal_text(text):
    return "".join(char if char in "\n\t" or (char.isprintable() and char != "\x7f")
                   else f"\\x{ord(char):02x}" for char in text)


def _srcinfo(text):
    fields = {}
    for line in text.splitlines():
        if " = " in line:
            key, value = line.strip().split(" = ", 1)
            fields.setdefault(key, []).append(value)
    for key in ("pkgbase", "pkgver", "pkgrel"):
        if len(fields.get(key, [])) != 1:
            raise InstallError("Recipe has missing or ambiguous package identity")
    if len(fields.get("epoch", [])) > 1 or any("SKIP" in values for values in fields.values()):
        raise InstallError("Recipe metadata has ambiguous identity or skipped source checksums")
    return fields


def _installed_bytes(prefix, path):
    """Read bounded owned installation data without following package symlinks."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(prefix) or path == prefix:
        raise InstallError("Installed metadata references a path outside its environment")
    for parent in (path.parent, *path.parent.parents):
        if parent == prefix.parent:
            break
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise InstallError("Installed metadata has an unsafe parent")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 8 * 1024 * 1024):
            raise InstallError("Installed metadata requires bounded owned ordinary files")
        content = source.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise InstallError("Installed file exceeds the inspection limit")
    return content


def installed_evidence(prefix):
    """Validate selected distribution bytes without importing its runtime namespace.

    RECORD is integrity evidence for an explicitly selected owned installation,
    not independent publisher authentication. The incoming wheel has its own digest.
    """
    prefix = Path(prefix)
    _private(prefix, directory=True)
    if VERSION_PATH.fullmatch("versions/" + prefix.name) is None:
        raise InstallError("Migration requires a versioned managed environment")
    matches = list((prefix / "lib").glob("python*/site-packages/logos_desktop-*.dist-info"))
    if len(matches) != 1:
        raise InstallError("Selected environment has ambiguous controller metadata")
    metadata = matches[0]
    site = metadata.parent
    version, digest = prefix.name.split("-", 1)
    contents = {}

    def read(path):
        value = _installed_bytes(prefix, path)
        contents[str(Path(os.path.abspath(path)).relative_to(prefix))] = hashlib.sha256(value).hexdigest()
        return value

    package = BytesParser().parsebytes(read(metadata / "METADATA"))
    if package["Name"] != PACKAGE or package["Version"] != version:
        raise InstallError("Selected distribution identity differs from its environment")
    direct = json.loads(read(metadata / "direct_url.json"))
    if direct.get("archive_info", {}).get("hashes", {}).get("sha256") != digest:
        raise InstallError("Selected environment lacks matching wheel provenance")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(read(metadata / "entry_points.txt").decode())
    entry = parser.get("console_scripts", PACKAGE, fallback="")
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:main", entry) is None:
        raise InstallError("Selected console entry point is not a bounded module target")
    module = entry.split(":", 1)[0]
    entry_path = site / (module.replace(".", "/") + ".py")
    launcher = prefix / "bin/logos-desktop"
    rows = list(csv.reader(io.StringIO(read(metadata / "RECORD").decode())))
    if not 1 <= len(rows) <= 4096:
        raise InstallError("Installed file inventory exceeds the inspection limit")
    recorded = set()
    total = 0
    for row in rows:
        if len(row) != 3 or not row[0] or Path(row[0]).is_absolute():
            raise InstallError("Invalid installed file record")
        path = Path(os.path.abspath(site / row[0]))
        if path in recorded:
            raise InstallError("Duplicate installed file record")
        recorded.add(path)
        content = read(path)
        total += len(content)
        if total > MAX_WHEEL:
            raise InstallError("Installed distribution exceeds the total inspection limit")
        if not row[1]:
            if path != metadata / "RECORD" and path.suffix != ".pyc":
                raise InstallError("Installed distribution has an unhashed source file")
            continue
        expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        if row[1] != expected or row[2] != str(len(content)):
            raise InstallError("Installed file differs from its distribution record")
    required = {entry_path, launcher, metadata / "METADATA", metadata / "entry_points.txt",
                metadata / "direct_url.json", metadata / "RECORD"}
    if not required.issubset(recorded):
        raise InstallError("Installed distribution does not own its entry point and metadata")
    configuration = read(prefix / "pyvenv.cfg").decode()
    if "include-system-site-packages = false" not in configuration:
        raise InstallError("Migration cannot admit foreign system packages")
    lines = read(launcher).decode().splitlines()
    python = str(prefix / "bin/python")
    direct_entry = bool(lines) and lines[0] == "#!" + python
    wrapped = (len(lines) >= 3 and lines[0] == "#!/bin/sh" and lines[1].startswith("'''exec' ")
               and lines[2] == "' '''" and shlex.split(lines[1][len("'''exec' "):]) == [python, "$0", "$@"])
    if not direct_entry and not wrapped:
        raise InstallError("Selected launcher does not bind the selected interpreter")
    return contents


def python_target(command):
    """Read Python script/module argv, including attached short-option operands."""
    arguments = iter(command[1:])
    for argument in arguments:
        if argument == "--":
            return next(arguments, None), None
        if argument == "--check-hash-based-pycs":
            next(arguments, None)
            continue
        if argument.startswith("--"):
            continue
        if not argument.startswith("-") or argument == "-":
            return argument, None
        for index, option in enumerate(argument[1:], 2):
            if option == "c":
                return None, None
            if option == "m":
                return None, argument[index:] or next(arguments, None)
            if option in ("W", "X"):
                if not argument[index:]:
                    next(arguments, None)
                break
    return None, None


def _migration_quiet(root):
    """Refuse same-account actors tied to the selected root, without signaling."""
    previous = root / _target(root)
    entry_points = list((previous / "lib").glob("python*/site-packages/logos_desktop-*.dist-info/entry_points.txt"))
    selected_module = None
    if len(entry_points) == 1:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(_installed_bytes(previous, entry_points[0]).decode())
        selected_module = parser.get("console_scripts", PACKAGE).split(":", 1)[0]
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            command = (process / "cmdline").read_bytes().split(b"\0")
            arguments = [os.fsdecode(value) for value in command if value]
            paths = [value for value in arguments if len(value) <= 4096 and not value.startswith("-")
                     and not any(ord(character) < 32 for character in value)]
            # Resolve invocation paths, including relative interpreters and aliases.
            # Do not inspect unrelated credential agents' protected environments.
            relevant = False
            for value in paths:
                path = Path(value)
                if not path.is_absolute():
                    if "/" not in value and not value.endswith(".py"):
                        continue
                    path = (process / "cwd").resolve(strict=True) / path
                # A venv interpreter is often a symlink to system Python. Keep
                # its invocation directory identity before resolving that link.
                if (path.is_relative_to(root) or path.parent.resolve().is_relative_to(root)
                        or path.resolve().is_relative_to(root)):
                    relevant = True
                    break
            if relevant:
                raise InstallError("Close controllers and helpers using the selected installation before migration")
            _, module = python_target(arguments)
            if selected_module and module:
                if module.split(".")[0] == selected_module.split(".")[0]:
                    environment = (process / "environ").read_bytes().split(b"\0")
                    active = next((Path(os.fsdecode(value.split(b"=", 1)[1])) for value in environment
                                   if value.startswith(b"VIRTUAL_ENV=")), None)
                    invocation = Path(arguments[0])
                    other_environment = (invocation.is_absolute() and
                                         (invocation.parent.parent / "pyvenv.cfg").is_file())
                    if active is not None:
                        if active.resolve().is_relative_to(root):
                            raise InstallError("Close controllers using the selected environment")
                    elif not other_environment:
                        raise InstallError("Selected controller module has an unknown environment. Close it first")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as error:
            raise InstallError("Cannot establish selected controller quiescence") from error


@contextmanager
def _controller_lease(directory, identity):
    """Use the existing account-home controller protocol without importing old code."""
    directory = Path(directory)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    lease = None
    try:
        for part in directory.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022 and not sticky_root:
                raise InstallError("Unsafe controller coordination ancestor")
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InstallError("Controller coordination must be private and owned")
        key = hashlib.sha256(os.fsencode(identity)).hexdigest()
        lease = os.open(key + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        info = os.fstat(lease)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise InstallError("Unsafe selected controller lease")
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("Close the controller for this selected profile before migration") from None
        yield
    finally:
        if lease is not None:
            os.close(lease)
        os.close(descriptor)


@contextmanager
def _migration_reservation(config_path):
    if any(name in os.environ for name in ("WINEPREFIX", "INSTALLDIR", "CONFIG_FILE")):
        raise InstallError("Unset profile environment overrides before explicit migration")
    content = _installed_bytes(config_path.parent, config_path)
    document = json.loads(content)
    if not isinstance(document, dict):
        raise InstallError("Migration requires a JSON profile")
    install = document.get("install_dir", document.get("INSTALLDIR"))
    selected = document.get("WINEPREFIX")
    if install:
        install = Path(install).expanduser()
        if not install.is_absolute():
            install = config_path.parent / install
    if selected:
        prefix = Path(selected).expanduser()
        if not prefix.is_absolute():
            prefix = (install or config_path.parent) / prefix
    else:
        prefix = install / "data/wine64_bottle" if install else None
    identity = f"prefix:{prefix.resolve()}" if prefix else f"profile:{config_path.resolve()}"
    directory = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/logos-desktop/coordination"

    def verify():
        if _installed_bytes(config_path.parent, config_path) != content:
            raise InstallError("Selected profile changed during migration")
        _migration_recovery_clear(directory, install, prefix)

    with _controller_lease(directory, identity):
        verify()
        yield verify


def _migration_recovery_clear(coordination, install, prefix):
    """Leave pending prefix transactions to the predecessor before replacing it."""
    state_home = os.environ.get("XDG_STATE_HOME", "")
    state = Path(state_home) if Path(state_home).is_absolute() else Path.home() / ".local/state"
    states = {(state / "logos-desktop", install),
              (coordination.parent, install)}
    if prefix is not None:
        prefix = prefix.resolve()
        key = hashlib.sha256(os.fsencode(prefix)).hexdigest()
        directory = coordination / "operations" / key
        try:
            directory.lstat()
        except FileNotFoundError:
            pass
        else:
            _private(directory.parent, directory=True)
            _private(directory, directory=True)
            entries = list(directory.iterdir())
            if len(entries) > 128:
                raise InstallError("Too many recorded recovery locations. Inspect them with the previous controller")
            for path in entries:
                if not path.name.startswith("state-") or path.suffix != ".json":
                    continue
                content = _installed_bytes(coordination, path)
                location = json.loads(content)
                if (not isinstance(location, dict) or len(content) > 16384
                        or path.name != "state-" + hashlib.sha256(content).hexdigest() + ".json"
                        or location.get("schema_version") != 1 or location.get("prefix") != str(prefix)
                        or not isinstance(location.get("state_dir"), str)
                        or not Path(location["state_dir"]).is_absolute()):
                    raise InstallError("Invalid known recovery location. Inspect it before migration")
                previous_install = location.get("install")
                if previous_install is not None and (not isinstance(previous_install, str)
                                                     or not Path(previous_install).is_absolute()):
                    raise InstallError("Invalid known installation recovery location")
                states.add((Path(location["state_dir"]), Path(previous_install) if previous_install else install))
    for state, installation in states:
        paths = []
        if installation is not None:
            key = hashlib.sha256(os.fsencode(installation.resolve())).hexdigest()
            paths.append(state / "runtime-selection" / key / "runtime.json")
        if prefix is not None:
            key = hashlib.sha256(os.fsencode(prefix)).hexdigest()
            paths.extend(state / "maintenance" / key / name for name in ("restore.json", "restore.pending"))
        for path in paths:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            raise InstallError(
                "Complete pending runtime/restore recovery with the previous controller before migration"
            )


def migrate(root, wheel, data, config_path, frontend):
    """Migrate an explicitly selected managed environment without a version jump."""
    if not config_path or not Path(config_path).expanduser().is_absolute():
        raise InstallError("Migration requires an explicit absolute configuration path")
    config_path = Path(config_path).expanduser()
    if not config_path.is_file() or config_path.is_symlink():
        raise InstallError("Migration requires an existing ordinary configuration file")
    if frontend == "tui":
        _tui_terminal()
    with _migration_reservation(config_path) as verify_profile, _locked(root):
        _reconcile(root)
        _private(root / "logos-desktop")
        if (root / "logos-desktop").read_text() != _launcher(root):
            raise InstallError("Selected stable launcher changed")
        previous = _target(root)
        evidence = installed_evidence(root / previous)
        provider = root / "provider.json"
        provider_before = None
        if provider.exists() or provider.is_symlink():
            provider_before = _provider(Identity("venv", root=root, provider_path=provider))
        _migration_quiet(root)
        target = _install(root, wheel, data)
        if target == previous:
            raise InstallError("Selected wheel is already installed")
        _migration_quiet(root)
        if installed_evidence(root / previous) != evidence:
            raise InstallError("Selected installation changed during migration")
        provider_after = _read(provider) if provider.exists() or provider.is_symlink() else None
        if provider_before != provider_after:
            raise InstallError("Provider changed during migration")
        verify_profile()
        record = _activate(root, target, data["version"], expected_previous=previous, previous_files=evidence)
    result = _launch(root, record, config_path, frontend)
    if result.status == "updated":
        return Result("migrated", "The renamed controller confirmed readiness. Previous environment retained",
                      data["version"])
    return result


def bootstrap(argv=None):
    """Install an exact wheel, or explicitly migrate a selected managed root."""
    parser = argparse.ArgumentParser(description=bootstrap.__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--frontend", choices=("qt", "tui"), default="qt")
    args = parser.parse_args(argv)
    root = _root(args.root, create=not args.migrate)
    data = _wheel({"version": args.version, "sha256": args.sha256, "size": args.size,
                   "filename": args.wheel.name}, stable=False)
    if args.migrate:
        result = migrate(root, args.wheel.absolute(), data, args.config, args.frontend)
        print(f"{result.status}: {result.message}")
        return 0 if result.status == "migrated" else 1
    with _locked(root):
        occupied = ("current", "logos-desktop", "operation.json")
        if any((root / name).exists() or (root / name).is_symlink() for name in occupied):
            raise InstallError("Bootstrap requires a fresh installation root")
        target = _install(root, args.wheel.absolute(), data)
        launcher = root / "logos-desktop"
        with launcher.open("x") as output:
            output.write(_launcher(root))
            output.flush()
            os.fchmod(output.fileno(), 0o700)
            os.fsync(output.fileno())
        _publish(root, target)
    print(f"Installed {args.version}. Start {root / 'logos-desktop'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=(
        "check", "handoff", "recover", "rollback", "bootstrap", "configure-provider",
    ))
    parser.add_argument("--config")
    parser.add_argument("--frontend", choices=("qt", "tui"), default="qt")
    args, remaining = parser.parse_known_args(argv)
    helper_keeps_terminal = True

    def release_tui_terminal():
        nonlocal helper_keeps_terminal
        helper_keeps_terminal = False

    try:
        if args.action == "bootstrap":
            return bootstrap(remaining)
        if remaining:
            parser.error("Unexpected arguments: " + shlex.join(remaining))
        identity = identify()
        if args.action == "configure-provider":
            result = configure_provider(identity)
            print(f"{result.status}: {result.message}")
            return 0
        if args.action == "recover":
            result = recover(identity)
            print(f"{result.status}: {result.message}")
            return 0 if result.status == "recovered" else 1
        if args.action == "rollback":
            if identity.owner != "venv":
                raise InstallError("Only the canonical environment supports pointer rollback")
            with _locked(identity.root):
                _recover(identity.root, _receipt(identity.root), explicit=True)
            print("Previous environment restored. Restart using the stable launcher.")
            return 0
        if args.action == "handoff" and identity.owner == "venv":
            recovery = recover(identity)
            if recovery.status != "recovered":
                print(f"{recovery.status}: {recovery.message}")
                return 1
        result = check(identity)
        if args.action == "handoff" and result.status == "available":
            print(f"Installed: {identity.installed_version}. Available: {result.version}")
            if identity.owner == "venv":
                if input("Type UPDATE to install and launch the new controller: ") != "UPDATE":
                    result = Result("cancelled", "Update was declined")
                else:
                    result = _wheel_update(identity, result.artifact, args.config, args.frontend,
                                           before_tui_launch=release_tui_terminal)
                    # A confirmed failed launch has no live successor reading this
                    # terminal. Unknown outcomes retain the conservative handoff.
                    if result.status == "failed":
                        helper_keeps_terminal = True
            else:
                result = _aur_update(identity, result.artifact, args.config, args.frontend)
        print(f"{result.status}: {result.message}")
        if result.status == "restart-required" and result.command:
            print("Restart command: " + shlex.join(result.command))
        return 0 if result.status in ("available", "current", "updated", "restart-required", "cancelled") else 1
    except (InstallError, OSError, ValueError, KeyError) as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1
    finally:
        if args.action == "handoff" and helper_keeps_terminal and sys.stdin.isatty():
            try:
                input("Press Enter to close this helper: ")
            except EOFError:
                pass


if __name__ == "__main__":
    sys.exit(main())
