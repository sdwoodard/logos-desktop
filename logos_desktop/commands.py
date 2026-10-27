"""Headless commands dispatched before controller startup and log setup."""

from pathlib import Path
from types import SimpleNamespace

from . import backup, config_store, doctor, instance, maintenance, network, support, system
from .config import EphemeralConfiguration, LegacyConfiguration, PersistentConfiguration


def configuration(path=None, *, document=None, environment=None):
    """Project stored configuration without constructing an application manager."""
    path = config_store.absolute_path(path or LegacyConfiguration.config_file_path())
    if document is None:
        document, _ = config_store.read_document(path)
    overrides = EphemeralConfiguration.project(document, str(path), environment=environment)
    raw = PersistentConfiguration.from_document(document, overrides.environment)
    return SimpleNamespace(_raw=raw, _overrides=overrides, config_file_path=str(path))


def _restore(args, conf):
    from .cli import CLI

    source = config_store.absolute_path(args.restore_snapshot)
    with instance.Session.for_config(conf._overrides) as session:
        conf._overrides._instance_session = session
        app = CLI(conf._overrides)
        try:
            def confirm(question, secondary=None):
                print(question)
                print(secondary or "")
                expected = f"restore {source}"
                answer = app._read_answer(f"Type exactly {expected!r} to confirm", "")
                if answer != expected:
                    raise maintenance.MaintenanceError("Restore was not confirmed")
                return True

            app.approve = confirm
            backup.restore(app, snapshot=source, backup_dir=source.parent)
        finally:
            while not app.close(timeout=.1):
                pass


def normal_graphical_start(args):
    """Distinguish opening the controller from an explicit CLI operation."""
    actions = (
        'repair_app', 'installation_status', 'install_app', 'update_installed_app',
        'run_installed_app', 'stop_installed_app', 'run_indexing', 'stop_indexing',
        'remove_library_catalog', 'remove_index_files', 'edit_config', 'install_dependencies',
        'backup', 'restore', 'update_self', 'update_latest_appimage', 'set_appimage',
        'install_icu', 'toggle_app_logging', 'create_shortcuts', 'uninstall', 'dirlink',
        'check_resources', 'get_support',
    )
    if any(getattr(args, name) for name in actions) or args.wine is not None or args.winetricks is not None:
        return False
    return (args.dialog or system.get_dialog()) in ('qt', 'tk')


def dispatch(args):
    """Return an exit status, or None when normal startup should continue."""
    selected = any((args.doctor, args.update_status, args.list_backups,
                    args.restore_snapshot, args.support_preview, args.support_export))
    if (args.confirm_export or args.overwrite_export) and not args.support_export:
        raise maintenance.MaintenanceError("Export confirmation options require --support-export PATH")
    if not selected:
        path = config_store.absolute_path(args.config or LegacyConfiguration.config_file_path())
        if not args.config and not path.exists():
            if normal_graphical_start(args):
                return None
            raise maintenance.MaintenanceError(
                "Configuration is missing. Set up Logos in the application or select an existing profile with --config")
        return None

    path = config_store.absolute_path(args.config or LegacyConfiguration.config_file_path())
    if not path.exists():
        raise maintenance.MaintenanceError(
            "Configuration is missing. Set up Logos or select a saved profile with --config"
        )
    conf = configuration(path)
    app = SimpleNamespace(conf=conf)
    if args.doctor:
        report = doctor.inspect(conf)
        print(report['summary'])
        _, prefix, _ = doctor.selected_paths(conf)
        instance.check_legacy(prefix)
        return int(any(check['status'] == 'error' for check in report['checks']))
    if args.support_preview or args.support_export:
        snapshot = support.prepare(app)
        print(support.preview(snapshot), end='', flush=True)
        if args.support_export:
            if not args.confirm_export:
                raise maintenance.MaintenanceError("Export requires --confirm-export after reviewing the preview")
            output = support.export(snapshot, args.support_export, overwrite=args.overwrite_export)
            print(f"Exported: {output}")
        return 0
    if args.list_backups:
        _, _, root = doctor.selected_paths(conf)
        if root is None:
            raise maintenance.MaintenanceError("No backup folder is configured")
        for item in backup.list_snapshots(root, doctor.stored(conf, 'faithlife_product')):
            print(f"Snapshot: {root / item.name}\nCreated: {item.created}\nBytes: {item.size}\n"
                  f"Roots: {', '.join(item.roots)}\nSource: {item.source or 'Unknown (not recorded)'}")
        return 0
    if args.restore_snapshot:
        _restore(args, conf)
        return 0
    if args.update_status:
        from . import utils

        product = doctor.stored(conf, 'faithlife_product')
        version = doctor.stored(conf, 'faithlife_product_version')
        if product != 'Logos' or not version:
            raise maintenance.MaintenanceError("Select a configured Logos installation first")
        _, prefix, _ = doctor.selected_paths(conf)
        executable = utils.find_installed_product(product, str(prefix)) if prefix else None
        installed = utils.get_current_logos_version(str(Path(executable).parent)) if executable else None
        lookup = network.NetworkRequests().product_release(product, version, 'stable', force=args.check_for_updates)
        state = network.product_update_state(installed, lookup)
        print(f"Update status: {state.status}\nInstalled: {installed or 'unknown'}\n"
              f"Target: {state.target or 'unknown'}\n{state.message}")
        return 0 if state.status in ('available', 'current') else 1
    return 0
