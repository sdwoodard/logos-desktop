import copy
from contextlib import ExitStack, contextmanager
import os
from typing import Any, Optional
from dataclasses import dataclass, field
import logging
from pathlib import Path
import threading

from ou_dedetai import network, utils, constants, wine, paths, maintenance, runtime, config_store

from ou_dedetai.constants import PROMPT_OPTION_DIRECTORY

@dataclass
class LegacyConfiguration:
    """Configuration and it's keys from before the user configuration class existed.
    
    Useful for one directional compatibility"""
    # Legacy Core Configuration
    FLPRODUCT: Optional[str] = None
    TARGETVERSION: Optional[str] = None
    TARGET_RELEASE_VERSION: Optional[str] = None
    current_logos_version: Optional[str] = None # Unused in new code
    curses_colors: Optional[str] = None
    INSTALLDIR: Optional[str] = None
    # WINETRICKSBIN: Optional[str] = None
    WINEBIN_CODE: Optional[str] = None
    WINE_EXE: Optional[str] = None
    WINECMD_ENCODING: Optional[str] = None
    LOGS: Optional[str] = None
    BACKUPDIR: Optional[str] = None
    LAST_UPDATED: Optional[str] = None # Unused in new code
    RECOMMENDED_WINE64_APPIMAGE_URL: Optional[str] = None # Unused in new code
    LLI_LATEST_VERSION: Optional[str] = None # Unused in new code
    logos_release_channel: Optional[str] = None
    lli_release_channel: Optional[str] = None

    # Legacy Extended Configuration
    APPIMAGE_LINK_SELECTION_NAME: Optional[str] = None
    APPDIR_BINDIR: Optional[str] = None
    CHECK_UPDATES: Optional[bool] = None
    CONFIG_FILE: Optional[str] = None
    CUSTOMBINPATH: Optional[str] = None
    DEBUG: Optional[bool] = None
    DELETE_LOG: Optional[str] = None
    DIALOG: Optional[str] = None # Unused in new code
    LOGOS_LOG: Optional[str] = None
    wine_log: Optional[str] = None
    LOGOS_EXE: Optional[str] = None # Unused in new code
    # This is the logos installer executable name (NOT path)
    LOGOS_EXECUTABLE: Optional[str] = None
    LOGOS_VERSION: Optional[str] = None
    # This wasn't overridable in the bash version of this installer (at 554c9a6),
    # nor was it used in the python version (at 8926435)
    # LOGOS64_MSI: Optional[str]
    LOGOS64_URL: Optional[str] = None
    SELECTED_APPIMAGE_FILENAME: Optional[str] = None
    SKIP_DEPENDENCIES: Optional[bool] = None
    use_python_dialog: Optional[str] = None
    VERBOSE: Optional[bool] = None
    WINEDEBUG: Optional[str] = None
    WINEDLLOVERRIDES: Optional[str] = None
    WINEPREFIX: Optional[str] = None
    WINESERVER_EXE: Optional[str] = None

    @classmethod
    def bool_keys(cls) -> list[str]:
        """Returns a list of keys that are of type bool"""
        return list(config_store.BOOLEAN_FIELDS.intersection(cls.__dataclass_fields__))

    @classmethod
    def config_file_path(cls) -> str:
        value = os.environ["CONFIG_FILE"] if "CONFIG_FILE" in os.environ else constants.DEFAULT_CONFIG_PATH
        return str(config_store.absolute_path(value))

    @classmethod
    def load(cls) -> "LegacyConfiguration":
        """Discover legacy input without moving or rewriting it."""
        destination = LegacyConfiguration.config_file_path()
        source = destination
        if "CONFIG_FILE" not in os.environ and not Path(destination).exists():
            for candidate in constants.LEGACY_CONFIG_FILES:
                if Path(candidate).is_file():
                    source = str(config_store.absolute_path(candidate))
                    break
        loaded = cls.load_from_path(source)
        loaded._destination = destination
        return loaded

    @classmethod
    def load_from_path(cls, config_file_path: str) -> "LegacyConfiguration":
        path = config_store.absolute_path(config_file_path)
        document, _ = config_store.read_document(path)
        document = config_store.apply_changes(document, environment_document(), path)
        install = document.get("install_dir")
        for name in config_store.PATH_FIELDS - {"install_dir", "INSTALLDIR", "wine_binary", "WINE_EXE"}:
            if document.get(name) is not None:
                document[name] = str(config_store.absolute_path(document[name], base=install or path.parent))
        output = {name: document[name] for name in cls.__dataclass_fields__ if name in document}
        output["CONFIG_FILE"] = str(path)
        return cls(**output)

    @classmethod
    def load_from_env(cls) -> "LegacyConfiguration":
        output: dict = {}
        # Now update from ENV
        for var in LegacyConfiguration().__dict__.keys():
            env_var = os.getenv(var)
            if env_var is not None:
                if var in LegacyConfiguration.bool_keys():
                    output[var] = config_store.boolean(env_var, var, "environment")
                else:
                    output[var] = env_var
        return LegacyConfiguration(**output)


def environment_document():
    """Capture only explicitly supplied legacy overrides, with invocation path bases."""
    legacy = LegacyConfiguration.load_from_env()
    output = {name: value for name, value in legacy.__dict__.items() if value is not None}
    for name in config_store.PATH_FIELDS:
        if name in output and output[name] not in (constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL):
            output[name] = str(config_store.absolute_path(output[name]))
    return config_store.canonical(output, Path.cwd() / "environment")


