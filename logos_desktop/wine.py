from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import logging
import os
import re
import shutil
import secrets
import stat
import subprocess
from pathlib import Path
from packaging.version import Version
import tempfile
import threading
import time
from typing import IO, Optional

from logos_desktop import artifacts, compatibility, constants, maintenance, runtime
from logos_desktop.app import App
from logos_desktop.compatibility import WineVersion as WineRelease

from . import network
from . import system
from . import utils
from . import paths

def check_wineserver(app: App) -> bool:
    run_wine_during_install(app, app.conf.wineserver_binary, timeout=30)
    return True


def wineserver_kill(app: App):
    return run_wine_during_install(app, app.conf.wineserver_binary, exe_args=["-k"], timeout=30)


def wineserver_wait(app: App):
    return run_wine_during_install(app, app.conf.wineserver_binary, exe_args=["-w"], timeout=60)


def get_devel_or_stable(version: str) -> str:
    output = version if version.startswith("wine-") else f"wine-{version}"
    return compatibility.parse_wine_version(output).channel.value


# FIXME: consider raising exceptions on error
def get_wine_release(binary: str) -> tuple[Optional[WineRelease], str]:
    try:
        with maintenance.artifact_use():
            from . import processes, updates

            cmd = [runtime.executable(binary), "--version"]
            output, status = processes.capture(cmd, env=updates.fresh_environment(), timeout=15,
                                               limit=16384, label="Wine version probe")
            if status != 0:
                raise RuntimeError("Wine version probe failed")
            version_string = output.decode("utf-8").strip()
        logging.debug(f"Version string: {str(version_string)}")
        return compatibility.parse_wine_version(version_string), "yes"

    except subprocess.CalledProcessError as e:
        return None, f"Error running command: {e}"

    except ValueError as e:
        return None, f"Error parsing version: {e}"

    except Exception as e:
        return None, f"Error: {e}"


def product_release(value: str) -> Version:
    """Parse the four numeric fields used by the product installer feed."""
    if not isinstance(value, str) or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value) is None:
        raise maintenance.MaintenanceError(f"Invalid target product release: {value!r}")
    return Version(value)


def check_wine_rules(
    wine_release: Optional[WineRelease],
    release_version: Optional[str],
    faithlife_product_version: str
):
    logging.debug(f"Checking {wine_release} for {release_version}.")
    target = None
    if faithlife_product_version == "10" and release_version is not None:
        target = product_release(release_version)
    return compatibility.wine_eligibility(wine_release, target, faithlife_product_version)


def require_compatible(app: App, binary: str | Path):
    release, reason = get_wine_release(str(binary))
    if release is None:
        raise maintenance.MaintenanceError(f"Cannot use the selected Wine runtime: {reason}")
    compatible, reason = check_wine_rules(
        release, app.conf.faithlife_product_release, app.conf.faithlife_product_version,
    )
    if not compatible:
        raise maintenance.MaintenanceError(reason)


def native_launcher(binary: str | Path) -> str:
    """Select the companion without resolving away a runtime's wrapper path.

    Script launchers are supported, but their version does not certify architecture.
    Reject known incompatible ELF files without booting a prefix to probe a wrapper.
    """
    selected = Path(binary)
    if not selected.exists() or selected.name.lower().endswith(".appimage"):
        return str(selected)
    companion = selected.parent / "wine64"
    if selected.name == "wine" and companion.is_file() and os.access(companion, os.X_OK):
        selected = companion
    if not selected.is_file():
        raise maintenance.MaintenanceError(f"Wine launcher is not a regular file: {selected}")
    with selected.open("rb") as stream:
        header = stream.read(20)
    if header.startswith(b"\x7fELF"):
        if len(header) < 20 or header[4:6] != b"\x02\x01" or header[18:20] != b"\x3e\x00":
            raise maintenance.MaintenanceError(f"Wine requires an x86_64 launcher: {selected}")
    return str(selected)


def check_wine_version_and_branch(release_version: Optional[str], test_binary,
                                  faithlife_product_version, *, server=None):
    if not os.path.exists(test_binary):
        reason = "Binary does not exist."
        return False, reason

    if not os.access(test_binary, os.X_OK):
        reason = "Binary is not executable."
        return False, reason

    wine_release, error_message = get_wine_release(test_binary)

    if wine_release is None:
        return False, error_message

    if server is not None:
        from . import processes, updates

        try:
            output, status = processes.capture(
                [runtime.executable(server), "--version"], env=updates.fresh_environment(),
                timeout=15, limit=16384, label="Wineserver version probe",
            )
            if status != 0:
                return False, "Could not identify the matching wineserver version"
            server_version = compatibility.parse_wineserver_version(output.decode("utf-8"))
            if server_version != wine_release.version:
                return False, "Wine and wineserver versions do not match"
        except (OSError, ValueError, RuntimeError) as error:
            return False, f"Could not probe the matching wineserver: {error}"

    result, message = check_wine_rules(
        wine_release,
        release_version,
        faithlife_product_version
    )
    if not result:
        return result, message

    return True, "None"


