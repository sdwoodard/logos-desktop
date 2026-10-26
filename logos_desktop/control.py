"""These functions take no arguments by default.
They can be called from CLI, GUI, or TUI.
"""

from contextlib import ExitStack
import json
import os
import stat
from pathlib import Path

from logos_desktop import constants
from logos_desktop import maintenance, backup_store
from logos_desktop import paths, runtime
from logos_desktop import system, support
from logos_desktop.app import App, UserExitedFromAsk


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
    from logos_desktop.config import get_logos_appdata_dir, get_wine_user

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
    from logos_desktop.config import PersistentConfiguration

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
        portable = install / constants.PORTABLE_CONFIG_NAME
        if not os.path.lexists(portable):
            portable = install / f"{constants.BINARY_NAME}.json"
        paths.validate_removal_path(portable, protected)
        with portable.open() as source:
            portable_config = json.load(source)
        if not isinstance(portable_config, dict):
            raise ValueError(f"Installation ownership is not established: {install}")
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
        return False
    app.status("Uninstalling…")
    ordered = sorted(targets.values(), key=lambda target: not stat.S_ISDIR(target.info.st_mode))
    maintenance.assert_quiescent(context)
    with maintenance.artifact_use(exclusive=True):
        paths.remove_targets(ordered, protected)

    # Reloading would recreate a cache that the user just asked to remove.
    overrides = app.conf._overrides
    # Deletion ends pending migration. Keep later writes on the selected path.
    if hasattr(overrides, "config_destination"):
        overrides.config_destination = str(config_path)
    if hasattr(overrides, "runtime_selection"):
        overrides.runtime_selection = {}
    empty = PersistentConfiguration.from_document({}, getattr(overrides, "environment", {}))
    empty._file_path = str(config_path)
    empty._file_bytes = None
    app.conf._raw = empty
    if callable(getattr(app.conf, "rebind", None)):
        app.conf.rebind(empty)
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
    return True


def get_support(app: App, cancel=None) -> str | None:
    """Preview a frozen sanitized snapshot and confirm before publication."""

    if vars(app.conf._overrides).get("assume_yes"):
        raise maintenance.MaintenanceError("Support export requires explicit preview confirmation without --assume-yes")
    snapshot = support.prepare(app, cancel=cancel)
    app.info(support.preview(snapshot))
    if not app.approve("Export this exact sanitized support snapshot?",
                       "Raw logs, configuration, environment and cache are omitted."):
        return None

    # Save in ~/Downloads or ~/
    output_dir = Path(os.path.expanduser("~/Downloads"))
    if not output_dir.exists():
        output_dir = output_dir.parent

    output_path = app.ask("Where do you want to save the support zip?", [
        str(output_dir / constants.DEFAULT_SUPPORT_FILE_NAME),
        constants.PROMPT_OPTION_NEW_FILE,
    ])

    if not output_path or not str(output_path).strip():
        raise UserExitedFromAsk
    output = Path(output_path).expanduser().absolute()
    overwrite = False
    if os.path.lexists(output):
        overwrite = app.approve("Replace the existing support output?", str(output))
        if not overwrite:
            return None

    app.status(f"Writing support package to: {output_path}", percent=0)
    support.export(snapshot, output, overwrite=overwrite, cancel=cancel)

    app.status(f"Wrote support bundle to: {output_path}", percent=100)
    try:
        answer = app.ask(
            "How would you like to continue to get support?\n"
            "Keep this archive private and review its contents before sharing.\n"
            "The project repository and issues are private during development.\n"
            "Describe what went wrong and which actions you took.",
            ['Keep archive local', 'Open private project issue', 'Show links'],
        )
    except UserExitedFromAsk:
        return output_path
    if answer == "Open private project issue":
        system.run_command(["xdg-open", constants.REPOSITORY_NEW_ISSUE_LINK], timeout=10, check=False)
    elif answer == "Show links":
        app.info(
            "Private project links:\n"
            f"- Repository: {constants.REPOSITORY_LINK}\n"
            f"- Issues: {constants.REPOSITORY_ISSUES_LINK}\n"
        )
    return output_path
