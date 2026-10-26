import re

import distro
import logging
import os
import psutil
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

from collections.abc import MutableMapping
from dataclasses import dataclass
from packaging.version import Version
from pathlib import Path
from typing import Optional, Tuple

from ou_dedetai import constants, maintenance, network
from ou_dedetai.app import App


# Command-based distro fallbacks inherit the C environment from operation workers.
# Linux release files provide identity without that implicit subprocess boundary.
DISTRIBUTION = distro.LinuxDistribution(include_lsb=False, include_uname=False, include_oslevel=False)


def fix_ld_library_path(env: Optional[MutableMapping[str, str]]) -> dict[str, str]:
    """Removes pyinstaller bundled dynamic linked libraries when executing commands

    - https://pyinstaller.org/en/latest/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application
    - https://pyinstaller.org/en/latest/runtime-information.html#library-path-considerations
    """
    # even when we don't pass env to Popen (or similar), we still don't want to use
    # pyinstaller's dynamic linked libraries.
    # In order to avoid this, we need to set env
    if env is None:
        env = dict(os.environ)
    else:
        env = dict(env)

    # Only do this when running in binary mode
    # Since LD_LIBRARY_PATH is core to how binaries work, we don't want it
    # modified unless we know we need to.
    if constants.RUNMODE == 'binary':
        # *BSD uses LD_LIBRARY_PATH, AIX uses LIBPATH
        for lp_key in ["LD_LIBRARY_PATH", "LIBPATH"]:
            lp_orig = env.get(lp_key + '_ORIG')
            if lp_orig is not None:
                env[lp_key] = lp_orig  # restore the original, unmodified value
            else:
                # This happens when LD_LIBRARY_PATH was not set.
                # Remove the env var as a last resort:
                env.pop(lp_key, None)

    return env


def run_command(command, retries=1, delay=0, **kwargs) -> Optional[subprocess.CompletedProcess]:
    check = kwargs.get("check", True)
    text = kwargs.get("text", True)
    capture_output = kwargs.get("capture_output", True)
    shell = kwargs.get("shell", False)
    env = kwargs.get("env", None)
    cwd = kwargs.get("cwd", None)
    encoding = kwargs.get("encoding", None)
    cmdinput = kwargs.get("input", None)
    stdin = kwargs.get("stdin", None)
    stdout = kwargs.get("stdout", None)
    stderr = kwargs.get("stderr", None)
    timeout = kwargs.get("timeout", None)
    bufsize = kwargs.get("bufsize", -1)
    executable = kwargs.get("executable", None)
    pass_fds = kwargs.get("pass_fds", ())
    errors = kwargs.get("errors", None)
    preexec_fn = kwargs.get("preexec_fn", None)
    close_fds = kwargs.get("close_fds", True)
    universal_newlines = kwargs.get("universal_newlines", None)
    startupinfo = kwargs.get("startupinfo", None)
    creationflags = kwargs.get("creationflags", 0)
    restore_signals = kwargs.get("restore_signals", True)
    start_new_session = kwargs.get("start_new_session", False)
    user = kwargs.get("user", None)
    group = kwargs.get("group", None)
    extra_groups = kwargs.get("extra_groups", None)
    umask = kwargs.get("umask", -1)
    pipesize = kwargs.get("pipesize", -1)
    process_group = kwargs.get("process_group", None)

    if retries < 1:
        retries = 1

    if isinstance(command, str) and not shell:
        command = command.split()

    env = fix_ld_library_path(env)

    for attempt in range(retries):
        try:
            result: subprocess.CompletedProcess = subprocess.run(
                command,
                check=check,
                text=text,
                shell=shell,
                capture_output=capture_output,
                input=cmdinput,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                encoding=encoding,
                cwd=cwd,
                env=env,
                timeout=timeout,
                bufsize=bufsize,
                executable=executable,
                errors=errors,
                pass_fds=pass_fds,
                preexec_fn=preexec_fn,
                close_fds=close_fds,
                universal_newlines=universal_newlines,
                startupinfo=startupinfo,
                creationflags=creationflags,
                restore_signals=restore_signals,
                start_new_session=start_new_session,
                user=user,
                group=group,
                extra_groups=extra_groups,
                umask=umask,
                pipesize=pipesize,
                process_group=process_group
            )
            return result
        except subprocess.CalledProcessError as e:
            logging.error(f"Error occurred in run_command() while executing \"{command}\": {e}.")
            logging.debug(f"Command failed with output:\n{e.stdout}\nand stderr:\n{e.stderr}") 
            # FIXME: Move this logic to the caller
            if "lock" in str(e):
                logging.debug(f"Database appears to be locked. Retrying in {delay} seconds…")
                time.sleep(delay)
            else:
                raise e
        except Exception as e:
            logging.error(f"An unexpected error occurred when running {command}: {e}")
            return None

    logging.error(f"Failed to execute after {retries} attempts: '{command}'")
    return None


