#!/usr/bin/env python3
import argparse
import curses
import logging.handlers
from typing import Callable, Tuple

from logos_desktop.app import UserExitedFromAsk
from logos_desktop.config import (
    EphemeralConfiguration, LegacyConfiguration, PersistentConfiguration, get_wine_prefix_path
)

import logging
import os
import sys
import threading

from . import cli
from . import artifacts
from . import constants
from . import releases
from . import msg
from . import maintenance
from . import system
from . import tui_app
from . import utils
from . import updates
from . import config_store
from . import instance


def get_parser():
    desc = "Installs FaithLife Bible Software with Wine."
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument(
        '-v', '--version', action='version',
        version=(
            f"{constants.APP_NAME}, "
            f"{constants.LLI_CURRENT_VERSION} by {constants.LLI_AUTHOR}"
        ),
    )

    # Define options that affect runtime config.
    cfg = parser.add_argument_group(title="runtime config options")
    cfg.add_argument(
        '-a', '--check-for-updates', action='store_true',
        help='force a check for updates'
    )
    cfg.add_argument('--dialog', choices=('qt', 'tk', 'tui'), help='choose the controller interface')
    cfg.add_argument('--previous-controller', metavar='PATH',
                     help='select an older controller executable for same-prefix admission checks')
    cfg.add_argument(
        '-K', '--skip-dependencies', action='store_true',
        help='skip dependencies check and installation',
    )
    cfg.add_argument(
        '-V', '--verbose', action='store_true',
        help='enable verbose mode',
    )
    cfg.add_argument(
        '-D', '--debug', action='store_true',
        help='enable Wine debug output',
    )
    cfg.add_argument(
        '-c', '--config', metavar='CONFIG_FILE',
        help=(
            "use a custom config file during installation "
            f"[default: {constants.DEFAULT_CONFIG_PATH}]"
        ),
    )
    cfg.add_argument(
        '-f', '--force-root', action='store_true',
        help=(
            "Running Wine as root is highly discouraged. "
            "Set this to do allow it anyways"
        ),
    )
    cfg.add_argument(
        '-p', '--custom-binary-path', metavar='CUSTOMBINPATH',
        help='specify a custom wine binary path',
    )
    cfg.add_argument(
        '-L', '--delete-log', action='store_true',
        help='delete the log file',
    )
    # FIXME: remove this deprecated option.
    # If we remove this today, scripts using -P will fail, as now it's an "unknown option"
    cfg.add_argument(
        '-P', '--passive', action='store_true',
        help='Legacy argument that used to specify to run the product installer non-interactively. '
        'Now this is the default. '
        'Consider agreeing to the terms as well --i-agree-to-faithlife-terms',
    )
    cfg.add_argument(
        '-y', '--assume-yes', action='store_true',
        help='Assumes yes (or default) to all prompts. '
        'Useful for entirely non-interactive installs. '
        'Consider agreeing to the terms as well --i-agree-to-faithlife-terms',
    )
    cfg.add_argument(
        '--i-agree-to-faithlife-terms', action='store_true',
        help='By passing this flag you agree to https://faithlife.com/terms',
    )
    cfg.add_argument(
        '-q', '--quiet', action='store_true',
        help='Suppress all non-error output',
    )

    # Define runtime actions (mutually exclusive).
    grp = parser.add_argument_group(
        title="subcommands",
        description=(
            "these options run specific subcommands; "
            "only 1 at a time is accepted"
        ),
    )
    cmd = grp.add_mutually_exclusive_group()
    cmd.add_argument('--doctor', action='store_true', help='inspect stored configuration without repairs')
    cmd.add_argument('--update-status', action='store_true', help='inspect stable product update availability')
    cmd.add_argument('--check-controller-updates', action='store_true',
                     help='check this controller installation provider without installing')
    cmd.add_argument('--list-backups', action='store_true', help='list completed snapshot metadata')
    cmd.add_argument('--restore-snapshot', metavar='PATH', help='verify and confirm an exact snapshot restore')
    cmd.add_argument('--support-preview', action='store_true', help='print the exact sanitized support snapshot')
    cmd.add_argument('--support-export', metavar='PATH', help='preview and explicitly export a sanitized zip archive')
    cfg.add_argument('--confirm-export', action='store_true', help='explicitly confirm support archive export')
    cfg.add_argument('--overwrite-export', action='store_true', help='allow replacing an existing support archive')
    cmd.add_argument('--repair', dest='repair_app', action='store_true', help='repair the selected installation')
    cmd.add_argument(
        '--status', dest='installation_status', action='store_true',
        help='inspect the selected installation without changing it',
    )
    cmd.add_argument(
        '--install-app', action='store_true',
        help='install FaithLife app',
    )
    cmd.add_argument(
        '--update-installed-app', action='store_true',
        help='update the installed FaithLife app to the latest release',
    )
    cmd.add_argument(
        '--run-installed-app', '-C', action='store_true',
        help='run installed FaithLife app',
    )
    cmd.add_argument(
        '--stop-installed-app', action='store_true',
        help='stop the installed FaithLife app if running',
    )
    cmd.add_argument(
        '--run-indexing', action='store_true',
        help='perform indexing',
    )
    cmd.add_argument('--stop-indexing', action='store_true', help='stop indexing for the selected installation')
    cmd.add_argument(
        '--remove-library-catalog', action='store_true',
        # help='remove library catalog database file'
        help=argparse.SUPPRESS,
    )
    cmd.add_argument(
        '--remove-index-files', action='store_true',
        help=argparse.SUPPRESS,
    )
    cmd.add_argument(
        '--edit-config', action='store_true',
        help='edit configuration file',
    )
    cmd.add_argument(
        '--install-dependencies', '-I', action='store_true',
        help="install your distro's dependencies",
    )
    cmd.add_argument(
        '--backup', action='store_true',
        help='backup current data',
    )
    cmd.add_argument(
        '--restore', action='store_true',
        help='restore data from backup',
    )
    cmd.add_argument(
        '--update-self', '-u', action='store_true',
        help=f'Update {constants.APP_NAME} to the latest release.',
    )
    cmd.add_argument(
        '--update-latest-appimage', '-U', action='store_true',
        help=f'Update the managed Wine AppImage. {releases.WINE_RECOMMENDATION_POLICY}',
    )
    cmd.add_argument(
        '--set-appimage', nargs=1, metavar=('APPIMAGE_FILE_PATH'),
        help='Update the AppImage symlink. Requires a path.',
    )
    cmd.add_argument(
        '--install-icu', action='store_true',
        help='Install ICU data files for Logos 30+',
    )
    cmd.add_argument(
        '--toggle-app-logging', action='store_true',
        help='enable/disable app logs',
    )
    cmd.add_argument(
        '--create-shortcuts', action='store_true',
        help='[re-]create app shortcuts',
    )
    cmd.add_argument(
        '--uninstall', action='store_true',
        help='Completely delete the faithlife software from your system',
    )
    cmd.add_argument(
        '--dirlink', action='store_true',
        # help='create directory link',
        help=argparse.SUPPRESS,
    )
    cmd.add_argument(
        '--check-resources', action='store_true',
        # help='check resources'
        help=argparse.SUPPRESS,
    )
    cmd.add_argument(
        '--get-support', action='store_true',
        help='Generates a support bundle and prints out where to go for support',
    )
    cmd.add_argument(
        '--wine', nargs="+",
        help=(
            'run wine command'
            '; WARNING: wine will not accept user input!'
        ),
    )
    cmd.add_argument(
        '--winetricks', nargs='*',
        help="run winetricks command",
    )
    return parser


