"""These functions take no arguments by default.
They can be called from CLI, GUI, or TUI.
"""

import copy
import glob
import json
import logging
import os
import stat
from pathlib import Path
import subprocess
from typing import Any
import webbrowser
from zipfile import ZipFile

from ou_dedetai import constants
from ou_dedetai import paths
from ou_dedetai import system
from ou_dedetai.app import App


def edit_file(config_file: str):
    system.run_command(["xdg-open", config_file])


def remove_all_index_files(app: App):
    if not app.conf.logos_exe:
        app.exit("Cannot remove index files, Logos is not installed")
    logos_dir = os.path.dirname(app.conf.logos_exe)
    index_paths = [
        os.path.join(logos_dir, "Data", "*", "BibleIndex"),
        os.path.join(logos_dir, "Data", "*", "LibraryIndex"),
        os.path.join(logos_dir, "Data", "*", "PersonalBookIndex"),
        os.path.join(logos_dir, "Data", "*", "LibraryCatalog"),
    ]
    for index_path in index_paths:
        pattern = os.path.join(index_path, "*")
        files_to_remove = glob.glob(pattern)

        for file_to_remove in files_to_remove:
            try:
                os.remove(file_to_remove)
                logging.info(f"Removed: {file_to_remove}")
            except OSError as e:
                logging.error(f"Error removing {file_to_remove}: {e}")

    app.status("Removed all LogosBible index files!", 100)


def remove_library_catalog(app: App):
    if not app.conf.logos_exe:
        app.exit("Cannot remove library catalog, Logos is not installed")
    logos_dir = os.path.dirname(app.conf.logos_exe)
    files_to_remove = glob.glob(f"{logos_dir}/Data/*/LibraryCatalog/*")
    for file_to_remove in files_to_remove:
        try:
            os.remove(file_to_remove)
            logging.info(f"Removed: {file_to_remove}")
        except OSError as e:
            logging.error(f"Error removing {file_to_remove}: {e}")


def uninstall(app: App):
    """Remove only preflighted installation data and explicitly selected cleanup."""
    from ou_dedetai.config import PersistentConfiguration

    config_path = Path(app.conf._overrides.config_path)
    install_value = app.conf._overrides.install_dir or app.conf._raw.install_dir
    install = Path(install_value) if install_value else None
    protected = (
        constants.CACHE_BASE,
        Path(constants.XDG_DATA_HOME),
        Path(constants.CONFIG_DIR).expanduser().parent,
        Path(constants.STATE_DIR).expanduser().parent,
        Path(__file__).resolve().parents[1],
    )
    targets: dict[Path, paths.RemovalTarget] = {}

    def add_target(path: Path, *, directory: bool = False) -> None:
        if not path.exists() and not path.is_symlink():
            return
        info = paths.validate_removal_path(path, protected)
        if stat.S_ISDIR(info.st_mode) != directory:
            raise ValueError(f"Unexpected uninstall target type: {path}")
        targets[path] = paths.RemovalTarget(path, info, path.parent.stat())

    add_target(config_path)
    if install is not None and (install.exists() or install.is_symlink()):
        add_target(install, directory=True)
        portable = install / f"{constants.BINARY_NAME}.json"
        paths.validate_removal_path(portable, protected)
        with portable.open() as source:
            portable_config = json.load(source)
        recorded = portable_config.get("install_dir", portable_config.get("INSTALLDIR"))
        if (
            not isinstance(recorded, str)
            or not Path(recorded).is_absolute()
            or Path(recorded).resolve() != install.resolve()
        ):
            raise ValueError(f"Installation ownership is not established: {install}")
        for child in ("data/bin", "data/wine64_bottle"):
            managed = install / child
            info = paths.validate_removal_path(managed, protected)
            if not stat.S_ISDIR(info.st_mode) or not managed.resolve().is_relative_to(install.resolve()):
                raise ValueError(f"Invalid installation layout: {managed}")

    if app.approve(
        "Do you also want to clear the cache and logs?",
        "Save a support archive first if you need these logs for debugging.",
    ):
        cache = Path(constants.CACHE_DIR)
        if cache.exists() or cache.is_symlink():
            paths.validate_cache_directory(cache, constants.CACHE_BASE, require_marker=True)
            add_target(cache, directory=True)

        state = Path(constants.STATE_DIR).expanduser()
        if state.exists() or state.is_symlink():
            paths.validate_removal_path(state, protected)
            log_names = [f"{constants.BINARY_NAME}.log", "wine.log", "wine.1.log"]
            log_names += [f"{constants.BINARY_NAME}.log.{index}.gz" for index in range(1, 6)]
            for name in log_names:
                add_target(state / name)

    selected = list(targets)
    for index, first in enumerate(selected):
        for second in selected[index + 1:]:
            if paths.disjoint(first, second):
                continue
            if install in (first, second) and config_path in (first, second):
                if config_path.resolve().is_relative_to(install.resolve()):
                    targets.pop(config_path, None)
                    continue
            raise ValueError(f"Overlapping uninstall targets: {first} and {second}")

    context = "These paths will be removed:\n\n" + "\n".join(str(path) for path in targets)
    if not app.approve("Are you sure you want to uninstall?", context):
        return
    app.status("Uninstalling…")
    ordered = sorted(targets.values(), key=lambda target: not stat.S_ISDIR(target.info.st_mode))
    paths.remove_targets(ordered, protected)

    # Reloading would recreate a cache that the user just asked to remove.
    app.conf._raw = PersistentConfiguration()
    for attribute in (
        "_wine64_path", "_wine64_identity", "_download_dir", "_wine_user", "_logos_exe",
        "_installed_faithlife_product_release", "_wine_binary_files", "_wine_appimage_files",
        "_user_download_dir", "_wine_output_encoding",
    ):
        setattr(app.conf, attribute, None)
    app._config_updated_event.set()
    app.status("Uninstalled")