# FIXME: #300 refactor this to either return Popen or raise. No None.
def popen_command(command, retries=1, delay=0, **kwargs) -> Optional[subprocess.Popen[bytes]]:
    shell = kwargs.get("shell", False)
    env = kwargs.get("env", None)
    cwd = kwargs.get("cwd", None)
    stdin = kwargs.get("stdin", None)
    stdout = kwargs.get("stdout", None)
    stderr = kwargs.get("stderr", None)
    bufsize = kwargs.get("bufsize", -1)
    executable = kwargs.get("executable", None)
    pass_fds = kwargs.get("pass_fds", ())
    preexec_fn = kwargs.get("preexec_fn", None)
    close_fds = kwargs.get("close_fds", True)
    universal_newlines = kwargs.get("universal_newlines", None)
    startupinfo = kwargs.get("startupinfo", None)
    creationflags = kwargs.get("creationflags", 0)
    restore_signals = kwargs.get("restore_signals", True)
    start_new_session = kwargs.get("start_new_session", False)
    user = kwargs.get("user", None)
    group = kwargs.get("group", None)
    extra_groups = kwargs.get("extra_groups", None)
    umask = kwargs.get("umask", -1)
    pipesize = kwargs.get("pipesize", -1)
    process_group = kwargs.get("process_group", None)
    encoding = kwargs.get("encoding", None)
    errors = kwargs.get("errors", None)

    if retries < 1:
        retries = 1

    if isinstance(command, str) and not shell:
        command = command.split()

    env = fix_ld_library_path(env)

    for _ in range(retries):
        try:
            process = subprocess.Popen(
                command,
                shell=shell,
                env=env,
                cwd=cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                bufsize=bufsize,
                executable=executable,
                pass_fds=pass_fds,
                preexec_fn=preexec_fn,
                close_fds=close_fds,
                universal_newlines=universal_newlines,
                startupinfo=startupinfo,
                creationflags=creationflags,
                restore_signals=restore_signals,
                start_new_session=start_new_session,
                user=user,
                group=group,
                extra_groups=extra_groups,
                umask=umask,
                pipesize=pipesize,
                process_group=process_group,
                encoding=encoding,
                errors=errors,
                text=False
            )
            return process

        except subprocess.CalledProcessError as e:
            logging.error(f"Error occurred in popen_command() while executing \"{command}\": {e}")
            # FIXME: Move this logic to the caller
            if "lock" in str(e):
                logging.debug(f"Database appears to be locked. Retrying in {delay} seconds…")
                time.sleep(delay)
            else:
                raise e
        except Exception as e:
            logging.error(f"An unexpected error occurred when running {command}: {e}")
            return None

    logging.error(f"Failed to execute after {retries} attempts: '{command}'")
    return None


def get_pids(query) -> list[psutil.Process]:
    results = []
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if process.info['cmdline'] is not None and query in process.info['cmdline']:
                results.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return results


def reboot(superuser_command: str):
    logging.info("Rebooting system.")
    command = f"{superuser_command} reboot now"
    run_command(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        capture_output=False,
        shell=True,
        text=True
    )
    sys.exit(0)