def parse_args(args, parser) -> Tuple[EphemeralConfiguration, Callable[[EphemeralConfiguration], None]]: 
    if args.dirlink or args.check_resources:
        parser.error("--dirlink and --check-resources have no implemented action. "
                     "Use --status for installation details")
    if args.config is not None:
        ephemeral_config = EphemeralConfiguration.load_from_path(args.config)
    else:
        ephemeral_config = EphemeralConfiguration.load_from_path(LegacyConfiguration.config_file_path())
    if args.check_for_updates:
        ephemeral_config.check_updates_now = True
    if args.dialog:
        ephemeral_config.dialog = args.dialog
    if ephemeral_config.dialog == "tk":
        ephemeral_config.dialog = "qt"

    if args.quiet:
        ephemeral_config.log_level = logging.WARNING
        ephemeral_config.quiet = True

    if args.verbose:
        ephemeral_config.log_level = logging.INFO

    if args.debug:
        ephemeral_config.log_level = logging.DEBUG
        if not ephemeral_config.wine_debug:
            ephemeral_config.wine_debug = constants.DEFAULT_WINEDEBUG
        # Developers may want to consider adding +relay for excessive debug output
        ephemeral_config.wine_debug+=',+loaddll,+pid,+threadname'

    if args.delete_log:
        ephemeral_config.delete_log = True

    if args.set_appimage:
        ephemeral_config.wine_appimage_path = str(config_store.absolute_path(args.set_appimage[0]))

    if args.skip_dependencies or constants.RUNMODE == 'snap':
        ephemeral_config.install_dependencies_skip = True

    if args.force_root:
        ephemeral_config.app_run_as_root_permitted = True

    if args.custom_binary_path:
        if os.path.isdir(args.custom_binary_path):
            ephemeral_config.custom_binary_path = str(config_store.absolute_path(args.custom_binary_path))
        else:
            message = f"Custom binary path does not exist: \"{args.custom_binary_path}\"\n"
            parser.exit(status=1, message=message)

    if args.assume_yes and not args.i_agree_to_faithlife_terms:
        message = ("Non-interactive installations MUST also agree to the EULA https://faithlife.com/terms "
                   "via the flag --i-agree-to-faithlife-terms\n")
        parser.exit(status=1, message=message)

    if args.assume_yes:
        ephemeral_config.assume_yes = True

    if args.i_agree_to_faithlife_terms:
        ephemeral_config.agreed_to_faithlife_terms = True


    def cli_operation(action: str) -> Callable[[EphemeralConfiguration], None]:
        """Wrapper for a function pointer to a given function under CLI
        
        Lazily instantiates CLI at call-time"""
        def _run(config: EphemeralConfiguration):
            app = cli.CLI(config)
            try:
                getattr(app, action)()
            finally:
                if not app.close():
                    raise RuntimeError("An application operation is still finishing")
        output = _run
        output.__name__ = action
        return output

    # Set action return function.
    actions = [
        'repair_app',
        'installation_status',
        'backup',
        'create_shortcuts',
        'edit_config',
        'install_dependencies',
        'install_icu',
        'remove_index_files',
        'uninstall',
        'remove_library_catalog',
        'restore',
        'run_indexing',
        'stop_indexing',
        'run_installed_app',
        'update_installed_app',
        'stop_installed_app',
        'set_appimage',
        'toggle_app_logging',
        'update_self',
        'update_latest_appimage',
        'wine',
        'winetricks',
        "get_support"
    ]

    run_action = None
    for arg in actions:
        if getattr(args, arg) or getattr(args, arg) == []:
            if arg == "set_appimage":
                if not utils.file_exists(ephemeral_config.wine_appimage_path):
                    e = f"Invalid file path: '{ephemeral_config.wine_appimage_path}'. File does not exist."
                    parser.error(e)
                if not utils.check_appimage(ephemeral_config.wine_appimage_path):
                    e = f"{ephemeral_config.wine_appimage_path} is not an AppImage."
                    parser.error(e)
            # Re-use this variable for either wine or winetricks execution
            elif arg == 'wine' or arg == 'winetricks':
                ephemeral_config.wine_args = getattr(args, arg)
            run_action = cli_operation(arg)
            break
    if getattr(args, "install_app"):
        run_action = install_app
    if args.repair_app and ephemeral_config.dialog == "qt":
        run_action = repair_app
    if run_action is None:
        run_action = run_control_panel
    cli_fields = {
        "check_for_updates": "check_updates_now", "dialog": "dialog", "quiet": "quiet",
        "verbose": "log_level", "debug": "log_level", "delete_log": "delete_log",
        "set_appimage": "wine_appimage_path", "skip_dependencies": "install_dependencies_skip",
        "custom_binary_path": "custom_binary_path",
    }
    ephemeral_config.cli_overrides = {
        field: getattr(ephemeral_config, field) for argument, field in cli_fields.items() if getattr(args, argument)
    }
    if args.debug:
        ephemeral_config.cli_overrides["wine_debug"] = ephemeral_config.wine_debug
    return ephemeral_config, run_action


