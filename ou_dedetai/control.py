"""These functions take no arguments by default.
They can be called from CLI, GUI, or TUI.
"""

import copy
from contextlib import ExitStack
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
import subprocess
from typing import Any
import webbrowser
from zipfile import ZipFile

from ou_dedetai import constants
from ou_dedetai import maintenance, backup_store
from ou_dedetai import paths, artifacts, runtime
from ou_dedetai import system
from ou_dedetai.app import App


def edit_file(config_file: str):
    system.dependency_command(["xdg-open", config_file], "open the configuration editor")


@maintenance.maintains_installation
def remove_all_index_files(app: App):
    _remove_data_files(("BibleIndex", "LibraryIndex", "PersonalBookIndex", "LibraryCatalog"))
    app.status("Removed index and catalog files", 100)


@maintenance.maintains_installation
def remove_library_catalog(app: App):
    _remove_data_files(("LibraryCatalog",))
    app.status("Removed library catalog files", 100)


def _remove_data_files(names):
    from ou_dedetai.config import get_logos_appdata_dir, get_wine_user

    context = maintenance.current_context()
    user = get_wine_user(str(context.prefix))
    if user is None or context.product not in ("Logos", "Verbum"):
        raise maintenance.MaintenanceError("Cannot find installed product data")
    live = Path(get_logos_appdata_dir(str(context.prefix), user, context.product))
    if not live.resolve().is_relative_to(context.prefix):
        raise maintenance.MaintenanceError("Product data escapes the selected prefix")
    targets = []
    directories = []
    with ExitStack() as stack:
        parent = stack.enter_context(backup_store.Directory(live))
        if not parent.path.resolve().is_relative_to(context.prefix):
            raise maintenance.MaintenanceError("Product data moved outside the selected prefix")
        data = stack.enter_context(backup_store.Directory(parent.path / "Data"))
        directories.extend([parent, data])
        for user_name in os.listdir(data.fd):
            user_directory = stack.enter_context(backup_store.Directory(data.path / user_name))
            directories.append(user_directory)
            for name in names:
                candidate = user_directory.path / name
                if not os.path.lexists(candidate):
                    continue
                directory = stack.enter_context(backup_store.Directory(candidate))
                if directory.mount != parent.mount:
                    raise maintenance.MaintenanceError("Index cleanup cannot cross a nested mount")
                directories.append(directory)
                for filename in os.listdir(directory.fd):
                    info = os.stat(filename, dir_fd=directory.fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                        raise maintenance.MaintenanceError(f"Unsupported index cleanup entry: {filename}")
                    targets.append((directory, filename, backup_store.identity(info)))
        maintenance.assert_quiescent(context)
        for directory in directories:
            directory.validate()
        for directory, name, expected in targets:
            current = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            if backup_store.identity(current) != expected:
                raise maintenance.MaintenanceError("Index cleanup entry changed after validation")
        for directory, name, expected in targets:
            directory.validate()
            os.unlink(name, dir_fd=directory.fd)


@maintenance.maintains_installation
def uninstall(app: App):
    """Remove only preflighted installation data and explicitly selected cleanup."""
    from ou_dedetai.config import PersistentConfiguration

    config_path = Path(app.conf._overrides.config_path)
    install_value = app.conf._overrides.install_dir or app.conf._raw.install_dir
    install = Path(install_value) if install_value else None
    context = maintenance.current_context()
    pending_runtime = runtime.selection_state(context.install) / "runtime.json"
    if os.path.lexists(pending_runtime):
        raise maintenance.RecoveryRequired(
            "Restart LogosDesktop to recover the pending runtime selection, then retry uninstall"
        )
    if install is not None and context.prefix != (install / "data/wine64_bottle").resolve():
        raise maintenance.MaintenanceError("Selected prefix does not match the installation being uninstalled")
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

    confirmation = "These paths will be removed:\n\n" + "\n".join(str(path) for path in targets)
    if not app.approve("Are you sure you want to uninstall?", confirmation):
        return
    app.status("Uninstalling…")
    ordered = sorted(targets.values(), key=lambda target: not stat.S_ISDIR(target.info.st_mode))
    maintenance.assert_quiescent(context)
    with maintenance.artifact_use(exclusive=True):
        paths.remove_targets(ordered, protected)

    # Reloading would recreate a cache that the user just asked to remove.
    app.conf._raw = PersistentConfiguration()
    if callable(getattr(app.conf, "invalidate_context", None)):
        app.conf.invalidate_context()
    for attribute in (
        "_wine64_path", "_wine64_identity", "_download_dir", "_wine_user", "_logos_exe",
        "_installed_faithlife_product_release", "_wine_binary_files", "_wine_appimage_files",
        "_user_download_dir", "_wine_output_encoding",
    ):
        setattr(app.conf, attribute, None)
    app._config_updated_event.set()
    app.status("Uninstalled")


def get_support(app: App) -> str:
    """Publish a closed support archive before presenting follow-up choices."""

    # Save in ~/Downloads or ~/
    output_dir = Path(os.path.expanduser("~/Downloads"))
    if not output_dir.exists():
        output_dir = output_dir.parent

    output_path = app.ask("Where do you want to save the support zip?", [
        str(output_dir / constants.DEFAULT_SUPPORT_FILE_NAME),
        constants.PROMPT_OPTION_NEW_FILE,
    ])

    if not output_path or not str(output_path).strip():
        from ou_dedetai.app import UserExitedFromAsk
        raise UserExitedFromAsk
    output = Path(output_path).expanduser().absolute()
    sources = [
        app.conf.config_file_path, app.conf.app_log_path, app.conf.app_wine_log_path,
        app.conf.app_wine_log_previous_path, app.conf._faithlife_crash_log,
    ]
    for source in filter(None, sources):
        source = Path(source)
        if output.resolve() == source.resolve() or (output.exists() and source.exists() and output.samefile(source)):
            raise maintenance.MaintenanceError("Support output cannot replace a configuration or log file")
    if output.is_symlink() or output.is_dir():
        raise maintenance.MaintenanceError("Support output must be an unlinked regular file")

    app.status(f"Writing support package to: {output_path}", percent=0)

    with tempfile.TemporaryDirectory(prefix=".logos-support-", dir=output.parent) as temporary:
        staged = Path(temporary) / "support.zip"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with maintenance.artifact_use():
            _write_support(app, staged)
        with staged.open("rb") as completed:
            os.fsync(completed.fileno())
        with ZipFile(staged) as archive:
            if archive.testzip() is not None or "context.json" not in archive.namelist():
                raise maintenance.MaintenanceError("Support archive validation failed")
        os.replace(staged, output)

    app.status(f"Wrote support bundle to: {output_path}", percent=100)
    answer = app.ask(
        "How would you like to continue to get support?\n"
        "Keep this archive private and review its contents before sharing.\n"
        "The project repository and issues are private during development.\n"
        "Describe what went wrong and which actions you took.",
        ['Keep archive local', 'Open private project issue', 'Show links'],
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


def _write_support(app, output_path):
    with ZipFile(output_path, "w") as zip:
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
                selected = Path(app.conf._raw.wine_binary)
                if not selected.is_absolute():
                    selected = Path(app.conf._raw.install_dir) / selected
                # AppImage resolution can import bytes into the prefix. Diagnostics
                # skip it rather than changing the failing installation.
                if selected.resolve().name.lower().endswith(".appimage"):
                    raise ValueError("AppImage execution is skipped during read-only diagnostics")
                run_commands += [[str(paths.executable(selected)), "--version"]]
            except (ValueError, OSError, artifacts.ArtifactError, maintenance.MaintenanceError) as error:
                logging.warning("Skipping runtime diagnostic: %s", error)

        subprocess_env = copy.deepcopy(os.environ)
        # Set LANG to enable support
        subprocess_env["LANG"] = "en_US.UTF-8"

        for command in run_commands:
            try:
                output = subprocess.check_output(
                    command,
                    text=True,
                    env=subprocess_env,
                    timeout=15,
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
        context_to_write["DIALOG"] = getattr(app.conf._overrides, "dialog", None) or "cli"

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