def get_dialog() -> str:
    """Choose Qt on a desktop or the terminal frontend on a usable terminal."""
    dialog = os.getenv('DIALOG')
    if dialog is not None:
        dialog = dialog.lower()
        if dialog not in ['cli', 'curses', 'tui', 'qt', 'tk']:
            print("Valid values for DIALOG are 'cli', 'curses', 'tui', 'qt' or 'tk'.", file=sys.stderr)
            sys.exit(1)
        return {"tk": "qt", "cli": "tui", "curses": "tui"}.get(dialog, dialog)
    if os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY'):
        return 'qt'
    if sys.__stdin__ is not None and sys.__stdin__.isatty():
        return 'tui'
    from .maintenance import MaintenanceError
    raise MaintenanceError("Open a desktop or terminal for the control panel, or select a command such as --status")


def get_architecture() -> Tuple[str, int]:
    """Queries the device and returns which cpu architure and bits is supported
    
    Returns:
        architecture: x86_64 x86_32 or """
    machine = platform.machine().lower()
    bits = struct.calcsize("P") * 8

    # FIXME: consider conforming to a standard for the architecture name
    # normally see arm64 in lowercase for example and risc as riscv64 on
    # debian's support architectures for example https://wiki.debian.org/SupportedArchitectures
    if "x86_64" in machine or "amd64" in machine:
        architecture = "x86_64"
    elif "i386" in machine or "i686" in machine:
        architecture = "x86_32"
    elif "arm" in machine or "aarch64" in machine:
        if bits == 64:
            architecture = "ARM64"
        else:
            architecture = "ARM32"
    elif "riscv" in machine or "riscv64" in machine:
        if bits == 64:
            architecture = "RISC-V 64"
        else:
            architecture = "RISC-V 32"
    else:
        architecture = "Unknown"

    return architecture, bits


def install_elf_interpreter():
    # TODO: This probably needs to be changed to another install step that requests the 
    # user to choose a specific ELF interpreter between box64, FEX-EMU, and hangover.
    # That or else we have to pursue a particular interpreter
    # for the install routine, depending on what's needed
    logging.critical("ELF interpretation is not yet coded in the installer.")
    # architecture, bits = get_architecture()
    # if "x86_64" not in architecture:
    #     if config.ELFPACKAGES is not None:
    #         utils.install_packages(config.ELFPACKAGES)
    #     else:
    #         logging.critical(f"ELFPACKAGES is not set.")
    #         sys.exit(1)
    # else:
    #     logging.critical(f"ELF interpreter is not needed.")


def check_architecture():
    architecture, bits = get_architecture()
    logging.debug(f"Current Architecture: {architecture}, {bits}bit.")
    if "x86_64" in architecture:
        pass
    elif "ARM64" in architecture:
        logging.critical("Unsupported architecture. Requires box64 or FEX-EMU or Wine Hangover to be integrated.") 
        install_elf_interpreter()
    elif "RISC-V 64" in architecture:
        logging.critical("Unsupported architecture. Requires box64 or FEX-EMU or Wine Hangover to be integrated.") 
        install_elf_interpreter()
    elif "x86_32" in architecture:
        logging.critical("Unsupported architecture. Requires box64 or FEX-EMU or Wine Hangover to be integrated.") 
        install_elf_interpreter()
    elif "ARM32" in architecture:
        logging.critical("Unsupported architecture. Requires box64 or FEX-EMU or Wine Hangover to be integrated.") 
        install_elf_interpreter()
    elif "RISC-V 32" in architecture:
        logging.critical("Unsupported architecture. Requires box64 or FEX-EMU or Wine Hangover to be integrated.") 
        install_elf_interpreter()
    else:
        logging.critical("System archictecture unknown.")


def get_os() -> Tuple[str, str]:
    """Gets OS information
    
    Returns:
        OS name
        OS release
    """
    # FIXME: Not working? Returns "Linux" on some systems? On Ubuntu 24.04 it
    # correctly returns "ubuntu".
    os_name = DISTRIBUTION.id()
    logging.info(f"OS name: {os_name}")
    os_release = DISTRIBUTION.version()
    logging.info(f"OS release: {os_release}")
    return os_name, os_release


class SuperuserCommandNotFound(maintenance.MaintenanceError):
    """Superuser command not found. Install pkexec or sudo or doas"""


class OpenGLIncompatible(Exception):
    """OpenGL version is incompatible."""