def run_control_panel(ephemeral_config: EphemeralConfiguration):
    dialog = ephemeral_config.dialog or system.get_dialog()
    logging.info(f"Using DIALOG: {dialog}")
    if dialog in ('qt', 'tk'):
        start_graphical(ephemeral_config)
    else:
        try:
            tui_app.control_panel_app(ephemeral_config)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            logging.info("Caught SystemExit, exiting gracefully…")
            raise
        except curses.error as e:
            logging.error(f"Curses error in run_control_panel(): {e}")
            raise e
        except Exception as e:
            logging.error(f"An error occurred in run_control_panel(): {e}")
            raise e

def repair_app(ephemeral_config: EphemeralConfiguration):
    from .repair import repair_installation

    start_graphical(ephemeral_config, function=repair_installation)


def start_graphical(ephemeral_config, *, function=None, install_only=False):
    try:
        from .qt.startup import start
    except ModuleNotFoundError as error:
        if not error.name or not error.name.startswith(("PySide6", "shiboken6")):
            raise
        raise maintenance.MaintenanceError(
            "The Qt interface requires the gui extra. Install logos-desktop[gui] or use --dialog tui") from error
    return start(ephemeral_config, function, install_only=install_only)


def install_app(ephemeral_config: EphemeralConfiguration):
    dialog = ephemeral_config.dialog or system.get_dialog()
    logging.info(f"Using DIALOG: {dialog}")
    if dialog in ('qt', 'tk'):
        start_graphical(ephemeral_config, install_only=True)
    else:
        app = cli.CLI(ephemeral_config)
        try:
            app.install_app()
        finally:
            if not app.close():
                raise RuntimeError("An installation operation is still finishing")


