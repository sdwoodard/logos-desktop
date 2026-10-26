import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ou_dedetai.app import App

from . import constants
from . import artifacts
from . import maintenance
from . import network
from . import runtime
from . import system
from . import utils
from . import wine

from ou_dedetai.system import OpenGLIncompatible


@dataclass(frozen=True)
class ProductInstaller:
    """Keep the target release attached to the exact accepted MSI generation."""
    release: str
    artifact: artifacts.AcceptedArtifact


# This step doesn't do anything per-say, but "collects" all the choices in one step
# The app would continue to work without this function
def ensure_choices(app: App):
    app.installer_step_count += 1

    app.status("Asking questions if needed…")

    # Prompts (by nature of access and debug prints a number of choices the user has
    logging.debug(f"> {app.conf.faithlife_product=}")
    logging.debug(f"> {app.conf.faithlife_product_version=}")
    logging.debug(f"> {app.conf.faithlife_product_release=}")
    logging.debug(f"> {app.conf.install_dir=}")
    logging.debug(f"> {app.conf.installer_binary_dir=}")
    # Runtime resolution waits until the dependency phase has completed.
    logging.debug(f"> {app.conf.faithlife_product_icon_path}")
    logging.debug(f"> {app.conf.faithlife_installer_download_url}")
    app.status("Install is running…")