def check_opengl_version(app: App, required_version="3.2") -> tuple[bool, str]:
    try:
        result = run_command(['glxinfo'], env=fix_ld_library_path(None), capture_output=True,
                             text=True, check=True, timeout=15)
    except FileNotFoundError:
        return False, "glxinfo command not found. Please install mesa-utils or equivalent."
    except subprocess.CalledProcessError as e:
        return False, f"glxinfo command failed: {e.stderr.strip()}"
    
    if not result:
        return False, "failed to launch glxinfo."

    match = re.search(r"OpenGL version string:\s+([\d\.]+)", result.stdout)
    if not match:
        return False, "Failed to parse OpenGL version from glxinfo output."

    opengl_version = match.group(1)
    if Version(opengl_version) >= Version(required_version):
        message = f"OpenGL Version: {opengl_version} is supported (>= {required_version})."
        logging.info(message)
        return True, message
    else:
        message = f"OpenGL Version: {opengl_version} is not supported (must be >= {required_version})."
        logging.info(message)
        raise OpenGLIncompatible()


def get_superuser_command() -> str:
    if shutil.which('pkexec'):
        return "pkexec"
    elif shutil.which('sudo'):
        return "sudo"
    elif shutil.which('doas'):
        return "doas"
    else:
        raise SuperuserCommandNotFound


@dataclass
class PackageManager:
    """Dataclass to pass around relevant OS context regarding system packages"""
    # Commands
    install: list[str]
    download: list[str]
    remove: list[str]
    query: list[str]

    query_prefix: str

    packages: str
    
    incompatible_packages: str
    # For future expansion:
    # elf_packages: str