def setup_config(cli_args=None) -> Tuple[EphemeralConfiguration, Callable[[EphemeralConfiguration], None]]:
    parser = get_parser()
    if cli_args is None:
        cli_args = parser.parse_args()

    return parse_args(cli_args, parser)


def is_app_installed(ephemeral_config: EphemeralConfiguration):
    persistent_config = PersistentConfiguration.load_from_path(ephemeral_config.config_path) 
    if persistent_config.faithlife_product is None or persistent_config.install_dir is None: 
        # Not enough information stored to find the product
        return False
    wine_prefix = ephemeral_config.wine_prefix or get_wine_prefix_path(str(persistent_config.install_dir)) 
    return utils.find_installed_product(persistent_config.faithlife_product, wine_prefix) 


def run(ephemeral_config: EphemeralConfiguration, action: Callable[[EphemeralConfiguration], None]): 
    if action == "disabled":
        print("That option is disabled.", file=sys.stderr)
        sys.exit(1)
    if action.__name__ == 'run_control_panel':
        action(ephemeral_config)
        return
    
    # Proceeding with the CLI interface

    install_required = [
        'backup',
        'create_shortcuts',
        'install_icu',
        'remove_index_files',
        'remove_library_catalog',
        'restore',
        'run_indexing',
        'run_installed_app',
        'update_installed_app',
        'set_appimage',
        'toggle_app_logging',
    ]
    if action.__name__ not in install_required:
        logging.info(f"Running function: {action.__name__}")
        action(ephemeral_config)
    elif is_app_installed(ephemeral_config):
        logging.info(f"Running function: {action.__name__}")
        action(ephemeral_config)
    else:
        print("App is not ready for this operation. Inspect --status or request --repair.", file=sys.stderr)
        sys.exit(1)