def ensure_install_dirs(app: App):
    app.installer_step_count += 1
    ensure_choices(app=app)
    app.installer_step += 1
    app.status("Ensuring installation directories…")
    bin_dir = Path(app.conf.installer_binary_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    logging.debug(f"> {bin_dir} exists?: {bin_dir.is_dir()}")

    logging.debug(f"> {app.conf.install_dir=}")
    logging.debug(f"> {app.conf.installer_binary_dir=}")

    logging.debug(f"> {app.conf.wine_prefix=}")


def ensure_sys_deps(app: App):
    app.installer_step_count += 1
    ensure_install_dirs(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status("Ensuring system dependencies are met…")

    if not app.conf.skip_install_system_dependencies:
        system.check_incompatibilities(app)
        utils.install_dependencies(app)
        logging.debug("> Done.")
    else:
        logging.debug("> Skipped.")


def check_for_known_bugs(app: App):
    """Checks for any known bug conditions and recommends user action.
    This is a best-effort check
    """
    # Begin workaround #435
    # FIXME: #435 Remove this check when the issue is fixed upstream in wine    
    # Check to see if our default browser is chromium, google-chrome, brave, or vivaldi 
    # in that case prompt the user to switch to the wine beta channel
    #
    # This check is best-effort and may fail if the .desktop entry for chromium uses a naming convention
    # other than the one used on the debian package.

    chromium_detected: bool = False
    snap_or_flatpak_detected: bool = False

    try:
        result = system.run_command(
            "xdg-mime query default x-scheme-handler/https"
        )
        if result:
            command_output: str = result.stdout
            desktop_file_name = command_output.strip()
            command_output = desktop_file_name.lower()

            # Abbr. list from https://en.wikipedia.org/wiki/Chromium_(web_browser)#Browsers_based_on_Chromium
            # Known chromium based .desktops:
            # chromium_chromium.desktop - Debian Chromium (.deb and snap)
            # google-chrome and com.google.Chrome.desktop - Debian Google Chrome
            # brave-browser.desktop and com.brave.Browser.desktop - Debian Brave
            # vivaldi-stable.desktop - Debian Vivaldi
            # dooble.desktop - Debian Dooble
            # opera.desktop - Debian Opera
            # org.qutebrowser.qutebrowser.desktop - Debian Qutebrowser
            # org.kde.falkon.desktop - Debian Falkon
            if (
               "chromium" in command_output
               or "chrome" in command_output
               or "brave" in command_output
               or "vivaldi" in command_output
               or "dooble" in command_output
               or "falkon" in command_output
               or "qutebrowser" in command_output
               or "opera" in command_output
            ):
               chromium_detected = True
            
            # Now check the .desktop's Exec line for either "flatpak" or "/snap/bin" if either are found, fail as well.
            xdg_data_dirs = os.getenv("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
           
            found: bool = False

            for xdg_data_dir in xdg_data_dirs.split(":"):
                desktop_file_path = (Path(xdg_data_dir) / "applications" / desktop_file_name)
                if desktop_file_path.exists():
                    found = True
                    lines = desktop_file_path.read_text().splitlines()
                    lines = list(filter(lambda line: line.lower().startswith("exec="), lines))
                    if any("/snap/bin" in line or "flatpak" in line for line in lines):
                        snap_or_flatpak_detected = True
            
            if not found:
                logging.warning("Cannot find the desktop file in question")
    except subprocess.CalledProcessError as e:
        logging.warning(
            "Failed to check if chromium will be used - "
            "sign in button MAY NOT function if chromium is the default browser. "
            f"Error was as follows: {e}"
        )

    if chromium_detected or snap_or_flatpak_detected:
        if chromium_detected:
            logging.warning("Detected chromium based browser")
        elif snap_or_flatpak_detected:
            logging.warning("Detected snap or flatpak browser")
        if app.conf.wine_binary == constants.WINE_RECOMMENDED_SIGIL:
            logging.info("Switching user to the beta wine branch due to issue #435")
            app.conf.wine_binary = constants.WINE_BETA_SIGIL
        elif app.conf.wine_binary == constants.WINE_BETA_SIGIL:
            # No need to do anything
            pass
        else:
            logging.warning("Sign In button may not launch the browser due to issue #435")

    # End workaround #435

def check_opengl(app: App):
    app.status("Checking available OpenGL version…")
    try:
        opengl_version, reason = system.check_opengl_version(app)
        app.status(reason)
    except OpenGLIncompatible:
        app.status("Incompatible OpenGL version.")
        question = "Incompatible OpenGL version. Logos will be unable to launch. Should the install continue anyways?"
        if app.approve(question):
            logging.debug("> User continuing with incompatible OpenGL.")
        else:
            from .app import UserExitedFromAsk

            raise UserExitedFromAsk

    logging.debug("> Done.")


def check_system_compatibility(app: App):
    try:
        check_for_known_bugs(app=app)
    except Exception:
        logging.exception("Failed to check for known bugs - assuming everything is fine and continuing install.")
    if (
        app.conf.faithlife_product_version != '9'
        and not str(app.conf.wine_binary).lower().endswith('appimage')
        and app.conf.wine_binary not in [constants.WINE_BETA_SIGIL, constants.WINE_RECOMMENDED_SIGIL]
    ):
        return

    check_opengl(app=app)


@maintenance.changes_installation
def select_recommended_wine(app: App):
    """Prepare an exact current runtime before publishing the user's new selection."""
    source = app.conf._network.wine_release_target()
    if source.status != "ok" or source.stale or source.target is None:
        raise maintenance.MaintenanceError(source.message)
    native = utils.current_native_wine(app, source.target)
    app.checkpoint()
    if native is not None:
        app.conf.wine_binary = native
        return

    lookup = app.conf._network.wine_appimage_release()
    if (lookup.status != "ok" or lookup.stale or lookup.release is None
            or lookup.release.version != source.target.version):
        raise maintenance.MaintenanceError(lookup.message or "Current Wine AppImage is unavailable")
    accepted = network.download_recommended_appimage(app, lookup.release)
    app.checkpoint()
    runtime.activate(app, accepted)


def ensure_appimage_download(app: App):
    app.installer_step_count += 1
    ensure_sys_deps(app=app)
    app.checkpoint()
    check_system_compatibility(app=app)
    app.installer_step += 1

    if (
        app.conf.faithlife_product_version != '9'
        and not str(app.conf.wine_binary).lower().endswith('appimage')
        and app.conf.wine_binary not in [constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL]
    ):
        return
    app.status("Ensuring wine AppImage is downloaded…")

    filename = Path(app.conf.wine_binary).name
    if Path(app.conf.wine_binary) != Path(app.conf.installer_binary_dir) / filename:
        logging.debug("Preserving the explicitly selected external AppImage")
        return
    versions = app.conf._network.wine_appimage_versions()
    release = next((release for release in (versions.latest, versions.pre_release)
                    if release is not None and release.name == filename), None)
    if release is None:
        logging.debug("The selected custom AppImage has no managed release URL")
        return
    if release.error:
        raise artifacts.ArtifactError(release.error)
    accepted = network.logos_reuse_download(
        release.download_url,
        release.name,
        app.conf.installer_binary_dir,
        app=app,
        kind="appimage",
        digest=release.digest,
        size=release.size,
    )
    runtime.activate(app, accepted)


def ensure_wine_executables(app: App):
    app.installer_step_count += 1
    ensure_appimage_download(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status("Ensuring wine executables are available…")

    create_wine_appimage_symlinks(app=app)
    wine.require_compatible(app, app.conf.wine64_binary)

    # PATH is modified if wine appimage isn't found, but it's not modified
    # during a restarted installation, so shutil.which doesn't find the
    # executables in that case.
    logging.debug(f"> {app.conf.wine_binary=}")
    logging.debug(f"> {app.conf.wine64_binary=}")
    logging.debug(f"> {app.conf.wineserver_binary=}")


def ensure_winetricks_executable(app: App):
    app.installer_step_count += 1
    ensure_wine_executables(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status("Ensuring winetricks executable is available…")

    system.ensure_winetricks(app=app)

    logging.debug(f"> {app.conf.winetricks_binary} is executable?: {os.access(app.conf.winetricks_binary, os.X_OK)}")


def ensure_product_installer_download(app: App):
    app.installer_step_count += 1
    ensure_winetricks_executable(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status(f"Ensuring {app.conf.faithlife_product} installer is downloaded…")

    release = app.conf.faithlife_product_release
    wine.product_release(release)
    product = app.conf.faithlife_product
    major = app.conf.faithlife_product_version
    overrides = app.conf._overrides
    name = getattr(overrides, "faithlife_installer_name", None) or f"{product}_v{release}-x64.msi"
    product_path = "/Verbum/" if product == "Verbum" else "/"
    url = getattr(overrides, "faithlife_installer_download_url", None) or (
        f"https://downloads.logoscdn.com/LBS{major}{product_path}Installer/{release}/{product}-x64.msi"
    )
    accepted = network.logos_reuse_download(
        url,
        name,
        str(Path(app.conf.install_dir) / "data"),
        app=app,
        kind="msi",
        msi_expectation=artifacts.MsiExpectation(product, release),
    )
    return ProductInstaller(release, accepted)


def ensure_wineprefix_init(app: App):
    app.installer_step_count += 1
    accepted = ensure_product_installer_download(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status("Ensuring wineprefix is initialized…")

    init_file = Path(f"{app.conf.wine_prefix}/system.reg")
    logging.debug(f"{init_file=}")
    if not init_file.is_file():
        init_file.parent.mkdir(parents=True, exist_ok=True)
        logging.debug(f"{init_file} does not exist")
        logging.debug("Initializing wineprefix.")
        wine.initializeWineBottle(app.conf.wine64_binary, app)
        # wine.light_wineserver_wait()
        wine.wineserver_wait(app)
        logging.debug("Wine init complete.")
    logging.debug(f"> {init_file} exists?: {init_file.is_file()}")
    return accepted


def ensure_wineprefix_config(app: App):
    app.installer_step_count += 1
    accepted = ensure_wineprefix_init(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status("Ensuring wineprefix configuration…")

    # Force winemenubuilder.exe='' in registry.
    logging.debug("Setting wineprefix registry to ignore winemenubuilder.exe.")
    wine.disable_winemenubuilder(app=app, wine64_binary=app.conf.wine64_binary)

    # Force fontsmooth=rgb in registry.
    logging.debug("Setting fontsmoothing=rgb in wineprefix registry.")
    wine.set_fontsmoothing_to_rgb(app=app, wine64_binary=app.conf.wine64_binary)
    return accepted


def ensure_fonts(app: App):
    """Ensure the arial font is installed"""
    app.installer_step_count += 1
    accepted = ensure_wineprefix_config(app=app)
    app.checkpoint()
    app.installer_step += 1

    wine.install_fonts(app)
    return accepted


def ensure_icu_data_files(app: App):
    app.installer_step_count += 1
    accepted = ensure_fonts(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status("Ensuring ICU data files are installed…")
    logging.debug('- ICU data files')

    wine.enforce_icu_data_files(app=app)

    logging.debug('> ICU data files installed')
    return accepted


@maintenance.changes_installation
def ensure_product_installed(app: App):
    app.installer_step_count += 1
    accepted = ensure_icu_data_files(app=app)
    app.checkpoint()
    app.installer_step += 1
    app.status(f"Ensuring {app.conf.faithlife_product} is installed…")

    try:
        installed_release = app.conf.installed_faithlife_product_release
    except Exception:
        logging.warning("Failed to determine the installed version. Proceeding with install.", exc_info=True)
        installed_release = None

    target_release = accepted.release

    if not app.is_installed() or (
        target_release is not None and installed_release != target_release
    ):
        wine.install_msi(app, accepted.artifact, target_release=target_release)

    # Clear installed version cache
    app.conf._installed_faithlife_product_release = None

    logging.debug(f"> {app.conf.logos_exe=}")


def ensure_config_file(app: App):
    app.installer_step_count += 1
    ensure_product_installed(app=app)
    app.installer_step += 1
    app.status("Ensuring config file is up-to-date…")

def ensure_launcher_executable(app: App):
    app.installer_step_count += 1
    ensure_config_file(app=app)
    app.installer_step += 1
    if constants.RUNMODE == 'binary':
        app.status(f"Copying launcher to {app.conf.install_dir}…")

        from . import updates

        launcher_exe = Path(app.conf.install_dir) / constants.BINARY_NAME
        copied = updates.copy_launcher(Path(sys.executable), launcher_exe)
        app._standalone_launcher_available = copied is not None
    else:
        app.status(
            "Running from source. Skipping launcher copy."
        )


def ensure_launcher_shortcuts(app: App):
    app.installer_step_count += 1
    ensure_launcher_executable(app=app)
    app.installer_step += 1
    app.status("Creating launcher shortcuts…")
    if constants.RUNMODE == 'binary':
        if getattr(app, "_standalone_launcher_available", False):
            app.status("Creating launcher shortcuts…")
            create_launcher_shortcuts(app)
        else:
            app.status("This installation method manages its controller shortcuts")
    else:
        # This is done so devs can run this without it clobbering their install
        app.status(
            f"Runmode is '{constants.RUNMODE}'. Won't create desktop shortcuts",
        )


def install(app: App):
    if not app.conf._raw.faithlife_product:
        app.conf.faithlife_product
    if not getattr(app.conf._raw, "faithlife_product_version", None):
        app.conf._change(faithlife_product_version=app.conf.faithlife_product_version)
    if not (getattr(app.conf._overrides, "install_dir", None) or app.conf._raw.install_dir):
        app.conf.install_dir
    with maintenance.installing(app):
        return _install(app)


def _install(app: App):
    """Entrypoint for installing"""
    app.installer_step = 1
    app.installer_step_count = 0
    try:
        wine.product_release(app.conf.faithlife_product_release)
        app.status('Installing…')
        ensure_launcher_shortcuts(app)
        app.status("Install Complete!", 100)
        # Trigger a config update event to refresh the UIs
        app._config_updated_event.set()
    finally:
        app.installer_step = 0
        app.installer_step_count = 0


def get_progress_pct(current, total):
    return round(current * 100 / total)


@maintenance.changes_installation
def create_wine_appimage_symlinks(app: App):
    if app.conf.wine_appimage_path is None:
        return
    source = Path(app.conf.wine_appimage_path).resolve(strict=True)
    if source.parent.parent.parent.name == ".artifacts":
        accepted = artifacts.installed(source, "appimage")
    else:
        accepted = artifacts.import_local(
            source, Path(app.conf.installer_binary_dir),
            artifacts.ArtifactSpec("local:" + str(source), source.name, "appimage"),
        )
    runtime.activate(app, accepted)


def desktop_exec(arguments: list[str], *, accepts_url: bool = False) -> str:
    """Serialize argv using both Desktop Entry string and Exec escaping rules."""
    if not arguments or not arguments[0] or "=" in arguments[0]:
        raise ValueError("Desktop executable is missing or contains an equal sign")

    for argument in arguments:
        if any(ord(character) < 32 or ord(character) == 127 for character in argument):
            raise ValueError("Desktop arguments cannot contain control characters")

    # KDE's xdg-mime lookup does not resolve a quoted executable token.
    # Keep complex paths as arguments, also avoiding GIO's percent-token lookup.
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./+-"
           for character in arguments[0]):
        arguments = ["/usr/bin/env", "--", *arguments]

    quoted = [arguments[0]]
    for argument in arguments[1:]:
        escaped = argument.replace("%", "%%")
        for character in ("\\", '"', "`", "$"):
            escaped = escaped.replace(character, "\\" + character)
        quoted.append('"' + escaped.replace("\\", "\\\\") + '"')
    if accepts_url:
        quoted.append("%u")
    return " ".join(quoted)


def create_desktop_file(
    filename: str,
    app_name: str,
    exec_args: list[str],
    generic_name: str | None = None,
    comment: str | None = None,
    icon_path: str | Path | None = None,
    wm_class: str | None = None,
    additional_keywords: list[str] | None = None,
    mime_types: list[str] | None = None,
    terminal: Optional[bool] = None,
    accepts_url: bool = False,
):
    if Path(filename).name != filename or not filename.endswith(".desktop"):
        raise ValueError("Desktop filename must be a single desktop entry name")
    exec_cmd = desktop_exec(exec_args, accepts_url=accepts_url)
    contents = f"""[Desktop Entry]
Name={app_name}
Type=Application
Exec={exec_cmd}
Categories=Education;Spirituality;Languages;Literature;Maps;
Keywords=Logos;Verbum;FaithLife;Bible;Control;Christianity;Jesus;{";".join(additional_keywords or [])}
"""
    contents += f"Terminal={"true" if terminal is True else "false"}\n"
    if generic_name:
        contents += f"GenericName={generic_name}\n"
    if comment:
        contents += f"Comment={comment}\n"
    if icon_path:
        escaped_icon = str(icon_path).replace("\\", "\\\\")
        contents += f"Icon={escaped_icon}\n"
    if mime_types:
        contents += f"MimeType={";".join(mime_types)}\n"

    if wm_class:
        contents += f"StartupWMClass={wm_class}\n"
    else:
        contents += "StartupNotify=false\n"

    xdg_data_home = Path(constants.XDG_DATA_HOME)
    if not xdg_data_home.is_absolute():
        raise ValueError("Desktop data directory must be absolute")
    launcher_path = xdg_data_home / 'applications' / filename
    if launcher_path.parent.is_symlink():
        raise ValueError("Desktop applications directory cannot be a symlink")
    launcher_path.parent.mkdir(parents=True, exist_ok=True)

    logging.info(f"Creating desktop launcher at {launcher_path}.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=launcher_path.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(contents)
        temporary.chmod(0o755)
        os.replace(temporary, launcher_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return launcher_path


@maintenance.uses_installation
def create_launcher_shortcuts(app: App):
    from . import desktop

    if constants.RUNMODE in ('snap', 'flatpak'):
        return
    if constants.RUNMODE == 'binary':
        launcher = [str(Path(app.conf.install_dir) / constants.BINARY_NAME)]
    elif constants.RUNMODE == 'script':
        launcher = [sys.executable, str(Path(sys.argv[0]).expanduser().resolve())]
    else:
        raise ValueError(f'Unsupported desktop launcher mode: {constants.RUNMODE}')

    selected = app.conf._overrides
    config = selected.config_destination or selected.config_path
    desktop_exec(launcher)
    plan = None
    try:
        plan = desktop.preview(config, launcher, product=app.conf.faithlife_product,
                               windows_executable=app.conf.logos_exe_windows_path)
        result = desktop.adopt(plan)
    except (OSError, ValueError, maintenance.MaintenanceError) as error:
        journal = plan.journal if plan is not None else None
        result = desktop.Result(journal, 'incomplete', (f'Desktop integration needs attention: {error}',))
    for issue in result.issues:
        logging.warning(issue)
    return result