def get_package_manager() -> PackageManager | None:
    major_ver = DISTRIBUTION.major_version()
    os_name = DISTRIBUTION.id()
    logging.debug(f"{os_name=}; {major_ver=}")
    # Check for package manager and associated packages.

    install_command: list[str]
    download_command: list[str]
    remove_command: list[str]
    query_command: list[str]
    query_prefix: str
    packages: str
    incompatible_packages: str

    if shutil.which('apt') is not None:  # debian, ubuntu, & derivatives
        install_command = ["apt", "install", "-y"]
        download_command = ["apt", "install", "--download-only", "-y"]
        remove_command = ["apt", "remove", "-y"]
        query_command =  ["dpkg", "-l"]
        query_prefix = '.i  '
        # Set default package list.
        packages = (
            "binutils wget winbind "  # wine
            "p7zip-full cabextract " # winetricks
            "xdg-utils " # For xdg-mime needed for custom url scheme registration
            "mesa-utils "  # verify opengl version
        )

        # FUSE 3 does not provide the libfuse.so.2 ABI used by older AppImage
        # runtimes. The t64 rename retains that ABI on these newer releases.
        # https://docs.appimage.org/user-guide/troubleshooting/fuse.html
        apt_major = int(major_ver) if major_ver.isdecimal() else 0
        if (
            (os_name == 'debian' and apt_major >= 13)
            or (os_name == 'ubuntu' and apt_major >= 24)
            or (os_name == 'linuxmint' and apt_major >= 22)
            or (os_name == 'elementary' and apt_major >= 8)
        ):
            packages += (
                "libfuse2t64 "  # appimages
            )
        else:
            packages += (
                "libfuse2 "  # appimages
            )
        incompatible_packages = ""  # appimagelauncher handled separately
    elif shutil.which('dnf') is not None:  # rhel, fedora
        install_command = ["dnf", "install", "-y"]
        download_command = ["dnf", "install", "--downloadonly", "-y"]
        remove_command = ["dnf", "remove", "-y"]
        # Fedora < 41 uses dnf4, while Fedora  41 uses dnf5. The dnf list
        # command is sligtly different between the two.
        # https://discussion.fedoraproject.org/t/after-f41-upgrade-dnf-says-no-packages-are-installed/135391
        # Fedora < 41
        # query_command =  ["dnf", "list", "installed"]
        # Fedora 41
        # query_command =  ["dnf", "list", "--installed"]

        # specifying a format is so we can match exactly on the package name
        query_command = ["rpm", "-qa", "--qf", "%{NAME}\n"]
        query_prefix = ''
        packages = (
            "fuse fuse-libs "  # appimages
            "mod_auth_ntlm_winbind samba-winbind samba-winbind-clients "  # wine
            "cabextract " # winetricks
            "xdg-utils " # For xdg-mime needed for custom url scheme registration
            "glx-utils"  # verify opengl version
        )
        incompatible_packages = ""  # appimagelauncher handled separately
    elif shutil.which('zypper') is not None:  # OpenSUSE
        install_command = ["zypper", "--non-interactive", "install"]
        download_command = ["zypper", "download"]
        remove_command = ["zypper", "--non-interactive", "remove"]
        query_command =  ["zypper", "se", "-si"]
        # This could also be a 'i+ | '
        query_prefix = 'i  | '
        packages = (
            "fuse "  # appimages
            "samba wget "  # wine
            "curl gawk grep "  # other
            "7zip cabextract "  # winetricks
            "xdg-utils " # For xdg-mime needed for custom url scheme registration
            "Mesa-demo-x"  # verify opengl version
        )
        incompatible_packages = ""  # appimagelauncher handled separately
    elif shutil.which('apk') is not None:  # alpine
        install_command = ["apk", "--no-interactive", "add"]
        download_command = ["apk", "--no-interactive", "fetch"]
        remove_command = ["apk", "--no-interactive", "del"]
        query_command = ["apk", "list", "-I"]
        query_prefix = ''
        packages = (
            "bash bash-completion "  # bash support
            "gcompat "  # musl to glibc
            #"fuse-common fuse fuse3 "  # appimages; incompatible with muslc
            "wget curl "  # network
            "7zip cabextract " # winetricks
            "samba sed grep gawk bash bash-completion "  # other
            "xdg-utils " # For xdg-mime needed for custom url scheme registration
            "mesa-demos"  # verify opengl version
        )
        incompatible_packages = ""  # appimagelauncher handled separately
    elif shutil.which('pamac') is not None:  # manjaro
        install_command = ["pamac", "install", "--no-upgrade", "--no-confirm"]
        download_command = ["pamac", "install", "--no-upgrade", "--download-only", "--no-confirm"]
        remove_command = ["pamac", "remove", "--no-confirm"]
        query_command =  ["pamac", "list", "-i"]
        query_prefix = ''
        packages = (
            "fuse2 "  # appimages
            "samba wget "  # wine
            "curl gawk grep "  # other
            "7zip cabextract "  # winetricks (7zip used to be called p7zip)
            "xdg-utils " # For xdg-mime needed for custom url scheme registration
            "mesa-utils "  # verify opengl version
        )
        incompatible_packages = ""  # appimagelauncher handled separately
    elif shutil.which('pacman') is not None:  # arch, steamOS
        install_command = ["pacman", "-Syu", "--noconfirm", "--needed"]
        download_command = ["pacman", "-Sw", "-y"]
        remove_command = ["pacman", "-R", "--noconfirm"]
        query_command =  ["pacman", "-Q"]
        query_prefix = ''
        if os_name == "steamos":  # steamOS
            # Discovery remains available, but no automatic transaction is validated.
            install_command = download_command = remove_command = []
            packages = (
                "patch wget sed grep gawk p7zip cabextract samba bc libxml2 curl print-manager "
                "system-config-printer cups-filters nss-mdns foomatic-db-engine foomatic-db-ppds "
                "foomatic-db-nonfree-ppds ghostscript glibc samba extra-rel/apparmor core-rel/libcurl-gnutls "
                "appmenu-gtk-module lib32-libjpeg-turbo qt5-virtualkeyboard wine-staging giflib lib32-giflib libpng "
                "lib32-libpng libldap lib32-libldap gnutls lib32-gnutls mpg123 lib32-mpg123 openal lib32-openal "
                "v4l-utils lib32-v4l-utils libpulse lib32-libpulse libgpg-error lib32-libgpg-error alsa-plugins "
                "lib32-alsa-plugins alsa-lib lib32-alsa-lib libjpeg-turbo lib32-libjpeg-turbo sqlite lib32-sqlite "
                "libxcomposite lib32-libxcomposite libxinerama lib32-libgcrypt libgcrypt lib32-libxinerama ncurses "
                "lib32-ncurses ocl-icd lib32-ocl-icd libxslt lib32-libxslt libva lib32-libva gtk3 lib32-gtk3 "
                "gst-plugins-base-libs lib32-gst-plugins-base-libs vulkan-icd-loader lib32-vulkan-icd-loader "
                "xdg-utils " # For xdg-mime needed for custom url scheme registration
                "mesa-utils "  # verify opengl version
            )
        else:  # arch
            packages = (
                "fuse2 "  # appimages
                "binutils libwbclient samba wget "  # wine
                "7zip cabextract " # winetricks (7zip used to be called pzip then p7zip)
                "openjpeg2 libxcomposite libxinerama "  # display
                "ocl-icd vulkan-icd-loader "  # hardware
                "alsa-plugins gst-plugins-base-libs libpulse openal "  # audio
                "libva mpg123 v4l-utils "  # video
                "libxslt sqlite "  # misc
                "xdg-utils " # For xdg-mime needed for custom url scheme registration
                "mesa-utils "  # verify opengl version
            )
        incompatible_packages = ""  # appimagelauncher handled separately
    elif os_name == "org.freedesktop.platform":
        # Flatpak
        # Dependencies are managed by the flatpak
        install_command = download_command = remove_command = query_command =  ["/usr/bin/true"] 
        query_prefix = ''
        packages = ""
        incompatible_packages = ""
    else:
        # Add more conditions for other package managers as needed.
        logging.critical("Your package manager is not yet supported. Please contact the developers.") 
        return None

    output = PackageManager(
        install=install_command,
        download=download_command,
        query=query_command,
        remove=remove_command,
        incompatible_packages=incompatible_packages,
        packages=packages,
        query_prefix=query_prefix
    )
    logging.debug(f"Package Manager: {output}")
    return output