def run_controller_update(args):
    """Dispatch an admitted update without constructing the Wine application."""
    from types import SimpleNamespace
    from . import network

    from . import controller_install

    identity = controller_install.identify()
    if identity.owner in ("venv", "aur"):
        config_path = args.config or LegacyConfiguration.config_file_path()
        overrides = EphemeralConfiguration.load_from_path(config_path)
        frontend = args.dialog or overrides.dialog or "qt"
        result = controller_install.handoff(identity, config_path, "qt" if frontend == "tk" else frontend)
        print(f"{result.status}: {result.message}")
        if result.command:
            import shlex

            print(shlex.join(result.command))
        if result.status not in ("handed-off", "current", "manual", "restart-required"):
            raise updates.UpdateError(result.message)
        return result

    updates.eligible()
    if constants.APP_RELEASE_VERSION is None:
        raise updates.UpdateError("This private build has no LogosDesktop release version")
    config_path = args.config or LegacyConfiguration.config_file_path()
    raw = PersistentConfiguration.load_from_path(config_path)
    overrides = EphemeralConfiguration.load_from_path(config_path)
    conf = SimpleNamespace(
        _raw=raw, _overrides=overrides, app_release_channel=raw.app_release_channel,
        download_dir=constants.CACHE_DIR, _network=network.NetworkRequests(args.check_for_updates),
    )
    app = SimpleNamespace(conf=conf, status=lambda message, percent=None: print(message))
    arguments = updates.resume_arguments(config_path, args.dialog or overrides.dialog)
    prepared = updates.prepare(app)
    prepared.restart(arguments)


def is_url_request(arguments):
    from urllib.parse import urlsplit

    return bool(arguments and len(arguments) == 2 and len(arguments[1]) <= 16384
                and urlsplit(arguments[1]).scheme.lower() in ('logos4', 'libronixdls')
                and not any(ord(char) < 32 for char in arguments[1]))