@dataclass
class EphemeralConfiguration:
    """A set of overrides that don't need to be stored.

    Populated from environment/command arguments/etc

    Changes to this are not saved to disk, but remain while the program runs
    """
    
    # See naming conventions in Config
    
    # Start user overridable via env or cli arg
    installer_binary_dir: Optional[str]
    install_dir: Optional[str]
    wineserver_binary: Optional[str]
    faithlife_product_version: Optional[str]
    faithlife_installer_name: Optional[str]
    faithlife_installer_download_url: Optional[str]
    log_level: Optional[str | int]
    app_log_path: Optional[str]
    app_wine_log_path: Optional[str]
    """Path to log wine's output to"""

    install_dependencies_skip: Optional[bool]
    """Whether to skip installing system package dependencies"""

    wine_dll_overrides: Optional[str]
    """Corresponds to wine's WINEDLLOVERRIDES"""
    wine_debug: Optional[str]
    """Corresponds to wine's WINEDEBUG"""
    wine_prefix: Optional[str]
    """Corresponds to wine's WINEPREFIX"""
    wine_output_encoding: Optional[str]
    """Override for what encoding wine's output is using"""

    # FIXME: seems like the wine appimage logic can be simplified
    wine_appimage_link_file_name: Optional[str]
    """Symlink file name to the active wine appimage."""

    wine_appimage_path: Optional[str]
    """Path to the selected appimage"""

    # FIXME: consider using PATH instead? (and storing this legacy env in PATH for this process)
    custom_binary_path: Optional[str]
    """Additional path to look for when searching for binaries."""

    delete_log: Optional[bool]
    """Whether to clear the log on startup"""

    check_updates_now: Optional[bool]
    """Whether or not to check updates regardless of if one's due"""    

    # Start internal values
    config_path: str
    """Active read source, rebound to config_destination after migration commits."""

    config_destination: Optional[str] = None
    """Selected write destination, distinct only during default legacy discovery."""
    environment: dict[str, Any] = field(default_factory=dict)
    """Only supplied environment values, kept separate from saved projections."""
    cli_overrides: dict[str, Any] = field(default_factory=dict)
    """Only supplied CLI values, reapplied when the saved document changes."""
    runtime_selection: dict[str, Any] = field(default_factory=dict)
    """An explicitly activated runtime wins for this controller invocation."""

    assume_yes: bool = False
    """Whether to assume yes to all prompts or ask the user
    
    Useful for non-interactive installs"""

    quiet: bool = False
    """Whether or not to output any non-error messages"""

    dialog: Optional[str] = None
    """Override if we want to select a specific type of front-end
    
    Qt and Textual are current interfaces. Legacy tk/curses/cli spellings are
    normalized by entry-point and restart dispatch.
    """

    wine_args: Optional[list[str]] = None
    """Arguments when running wine from cli"""

    terminal_app_prefer_dialog: Optional[bool] = None

    # Start of values just set via cli arg
    app_run_as_root_permitted: bool = False
    agreed_to_faithlife_terms: bool = False
    """The user expressed clear agreement with faithlife's terms.
    Normally the MSI would prompt for this as well.

    DO NOT set this unless clear beyond a reasonable doubt the user knows
    what they're doing - agreeing to https://faithlife.com/terms
    """

    @classmethod
    def from_legacy(cls, legacy: LegacyConfiguration) -> "EphemeralConfiguration":
        log_level = None
        wine_debug = legacy.WINEDEBUG
        if legacy.DEBUG:
            log_level = logging.DEBUG
            # FIXME: shouldn't this leave it untouched or fall back to default: `fixme-all,err-all`?
            wine_debug = ""
        elif legacy.VERBOSE:
            log_level = logging.INFO
            wine_debug = ""
        delete_log = None
        if legacy.DELETE_LOG is not None:
            delete_log = utils.parse_bool(legacy.DELETE_LOG)
        config_file = constants.DEFAULT_CONFIG_PATH
        if legacy.CONFIG_FILE is not None:
            config_file = legacy.CONFIG_FILE
        terminal_app_prefer_dialog = None
        if legacy.use_python_dialog is not None:
            terminal_app_prefer_dialog = utils.parse_bool(legacy.use_python_dialog)
        install_dir = None
        if legacy.INSTALLDIR is not None:
            install_dir = str(Path(os.path.expanduser(legacy.INSTALLDIR)).absolute())
        return EphemeralConfiguration(
            installer_binary_dir=legacy.APPDIR_BINDIR,
            wineserver_binary=legacy.WINESERVER_EXE,
            custom_binary_path=legacy.CUSTOMBINPATH,
            faithlife_product_version=legacy.LOGOS_VERSION,
            faithlife_installer_name=legacy.LOGOS_EXECUTABLE,
            faithlife_installer_download_url=legacy.LOGOS64_URL,
            log_level=log_level,
            wine_debug=wine_debug,
            wine_dll_overrides=legacy.WINEDLLOVERRIDES,
            wine_prefix=legacy.WINEPREFIX,
            app_wine_log_path=legacy.wine_log,
            app_log_path=legacy.LOGOS_LOG,
            config_path=config_file,
            check_updates_now=legacy.CHECK_UPDATES,
            delete_log=delete_log,
            install_dependencies_skip=legacy.SKIP_DEPENDENCIES,
            wine_appimage_link_file_name=legacy.APPIMAGE_LINK_SELECTION_NAME,
            wine_appimage_path=legacy.SELECTED_APPIMAGE_FILENAME,
            wine_output_encoding=legacy.WINECMD_ENCODING,
            terminal_app_prefer_dialog=terminal_app_prefer_dialog,
            dialog=legacy.DIALOG,
            install_dir=install_dir
        )

    @classmethod
    def load(cls) -> "EphemeralConfiguration":
        legacy = LegacyConfiguration.load()
        document, _ = config_store.read_document(legacy.CONFIG_FILE)
        return cls.project(document, legacy.CONFIG_FILE, destination=legacy._destination)

    @classmethod
    def load_from_path(cls, path: str) -> "EphemeralConfiguration":
        path = str(config_store.absolute_path(path))
        document, _ = config_store.read_document(path)
        return cls.project(document, path)

    @classmethod
    def project(cls, document, path, *, destination=None, environment=None, cli_overrides=None, runtime_selection=None):
        """Resolve saved paths, then apply only genuine environment and CLI overlays."""
        environment = environment_document() if environment is None else environment
        effective = config_store.apply_changes(document, environment, path)
        effective = config_store.apply_changes(effective, runtime_selection or {}, path)
        install = effective.get("install_dir")
        for name in config_store.PATH_FIELDS - {"install_dir", "INSTALLDIR", "wine_binary", "WINE_EXE"}:
            if effective.get(name) is not None:
                effective[name] = str(config_store.absolute_path(effective[name], base=install or Path(path).parent))
        legacy = LegacyConfiguration(**{
            name: value for name, value in effective.items() if name in LegacyConfiguration.__dataclass_fields__
        })
        legacy.CONFIG_FILE = str(path)
        result = cls.from_legacy(legacy)
        result.install_dir = environment.get("install_dir")
        result.config_destination = str(destination or path)
        result.environment = copy.deepcopy(environment)
        result.cli_overrides = copy.deepcopy(cli_overrides or {})
        result.runtime_selection = copy.deepcopy(runtime_selection or {})
        for name, value in result.cli_overrides.items():
            setattr(result, name, value)
        return result