def query_packages(package_manager: PackageManager, packages, mode="install") -> list[str]: 
    if mode not in ("install", "remove"):
        raise ValueError(f"Invalid query mode: {mode}")

    result = dependency_command(package_manager.query, "query packages")
    if not isinstance(result.stdout, str):
        raise maintenance.MaintenanceError("Package query did not return readable output")

    family = package_manager.query[0]
    installed = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if family == "dpkg":
            if len(fields) >= 2 and len(fields[0]) >= 2 and fields[0][1] == "i":
                installed.add(fields[1].split(":")[0])
        elif family == "zypper":
            columns = [column.strip() for column in line.split("|")]
            if len(columns) >= 2 and columns[0] in ("i", "i+"):
                installed.add(columns[1])
        elif family == "rpm":
            installed.add(line.strip())
        elif family == "apk":
            match = re.fullmatch(r"(.+)-\d[^ ]*", fields[0])
            if match:
                installed.add(match[1])
        else:
            installed.add(fields[0])

    present = mode == "remove"
    return [package for package in packages if (package.rsplit("/", 1)[-1] in installed) == present]


def have_dep(cmd):
    if shutil.which(cmd) is not None:
        return True
    else:
        return False


def check_dialog_version():
    if have_dep("dialog"):
        try:
            result = run_command(["dialog", "--version"])
            if result is None:
                print("Failed to run the 'dialog' command.")
                return None
            version_info = result.stdout.strip()
            if version_info.startswith("Version: "):
                version_info = version_info[len("Version: "):]
            return version_info
        except subprocess.CalledProcessError as e:
            print(f"Error running command: {e.stderr}")
        except FileNotFoundError:
            print("The 'dialog' command is not found. Please ensure it is installed and in your PATH.")
        return None


def test_dialog_version():
    version: Optional[str] = check_dialog_version()

    def parse_date(version):
        try:
            return version.split('-')[1]
        except IndexError:
            return ''

    minimum_version = "1.3-20201126-1"

    logging.debug(f"Current dialog version: {version}")
    if version is not None:
        minimum_version = parse_date(minimum_version)
        current_version = parse_date(version)
        logging.debug(f"Minimum dialog version: {minimum_version}. Installed version: {current_version}.")
        return current_version > minimum_version
    else:
        return None