def run_url_request(ephemeral_config):
    """Deliver only a selected product URL without constructing a controller."""
    from types import SimpleNamespace
    import subprocess
    from . import runtime, wine
    from .config import Config

    if os.getuid() == 0 and not ephemeral_config.app_run_as_root_permitted:
        raise maintenance.MaintenanceError("Running Wine as root requires --force-root")
    system.check_architecture()

    def no_prompt(*args, **kwargs):
        raise maintenance.MaintenanceError("The URL handler requires a complete installed profile")

    app = SimpleNamespace(ask=no_prompt, approve=no_prompt, exit=no_prompt,
                          _config_updated_event=threading.Event())
    app.conf = Config(ephemeral_config, app)
    arguments = ephemeral_config.wine_args
    if not is_url_request(arguments) or arguments[0] != app.conf.logos_exe_windows_path:
        raise maintenance.MaintenanceError("URL handler executable does not match the selected installation")
    with maintenance.configured(app) as context:
        instance.check_legacy(context.prefix)
        with maintenance.use(context), maintenance.artifact_use(), runtime.activation_guard(app):
            command, environment = wine._wine_invocation(app, app.conf.wine64_binary, None)
            # The URL may contain a login token. Bypass command and Wine logs.
            with subprocess.Popen([*command, *arguments], env=environment,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True) as process:
                interrupted = False
                while True:
                    try:
                        code = process.wait()
                        break
                    except KeyboardInterrupt:
                        interrupted = True
                if interrupted:
                    raise KeyboardInterrupt
                if code:
                    raise maintenance.MaintenanceError("The product URL handler failed")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--fetch-metadata":
        from . import metadata

        metadata.worker(sys.argv[2])
        return
    args = get_parser().parse_args()
    try:
        if args.previous_controller:
            instance.select_previous_controller(args.previous_controller)
        if args.check_controller_updates:
            from . import controller_install

            raise SystemExit(controller_install.main(["check"]))
        from . import commands

        status = commands.dispatch(args)
        if status is not None:
            raise SystemExit(status)
        if args.update_self:
            from . import controller_install

            if controller_install.identify().owner == "unsupported":
                updates.eligible()
        ephemeral_config, action = setup_config(args)
    except (config_store.ConfigurationError, maintenance.MaintenanceError, artifacts.ArtifactError, OSError, ValueError,
            KeyError, IndexError, TypeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    diagnostic = action.__name__ == 'installation_status'
    session = None
    try:
        if is_url_request(args.wine):
            run_url_request(ephemeral_config)
            return
        if not diagnostic:
            session = instance.Session.for_config(ephemeral_config)
            try:
                session.acquire()
            except instance.InstanceBusy:
                defaults = get_parser().parse_args([])
                explicit = any(value != getattr(defaults, name, None) for name, value in vars(args).items()
                               if name not in ('config', 'dialog', 'force_root', 'quiet'))
                dialog = ephemeral_config.dialog or system.get_dialog()
                if action is run_control_panel and dialog in ('qt', 'tk') and not explicit and session.focus(
                        os.environ.get('XDG_ACTIVATION_TOKEN', '')):
                    return
                raise
            ephemeral_config._instance_session = session
            if args.update_self:
                session.close()
                run_controller_update(args)
                return
            arguments = updates.resume_arguments(args.config or LegacyConfiguration.config_file_path(), args.dialog)
            updates.recover_startup(arguments)
        preserve_logs = action.__name__ in (
            'installation_status', 'get_support', 'stop_installed_app', 'stop_indexing', 'edit_config',
        )
        _run_owned(ephemeral_config, action, preserve_logs)
    except (maintenance.MaintenanceError, artifacts.ArtifactError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    finally:
        if session is not None:
            application = getattr(session, '_application', None)
            if application is not None:
                while not application.close(timeout=.1):
                    pass
            session.close()


def _run_owned(ephemeral_config, action, diagnostic):
    if not diagnostic:
        msg.initialize_logging(ephemeral_config.app_log_path)
    msg.update_log_level(ephemeral_config.log_level or constants.DEFAULT_LOG_LEVEL)
    system.check_architecture()

    # NOTE: DELETE_LOG is an outlier here. It's an action, but it's one that
    # can be run in conjunction with other actions, so it gets special
    # treatment here once config is set.
    app_log_path = ephemeral_config.app_log_path or constants.DEFAULT_APP_LOG_PATH
    if not diagnostic and ephemeral_config.delete_log and os.path.isfile(app_log_path):
        # Write empty file.
        with open(app_log_path, 'w') as f:
            f.write('')

    if os.getuid() == 0 and not ephemeral_config.app_run_as_root_permitted:
        print("Running Wine/winetricks as root is highly discouraged. Use -f|--force-root if you must run as root. "
              "See https://wiki.winehq.org/FAQ#Should_I_run_Wine_as_root.3F", file=sys.stderr)
        sys.exit(1)

    # Print terminal banner
    logging.info(f"{constants.APP_NAME}, {constants.LLI_CURRENT_VERSION} by {constants.LLI_AUTHOR}.")

    try:
        run(ephemeral_config, action)
    except (maintenance.MaintenanceError, artifacts.ArtifactError) as error:
        logging.error("Operation failed: %s", error)
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
    except UserExitedFromAsk:
        print("Operation cancelled", file=sys.stderr)
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == '__main__':
    main()