def get_support(app: App) -> str:
    """Creates a zip file with all the information to enable support and opens support
    """

    # Save in ~/Downloads or ~/
    output_dir = Path(os.path.expanduser("~/Downloads"))
    if not output_dir.exists():
        output_dir = output_dir.parent

    output_path = app.ask("Where do you want to save the support zip?", [
        str(output_dir / constants.DEFAULT_SUPPORT_FILE_NAME),
        constants.PROMPT_OPTION_NEW_FILE,
    ])

    app.status(f"Writing support package to: {output_path}", percent=0)

    if Path(output_path).exists():
        os.remove(output_path)

    with ZipFile(output_path, "x") as zip:
        if Path(app.conf.config_file_path).exists():
            zip.write(app.conf.config_file_path)
        if Path(app.conf.app_log_path).exists():
            zip.write(app.conf.app_log_path)
        if Path(app.conf.app_wine_log_path).exists():
            zip.write(app.conf.app_wine_log_path)
        if Path(app.conf.app_wine_log_previous_path).exists():
            zip.write(app.conf.app_wine_log_previous_path)
        # Only include LogosCrash.log for now. Logos.log can include
        # information about which books the user owns which is more information than
        # The user would probably expect to be included in a support package.
        if (
            app.conf._faithlife_crash_log is not None and
            Path(app.conf._faithlife_crash_log).exists()
        ):
            zip.write(app.conf._faithlife_crash_log)

        if Path("/etc/os-release").exists():
            zip.write("/etc/os-release")
        run_commands = [
            ["glxinfo"],
            ["free", "-h"],
            ["inxi", "-F"],
            ["df", "-h"],
            ["xdg-mime", "query", "default", "x-scheme-handler/https"],
            ["xdg-mime", "query", "default", "x-scheme-handler/logos4"],
            ["xdg-mime", "query", "default", "x-scheme-handler/libronixdls"]
        ]

        if app.conf._raw.wine_binary:
            try:
                run_commands += [[paths.executable(app.conf.wine_binary), "--version"]]
            except ValueError as error:
                logging.warning("Skipping runtime diagnostic: %s", error)

        subprocess_env = copy.deepcopy(os.environ)
        # Set LANG to enable support
        subprocess_env["LANG"] = "en_US.UTF-8"

        for command in run_commands:
            try:
                output = subprocess.check_output(
                    command,
                    text=True,
                    env=subprocess_env
                )
                zip.writestr(f"/tmp/{" ".join(command).replace("/","_")}", output)
            except Exception as e:
                # Some of these commands may not be found.
                logging.debug(
                    "Failed to gather extra information: "
                    + " ".join(command) + ": " + str(e)
                )

        include_envs = [
            "WINEDEBUG",
            # Which desktop environment is running
            "XDG_CURRENT_DESKTOP",
            "DESKTOP_SESSION",
            # Needed to detect lxde
            "GDMSESSION",
            # Wayland vs X11
            "XDG_SESSION_TYPE",
            "DISPLAY",
            "LANG"
        ]

        context_to_write: dict[str, Any] = {}
        for env in include_envs:
            if os.getenv(env):
                context_to_write[env] = os.getenv(env)

        # Also add our DIALOG to the list
        context_to_write["DIALOG"] = system.get_dialog()

        # These aren't really envs, but still relevant. Consider a different file?
        context_to_write["RUNMODE"] = constants.RUNMODE
        context_to_write["CACHE_DIR"] = constants.CACHE_DIR
        context_to_write["DATA_HOME"] = constants.DATA_HOME
        context_to_write["CONFIG_DIR"] = constants.CONFIG_DIR
        context_to_write["STATE_DIR"] = constants.STATE_DIR

        context_to_write["network_cache"] = app.conf._network._cache._as_dict()
        context_to_write["ephemeral_config"] = app.conf._overrides.__dict__
        context_to_write["persistent_config"] = app.conf._raw._as_dict()

        zip.writestr("context.json", json.dumps(context_to_write, indent=4))

        app.status(f"Wrote support bundle to: {output_path}", percent=100)

        answer = app.ask(
            "How would you like to continue to get support?\n"
            "Keep this archive private and review its contents before sharing.\n"
            "The project repository and issues are private during development.\n"
            "Describe what went wrong and which actions you took.",
            [
                'Keep archive local',
                'Open private project issue',
                "Show links"
            ]
        )

        if answer == "Open private project issue":
            webbrowser.open(constants.REPOSITORY_NEW_ISSUE_LINK)
        elif answer == "Show links":
            app.info(
                "Private project links:\n"
                f"- Repository: {constants.REPOSITORY_LINK}\n"
                f"- Issues: {constants.REPOSITORY_ISSUES_LINK}\n"
            )

    return output_path