def remove_appimagelauncher(app: App):
    package_manager = get_package_manager()
    if package_manager is None:
        raise maintenance.MaintenanceError("No supported package manager can remove AppImageLauncher")
    executable = shutil.which("AppImageLauncher")
    if executable is None:
        return
    if DISTRIBUTION.id() == "steamos":
        raise maintenance.MaintenanceError(
            "Remove AppImageLauncher using SteamOS-supported system maintenance, then retry. "
            "Automatic SteamOS package changes are not validated"
        )

    family = package_manager.query[0]
    if family == "dpkg":
        query = ["dpkg-query", "-S", executable]
    elif family in ("rpm", "zypper"):
        query = ["rpm", "-qf", "--qf", "%{NAME}\n", executable]
    elif family in ("pacman", "pamac"):
        query = ["pacman", "-Qqo", executable]
    else:
        raise maintenance.MaintenanceError("Remove the unmanaged AppImageLauncher installation manually, then retry")

    owner = dependency_command(query, "identify the AppImageLauncher package").stdout.strip()
    if family == "dpkg":
        owner = owner.split(": ", 1)[0]
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.:-]*", owner) is None:
        raise maintenance.MaintenanceError("Could not identify one owning package for AppImageLauncher")

    app.status(f"Removing AppImageLauncher package {owner}")
    arguments = [*package_manager.remove, owner]
    dependency_command([app.superuser_command, *arguments], "remove AppImageLauncher")
    raise maintenance.MaintenanceError("AppImageLauncher was removed. Reboot before retrying installation")


def postinstall_dependencies_alpine(superuser_command: str) -> list[str]:
    # user = os.getlogin()
    command: list[str] = [
        #superuser_command, "modprobe ", "fuse ", "&& ",
        #superuser_command, "bash ", "-c ", "if \( ! rc-service -e fuse ); then rc-update add fuse boot ", "&& ", 
        #superuser_command, "sed ", "-i ", "'s/#user_allow_other/user_allow_other/g' ", "/etc/fuse.conf ", "&& ", 
        #superuser_command, "addgroup ", "fuse ", "&& ",
        #superuser_command, "adduser ", f"{user} ", "fuse ", "&& ",
        #superuser_command, "bash ", "-c ", "if \( ! rc-service -e fuse ); then rc-service fuse restart ", "&& ", 
    ]
    return command


def preinstall_dependencies(superuser_command: str):
    command = []
    logging.debug("Performing pre-install dependencies…")
    os_name, _ = get_os()
    logging.debug("No pre-install dependencies required for %s", os_name)
    return command


def postinstall_dependencies(superuser_command: str):
    command = []
    logging.debug("Performing post-install dependencies…")
    os_name, _ = get_os()
    if os_name == "alpine":
        command = postinstall_dependencies_alpine(superuser_command)
    else:
        logging.debug("No post-install dependencies required.")
    return command


def dependency_command(arguments: list[str], stage: str):
    """Check both raised failures and missing results from the system adapter."""
    try:
        result = run_command(arguments)
    except (OSError, subprocess.SubprocessError) as error:
        raise maintenance.MaintenanceError(f"Could not {stage}: {arguments!r}: {error}") from error
    if result is None or result.returncode != 0:
        code = result.returncode if result is not None else "no result"
        raise maintenance.MaintenanceError(f"Could not {stage} ({code}): {arguments!r}")
    return result


def install_dependencies(app: App, target_version=10):
    if app.conf.skip_install_system_dependencies:
        return

    manager = get_package_manager()
    if manager is None:
        raise maintenance.MaintenanceError("This system's package manager is unsupported")
    required = manager.packages.split()
    incompatible = manager.incompatible_packages.split()
    missing = query_packages(manager, required)
    conflicts = query_packages(manager, incompatible, mode="remove")
    if not missing and not conflicts:
        app.status("All dependencies are met.", 100)
        return

    details = f"Install: {missing}\nRemove: {conflicts}"
    if DISTRIBUTION.id() == "steamos":
        raise maintenance.MaintenanceError(
            f"Automatic SteamOS package changes are not validated.\n{details}\n"
            "Use SteamOS-supported system maintenance to resolve these dependencies, then retry. "
            "Filesystem protection and system packages were not changed"
        )
    if not app.approve("Apply these system dependency changes?", details):
        from .app import UserExitedFromAsk

        raise UserExitedFromAsk

    elevation = app.superuser_command
    steps = list(preinstall_dependencies(elevation))
    if missing:
        steps.append([elevation, *manager.install, *missing])
    if conflicts:
        steps.append([elevation, *manager.remove, *conflicts])
    steps.extend(postinstall_dependencies(elevation))
    app.status("Installing dependencies…")
    for arguments in steps:
        dependency_command(arguments, "apply dependency changes")

    remaining = query_packages(manager, required)
    conflicts_left = query_packages(manager, incompatible, mode="remove")
    if remaining or conflicts_left:
        raise maintenance.MaintenanceError(
            f"Dependencies remain unmet. Missing: {remaining}. Conflicting: {conflicts_left}"
        )
    if conflicts:
        raise maintenance.MaintenanceError("Dependency removal completed. Reboot before retrying installation")