def initializeWineBottle(wine64_binary: str, app: App):
    app.status("Initializing wine bottle…")
    logging.debug(f"{wine64_binary=}")
    # Avoid wine-mono window
    wine_dll_override="mscoree="
    logging.debug(f"Running: {wine64_binary} wineboot --init")
    run_wine_during_install(
        app=app,
        wine_binary=wine64_binary,
        exe='wineboot',
        exe_args=['--init'],
        init=True,
        additional_wine_dll_overrides=wine_dll_override
    )


@maintenance.uses_installation
def set_win_version(app: App, exe: str, windows_version: str):
    if exe == "logos":
        # This operation is equivilent to f"winetricks -q settings {windows_version}"
        # but faster
        run_wine_during_install(
            app,
            app.conf.wine_binary,
            exe_args=('winecfg', '/v', windows_version)
        )

    elif exe == "indexer":
        reg = f"HKCU\\Software\\Wine\\AppDefaults\\{app.conf.faithlife_product}Indexer.exe"
        exe_args = [
            'add',
            reg,
            "/v", "Version",
            "/t", "REG_SZ",
            "/d", f"{windows_version}", "/f",
            ]
        process = run_wine_during_install(
            app,
            app.conf.wine_binary,
            exe='reg',
            exe_args=exe_args
        )
        if process is None:
            raise maintenance.MaintenanceError("Failed to set the indexer's Windows version")


def wine_reg_query(app: App, key_name: str, value_name: str) -> Optional[str]:
    """Query the registry

    Example command and output:

    ```    
    $ wine reg query "HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts" /v "Arial (TrueType)"

    HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts
        Arial (TrueType)    REG_SZ    Z:\\usr\\share\\fonts\\truetype\\msttcorefonts\\Arial.ttf

    ```
    """
    process = run_wine_completed_process(
        app=app,
        wine_binary=app.conf.wine64_binary,
        exe="reg.exe",
        exe_args=[
            "query",
            key_name,
            "/v",
            value_name
        ]
    )
    if process is None:
        raise maintenance.MaintenanceError("Registry query could not be started")
    if process.returncode == 1:
        # Key not found
        return None
    if process.returncode != 0:
        raise maintenance.MaintenanceError(f"Registry query failed with exit code {process.returncode}")

    lines = [line for line in process.stdout.splitlines() if line.strip()]
    # Wine reg.exe uses four spaces between columns. Preserve data whitespace.
    pattern = rf"    {re.escape(value_name)}    REG_SZ(?:    (.*))?"
    if len(lines) == 2 and lines[0].strip().casefold() == key_name.casefold():
        match = re.fullmatch(pattern, lines[1])
        if match is not None and "\x00" not in lines[1]:
            return match.group(1) or ""
    raise maintenance.MaintenanceError("Registry query returned an unexpected value or type")


def wine_reg_install(app: App, name: str, reg_text: str, wine64_binary: str):
    with tempfile.TemporaryDirectory() as tempdir:
        reg_file = Path(tempdir) / name
        reg_file.write_text(reg_text)
        app.status(f"Installing registry file: {reg_file}")  
        run_wine_during_install(
            app=app, wine_binary=wine64_binary, exe="regedit.exe",
            exe_args=[str(reg_file)], timeout=60,
        )
        wineserver_wait(app)
        logging.info("Registry file installed: %s", name)


def disable_winemenubuilder(app: App, wine64_binary: str):
    name='disable-winemenubuilder.reg'
    reg_text = r'''REGEDIT4

[HKEY_CURRENT_USER\Software\Wine\DllOverrides]
"winemenubuilder.exe"=""
'''
    wine_reg_install(app, name, reg_text, wine64_binary)


@maintenance.uses_installation
def set_renderer(app: App, wine64_binary: str, value: str):
    name=f'set-renderer-to-{value}.reg'
    reg_text = rf'''REGEDIT4

[HKEY_CURRENT_USER\Software\Wine\Direct3D]
"renderer"="{value}"
'''
    wine_reg_install(app, name, reg_text, wine64_binary)