@dataclass
class PersistentConfiguration:
    """This class stores the options the user chose

    Normally shouldn't be used directly, as it's types may be None,
    doesn't handle updates. Use through the `App`'s `Config` instead.
    
    Easy reading to/from JSON and supports legacy keys
    
    These values should be stored across invocations
    
    MUST be saved explicitly
    """

    # See naming conventions in Config

    faithlife_product: Optional[str] = None
    faithlife_product_version: Optional[str] = None
    faithlife_product_release: Optional[str] = None
    faithlife_product_logging: Optional[bool] = None
    install_dir: Optional[str] = None
    wine_binary: Optional[str] = None
    # FIXME: Should this be stored independently of wine_binary
    # (and potentially get out of sync), or
    # Should we derive this value from wine_binary?
    wine_binary_code: Optional[str] = None
    """This is the type of wine installation used"""
    backup_dir: Optional[str] = None

    # Color to use in curses. Either "System", "Logos", "Light", or "Dark"
    curses_color_scheme: Optional[str] = None
    # Faithlife's release channel. Either "stable" or "beta"
    faithlife_product_release_channel: str = "stable"
    # The Installer's release channel. Either "stable" or "beta"
    app_release_channel: str = "stable"

    _legacy: Optional[LegacyConfiguration] = None
    """A Copy of the legacy configuration.
    
    Merge this when writing.
    Kept just in case the user wants to go back to an older installer version
    """

    @classmethod
    def load_from_path(cls, config_file_path: str, *, environment=True) -> "PersistentConfiguration":
        path = config_store.absolute_path(config_file_path)
        document, content = config_store.read_document(path)
        if isinstance(environment, dict):
            overrides = environment
        elif environment:
            overrides = environment_document()
        else:
            overrides = {}
        loaded = cls.from_document(document, overrides)
        loaded._file_path = str(path)
        loaded._file_bytes = content
        return loaded

    @classmethod
    def from_document(cls, document, overrides):
        effective = config_store.apply_changes(document, overrides, "configuration")
        public = {name for name in cls.__dataclass_fields__ if not name.startswith("_")}
        legacy_names = LegacyConfiguration.__dataclass_fields__
        loaded = cls(**{name: value for name, value in effective.items() if name in public})
        loaded._legacy = LegacyConfiguration(**{
            name: value for name, value in effective.items() if name in legacy_names
        })
        loaded._extra = {name: value for name, value in effective.items()
                         if name not in public and name not in legacy_names}
        loaded._saved = copy.deepcopy(document)
        loaded._environment = copy.deepcopy(overrides)
        loaded._baseline = loaded._as_dict()
        return loaded

    @classmethod
    def from_legacy(cls, legacy: LegacyConfiguration) -> "PersistentConfiguration":
        faithlife_product_logging = None
        if legacy.LOGS is not None:
            faithlife_product_logging = utils.parse_bool(legacy.LOGS)
        install_dir = None
        if legacy.INSTALLDIR is not None:
            install_dir = str(Path(os.path.expanduser(legacy.INSTALLDIR)).absolute())
        return PersistentConfiguration(
            faithlife_product=legacy.FLPRODUCT,
            backup_dir=legacy.BACKUPDIR,
            curses_color_scheme=legacy.curses_colors,
            faithlife_product_release=legacy.TARGET_RELEASE_VERSION,
            faithlife_product_release_channel=legacy.logos_release_channel or 'stable',
            faithlife_product_version=legacy.TARGETVERSION,
            install_dir=install_dir,
            app_release_channel=legacy.lli_release_channel or 'stable',
            wine_binary=legacy.WINE_EXE,
            wine_binary_code=legacy.WINEBIN_CODE,
            faithlife_product_logging=faithlife_product_logging,
            _legacy=legacy
        )

    def _as_dict(self) -> dict[str, Any]:
        output = copy.deepcopy(getattr(self, "_extra", {}))
        if self._legacy is not None:
            output.update({name: value for name, value in self._legacy.__dict__.items()
                           if not name.startswith("_") and value is not None})
        output.update({name: getattr(self, name) for name in self.__dataclass_fields__ if not name.startswith("_")})
        output = config_store.canonical(output, getattr(self, "_file_path", "configuration"))
        output = {name: value for name, value in output.items() if value is not None}
        if self.install_dir is not None:
            path_fields = {
                "wine_binary", "backup_dir", "WINE_EXE", "BACKUPDIR",
                "LOGOS_LOG", "wine_log", "WINESERVER_EXE", "CUSTOMBINPATH",
                "SELECTED_APPIMAGE_FILENAME",
            }
            # Startup consumes prefix and binary-directory overrides as absolute
            # paths before it can resolve installation-relative runtime values.
            for k, v in output.items():
                if k in ["install_dir", "INSTALLDIR"]:
                    if v is not None:
                        output[k] = str(v)
                    continue
                if k in path_fields and isinstance(v, str):
                    output[k] = utils.get_relative_path(v, self.install_dir)
        return output

    @runtime.configuration_writer
    def write_config(self, *, checked=False, config_file_path=None, validator=None, restoration=None):
        """Commit only changed fields, preserving saved values hidden by overrides."""
        path = config_file_path or getattr(self, "_file_path", None) or LegacyConfiguration.config_file_path()
        output = self._as_dict()
        baseline = getattr(self, "_baseline", {})
        changes = {name: output.get(name) for name in output.keys() | baseline.keys()
                   if output.get(name) != baseline.get(name)}
        saved = config_store.save(
            path, changes, source_path=getattr(self, "_file_path", path),
            source_bytes=getattr(self, "_file_bytes", None),
            overrides=getattr(self, "_environment", {}),
            expected=getattr(self, "_saved", None),
            validator=validator, restoration=restoration,
        )
        loaded = self.from_document(config_store.canonical(saved.document, path), getattr(self, "_environment", {}))
        loaded._file_path = str(config_store.absolute_path(path))
        loaded._file_bytes = config_store.encode(saved.document)
        self.__dict__.update(loaded.__dict__)
        return saved

# Needed this logic outside this class too for before when the app is initialized
def get_wine_prefix_path(install_dir: str) -> str:
    return f"{install_dir}/data/wine64_bottle"


def get_wine_user(wine_prefix: str) -> Optional[str]:
    users_path = f"{wine_prefix}/drive_c/users"
    if not os.path.exists(users_path):
        return None
    # Cache a successful result (as it goes out to the fs)
    users = os.listdir(users_path)
    try:
        users.remove("Public")
    except ValueError:
        pass
    if users:
        return users[0]
    return None