@maintenance.uses_installation
def ensure_winetricks(
    app: App,
    version=constants.WINETRICKS_VERSION,
    status_messages: bool = True
):
    from . import artifacts, runtime

    if (
        app.conf.wine_binary_code in ["Recommended", "AppImage"]
        and app.conf.wine_appimage_path is not None
        and app.conf.wine_appimage_path.exists()
    ):
        binary = runtime.executable(app.conf.wine_appimage_path, app)
        concrete = Path(binary).resolve()
        if concrete.parent.parent.parent.name != ".artifacts":
            accepted = artifacts.import_local(
                concrete, Path(app.conf.installer_binary_dir),
                artifacts.ArtifactSpec("local:" + str(concrete), concrete.name, "appimage"),
            )
            concrete = accepted.path
        return str(concrete.parent / "winetricks")

    winetricks_path = Path(app.conf.installer_binary_dir) / "winetricks"
    script_spec = artifacts.ArtifactSpec(
        f"https://github.com/Winetricks/winetricks/{version}", "winetricks", "winetricks",
    )
    default_release = version == constants.WINETRICKS_VERSION
    if default_release:
        script_spec = artifacts.ArtifactSpec(
            f"https://raw.githubusercontent.com/Winetricks/winetricks/{version}/src/winetricks",
            "winetricks", "winetricks",
            artifacts.Digest("sha256", constants.WINETRICKS_SHA256, "official tagged script observed over HTTPS"),
        )
    if winetricks_path.is_symlink():
        concrete = winetricks_path.resolve()
        if concrete.name == "winetricks" and concrete.parent.parent.name == script_spec.key:
            try:
                installed = artifacts.installed(concrete, "winetricks")
                if script_spec.digest is None or installed.sha256 == script_spec.digest.value:
                    return str(installed.path)
            except (OSError, artifacts.ArtifactError):
                pass

    # Otherwise download
    if status_messages:
        app.status(f"Installing winetricks v{version}…")
    if default_release:
        script = network.logos_reuse_download(
            script_spec.url, script_spec.name, app.conf.installer_binary_dir,
            app=app, status_messages=status_messages, kind=script_spec.kind, digest=script_spec.digest,
        )
    else:
        base_url = "https://codeload.github.com/Winetricks/winetricks/zip/refs/tags"
        zip_name = f"{version}.zip"
        accepted = network.logos_reuse_download(
            f"{base_url}/{version}", zip_name, app.conf.download_dir,
            app=app, status_messages=status_messages, kind="winetricks-zip",
        )
        with tempfile.TemporaryDirectory(prefix="winetricks-", dir=app.conf.download_dir) as directory:
            source = Path(directory) / "winetricks"
            with zipfile.ZipFile(accepted.check()) as archive:
                source.write_bytes(archive.read(artifacts.winetricks_member(archive)))
            script = artifacts.import_local(
                source, Path(app.conf.installer_binary_dir), script_spec,
            )
    runtime._replace_link(winetricks_path, str(script.path))
    return str(script.path)


def check_incompatibilities(app: App):
    # Check for AppImageLauncher
    if shutil.which('AppImageLauncher'):
        question_text = "Remove AppImageLauncher? A reboot will be required."
        secondary = (
            "Your system currently has AppImageLauncher installed.\n"
            f"{constants.APP_NAME} is not compatible with AppImageLauncher.\n"
            "For upstream background, see: https://github.com/FaithLife-Community/OuDedetai/issues/114"
        )
        app.approve_or_exit(question_text, secondary)
        remove_appimagelauncher(app)