def set_fontsmoothing_to_rgb(app: App, wine64_binary: str):
    # Possible registry values:
    # "disable":      FontSmoothing=0; FontSmoothingOrientation=1; FontSmoothingType=0
    # "gray/grey":    FontSmoothing=2; FontSmoothingOrientation=1; FontSmoothingType=1
    # "bgr":          FontSmoothing=2; FontSmoothingOrientation=0; FontSmoothingType=2
    # "rgb":          FontSmoothing=2; FontSmoothingOrientation=1; FontSmoothingType=2
    # https://github.com/Winetricks/winetricks/blob/8cf82b3c08567fff6d3fb440cbbf61ac5cc9f9aa/src/winetricks#L17411

    name='set-fontsmoothing-to-rgb.reg'
    reg_text = r'''REGEDIT4

[HKEY_CURRENT_USER\Control Panel\Desktop]
"FontSmoothing"="2"
"FontSmoothingGamma"=dword:00000578
"FontSmoothingOrientation"=dword:00000001
"FontSmoothingType"=dword:00000002
'''
    wine_reg_install(app, name, reg_text, wine64_binary)


@maintenance.changes_installation
def install_msi(app: App, accepted: artifacts.AcceptedArtifact, *, target_release: str):
    target_version = product_release(target_release)
    app.status(f"Running MSI installer: {accepted.spec.name}.")
    # Define the Wine executable and initial arguments for msiexec
    wine_binary = app.conf.wine64_binary
    if accepted.spec.kind != "msi":
        raise artifacts.ArtifactError("MSI installation requires an accepted MSI artifact")
    accepted.check(checkpoint=app.checkpoint)
    exe_args = ["/i"]
    passive = False

    # Ensure the user agrees to the EULA. Exit if they don't.
    # This code block tells the MSI installer to run non-interactively.
    # There have been 2 reports in the wild, once on ctrlalt24's computer and on Itsjoe
    # where the installer wouldn't launch interactively.
    # Since the launcher only prompts for EULA, and askes where to install 
    # (which is pointless as it's in it's own wine prefix), we can just skip the interaction
    # entirely once we confirm the user has agreed to EULA (for Faithlife's sake)
    if (
        app.conf._overrides.agreed_to_faithlife_terms or
        app.approve_or_exit("Do you agree to Faithlife's EULA?\nhttps://faithlife.com/terms")
    ):
        passive = True

    mst_path = None
    if target_version > Version("39.0.0.0"):
        mst_path = constants.APP_ASSETS_DIR / "LogosStubFailOK.mst"
        if not mst_path.is_file() or mst_path.stat().st_size == 0:
            raise maintenance.MaintenanceError(f"Required MSI transform is missing: {mst_path}")

    context = maintenance.Context.from_app(app)
    _wait_for_msi_prefix(context, checkpoint=app.checkpoint)
    with _msi_inputs(app, accepted, mst_path) as inputs:
        exe_args.append(inputs.windows_msi)
        if passive:
            exe_args.append("/passive")
        if inputs.windows_transform is not None:
            exe_args.append(f"TRANSFORMS={inputs.windows_transform}")
        app.checkpoint()
        inputs.accepted.check(checkpoint=app.checkpoint)
        if (context.prefix / "dosdevices/c:").resolve(strict=True) != inputs.accepted.path.parent.parent:
            raise maintenance.MaintenanceError("Installer C drive mapping changed before execution")
        failure = None
        try:
            logging.info("Running: %s msiexec %s", wine_binary, " ".join(exe_args))
            result = run_wine_during_install(app, wine_binary, exe="msiexec", exe_args=exe_args, timeout=None)
            if result is not None:
                result.wait()
            if result is None or result.returncode != 0:
                raise maintenance.MaintenanceError("Logos MSI installation failed")
            return result
        except BaseException as error:
            failure = error
            raise
        finally:
            # Cancellation must not remove inputs or release admission while a
            # Windows Installer service or detached Wine child still uses them.
            try:
                _wait_for_msi_prefix(context)
            except BaseException:
                if failure is None:
                    raise
                logging.exception("Installer settlement was interrupted after an earlier failure")


