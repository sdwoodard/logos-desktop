import atexit
from datetime import datetime
import enum
import gzip
import glob
import inspect
import json
import logging
import os
import queue
import psutil
import re
import shutil
import signal
import sys
import tarfile
import time
from ou_dedetai.app import App
from packaging.version import Version
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

from . import constants
from . import paths
from . import maintenance
from . import network
from . import system
from . import wine


# FIXME: This function isn't used and should probably be removed.
def get_calling_function_name():
    if 'inspect' in sys.modules:
        stack = inspect.stack()
        caller_frame = stack[1]
        caller_name = caller_frame.function
        return caller_name
    else:
        return "Inspect Not Enabled"


# FIXME: Not good practice to clobber a built-in [list] as a variable name.
def append_unique(list, item):
    if item not in list:
        list.append(item)
    else:
        logging.debug(f"{item} already in {list}.")


def die_if_running(app: App):

    def remove_pid_file():
        if os.path.exists(constants.PID_FILE):
            os.remove(constants.PID_FILE)

    if os.path.isfile(constants.PID_FILE):
        with open(constants.PID_FILE, 'r') as f:
            pid = f.read().strip()
            message = f"The script is already running on PID {pid}. Should it be killed to allow this instance to run?"
            if app.approve(message):
                os.kill(int(pid), signal.SIGKILL)

    atexit.register(remove_pid_file)
    with open(constants.PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def die(message):
    logging.critical(message)
    sys.exit(1)


def restart_lli():
    raise RuntimeError("Use the prepared controller transaction to restart its exact target")


def get_user_downloads_dir() -> str:
    home = Path.home()
    downloads_path = str(home / 'Downloads')

    xdg_config = Path(os.getenv('XDG_CONFIG_HOME', home / '.config'))
    user_dirs_file = xdg_config / 'user-dirs.dirs'
    if user_dirs_file.is_file():
        with user_dirs_file.open() as f:
            for line in f.readlines():
                if 'DOWNLOAD' in line:
                    downloads_path = line.rstrip().split('=')[1].replace(
                        '$HOME',
                        str(home)
                    ).strip('"')
                    break
    return downloads_path


def delete_symlink(symlink_path):
    symlink_path = Path(symlink_path)
    if symlink_path.is_symlink():
        try:
            symlink_path.unlink()
            logging.info(f"Symlink at {symlink_path} removed successfully.")
        except Exception as e:
            logging.error(f"Error removing symlink: {e}")


# FIXME: should this be in control?
def install_dependencies(app: App):
    if app.conf.skip_install_system_dependencies:
        app.status("System dependency installation skipped")
        return
    if app.conf.faithlife_product_version:
        targetversion = int(app.conf.faithlife_product_version)
    else:
        targetversion = 10
    app.status(f"Checking {app.conf.faithlife_product} {str(targetversion)} dependencies…") 

    if targetversion == 10:
        system.install_dependencies(app, target_version=10)
    elif targetversion == 9:
        raise maintenance.MaintenanceError("Logos 9 installation is not supported")
    else:
        raise maintenance.MaintenanceError(f"Unsupported product version: {targetversion}")

    app.status("Installed dependencies.", 100)


def file_exists(file_path: Optional[str | bytes | Path]) -> bool:
    if file_path is not None:
        expanded_path = os.path.expanduser(file_path)
        return os.path.isfile(expanded_path)
    else:
        return False


# FIXME: Surely the appropriate JSON filename and key name depend on Product
# Name and don't always include "Logos"?
def get_current_logos_version(logos_appdata_dir: Optional[str]) -> Optional[str]:
    if logos_appdata_dir is None:
        return None
    path = f"{logos_appdata_dir}/System/Logos.deps.json"
    logos_version_number: Optional[str] = None
    if Path(path).exists():
        with open(path, 'r') as json_file:
            json_data = json.load(json_file)
        for key in json_data.get('libraries', dict()):
            if key.startswith('Logos') and '/' in key:
                logos_version_number = key.split('/')[1]

        logging.debug(f"{logos_version_number=}")
        if logos_version_number is not None:
            return logos_version_number
        else:
            logging.debug("Couldn't determine installed Logos version.")
            return None
    return None


def get_winebin_code_and_desc(app: App, binary) -> Tuple[str, str | None]:
    """Gets the type of wine in use and it's description
    
    Returns:
        code: One of: Recommended, AppImage, System, Proton, PlayOnLinux, Custom
        description: Description of the above
    """
    # Set binary code, description, and path based on path
    codes = {
        "Recommended": "Use the current released Wine development version from an available provider",
        "AppImage": "AppImage of Wine64",
        "System": "Use an installed system Wine binary that meets the configured product requirements.",
        "Proton": "Install using the Steam Proton fork of WINE.",
        "PlayOnLinux": "Install using a PlayOnLinux WINE64 binary.",
        "Custom": "Use a WINE64 binary from another directory.",
    }
    if isinstance(binary, Path):
        binary = str(binary)
    if binary == constants.WINE_RECOMMENDED_SIGIL:
        code = "Recommended"
    elif binary.lower().endswith('.appimage'):
        code = "AppImage"
    elif "/usr/bin/" in binary:
        code = "System"
    elif "Proton" in binary:
        code = "Proton"
    elif "PlayOnLinux" in binary:
        code = "PlayOnLinux"
    else:
        code = "Custom"
    desc = codes.get(code)
    logging.debug(f"{binary} code & desc: {code}; {desc}")
    return code, desc


def get_wine_options(app: App) -> List[str]:
    appimages = app.conf.wine_app_image_files
    binaries = app.conf.wine_binary_files
    logging.debug(f"{appimages=}")
    logging.debug(f"{binaries=}")
    wine_binary_options = []

    # Add AppImages to list
    os_name, _ = system.get_os()
    if os_name != "alpine":
        wine_binary_options.append(constants.WINE_RECOMMENDED_SIGIL)
        wine_binary_options.extend(appimages)

    sorted_binaries = sorted(list(set(binaries)))
    logging.debug(f"{sorted_binaries=}")

    wine_binary_options.extend(sorted_binaries)
    logging.debug(f"{wine_binary_options=}")
    return wine_binary_options


def current_native_wine(app: App, target):
    """Prefer an exact installed target without installing or changing its files."""
    from . import artifacts

    for candidate in app.conf.wine_binary_files:
        app.checkpoint()
        try:
            selected = wine.native_launcher(candidate)
            server = str(Path(selected).parent / "wineserver")
            override = getattr(app.conf._overrides, "wineserver_binary", None)
            if isinstance(override, str):
                server = app.conf._absolute_from_install_dir(override)

            files = (Path(selected), Path(server))
            before = [(path.resolve(strict=True), artifacts.regular_hash(path.resolve(strict=True))) for path in files]
            facts, _ = wine.get_wine_release(selected)
            if facts is None or facts.version != Version(target.version):
                continue
            compatible, _ = wine.check_wine_version_and_branch(
                app.conf.faithlife_product_release, selected, app.conf.faithlife_product_version, server=server,
            )
            after = [(path.resolve(strict=True), artifacts.regular_hash(path.resolve(strict=True))) for path in files]
            if not compatible or before != after:
                continue
            logging.info("Current installed native Wine %s: %s SHA256 %s, %s SHA256 %s",
                         target.version, selected, before[0][1], server, before[1][1])
            return selected
        except (OSError, ValueError, artifacts.ArtifactError, maintenance.MaintenanceError) as error:
            logging.info("Cannot recommend native Wine candidate %s: %s", candidate, error)
    return None


# FIXME: This function is not used and should probably be removed.
def get_procs_using_file(file_path):
    procs = set()
    for proc in psutil.process_iter(['pid', 'open_files', 'name']):
        try:
            paths = [f.path for f in proc.open_files()]
            if len(paths) > 0 and file_path in paths:
                procs.add(proc.pid)
        except psutil.AccessDenied:
            pass
    return procs


def find_installed_product(faithlife_product: str, wine_prefix: str) -> Optional[str]:
    if faithlife_product and wine_prefix:
        drive_c = Path(f"{wine_prefix}/drive_c/")
        name = faithlife_product
        exe = None
        for root, _, files in drive_c.walk(follow_symlinks=False):
            if root.name == name and f"{name}.exe" in files:
                exe = str(root / f"{name}.exe")
                break
        return exe
    return None


def enough_disk_space(dest_dir, bytes_required: int) -> bool:
    free_bytes = shutil.disk_usage(dest_dir).free
    logging.debug(f"{free_bytes=}; {bytes_required=}")
    return free_bytes > bytes_required


def get_path_size(file_path: Path|str) -> int:
    file_path = Path(file_path)
    if not file_path.exists():
        path_size = 0
    else:
        path_size = sum(f.stat().st_size for f in file_path.rglob('*')) + file_path.stat().st_size
    return path_size


def get_folder_group_size(
    src_dirs: List[Path] | Tuple[Path],
    q: queue.Queue[int] | None = None,
) ->  int:
    src_size = 0
    for d in src_dirs:
        if not d.is_dir():
            continue
        src_size += get_path_size(d)
    if q is not None:
        q.put(src_size)

    return src_size


def get_latest_folder(folder_path: Path|str) -> Optional[Path]:
    folders = [f for f in Path(folder_path).glob('*')]
    if not folders:
        logging.warning(f"No folders found in {folder_path}")
        return None
    folders.sort()
    logging.info(f"Found {len(folders)} backup folders.")
    latest = folders[-1]
    logging.info(f"Latest folder: {latest}")
    return latest


class VersionComparison(enum.Enum):
    OUT_OF_DATE = enum.auto()
    UP_TO_DATE = enum.auto()
    DEVELOPMENT = enum.auto()


def compare_logos_linux_installer_version(app: App) -> Optional[VersionComparison]:
    latest_version = app.conf.app_latest_version
    if latest_version is None:
        return None

    if constants.APP_RELEASE_VERSION is None:
        return None
    current = Version(constants.APP_RELEASE_VERSION)
    latest = Version(latest_version)

    if current < latest:
        # Current release is older than recommended.
        output = VersionComparison.OUT_OF_DATE
    elif current > latest:
        # Installed version is custom.
        output = VersionComparison.DEVELOPMENT
    elif current == latest:
        # Current release is latest.
        output = VersionComparison.UP_TO_DATE

    logging.debug(f"LLI self-update check: {output=}")
    return output


# FIXME: This actually compares any wine binary with recommended version. Maybe
# the function should be renamed to 'check_recommended_wine_version'?
def compare_recommended_appimage_version(app: App):
    status = None
    message = None
    from . import runtime

    try:
        wine_exe_path = runtime.executable(app.conf.wine_binary, app)
    except (RuntimeError, OSError, ValueError) as error:
        return -1, str(error)
    wine_release, error_message = wine.get_wine_release(wine_exe_path)
    if wine_release is not None and wine_release is not False:
        current_version = wine_release.version
        logging.debug(f"Current wine release: {current_version}")

        try:
            recommended_version = Version(app.conf.wine_appimage_recommended_version)
        except (RuntimeError, OSError, ValueError) as error:
            return -1, str(error)
        logging.debug(f"Recommended wine release: {recommended_version}")
        if current_version < recommended_version:
            # Current release is older than recommended.
            status = 0
            message = "yes"
        elif current_version == recommended_version:
            # Current release is latest.
            status = 1
            message = "uptodate"
        elif current_version > recommended_version:
            # Installed version is custom
            status = 2
            message = "no"
    else:
        # FIXME: should this raise an exception?
        status = -1
        message = f"Error: {error_message}"

    logging.debug(f"{status=}; {message=}")
    return status, message


def get_lli_release_version(lli_binary):
    from . import artifacts, updates

    if not isinstance(lli_binary, artifacts.AcceptedArtifact) or lli_binary.spec.digest is None:
        raise artifacts.ArtifactError("Version inspection requires an accepted controller and release digest")
    return updates.probe(lli_binary.check())


def is_appimage(file_path):
    # Ref:
    # - https://cgit.freedesktop.org/xdg/shared-mime-info/commit/?id=c643cab25b8a4ea17e73eae5bc318c840f0e3d4b
    # - https://github.com/AppImage/AppImageSpec/blob/master/draft.md#image-format
    # Note:
    # result is a tuple: (is AppImage: True|False, AppImage type: 1|2|None)
    # result = (False, None)
    expanded_path = Path(file_path).expanduser().resolve()
    logging.debug(f"Converting path to expanded_path: {expanded_path}")
    if file_exists(expanded_path):
        logging.debug(f"{expanded_path} exists!")
        with expanded_path.open('rb') as f:
            f.seek(1)
            elf_sig = f.read(3)
            f.seek(8)
            ai_sig = f.read(2)
            f.seek(10)
            v_sig = f.read(1)

        appimage_check = elf_sig == b'ELF' and ai_sig == b'AI'
        appimage_type = int.from_bytes(v_sig)

        return appimage_check, appimage_type
    else:
        logging.error(f"File does not exist: {expanded_path}")
        return False, None


def check_appimage(filestr):
    logging.getLogger(__name__).debug("Checking if %s is a usable AppImage", filestr)
    if filestr is None:
        logging.error("check_appimage: received None for file.")
        return False

    from . import artifacts

    try:
        artifacts.validate_format(Path(filestr), "appimage")
        return True
    except (artifacts.ArtifactError, OSError):
        return False


def find_appimage_files(app: App) -> list[str]:
    appimages = []
    directories = [
        app.conf.installer_binary_dir,
        os.path.expanduser("~") + "/bin",
        app.conf.download_dir
    ]
    if app.conf._overrides.custom_binary_path is not None:
        directories.append(app.conf._overrides.custom_binary_path)

    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or higher is required for .rglob() flag `case-sensitive` ")

    for d in directories:
        appimage_paths = Path(d).glob('wine*.appimage', case_sensitive=False)
        for p in appimage_paths:
            if p is not None and check_appimage(p):
                appimages.append(str(p))

    return appimages


def find_wine_binary_files(app: App, release_version: Optional[str]) -> list[str]:
    directories = [path for path in os.get_exec_path() if path and Path(path).is_absolute()]
    patterns = [
        "/usr/local/bin",
        os.path.expanduser("~") + "/bin",
        os.path.expanduser("~") + "/PlayOnLinux/wine/linux-amd64/*/bin",
        os.path.expanduser("~") + "/.steam/steam/steamapps/common/Proton*/files/bin",
    ]

    custom = app.conf._overrides.custom_binary_path
    if custom is not None:
        directories.insert(0, str(Path(custom).absolute()))
    for pattern in patterns:
        directories.extend(sorted(glob.glob(pattern)))

    binaries = []
    seen = set()
    for directory in dict.fromkeys(directories):
        for name in ("wine64", "wine"):
            candidate = Path(directory) / name
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            try:
                selected = wine.native_launcher(candidate)
                override = getattr(app.conf._overrides, "wineserver_binary", None)
                server = Path(selected).parent / "wineserver"
                if isinstance(override, str):
                    server = Path(app.conf._absolute_from_install_dir(override))
                paths.executable(server)
                identity = Path(selected).stat()
                server_identity = server.stat()
                key = (identity.st_dev, identity.st_ino, server_identity.st_dev, server_identity.st_ino,
                       str(Path(selected).parent.resolve()))
                if key in seen:
                    continue
                compatible, reason = wine.check_wine_version_and_branch(
                    release_version, selected, app.conf.faithlife_product_version, server=str(server),
                )
                if compatible:
                    seen.add(key)
                    binaries.append(selected)
                else:
                    logging.info("Ignoring Wine candidate %s: %s", candidate, reason)
            except (OSError, ValueError, maintenance.MaintenanceError) as error:
                logging.info("Ignoring Wine candidate %s: %s", candidate, error)
    return binaries


@maintenance.changes_installation
def set_appimage_symlink(app: App, source=None):
    from . import artifacts, runtime

    selected = source if source is not None else app.conf.wine_appimage_path
    if selected is None:
        return
    local = Path(selected).resolve(strict=True)
    if local.parent.parent.parent.name == ".artifacts":
        accepted = artifacts.installed(local, "appimage")
    else:
        accepted = artifacts.import_local(
            local, Path(app.conf.installer_binary_dir),
            artifacts.ArtifactSpec("local:" + str(local), local.name, "appimage"),
        )
    runtime.activate(app, accepted)


def update_to_latest_lli_release(app: App):
    if not constants.APP_SELF_UPDATES_ENABLED:
        app.status(constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
        return

    return network.update_lli_binary(app=app)


def handoff_controller(app, identity, frontend):
    """Release profile admission before transferring control to a terminal.

    Cancellation, errors and unaccepted results restore admission. After Popen
    accepts handoff, the frontend closes without cancelling the terminal or Logos.
    """
    from . import controller_install

    with app._profile_lock:
        session = app._instance_session
        if session is not None:
            session.close()
            app._instance_session = None
        accepted = False
        try:
            result = controller_install.handoff(
                identity, app.conf.config_file_path, frontend, checkpoint=getattr(app, "checkpoint", None),
            )
            accepted = result.status == "handed-off"
            return result
        finally:
            if not accepted and session is not None:
                try:
                    session.acquire()
                    session.listen()
                    app._instance_session = session
                except Exception:
                    session.close()
                    app.exit("Controller ownership could not be restored after handoff")
                    raise


def validate_product_update_document(conf, target, document):
    """Check the post-rebind selection before publishing an admitted update."""
    from . import config_store
    from .config import EphemeralConfiguration, PersistentConfiguration

    old = conf._overrides
    projected = EphemeralConfiguration.project(
        document, conf.config_file_path, environment=old.environment,
        cli_overrides=old.cli_overrides, runtime_selection=old.runtime_selection,
    )
    overlays = config_store.apply_changes(old.environment, old.runtime_selection, conf.config_file_path)
    effective = PersistentConfiguration.from_document(document, overlays)
    if projected.faithlife_installer_download_url or projected.faithlife_installer_name:
        raise maintenance.MaintenanceError(
            "Custom installer sources are not supported by the stable-release update workflow")
    if (effective.faithlife_product != "Logos" or effective.faithlife_product_release != target
            or (projected.faithlife_product_version or effective.faithlife_product_version) != "10"
            or effective.faithlife_product_release_channel != "stable"
            or effective.app_release_channel != "stable"):
        raise maintenance.MaintenanceError("An override conflicts with the latest stable Logos release")


@maintenance.changes_installation
def update_faithlife_product(app: App):
    from ou_dedetai import installer  # local import to avoid circular dependency
    from . import config_store
    from functools import partial

    lookup = app.conf._network.product_release(
        app.conf.faithlife_product, app.conf.faithlife_product_version, 'stable', force=True,
    )
    state = network.product_update_state(app.conf.refresh_installed_faithlife_product_release(), lookup)
    if not state.actionable:
        raise maintenance.MaintenanceError(state.message)
    latest = state.target
    validator = partial(validate_product_update_document, app.conf, latest)
    document = config_store.apply_changes(
        app.conf._raw._saved, {"faithlife_product_release": latest}, app.conf.config_file_path,
    )
    validator(document)
    app.status(f"Updating {app.conf.faithlife_product} to {latest}…")
    with app.conf.validated_writes(validator):
        app.conf.faithlife_product_release = latest
        installer.install(app)


# FIXME: consider moving this to control
@maintenance.changes_installation
def update_to_latest_recommended_appimage(app: App):
    if app.conf.wine_binary_code not in ["AppImage", "Recommended"]:
        logging.debug("AppImage commands disabled since we're not using an appimage")
        return
    from . import runtime

    accepted = network.download_recommended_appimage(app)
    runtime.activate(app, accepted)


def get_downloaded_file_path(download_dir: str, filename: str):
    dirs = [
        Path(download_dir),
        Path.home(),
        Path.cwd(),
    ]
    for d in dirs:
        file_path = Path(d) / filename
        if file_path.is_file():
            logging.info(f"'{filename}' exists in {str(d)}.")
            return str(file_path)
    logging.debug(f"File not found: {filename}")


def grep(regexp, filepath):
    fp = Path(filepath)
    ct = 0
    try:
        found = False
        with fp.open() as f:
            for line in f:
                ct += 1
                text = line.rstrip()
                if re.search(regexp, text):
                    logging.debug(f"{filepath}:{ct}:{text}")
                    found = True
        return found
    except FileNotFoundError as e:
        logging.error(e)
        raise e


def untar_file(file_path, output_dir):
    """Extract regular data into a fresh private directory or raise on failure."""
    output = Path(output_dir)
    if not output.is_absolute() or not paths.disjoint(Path(file_path), output):
        raise ValueError("Archive and extraction directory must be disjoint absolute paths")
    if output.is_symlink() or (output.exists() and any(output.iterdir())):
        raise ValueError("Extraction requires an empty, unlinked directory")

    # Tar readers can stop before the gzip trailer, so verify the whole stream.
    with gzip.open(file_path, "rb") as compressed:
        while compressed.read(1024 * 1024):
            pass

    with tarfile.open(file_path, "r:gz") as archive:
        members = archive.getmembers()
        names: dict[PurePosixPath, bool] = {}
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not member.isfile() and not member.isdir():
                raise ValueError(f"Unsupported archive member: {member.name}")
            if name in names:
                raise ValueError(f"Duplicate archive path: {member.name}")
            names[name] = member.isdir()

        for name in names:
            if any(parent in names and not names[parent] for parent in name.parents):
                raise ValueError(f"Archive file is used as a directory: {name}")

        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.validate_removal_path(output)
        archive.extractall(path=output, filter="data")


def is_relative_path(path: str | Path) -> bool:
    if isinstance(path, str):
        path = Path(path)
    return not path.is_absolute()


def get_relative_path(path: Path | str, base_path: str) -> str | Path:
    return paths.relative_to_install(path, base_path)


def stopwatch(start_time=None, interval=10.0):
    if start_time is None:
        start_time = time.time()

    current_time = time.time()
    elapsed_time = current_time - start_time

    if elapsed_time >= interval:
        last_log_time = current_time
        return True, last_log_time
    else:
        return False, start_time


def get_timestamp():
    return datetime.today().strftime('%Y-%m-%dT%H%M%S')


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ValueError("Boolean settings must be strings or JSON booleans")
    return value.lower() in ['true', '1', 'y', 'yes']
