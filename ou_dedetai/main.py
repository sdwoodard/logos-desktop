#!/usr/bin/env python3
import argparse
import curses
import logging.handlers
from typing import Callable, Tuple

from ou_dedetai.app import UserExitedFromAsk
from ou_dedetai.config import (
    EphemeralConfiguration, LegacyConfiguration, PersistentConfiguration, get_wine_prefix_path
)

import logging
import os
import sys

from . import cli
from . import artifacts
from . import constants
from . import msg
from . import maintenance
from . import system
from . import tui_app
from . import utils
from . import updates
from . import config_store


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
    cfg.add_argument('--dialog', choices=('tk', 'tui'), help='choose the controller interface')
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
        help='Update the to the latest AppImage.',
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
    if args.config is not None:
        ephemeral_config = EphemeralConfiguration.load_from_path(args.config)
    else:
        ephemeral_config = EphemeralConfiguration.load()
    if args.check_for_updates:
        ephemeral_config.check_updates_now = True
    if args.dialog:
        ephemeral_config.dialog = args.dialog

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
                    raise argparse.ArgumentTypeError(e)
                if not utils.check_appimage(ephemeral_config.wine_appimage_path):
                    e = f"{ephemeral_config.wine_appimage_path} is not an AppImage."
                    raise argparse.ArgumentTypeError(e)
            # Re-use this variable for either wine or winetricks execution
            elif arg == 'wine' or arg == 'winetricks':
                ephemeral_config.wine_args = getattr(args, arg)
            run_action = cli_operation(arg)
            break
    if getattr(args, "install_app"):
        run_action = install_app
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
    if dialog == 'tk':
        from . import gui_app

        gui_app.start_gui_app(ephemeral_config)
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

def install_app(ephemeral_config: EphemeralConfiguration):
    dialog = ephemeral_config.dialog or system.get_dialog()
    logging.info(f"Using DIALOG: {dialog}")
    if dialog == 'tk':
        from . import gui_app

        gui_app.start_gui_app(ephemeral_config, install_only=True)
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


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--fetch-metadata":
        from . import metadata

        metadata.worker(sys.argv[2])
        return
    args = get_parser().parse_args()
    if args.update_self:
        try:
            run_controller_update(args)
        except (artifacts.ArtifactError, OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(1) from error
        return
    arguments = updates.resume_arguments(args.config or LegacyConfiguration.config_file_path(), args.dialog)
    updates.recover_startup(arguments)
    try:
        ephemeral_config, action = setup_config(args)
    except config_store.ConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
    diagnostic = action.__name__ in ("installation_status", "get_support", "stop_installed_app", "edit_config")
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

    # Run safety checks.
    # FIXME: Fix utils.die_if_running() for GUI; as it is, it breaks GUI
    # self-update when updating LLI as it asks for a confirmation in the CLI.
    # Disabled until it can be fixed. Avoid running multiple instances of the
    # program.
    # utils.die_if_running()
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
        # This isn't a critical failure, the user doesn't need a traceback,
        # they are the ones who told us to exit.
        pass


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