def _wait_for_msi_prefix(context, *, checkpoint=None):
    deadline = time.monotonic() + 60
    idle_since = None
    deferred = None
    while True:
        try:
            if checkpoint is not None:
                checkpoint()
            try:
                maintenance.assert_quiescent(context)
            except maintenance.MaintenanceBusy:
                idle_since = None
                if checkpoint is not None and time.monotonic() >= deadline:
                    raise
            else:
                if idle_since is not None and time.monotonic() - idle_since >= 0.3:
                    break
                if idle_since is None:
                    idle_since = time.monotonic()
            time.sleep(0.1)
        except BaseException as error:
            if checkpoint is not None:
                raise
            # An interrupted wait or unreadable process outcome must not release
            # installation ownership. Propagate it only after observed settlement.
            deferred = deferred or error
            idle_since = None
    if deferred is not None:
        raise deferred


@dataclass(frozen=True)
class _MsiInputs:
    accepted: artifacts.AcceptedArtifact
    windows_msi: str
    windows_transform: str | None


def _copy_msi_input(source, directory_fd, name, size, digest, checkpoint):
    """Copy only the bounded expected bytes into a new private input file."""
    incoming = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(incoming, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise artifacts.ArtifactError("Installer input source changed")
        output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        with os.fdopen(output, "wb") as target:
            copied = 0
            actual = hashlib.sha256()
            while True:
                checkpoint()
                chunk = stream.read(min(1024 * 1024, size - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > size:
                    raise artifacts.ArtifactError("Installer input grew while staging")
                actual.update(chunk)
                target.write(chunk)
            after = os.fstat(stream.fileno())
            if (
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or copied != size or actual.hexdigest() != digest
            ):
                raise artifacts.ArtifactError("Installer input changed while staging")
            target.flush()
            os.fchmod(target.fileno(), 0o400)
            os.fsync(target.fileno())


@contextmanager
def _msi_inputs(app, accepted, transform):
    """Stage checked inputs at short C names without changing their artifact role."""
    checkpoint = app.checkpoint
    checkpoint()
    accepted.check(checkpoint=checkpoint)
    prefix = maintenance.Context.from_app(app).prefix
    drive_fd = stage_fd = None
    stage_name = None
    failed = False
    try:
        devices = prefix / "dosdevices"
        drive = devices / "c:"
        if devices.is_symlink() or not drive.is_symlink():
            raise maintenance.MaintenanceError("Installer staging requires a verified prefix-local C drive")
        target = drive.resolve(strict=True)
        if target == prefix or not target.is_relative_to(prefix):
            raise maintenance.MaintenanceError("Installer C drive must stay inside the selected Wine prefix")
        drive_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(drive_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise maintenance.MaintenanceError("Installer C drive must be owned and not writable by other users")

        transform_size = 0
        transform_hash = None
        if transform is not None:
            transform_hash = artifacts.regular_hash(transform, checkpoint=checkpoint)
            transform_size = transform.stat().st_size
            if transform_size == 0:
                raise maintenance.MaintenanceError("Required MSI transform is empty")
        space = os.fstatvfs(drive_fd)
        required = accepted.size + transform_size + 16 * 1024 * 1024
        available = space.f_bavail * space.f_frsize
        if available < required:
            raise maintenance.MaintenanceError(
                f"Installer input staging needs {required} bytes, but only {available} bytes are available"
            )

        checkpoint()
        stage_name = "LogosDesktopInstall-" + secrets.token_hex(6)
        os.mkdir(stage_name, 0o700, dir_fd=drive_fd)
        stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=drive_fd)
        stage_info = os.fstat(stage_fd)
        stage = target / stage_name
        _copy_msi_input(accepted.path, stage_fd, "installer.msi", accepted.size, accepted.sha256, checkpoint)
        staged = replace(accepted, path=stage / "installer.msi")
        staged.check(checkpoint=checkpoint)
        if transform is not None:
            _copy_msi_input(transform, stage_fd, "installer.mst", transform_size, transform_hash, checkpoint)
            if (artifacts.regular_hash(transform, checkpoint=checkpoint) != transform_hash
                    or artifacts.regular_hash(stage / "installer.mst", checkpoint=checkpoint) != transform_hash):
                raise artifacts.ArtifactError("MSI transform changed while staging")
        os.fsync(stage_fd)
        if drive.resolve(strict=True) != target:
            raise maintenance.MaintenanceError("Installer C drive mapping changed while staging")
        checkpoint()
        windows = "\\\\?\\C:\\" + stage_name + "\\"
        yield _MsiInputs(staged, windows + "installer.msi", windows + "installer.mst" if transform else None)
    except BaseException as error:
        failed = True
        if isinstance(error, OSError):
            raise maintenance.MaintenanceError(f"Cannot stage private installer inputs: {error}") from error
        raise
    finally:
        if stage_fd is not None:
            try:
                current = os.stat(stage_name, dir_fd=drive_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (stage_info.st_dev, stage_info.st_ino):
                    raise maintenance.MaintenanceError("Installer staging directory changed, inputs preserved")
                for name in ("installer.msi", "installer.mst"):
                    try:
                        os.unlink(name, dir_fd=stage_fd)
                    except FileNotFoundError:
                        pass
                os.rmdir(stage_name, dir_fd=drive_fd)
            except (OSError, maintenance.MaintenanceError) as error:
                if not failed:
                    raise maintenance.MaintenanceError(
                        "Cannot clean owned installer staging, directory preserved"
                    ) from error
                logging.warning("Installer staging cleanup failed after an earlier error: %s", error)
            finally:
                os.close(stage_fd)
                os.close(drive_fd)
        elif drive_fd is not None:
            os.close(drive_fd)


@maintenance.uses_installation
def windows_path(app: App, wine_binary: str, source: Path) -> str:
    """Use an extended Windows name so MSI can read long immutable store paths."""
    source = Path(source)
    if not source.is_absolute() or any(
        re.search(r'[<>:"|?*\\\x00-\x1f\x7f]', part)
        or part.endswith((" ", "."))
        for part in source.parts[1:]
    ):
        raise maintenance.MaintenanceError("Unsupported Windows installer input path")

    # Wine 11.19 winepath corrupts stdout at MAX_PATH, even with exit status zero.
    # Read the selected prefix's actual mappings instead of guessing a Z drive.
    if len(os.fsencode(source)) + 2 >= 260:
        try:
            resolved = source.resolve(strict=True)
            mappings = Path(app.conf.wine_prefix) / "dosdevices"
            candidates = []
            for drive in sorted(mappings.glob("[a-z]:")):
                if not drive.is_symlink():
                    continue
                target = drive.resolve(strict=True)
                if not target.is_dir() or not resolved.is_relative_to(target):
                    continue
                relative = resolved.relative_to(target)
                candidates.append((len(target.parts), drive.name[0].upper(), relative))
            if not candidates:
                raise ValueError("No DOS drive maps the installer input")
            _, drive_letter, relative = max(candidates)
            for part in relative.parts:
                if (
                    re.search(r'[<>:"|?*\\\x00-\x1f\x7f]', part)
                    or part.endswith((" ", "."))
                    or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
                    or len(part.encode("utf-16-le")) // 2 > 255
                ):
                    raise ValueError("Unrepresentable Windows path component")
            pathname = "\\\\?\\" + drive_letter + ":\\" + "\\".join(relative.parts)
            if len(pathname.encode("utf-16-le")) // 2 >= 32767:
                raise ValueError("Windows installer input path is too long")
            return pathname
        except (OSError, ValueError, RuntimeError) as error:
            raise maintenance.MaintenanceError(
                "Cannot map long installer input through the selected Wine prefix"
            ) from error

    converted = run_wine_completed_process(app, wine_binary, exe_args=["winepath", "-w", str(source)])
    if converted is None or converted.returncode != 0 or not isinstance(converted.stdout, str):
        raise maintenance.MaintenanceError(f"winepath failed to convert installer input: {source.name}")
    pathname = converted.stdout.strip()
    if pathname.startswith("\\\\?\\"):
        pathname = pathname[4:]
    if re.fullmatch(r"[A-Za-z]:\\[^\r\n\x00]+", pathname) is None:
        raise maintenance.MaintenanceError(f"winepath returned an invalid installer path: {source.name}")
    return "\\\\?\\" + pathname


def run_winetricks(app: App, *args):
    cmd = [*args]
    if "-q" not in args and app.conf.winetricks_binary:
        cmd.insert(0, "-q")
    logging.info(f"running \"winetricks {' '.join(cmd)}\"")
    run_wine_during_install(app, app.conf.winetricks_binary, exe_args=cmd, timeout=None)
    logging.info(f"\"winetricks {' '.join(cmd)}\" DONE!")
    logging.debug(f"procs using {app.conf.wine_prefix}:")
    for proc in utils.get_procs_using_file(app.conf.wine_prefix):
        logging.debug(f"winetricks proc: {proc=}")
    else:
        logging.debug('winetricks proc: <None>')


def install_fonts(app: App):
    """Check and, when authorized, repair the selected prefix's Arial prerequisite."""
    from . import fonts

    fonts.ensure_arial(app)


def get_winecmd_encoding(app: App) -> Optional[str]:
    # Get wine system's cmd.exe encoding for proper decoding to UTF8 later.
    logging.debug("Getting wine system's cmd.exe encoding.")
    registry_value = get_registry_value(
        'HKCU\\Software\\Wine\\Fonts',
        'Codepages',
        app
    )
    if registry_value is not None:
        codepages: str = registry_value.split(',')
        return codepages[-1]
    else:
        m = "wine.wine_proc: wine.get_registry_value returned None."
        logging.error(m)
        return None


@maintenance.uses_installation
def run_wine_completed_process(
    app: App,
    wine_binary: str,
    exe=None,
    exe_args=None,
    additional_wine_dll_overrides: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    """Keep parsed stdout strict and escape non-UTF-8 diagnostic bytes separately."""
    command, env = _wine_invocation(app, wine_binary, additional_wine_dll_overrides)
    if exe is not None:
        command.append(exe)
    if exe_args:
        command.extend(exe_args)
    try:
        result = subprocess.run(
            command, env=system.fix_ld_library_path(env), capture_output=True,
            timeout=60,
        )
        for stream, errors in (("stdout", "strict"), ("stderr", "backslashreplace")):
            data = getattr(result, stream, None)
            if data is not None:
                text = data.decode("utf-8", errors=errors)
                setattr(result, stream, text.replace("\r\n", "\n").replace("\r", "\n"))
        return result
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise maintenance.MaintenanceError(f"Wine helper failed: {command!r}: {error}") from error


@maintenance.uses_installation
def run_wine_process(
    app: App,
    wine_binary: str | Path,
    stdout: IO[str],
    stderr: IO[str],
    stdin = None,
    exe=None,
    exe_args=None,
    additional_wine_dll_overrides: Optional[str] = None,
    detach_on_close: bool = True,
) -> Optional[subprocess.Popen[bytes]]:
    """Runs wine directly
    
    Args:
    - app: App
    - stdout: Where to send stdout, must be a file descriptor
    - stderr: Where to send stderr, must be a file descriptor
    - stderr: Where stdin should come from, must be a file descriptor
    - exe: The windows executable to run
    - exe_args: arguments to the aforementioned exe
    - init: whether or not this call is to initialize the bottle
    - additional_wine_dll_overrides: Add to WINEDLLOVERRIDES
    """
    command, env = _wine_invocation(app, wine_binary, additional_wine_dll_overrides)
    if exe is not None:
        command.append(exe)
    if exe_args:
        command.extend(exe_args)

    cmd = f"Running wine cmd: '{' '.join(command)}'"
    logging.debug(cmd)
    stdout.write(f"{utils.get_timestamp()}: {cmd}\n")
    prefix_lease = None
    if not maintenance.installation_owned():
        prefix_lease = maintenance.Lease(maintenance.Context.from_app(app))
    lease = maintenance.artifact_use()
    try:
        lease.__enter__()
    except BaseException:
        if prefix_lease is not None:
            prefix_lease.close()
        raise
    transferred = False
    process = None

    def release():
        try:
            closing = getattr(app, "_closing", None)
            while process.poll() is None:
                if detach_on_close and isinstance(closing, threading.Event) and closing.wait(0.1):
                    return
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if prefix_lease is not None:
                prefix_lease.close()
            lease.__exit__(None, None, None)

    try:
        process = system.popen_command(
            command, stdout=stdout, stderr=stderr, stdin=stdin, env=env,
            start_new_session=True, encoding='utf-8',
        )
        if process is None:
            return None
        if isinstance(app, App):
            app.start_thread(release, daemon_bool=detach_on_close)
        else:
            threading.Thread(target=release, name="Artifact process lifetime", daemon=detach_on_close).start()
        transferred = True
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
        raise
    finally:
        if not transferred:
            if prefix_lease is not None:
                prefix_lease.close()
            lease.__exit__(None, None, None)


@maintenance.uses_installation
def run_wine_application(
    app: App,
    wine_binary: str | Path,
    exe=None,
    exe_args=None,
    additional_wine_dll_overrides: Optional[str] = None,
) -> Optional[subprocess.Popen[bytes]]:
    """Run a wine application.
     
    Store the log in a dedicated file, keeping the previous log.
    """
    current_log_path = Path(app.conf.app_wine_log_path)
    previous_log_path = Path(app.conf.app_wine_log_previous_path)
    if current_log_path.exists():
        shutil.move(current_log_path, previous_log_path)
    with open(current_log_path, 'w') as wine_log:
        return run_wine_process(
            app=app,
            wine_binary=wine_binary,
            stdout=wine_log,
            stderr=wine_log,
            exe=exe,
            exe_args=exe_args,
            additional_wine_dll_overrides=additional_wine_dll_overrides,
        )


@maintenance.uses_installation
def run_wine_during_install(
    app: App,
    wine_binary: str | Path,
    exe=None,
    exe_args=None,
    init=False,
    additional_wine_dll_overrides: Optional[str] = None,
    timeout: float | None = 120,
) -> Optional[subprocess.Popen[bytes]]:
    """Wait for Wine and raise a domain error if spawn or execution fails."""
    with tempfile.NamedTemporaryFile(mode="w+") as temp_file:
        process = run_wine_process(
            app=app,
            wine_binary=wine_binary,
            stdout=temp_file,
            stderr=temp_file,
            exe=exe,
            exe_args=exe_args,
            additional_wine_dll_overrides=additional_wine_dll_overrides,
            detach_on_close=False,
        )
        if process is None:
            raise maintenance.MaintenanceError("Wine command could not be started")
        command = [str(wine_binary), *([exe] if exe else []), *map(str, exe_args or [])]
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise maintenance.MaintenanceError(f"Wine command timed out: {command!r}") from error
        finally:
            temp_file.seek(0)
            for line in temp_file:
                logging.debug("> %s", line.rstrip())
        if process.returncode != 0:
            raise maintenance.MaintenanceError(
                f"Wine command failed with exit code {process.returncode}: {command!r}"
            )
    return process


# FIXME: Consider when to re-run this if it changes.
# Perhaps we should have a "apply installation updates"
# or similar mechanism to ensure all of our latest methods are installed
# including but not limited to: system packages, icu files, fonts, registry
# edits, etc.
#
# Seems like we want to have a more holistic mechanism for ensuring
# all users use the latest and greatest.
# Sort of like an update, but for wine and all of the bits underneath "Logos" itself
@maintenance.uses_installation
def enforce_icu_data_files(app: App):
    app.status("Downloading ICU files…")
    release = app.conf._network.icu_latest_version()
    icu_filename = f"icu-win-{release.version}.tar.gz"
    accepted = network.logos_reuse_download(
        release.download_url,
        icu_filename,
        app.conf.download_dir,
        app=app,
        kind="icu",
        digest=release.digest,
        size=release.size,
    )

    app.status("Copying ICU files…")

    with tempfile.TemporaryDirectory(prefix="logos-desktop-icu-") as temporary:
        staging = Path(temporary) / "extracted"
        utils.untar_file(accepted.check(), staging)
        candidates = [path for path in (staging / "windows", staging / "icu-win/windows") if path.is_dir()]
        if len(candidates) != 1:
            raise ValueError("ICU archive must contain one supported Windows subtree")
        copy_icu_files(candidates[0], Path(app.conf.wine_prefix))

    app.status("ICU files copied.", 100)


def _icu_parent(prefix_fd: int, parts: tuple[str, ...], *, create: bool) -> int | None:
    """Open destination directories without following links, anchored at the prefix."""
    current = os.dup(prefix_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=current)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                os.close(current)
                return None
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


@maintenance.uses_prefix
def copy_icu_files(source: Path, prefix: Path) -> None:
    """Apply a validated ICU subtree without writing through destination links."""
    if not source.is_dir() or source.is_symlink() or not paths.disjoint(source, prefix):
        raise ValueError("ICU source must be a real directory disjoint from the prefix")
    paths.validate_removal_path(prefix)
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise ValueError("ICU archive has no files in its Windows subtree")
    for path in source.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"Invalid staged ICU member: {path}")

    prefix_fd = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Inspect every existing parent before creating or replacing anything.
        for file in files:
            relative = file.relative_to(source)
            parent_fd = _icu_parent(prefix_fd, ("drive_c", "windows", *relative.parts[:-1]), create=False)
            if parent_fd is None:
                continue
            try:
                try:
                    info = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    raise ValueError(f"Invalid ICU destination: {relative}")
            finally:
                os.close(parent_fd)

        for file in files:
            relative = file.relative_to(source)
            parent_fd = _icu_parent(prefix_fd, ("drive_c", "windows", *relative.parts[:-1]), create=True)
            temporary = f".logos-desktop-icu-{secrets.token_hex(12)}"
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent_fd)
                with os.fdopen(descriptor, "wb") as target, file.open("rb") as incoming:
                    shutil.copyfileobj(incoming, target)
                os.replace(temporary, relative.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                os.close(parent_fd)
    finally:
        os.close(prefix_fd)


def get_registry_value(reg_path, name, app: App):
    with maintenance.use(maintenance.Context.from_app(app)):
        return _get_registry_value(reg_path, name, app)


def _get_registry_value(reg_path, name, app: App):
    logging.debug("Get registry value %s in %s", name, reg_path)
    # Encoding discovery cannot use a helper that requests wine_output_encoding.
    value = None
    cmd, env = _wine_invocation(app, app.conf.wine64_binary, None)
    cmd.extend(['reg', 'query', reg_path, '/v', name])
    err_msg = f"Failed to get registry value: {reg_path}\\{name}"
    encoding = app.conf._wine_output_encoding
    if encoding is None:
        encoding = 'UTF-8'
    try:
        result = system.run_command(cmd, encoding=encoding, env=env, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise maintenance.MaintenanceError(err_msg) from error
    if result is None:
        raise maintenance.MaintenanceError(err_msg)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise maintenance.MaintenanceError(f"{err_msg}, exit code {result.returncode}")
    if result.stdout is not None:
        for line in result.stdout.splitlines():
            if line.strip().startswith(name):
                value = line.split()[-1].strip()
                logging.debug(f"Registry value: {value}")
                break
    else:
        logging.critical(err_msg)
    return value


def _wine_invocation(app, binary, overrides):
    with runtime.activation_guard(app):
        executable = runtime.executable(binary, app)
        env = get_wine_env(app, overrides)
        concrete = Path(executable).resolve()
        if concrete.name.lower().endswith(".appimage"):
            env["WINE"] = env["WINELOADER"] = str(concrete)
            env["WINESERVER"] = str(concrete.parent / "wineserver")
        else:
            loader = env.get("WINELOADER") or env.get("WINE")
            if loader:
                pinned = Path(runtime.executable(loader, app))
                env["WINE"] = env["WINELOADER"] = str(pinned)
                if pinned.resolve().name.lower().endswith(".appimage"):
                    env["WINESERVER"] = str(pinned.resolve().parent / "wineserver")
                elif env.get("WINESERVER"):
                    env["WINESERVER"] = runtime.executable(env["WINESERVER"], app)
        explicit_server = getattr(app.conf._overrides, "wineserver_binary", None)
        if explicit_server is not None:
            env["WINESERVER"] = runtime.executable(app.conf.wineserver_binary, app)
        return [executable], env


def get_wine_env(app: App, additional_wine_dll_overrides: Optional[str]=None) -> dict[str, str]:
    logging.debug("Getting wine environment.")
    wine_env = os.environ.copy()
    winepath = Path(app.conf.wine_binary)
    if winepath.name != 'wine64':  # AppImage
        winepath = Path(app.conf.wine64_binary)
    wine_env_defaults = {
        'WINE': str(winepath),
        'WINEDEBUG': app.conf.wine_debug,
        'WINEDLLOVERRIDES': app.conf.wine_dll_overrides,
        'WINELOADER': str(winepath),
        'WINEPREFIX': app.conf.wine_prefix,
        'WINESERVER': app.conf.wineserver_binary,
    }
    for k, v in wine_env_defaults.items():
        wine_env[k] = v

    # Preserve the prefix's Windows account when changing Wine distributions.
    windows_user = getattr(app.conf, 'wine_user', None)
    if isinstance(windows_user, str) and windows_user:
        wine_env['USER'] = windows_user

    proton_files = winepath.resolve().parent.parent
    graphics_dlls = proton_files / 'lib/vkd3d/x86_64-windows'
    if (proton_files.parent / 'proton').is_file() and graphics_dlls.is_dir():
        # Direct Proton Wine launches also need its packaged WineD3D dependencies.
        existing = wine_env.get('WINEPATH', '')
        if str(graphics_dlls) not in existing.split(';'):
            wine_env['WINEPATH'] = ';'.join(value for value in (existing, str(graphics_dlls)) if value)

    if additional_wine_dll_overrides is not None:
        wine_env["WINEDLLOVERRIDES"] += ";" + additional_wine_dll_overrides

    updated_env = {k: wine_env.get(k) for k in wine_env_defaults.keys()}
    logging.debug(f"Wine env: {updated_env}")
    # Extra safe calling this here, it should be called run run_command anyways
    return system.fix_ld_library_path(wine_env)