def get_appdata_dir(
    wine_prefix: str,
    wine_user: str,
) -> str:
    return f'{wine_prefix}/drive_c/users/{wine_user}/AppData/'


def get_logos_appdata_dir(
    wine_prefix: str,
    wine_user: str,
    faithlife_product: str
) -> str:
    appdata = get_appdata_dir(wine_prefix=wine_prefix, wine_user=wine_user)
    return f'{appdata}/Local/{faithlife_product}'


def get_logos_user_id(
    logos_appdata_dir: str
) -> Optional[str]:
    logos_data_path = Path(logos_appdata_dir) / "Data"
    # If somehow the directory does not exist - like if the user removed it manually while trying to reset their data.
    if not logos_data_path.exists():
        return None
    contents = os.listdir(logos_data_path)
    children = [logos_data_path / child for child in contents]
    file_children = [child for child in children if child.is_dir()]
    if file_children and len(file_children) > 0:
        return file_children[0].name
    else:
        return None

class Config:
    """Set of configuration values. 
    
    If the user hasn't selected a particular value yet, they will be prompted in the UI.
    """

    # Naming conventions:
    # Use `dir` instead of `directory`
    # Use snake_case
    # prefix with faithlife_ if it's theirs
    # prefix with app_ if it's ours (and otherwise not clear)
    # prefix with wine_ if it's theirs
    # suffix with _binary if it's a linux binary
    # suffix with _exe if it's a windows binary structured as a linux path
    # suffix with _exe_windows_path if it's a windows binary structured as a windows path
    # suffix with _path if it's a file path
    # suffix with _file_name if it's a file's name (with extension)

    # Storage for the keys
    _raw: PersistentConfiguration

    # Overriding programmatically generated values from ENV
    _overrides: EphemeralConfiguration

    _network: network.NetworkRequests

    # Start Cache of values unlikely to change during operation.
    # i.e. filesystem traversals
    _wine_user: Optional[str] = None
    _wine64_path: Optional[str] = None
    _download_dir: Optional[str] = None
    _user_download_dir: Optional[str] = None
    _wine_output_encoding: Optional[str] = None
    _installed_faithlife_product_release: Optional[str] = None
    _wine_binary_files: Optional[list[str]] = None
    _wine_appimage_files: Optional[list[str]] = None

    # Start constants
    _curses_color_scheme_valid_values = ["System", "Light", "Dark", "Logos"]

    def __init__(self, ephemeral_config: EphemeralConfiguration, app) -> None:
        from ou_dedetai.app import App
        self.app: "App" = app
        self._overrides = ephemeral_config
        self._raw = PersistentConfiguration.load_from_path(
            ephemeral_config.config_path, environment=getattr(ephemeral_config, "environment", True),
        )
        self.rebind(self._raw)
        self.persistence_warning = ""
        self._write_validation = threading.local()
        self.invalidate_context()
        self._download_dir = self._user_download_dir = None
        self._network = self._create_network_requests_with_hook()

    def rebind(self, raw):
        """Publish one saved/effective context after save, reload or journal recovery."""
        previous = self._overrides
        path = getattr(raw, "_file_path", previous.config_path)
        environment = getattr(previous, "environment", getattr(raw, "_environment", {}))
        runtime_selection = getattr(previous, "runtime_selection", {})
        if hasattr(raw, "_saved"):
            transient = config_store.apply_changes(environment, runtime_selection, path)
            rebound = PersistentConfiguration.from_document(raw._saved, transient)
            rebound._file_path, rebound._file_bytes = raw._file_path, raw._file_bytes
            raw = rebound
        overrides = EphemeralConfiguration.project(
            getattr(raw, "_saved", raw._as_dict()), path,
            destination=getattr(previous, "config_destination", None) or path,
            environment=environment,
            cli_overrides=getattr(previous, "cli_overrides", {}),
            runtime_selection=runtime_selection,
        )
        for name in ("assume_yes", "quiet", "agreed_to_faithlife_terms", "app_run_as_root_permitted", "wine_args"):
            if hasattr(previous, name):
                setattr(overrides, name, getattr(previous, name))
        self._raw, self._overrides = raw, overrides
        self.invalidate_context()

    def invalidate_context(self):
        for name in (
            "_wine_user", "_wine_user_identity", "_wine64_path", "_wine64_identity",
            "_wine_output_encoding", "_installed_faithlife_product_release", "_installed_release_identity",
            "_wine_binary_files", "_wine_appimage_files", "_recommended_wine_context", "_recommended_wine_status",
        ):
            setattr(self, name, None)

    def _change(self, *, legacy_changes=None, **changes):
        """Keep in-memory edits and their disk patch inside the same writer admission."""
        try:
            with runtime.configuration_lock():
                previous = {name: getattr(self._raw, name) for name in changes}
                previous_legacy = copy.deepcopy(self._raw._legacy)
                for name, value in changes.items():
                    setattr(self._raw, name, value)
                if legacy_changes:
                    if self._raw._legacy is None:
                        self._raw._legacy = LegacyConfiguration()
                    for name, value in legacy_changes.items():
                        setattr(self._raw._legacy, name, value)
                try:
                    saved = self._write(checked=True)
                except BaseException:
                    for name, value in previous.items():
                        setattr(self._raw, name, value)
                    self._raw._legacy = previous_legacy
                    raise
                self.invalidate_context()
        except OSError as error:
            raise config_store.ConfigurationError("Configuration write admission failed") from error
        warning = getattr(self, "persistence_warning", "")
        if warning:
            try:
                self.app.status(warning)
            except Exception:
                logging.exception("Configuration committed, but its warning could not be displayed")
        return saved

    def restore_changes(self, previous, published):
        """Restore operation-owned stored fields without replacing later edits."""
        with runtime.configuration_lock(), self.validated_writes(None):
            saved = self._write(checked=True, restoration=(previous, published))
        return saved.conflicts

    def _create_network_requests_with_hook(self) -> network.NetworkRequests:
        def _network_cache_hook():
            self.app._config_updated_event.set()

        return network.NetworkRequests(
            self._overrides.check_updates_now,
            hook=_network_cache_hook
        )
 
    def _ask_if_not_found(
        self,
        parameter: str,
        question: str,
        options: list[str],
        dependent_parameters: Optional[list[str]] = None
    ) -> str:  
        if not getattr(self._raw, parameter):
            answer = self.app.ask(question, options)
            if getattr(Config, parameter, None) and getattr(Config, parameter).fset is not None:
                getattr(Config, parameter).fset(self, answer)
            else:
                changes = {name: None for name in dependent_parameters or []}
                changes[parameter] = answer
                self._change(**changes)
        # parameter given should be a string
        return str(getattr(self._raw, parameter))

    def _write(self, *, checked=False, restoration=None):
        """Save the selected profile and report a committed portable-copy failure."""
        destination = getattr(self._overrides, "config_destination", None) or self._overrides.config_path
        options = {"checked": True, "config_file_path": destination}
        if restoration is not None:
            options["restoration"] = restoration
        if self.write_validator is not None:
            options["validator"] = self.write_validator
        saved = self._raw.write_config(**options)
        self._overrides.config_path = destination
        if isinstance(self._overrides, EphemeralConfiguration):
            self.rebind(self._raw)
        self.persistence_warning = saved.warning
        self.app._config_updated_event.set()
        if saved.warning:
            logging.warning(saved.warning)
        return saved

    @property
    def write_validator(self):
        return getattr(getattr(self, "_write_validation", None), "callback", None)

    @contextmanager
    def validated_writes(self, validator):
        """Check merged documents before any operation-owned configuration publication."""
        previous = self.write_validator
        self._write_validation.callback = validator
        try:
            yield
        finally:
            self._write_validation.callback = previous

    def _relative_from_install_dir(self, path: Path | str) -> str:
        """Store descendants relatively without changing external path identity."""
        return paths.relative_to_install(path, self.install_dir)

    def _absolute_from_install_dir(self, path: Path | str) -> str:
        """Takes in a possibly relative path under install dir and turns it into an
        absolute path

        Args:
            path - can be absolute or relative to install dir
        
        Returns:
            path - absolute
        """
        return str(paths.absolute_from_install(path, self.install_dir))

    def reload(self):
        """Reload the bound profile without journal recovery or unrelated discovery."""
        path = self._overrides.config_path
        environment = getattr(self._overrides, "environment", True)
        loaded = PersistentConfiguration.load_from_path(path, environment=environment)
        with maintenance.change_selection(self, loaded.install_dir, document=loaded._as_dict()):
            with runtime.configuration_lock():
                if self._overrides.config_path != path:
                    raise maintenance.MaintenanceBusy("Configuration source changed during reload. Retry it")
                current = PersistentConfiguration.load_from_path(path, environment=environment)
                if config_store.selection_identity(current._saved) != config_store.selection_identity(loaded._saved):
                    raise maintenance.MaintenanceBusy("Installation changed during reload. Retry after it settles")
                self.rebind(current)
                self._network = self._create_network_requests_with_hook()
                self.app._config_updated_event.set()

    @property
    def config_file_path(self) -> str:
        return self._overrides.config_path

    @property
    def faithlife_product(self) -> str:
        question = "Choose which FaithLife product the script should install: "
        options = constants.FAITHLIFE_PRODUCTS
        return self._ask_if_not_found(
            "faithlife_product",
            question,
            options,
            [
                "faithlife_product_version",
                "faithlife_product_release"
            ]
        )

    @faithlife_product.setter
    def faithlife_product(self, value: Optional[str]):
        if self._raw.faithlife_product != value:
            with maintenance.change_selection(self):
                changes = dict(faithlife_product=value, faithlife_product_version=None, faithlife_product_release=None)
                if self._overrides.install_dir is None:
                    changes["install_dir"] = None
                self._change(**changes)

    @property
    def faithlife_product_version(self) -> str:
        if getattr(self._overrides, "faithlife_product_version", None) is not None:
            return self._overrides.faithlife_product_version
        if self._raw.faithlife_product_version is not None:
            return self._raw.faithlife_product_version
        return "10"

    @faithlife_product_version.setter
    def faithlife_product_version(self, value: Optional[str]):
        if self._raw.faithlife_product_version != value:
            with maintenance.change_selection(self):
                changes = dict(faithlife_product_version=value, faithlife_product_release=None)
                if self._overrides.install_dir is None:
                    changes["install_dir"] = None
                self._change(**changes)

    @property
    def faithlife_product_releases(self) -> list[str]:
        return self._network.faithlife_product_releases(
            product=self.faithlife_product,
            version=self.faithlife_product_version,
            channel=self.faithlife_product_release_channel
        )

    @property
    def faithlife_product_release(self) -> str:
        if self._raw.faithlife_product_release is not None:
            return self._raw.faithlife_product_release
        question = (
            f"Which version of {self.faithlife_product} {self.faithlife_product_version} do you want to install?: "
        )
        options = self.faithlife_product_releases
        return self._ask_if_not_found("faithlife_product_release", question, options)

    @property
    def is_installed_faithlife_product_release_latest(self) -> Optional[bool]:
        if not self._raw.faithlife_product or not self._raw.faithlife_product_version:
            return None
        lookup = self._network.product_release(self._raw.faithlife_product, self._raw.faithlife_product_version,
                                               self.faithlife_product_release_channel, refresh=False)
        if lookup.stale or lookup.status != "ok" or not lookup.versions:
            return None
        releases = lookup.versions
        installed = self.installed_faithlife_product_release
        if installed is None:
            return None
        # Now unfortunately the format differs between the releases and the installed one
        # the releases has it in the form 51.1.0.0003 (notice the padded 0 on the build number)
        # While the installed version has it in the form 51.1.0.3
        # Normalize both to tuples of ints so the padding doesn't matter.
        def _normalize(version: str) -> tuple[int, ...]:
            return tuple(int(part) for part in version.split("."))

        return _normalize(releases[0]) == _normalize(installed)

    def refresh_release_snapshots(self, *, force=False):
        """Called by the owned release worker, never by widget rendering."""
        from . import processes

        if self._raw.faithlife_product and self._raw.faithlife_product_version:
            self._network.product_release(self._raw.faithlife_product, self._raw.faithlife_product_version,
                                          self.faithlife_product_release_channel, force=force)
        if processes.cancelled():
            return
        if self._raw.wine_binary and self._raw.wine_binary_code in ("Recommended", "AppImage"):
            context = self._raw.install_dir, self._raw.wine_binary
            lookup = self._network.wine_appimage_release()
            if lookup.status != "ok" or lookup.stale:
                status = (-1, f"Current Wine provider information is stale or unavailable: {lookup.message}")
            else:
                status = utils.compare_recommended_appimage_version(self.app)
            if context == (self._raw.install_dir, self._raw.wine_binary):
                self._recommended_wine_status = status
                self._recommended_wine_context = context

    @property
    def recommended_wine_status(self):
        context = self._raw.install_dir, self._raw.wine_binary
        if context != getattr(self, "_recommended_wine_context", None):
            return -1, "Recommended Wine information has not been checked for this selection"
        return self._recommended_wine_status

    @faithlife_product_release.setter
    def faithlife_product_release(self, value: Optional[str]):
        if self._raw.faithlife_product_release == value:
            return
        with ExitStack() as admission:
            if self._overrides.install_dir or self._raw.install_dir:
                context = admission.enter_context(maintenance.configured(self.app))
                admission.enter_context(maintenance.use(context))
            self._change(faithlife_product_release=value)

    @property
    def faithlife_product_icon_path(self) -> str:
        return str(constants.APP_IMAGE_DIR / "logos-desktop-icon.png")

    @property
    def faithlife_product_logging(self) -> bool:
        """Whether or not the installed faithlife product is configured to log"""
        if self._raw.faithlife_product_logging is not None:
            return self._raw.faithlife_product_logging
        return False
    
    @faithlife_product_logging.setter
    def faithlife_product_logging(self, value: bool):
        if self._raw.faithlife_product_logging != value:
            try:
                self._change(faithlife_product_logging=value)
            except maintenance.MaintenanceError as error:
                raise maintenance.MaintenanceError(
                    f"Runtime logging changed, but its setting could not be saved: {error}"
                ) from error

    @property
    def faithlife_installer_name(self) -> str:
        if self._overrides.faithlife_installer_name is not None:
            return self._overrides.faithlife_installer_name
        return f"{self.faithlife_product}_v{self.faithlife_product_release}-x64.msi"

    @property
    def faithlife_installer_download_url(self) -> str:
        if self._overrides.faithlife_installer_download_url is not None:
            return self._overrides.faithlife_installer_download_url
        after_version_url_part = "/Verbum/" if self.faithlife_product == "Verbum" else "/"
        return f"https://downloads.logoscdn.com/LBS{self.faithlife_product_version}{after_version_url_part}Installer/{self.faithlife_product_release}/{self.faithlife_product}-x64.msi"

    @property
    def faithlife_product_release_channel(self) -> str:
        return self._raw.faithlife_product_release_channel

    @property
    def app_release_channel(self) -> str:
        return self._raw.app_release_channel

    @property
    @maintenance.uses_owner
    def winetricks_binary(self) -> str:
        """Download winetricks if it doesn't exist"""
        from ou_dedetai import system
        return system.ensure_winetricks(
            app=self.app,
            status_messages=False
        )

    @property
    def install_dir_default(self) -> str:
        return str(Path(constants.DATA_HOME) / 'installation')

    @property
    def install_dir(self) -> str:
        if self._overrides.install_dir:
            return self._overrides.install_dir
        default = self.install_dir_default
        question = f"Where should {self.faithlife_product} files be installed to?: "
        options = [default, PROMPT_OPTION_DIRECTORY]
        output = self._ask_if_not_found("install_dir", question, options)
        return output
    
    @install_dir.setter
    def install_dir(self, value: Optional[str | Path]):
        if value is not None:
            value = str(config_store.absolute_path(value))
        if self._raw.install_dir != value:
            with maintenance.change_selection(self, value):
                self._change(install_dir=value)

    @property
    # This used to be called APPDIR_BINDIR
    def installer_binary_dir(self) -> str:
        if self._overrides.installer_binary_dir is not None:
            return self._overrides.installer_binary_dir
        return f"{self.install_dir}/{constants.RELATIVE_BINARY_DIR}"

    @property
    def _logos_appdata_dir(self) -> Optional[str]:
        """Path to the user's Logos installation under AppData"""
        # We don't want to prompt the user in this function
        if (
            self._raw.faithlife_product is None
            or self._raw.install_dir is None
        ):
            return None
        wine_user = self.wine_user
        if wine_user is None:
            return None
        return get_logos_appdata_dir(
            self.wine_prefix,
            wine_user,
            self.faithlife_product
        )

    @property
    def _faithlife_logs_dir(self) -> Optional[str]:
        """Path to the directory containing faithlife logs"""
        # We don't want to prompt the user in this function
        wine_user = self.wine_user
        if (
            wine_user is None
            or self._raw.faithlife_product is None
            or self._raw.install_dir is None
        ):
            return None
        appdata = get_appdata_dir(
            self.wine_prefix,
            wine_user,
        )
        return f'{appdata}/Local/Faithlife/Logs/{self.faithlife_product}'

    @property
    def _faithlife_crash_log(self) -> Optional[str]:
        """Path to the LogosCrash.log or VerbumCrash.log respectively"""
        faithlife_logs = self._faithlife_logs_dir
        if not faithlife_logs or self._raw.faithlife_product is None:
            return None
        # FIXME: Confirm Verbum's Crash log has this name - not aware of a Verbum crash right now
        # to confirm this.
        return f"{faithlife_logs}/{self._raw.faithlife_product}Crash.log"

    @property
    def _logos_user_id(self) -> Optional[str]:
        """Name of the Logos user id throughout the app"""
        logos_appdata_dir = self._logos_appdata_dir
        if logos_appdata_dir is None:
            return None
        return get_logos_user_id(logos_appdata_dir)

    @property
    # This used to be called WINEPREFIX
    def wine_prefix(self) -> str:
        if self._overrides.wine_prefix is not None:
            return self._overrides.wine_prefix
        return get_wine_prefix_path(self.install_dir)

    @property
    def wine_binary(self) -> str:
        """Returns absolute path to the wine binary"""
        output = self._raw.wine_binary
        if output is None:
            question = (
                f"Which Wine AppImage or binary should the script use to install {self.faithlife_product} "
                f"v{self.faithlife_product_version} in {self.install_dir}?: "
            )
            options = utils.get_wine_options(self.app)

            choice = self.app.ask(question, options)

            output = choice
            self.wine_binary = choice
            output = self._raw.wine_binary

        if output in (constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL):
            self.wine_binary = output
            output = self._raw.wine_binary

        with maintenance.configured(self.app) as context, maintenance.use(context):
            return self._absolute_from_install_dir(output)

    @wine_binary.setter
    def wine_binary(self, value: str):
        """Resolve recommendations before entering the concrete selection write lease."""
        if value in (constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL):
            from . import installer

            installer.select_recommended_wine(self.app)
            return

        self._set_wine_binary(value)

    @maintenance.uses_owner
    def _set_wine_binary(self, value: str):
        relative = self._relative_from_install_dir(value)

        if self._raw.wine_binary != relative:
            previous_selection = getattr(self._overrides, "runtime_selection", {})
            if hasattr(self._overrides, "runtime_selection"):
                self._overrides.runtime_selection = {}
            try:
                self._change(
                    wine_binary=relative, wine_binary_code=None,
                    legacy_changes={"SELECTED_APPIMAGE_FILENAME": None},
                )
            except BaseException:
                if hasattr(self._overrides, "runtime_selection"):
                    self._overrides.runtime_selection = previous_selection
                raise

    @property
    def wine_binary_files(self) -> list[str]:
        if self._wine_binary_files is None:
            self._wine_binary_files = utils.find_wine_binary_files(
                self.app,
                self._raw.faithlife_product_release
            )
        return self._wine_binary_files

    @property
    def wine_app_image_files(self) -> list[str]:
        if self._wine_appimage_files is None:
            self._wine_appimage_files = utils.find_appimage_files(self.app)
        return self._wine_appimage_files

    @property
    @maintenance.uses_owner
    def wine_binary_code(self) -> str:
        """Wine binary code.
        
        One of: Recommended, AppImage, System, Proton, PlayOnLinux, Custom"""
        if self._raw.wine_binary_code is None:
            self._change(wine_binary_code=utils.get_winebin_code_and_desc(self.app, self.wine_binary)[0])
        return self._raw.wine_binary_code

    @property
    def wine64_binary(self) -> str:
        selected = self.wine_binary
        wine_path = Path(selected)
        if not wine_path.exists() or wine_path.name.lower().endswith(".appimage"):
            return str(wine_path)

        identity = (self.install_dir, selected, str(Path(selected).resolve()))
        if self._wine64_path and getattr(self, "_wine64_identity", None) == identity:
            return self._wine64_path
        self._wine64_path = None
        self._wine64_identity = identity
        self._wine64_path = wine.native_launcher(selected)
        return self._wine64_path
    
    @property
    # This used to be called WINESERVER_EXE
    def wineserver_binary(self) -> str:
        override = getattr(self._overrides, "wineserver_binary", None)
        if override is not None:
            return self._absolute_from_install_dir(override)
        return str(Path(self.wine64_binary).parent / 'wineserver')

    # FIXME: seems like the logic around wine appimages can be simplified
    # Should this be folded into wine_binary?
    @property
    def wine_appimage_path(self) -> Optional[Path]:
        """Path to the wine appimage
        
        Returns:
            Path if wine is set to use an appimage, otherwise returns None"""
        if self._overrides.wine_appimage_path is not None:
            return Path(self._absolute_from_install_dir(self._overrides.wine_appimage_path)) 
        if self.wine_binary.lower().endswith(".appimage"):
            return Path(self._absolute_from_install_dir(self.wine_binary))
        return None
    
    @wine_appimage_path.setter
    @maintenance.uses_owner
    def wine_appimage_path(self, value: Optional[str | Path]) -> None:
        if isinstance(value, Path):
            value = str(value)
        if self._overrides.wine_appimage_path != value:
            self._overrides.wine_appimage_path = value
            if hasattr(self._overrides, "cli_overrides"):
                self._overrides.cli_overrides["wine_appimage_path"] = value
            # Reset dependents
            self._raw.wine_binary_code = None
            # NOTE: we don't save this persistently, it's assumed
            # it'll be saved under wine_binary if it's used

    @property
    def wine_appimage_link_file_name(self) -> str:
        if self._overrides.wine_appimage_link_file_name is not None:
            return self._overrides.wine_appimage_link_file_name
        return 'selected_wine.AppImage'

    @property
    def wine_appimage_recommended_url(self) -> str:
        """Resolve an existing provider's exact current released development target."""
        return self.current_wine_appimage().download_url

    def current_wine_appimage(self):
        lookup = self._network.wine_appimage_release()
        if lookup.status != "ok" or lookup.stale or lookup.release is None or lookup.release.error:
            raise maintenance.MaintenanceError(lookup.message or "Current Wine AppImage is unavailable")
        return lookup.release
    
    @property
    def wine_appimage_beta_url(self) -> Optional[str]:
        """Legacy alias for the published development target, without an older fallback."""
        return self.wine_appimage_recommended_url
    
    @property
    def wine_appimage_beta_file_name(self) -> Optional[str]:
        return self.wine_appimage_recommended_file_name

    @property
    def wine_appimage_recommended_file_name(self) -> str:
        """Returns the file name of the recommended appimage with extension"""
        return self.current_wine_appimage().name

    @property
    def wine_appimage_recommended_version(self) -> str:
        return self.current_wine_appimage().version

    @property
    def wine_dll_overrides(self) -> str:
        """Used to set WINEDLLOVERRIDES"""
        if self._overrides.wine_dll_overrides is not None:
            return self._overrides.wine_dll_overrides
        # Default is no overrides
        return ''

    @property
    def wine_debug(self) -> str:
        """Used to set WINEDEBUG"""
        if self._overrides.wine_debug is not None:
            return self._overrides.wine_debug
        return constants.DEFAULT_WINEDEBUG

    @property
    def wine_output_encoding(self) -> Optional[str]:
        """Attempt to guess the encoding of the wine output"""
        if self._overrides.wine_output_encoding is not None:
            return self._overrides.wine_output_encoding
        if self._wine_output_encoding is None:
            self._wine_output_encoding = wine.get_winecmd_encoding(self.app)
        return self._wine_output_encoding

    @property
    def app_wine_log_path(self) -> str:
        if self._overrides.app_wine_log_path is not None:
            return self._overrides.app_wine_log_path
        return constants.DEFAULT_APP_WINE_LOG_PATH

    @property
    def app_wine_log_previous_path(self) -> str:
        wine_log = Path(self.app_wine_log_path)
        return str(wine_log.with_suffix(".1" + wine_log.suffix))

    @property
    def app_log_path(self) -> str:
        if self._overrides.app_log_path is not None:
            return self._overrides.app_log_path
        return constants.DEFAULT_APP_LOG_PATH

    def toggle_faithlife_product_release_channel(self):
        if self._raw.faithlife_product_release_channel == "stable":
            new_channel = "beta"
        else:
            new_channel = "stable"
        self._change(faithlife_product_release_channel=new_channel)
    
    def toggle_installer_release_channel(self):
        new_channel = "beta" if self._raw.app_release_channel == "stable" else "stable"
        self._change(app_release_channel=new_channel)
    
    @property
    def backup_dir(self) -> Path:
        question = "New or existing folder to store backups in: "
        options = [PROMPT_OPTION_DIRECTORY]
        value = self._ask_if_not_found("backup_dir", question, options)
        return Path(self._absolute_from_install_dir(value))
    
    @property
    def curses_color_scheme(self) -> str:
        """Color for the curses dialog
        
        returns one of: System, Logos, Light or Dark"""
        return self._raw.curses_color_scheme or 'Logos'

    @curses_color_scheme.setter
    def curses_color_scheme(self, value: Optional[str]):
        if value is not None and value not in self._curses_color_scheme_valid_values:
            raise ValueError(
                "Invalid curses theme, expected one of: "
                f"{", ".join(self._curses_color_scheme_valid_values)} but got: {value}"
            )
        self._change(curses_color_scheme=value)
    
    def cycle_curses_color_scheme(self):
        new_index = self._curses_color_scheme_valid_values.index(self.curses_color_scheme) + 1
        if new_index == len(self._curses_color_scheme_valid_values):
            new_index = 0
        self.curses_color_scheme = self._curses_color_scheme_valid_values[new_index]

    @property
    def logos_exe(self) -> Optional[str]:
        if (
            # Ensure we have all the context we need before attempting
            self._raw.faithlife_product is not None
            and self._raw.install_dir is not None
            and self._logos_appdata_dir is not None
        ):
            return f"{self._logos_appdata_dir}/{self._raw.faithlife_product}.exe"
        return None

    @property
    def wine_user(self) -> Optional[str]:
        # We don't want to prompt the user for install_dir if it isn't set
        # or anything that is used in the default install dir
        if (
            not self._raw.install_dir
            or not self._raw.faithlife_product 
        ):
            return None
        # Cache a successful result (as it goes out to the fs)
        identity = self.wine_prefix, self._raw.faithlife_product
        if getattr(self, "_wine_user_identity", None) != identity:
            self._wine_user = None
            self._wine_user_identity = identity
        if not self._wine_user:
            self._wine_user = get_wine_user(self.wine_prefix)
        return self._wine_user

    @property
    def logos_appdata_windows_path(self) -> Optional[str]:
        """Path to the Logos appdata dir within windows. 
        
        Structured like: C:\\Users\\user\\AppData\\Local...
        """
        if self.wine_user is None:
            return None
        # We don't want to prompt here.
        if self._raw.faithlife_product is None:
            return None
        return f'C:\\users\\{self.wine_user}\\AppData\\Local\\{self._raw.faithlife_product}'

    @property
    def logos_exe_windows_path(self) -> Optional[str]:
        if self.logos_appdata_windows_path is None or self._raw.faithlife_product is None:
            return None
        return f'{self.logos_appdata_windows_path}\\{self._raw.faithlife_product}.exe'

    @property
    def logos_cef_exe_windows_path(self) -> Optional[str]:
        if self.logos_appdata_windows_path is None:
            return None
        # This name is the same even in Verbum
        return f'{self.logos_appdata_windows_path}\\System\\LogosCEF.exe'

    @property
    def logos_indexer_exe_windows_path(self) -> Optional[str]:
        if self.logos_appdata_windows_path is None or self._raw.faithlife_product is None:
            return None
        return f'{self.logos_appdata_windows_path}\\System\\{self._raw.faithlife_product}Indexer.exe'

    @property
    def logos_system_exe_windows_path(self) -> Optional[str]:
        if self.logos_appdata_windows_path is None or self._raw.faithlife_product is None:
            return None
        return f'{self.logos_appdata_windows_path}\\System\\{self._raw.faithlife_product}.exe'

    @property
    def log_level(self) -> str | int:
        if self._overrides.log_level is not None:
            return self._overrides.log_level
        return constants.DEFAULT_LOG_LEVEL

    @property
    def skip_install_system_dependencies(self) -> bool:
        return bool(self._overrides.install_dependencies_skip)

    @skip_install_system_dependencies.setter
    def skip_install_system_dependencies(self, val: bool):
        self._overrides.install_dependencies_skip = val
        if hasattr(self._overrides, "cli_overrides"):
            self._overrides.cli_overrides["install_dependencies_skip"] = val

    @property
    def download_dir(self) -> str:
        if self._download_dir is None:
            self._download_dir = constants.CACHE_DIR
        paths.ensure_cache_directory(Path(self._download_dir), constants.CACHE_BASE)
        return self._download_dir
    
    @property
    def user_download_dir(self) -> str:
        if self._user_download_dir is None:
            self._user_download_dir = utils.get_user_downloads_dir()
        return self._user_download_dir

    @property
    def installed_faithlife_product_release(self) -> Optional[str]:
        if self._raw.install_dir is None:
            return None
        appdata = self._logos_appdata_dir
        metadata = Path(appdata) / "System/Logos.deps.json" if appdata else None
        try:
            info = metadata.stat() if metadata else None
        except FileNotFoundError:
            info = None
        identity = (
            str(metadata),
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if info else None,
        )
        if (getattr(self, "_installed_release_identity", None) != identity
                or (info is not None and self._installed_faithlife_product_release is None)):
            # Pending promotion replaces System metadata after the MSI has settled.
            self._installed_faithlife_product_release = utils.get_current_logos_version(appdata)
            self._installed_release_identity = identity
        return self._installed_faithlife_product_release

    def refresh_installed_faithlife_product_release(self) -> Optional[str]:
        """Reread installed metadata when a mutation decision cannot use cached evidence."""
        self._installed_release_identity = None
        self._installed_faithlife_product_release = None
        return self.installed_faithlife_product_release

    @property
    def app_latest_version_url(self) -> Optional[str]:
        release = self._network.app_latest_version(self.app_release_channel)
        return release.download_url if release is not None else None

    @property
    def app_latest_version(self) -> Optional[str]:
        release = self._network.app_latest_version(self.app_release_channel)
        return release.version if release is not None else None

    def controller_release(self, *, force=False, refresh=False):
        return self._network.controller_release(self.app_release_channel, force=force, refresh=refresh)

    @property
    def icu_latest_version(self) -> str:
        return self._network.icu_latest_version().version

    @property
    def icu_latest_version_url(self) -> str:
        return self._network.icu_latest_version().download_url
