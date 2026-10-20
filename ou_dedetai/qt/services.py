"""Production operations over the shared installation and maintenance services."""

from dataclasses import replace
from functools import partial
import logging
from pathlib import Path
import shutil
import threading

from packaging.version import InvalidVersion, Version

from ou_dedetai import backup, backup_store, constants, control, installer, maintenance
from ou_dedetai import config_store, repair, runtime, updates, utils, wine
from ou_dedetai.app import UserExitedFromAsk
from ou_dedetai.config import Config, EphemeralConfiguration, PersistentConfiguration
from ou_dedetai.logos import LogosManager, State
from .bridge import Result
from .window import Installation


class Services:
    production = True

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = Installation("Logos", "Unknown", "Not selected", "", installed=False)
        self.prepared = None

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def refresh(self, app, cancel):
        conf = app.conf
        raw = conf._raw
        snapshot = Installation(
            raw.faithlife_product or "Logos", "Unknown",
            raw.wine_binary or "Not selected", conf._overrides.install_dir or raw.install_dir or "",
            installed=False, running=app.logos.logos_state == State.RUNNING,
            state=app.logos.logos_state.name.title(), indexing=app.logos.indexing_state.name.title(),
            health="Choose Install or use an existing installation profile.", profile=conf.config_file_path,
            backup=raw.backup_dir or "", product_channel=conf.faithlife_product_release_channel,
            controller_channel=conf.app_release_channel, logging=conf.faithlife_product_logging,
            selected=bool(raw.install_dir and raw.faithlife_product == "Logos"),
            runtimes=self.snapshot().runtimes,
        )
        try:
            executable = conf.logos_exe
            installed = bool(snapshot.selected and executable and Path(executable).is_file())
            health = "Choose Install or use an existing installation profile."
            if snapshot.selected:
                context = maintenance.Context.from_app(app, prompt=False)
                health = "Installation is ready" if installed else "Product executable was not found"
                if context.needs_recovery() or runtime.pending(context.install):
                    health = "Pending recovery records. Use explicit Repair before changing this installation."
                elif repair.inspect(context):
                    health = "A failed product upgrade needs explicit Repair."
            version = (conf.installed_faithlife_product_release or "Unknown") if installed else ""
            snapshot = replace(snapshot, installed=installed, version=version,
                               health=app.logos.enforcement_error or health,
                               product_update=self._product_update(conf, installed))
        except Exception as error:
            logging.exception("Cannot inspect selected installation")
            snapshot = replace(snapshot, health=f"Inspection failed for this profile: {error}")

        with self._lock:
            self._snapshot = snapshot
        return Result("succeeded", "Installation state refreshed")

    @staticmethod
    def _product_update(conf, installed):
        if not installed:
            return "Install a product first"
        lookup = conf._network.product_release("Logos", conf.faithlife_product_version, "stable", refresh=False)
        if lookup.status != "ok" or lookup.stale or not lookup.versions:
            return "Not checked or unavailable"
        try:
            current = Version(conf.installed_faithlife_product_release) >= Version(lookup.versions[0])
        except (TypeError, InvalidVersion):
            return "Not checked or unavailable"
        return "Current" if current else "Update available"

    def toggle(self, app, cancel):
        return self.operation("stop" if self.snapshot().running else "launch")(app, cancel)

    def preview(self, draft):
        return self.install(draft)

    @staticmethod
    def _validate_proposal(conf, draft, document):
        """Use the same pure projection as rebind before approving any writes."""
        old = conf._overrides
        projected = EphemeralConfiguration.project(
            document, conf.config_file_path, environment=old.environment,
            cli_overrides=old.cli_overrides, runtime_selection=old.runtime_selection,
        )
        overlays = config_store.apply_changes(old.environment, old.runtime_selection, conf.config_file_path)
        effective = PersistentConfiguration.from_document(document, overlays)
        Services._validate_product_source(projected)
        destination = Path(draft.destination).expanduser().resolve()
        install = projected.install_dir or effective.install_dir
        prefix = projected.wine_prefix or destination / "data/wine64_bottle"
        binaries = projected.installer_binary_dir or destination / constants.RELATIVE_BINARY_DIR
        if (Path(install).resolve() != destination
                or Path(prefix).resolve() != destination / "data/wine64_bottle"
                or Path(binaries).resolve() != destination / constants.RELATIVE_BINARY_DIR
                or effective.faithlife_product != draft.product
                or effective.faithlife_product_release_channel != "stable"
                or effective.app_release_channel != "stable"
                or (projected.faithlife_product_version or effective.faithlife_product_version) != "10"):
            raise maintenance.MaintenanceError("Saved or invocation overrides conflict with this installation draft")

        selected = projected.wine_appimage_path or effective.wine_binary
        if selected in (constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL):
            matches = selected == draft.runtime
        else:
            selected = Path(selected).expanduser()
            if not selected.is_absolute():
                selected = destination / selected
            matches = selected.resolve() == Path(draft.runtime).expanduser().resolve()
        if not matches:
            raise maintenance.MaintenanceError("The proposed configuration selects another Wine runtime")
        if draft.release and effective.faithlife_product_release != draft.release:
            raise maintenance.MaintenanceError("The proposed configuration selects another product release")

    @staticmethod
    def _validate_product_source(projected):
        if projected.faithlife_installer_download_url or projected.faithlife_installer_name:
            raise maintenance.MaintenanceError(
                "Custom installer sources are not supported by the graphical stable-release workflow")

    @staticmethod
    def _latest_target(app):
        lookup = app.conf._network.product_release("Logos", "10", "stable")
        app.checkpoint()
        if lookup.status != "ok" or lookup.stale or not lookup.versions:
            raise maintenance.MaintenanceError("The latest stable Logos release is unavailable. Check and retry")
        return lookup.versions[0]

    @staticmethod
    def _release_changes(conf, target):
        changes = dict(faithlife_product="Logos", faithlife_product_version="10",
                       faithlife_product_release=target, faithlife_product_release_channel="stable",
                       app_release_channel="stable")
        document = config_store.apply_changes(conf._raw._saved, changes, conf.config_file_path)
        Services._validate_release_document(conf, target, document)
        return changes

    @staticmethod
    def _validate_release_document(conf, target, document):
        old = conf._overrides
        projected = EphemeralConfiguration.project(
            document, conf.config_file_path, environment=old.environment,
            cli_overrides=old.cli_overrides, runtime_selection=old.runtime_selection,
        )
        overlays = config_store.apply_changes(old.environment, old.runtime_selection, conf.config_file_path)
        effective = PersistentConfiguration.from_document(document, overlays)
        Services._validate_product_source(projected)
        if (effective.faithlife_product != "Logos" or effective.faithlife_product_release != target
                or (projected.faithlife_product_version or effective.faithlife_product_version) != "10"
                or effective.faithlife_product_release_channel != "stable"
                or effective.app_release_channel != "stable"):
            raise maintenance.MaintenanceError("An override conflicts with the latest stable Logos release")

    def install(self, draft):
        def run(app, cancel):
            destination = Path(draft.destination).expanduser()
            if not draft.destination.strip() or not destination.is_absolute():
                raise ValueError("Choose an absolute installation location")
            if draft.product != "Logos":
                raise ValueError("This interface installs Logos")
            if not draft.runtime.strip():
                raise ValueError("Choose a Wine runtime")

            overrides = app.conf._overrides
            expected_prefix = destination / "data/wine64_bottle"
            expected_bin = destination / constants.RELATIVE_BINARY_DIR
            for actual, expected in ((overrides.install_dir, destination), (overrides.wine_prefix, expected_prefix),
                                     (overrides.installer_binary_dir, expected_bin)):
                if actual and Path(actual).resolve() != expected.resolve():
                    raise maintenance.MaintenanceError(
                        "The selected profile or invocation overrides target another installation. "
                        "Start with a separate empty configuration file for a new installation")

            changes = dict(
                faithlife_product=draft.product, faithlife_product_version="10",
                install_dir=str(destination), faithlife_product_release=draft.release or None,
                wine_binary=draft.runtime, wine_binary_code=None,
                faithlife_product_release_channel="stable", app_release_channel="stable",
            )
            document = config_store.apply_changes(app.conf._raw._saved, changes, app.conf.config_file_path)
            self._validate_proposal(app.conf, draft, document)

            if app.is_installed():
                raise maintenance.MaintenanceError("An installation is already selected. Use Updates or Repair")

            if not app.approve("Install this product?", f"{draft.product}\nConfiguration: {app.conf.config_file_path}\n"
                               f"Installation: {destination}\nPrefix: {expected_prefix}\nWine: {draft.runtime}\n"
                               f"Wineserver override: {overrides.wineserver_binary or 'Selected Wine companion'}"):
                return Result("cancelled", "Installation was not started")
            app.checkpoint()

            target_draft = replace(draft, release=self._latest_target(app))
            changes["faithlife_product_release"] = target_draft.release
            document = config_store.apply_changes(app.conf._raw._saved, changes, app.conf.config_file_path)
            self._validate_proposal(app.conf, target_draft, document)

            validate_release = partial(self._validate_release_document, app.conf, target_draft.release)
            validate_install = partial(self._validate_proposal, app.conf, target_draft)
            with app.conf.validated_writes(validate_install):
                with maintenance.change_selection(app.conf, str(destination), document=document):
                    app.conf._change(**changes)
            with app.conf.validated_writes(validate_release):
                app.conf.skip_install_system_dependencies = not draft.install_dependencies
                admitted = maintenance.Context.from_app(app, prompt=False)
                if (admitted.install != destination.resolve() or admitted.prefix != expected_prefix.resolve()
                        or admitted.product != draft.product):
                    raise maintenance.MaintenanceError("Installation context changed after confirmation")
                self._validate_proposal(app.conf, target_draft, app.conf._raw._saved)

                installer.install(app)
            return Result("succeeded", "Installation completed")
        return self._settled(run)

    def select_profile(self, path):
        def run(app, cancel):
            selected = Path(path).expanduser()
            if not str(path).strip() or not selected.is_absolute() or not selected.is_file():
                raise ValueError("Choose an existing absolute configuration file")

            if any(state != State.STOPPED for state in (app.logos.logos_state, app.logos.indexing_state)):
                raise maintenance.MaintenanceBusy("Stop this installation before selecting another profile")

            overrides = EphemeralConfiguration.load_from_path(str(selected))
            candidate = Config(overrides, app)
            context = maintenance.Context.from_app(type("Selection", (), {"conf": candidate})(), prompt=False)
            if context.product != "Logos":
                raise ValueError("Choose a Logos installation profile")
            if not utils.find_installed_product(context.product, str(context.prefix)):
                raise ValueError("The chosen profile does not identify an installed product")

            description = f"Configuration: {selected}\nPrefix: {context.prefix}"
            if not app.approve("Use this existing installation?", description):
                return Result("cancelled", "Installation selection was not changed")
            app.checkpoint()

            if not app.logos.close():
                raise maintenance.MaintenanceBusy("The previous installation is still settling")

            app.conf = candidate
            app.logos = LogosManager(app)
            app.logos.start_monitoring()
            return Result("succeeded", "Existing installation selected without reinstalling")
        return self._settled(run)

    def settings(self, backup_dir, product_channel, controller_channel, *, profile=None):
        def run(app, cancel):
            if profile is not None and profile != app.conf.config_file_path:
                raise maintenance.MaintenanceError("Settings belong to another profile. Reload before saving")
            if product_channel != "stable" or controller_channel != "stable":
                raise ValueError("The graphical interface uses stable releases")
            if backup_dir and (not Path(backup_dir).is_absolute() or not Path(backup_dir).is_dir()):
                raise ValueError("Choose an existing absolute backup directory")
            app.checkpoint()
            changes = self._stable_changes(app.conf)
            app.conf._change(backup_dir=backup_dir or None, **changes)
            return Result("succeeded", "Settings saved")
        return self._settled(run)

    def operation(self, name):
        def run(app, cancel):
            app.checkpoint()
            if name == "launch":
                app.logos.start()
                return Result("succeeded", "Launch requested. Waiting for product processes")

            if name == "stop":
                app.logos.stop()
                return Result("succeeded", "Selected product processes stopped")

            if name == "index":
                app.logos.index()
                return Result("succeeded", "Indexing started. Its process state is shown on Home")

            if name == "stop_index":
                app.logos.stop_indexing()
                return Result("succeeded", "Selected indexer stopped")

            if name == "check":
                lookup = app.conf._network.product_release("Logos", app.conf.faithlife_product_version,
                                                          "stable", force=True)
                app.checkpoint()
                if lookup.status != "ok" or lookup.stale:
                    return Result("unknown", "Stable release information is unavailable",
                                  "Check the connection and try again")
                return Result("succeeded", "Checked the latest stable Logos release")

            if name == "backup":
                path = backup.BackupTask(app, stop=cancel).run()
                return Result("succeeded", f"Verified backup: {path}")

            if name == "restore":
                if not app.approve("Restore a completed backup?", "This replaces the selected product's user data."):
                    return Result("cancelled", "Restore was not started")
                warning = backup.RestoreTask(app, stop=cancel).run()
                return Result("succeeded", warning or "Restore completed. Selected data verified")

            if name == "support":
                path = control.get_support(app)
                return Result("succeeded", f"Private support archive: {path}")

            if name == "controller_update":
                if not constants.APP_SELF_UPDATES_ENABLED:
                    return Result("unknown", constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
                if not app.approve("Update LogosDesktop and restart?", "Use the latest stable controller release."):
                    return Result("cancelled", "Controller update was not started")
                app.checkpoint()
                prepared = updates.prepare(app, channel="stable")
                try:
                    app.checkpoint()
                except BaseException:
                    prepared.cancel()
                    raise
                self.prepared = prepared
                return Result("succeeded", "Controller update prepared for restart")

            if name == "logging":
                app.logos.switch_logging(action="disable" if app.conf.faithlife_product_logging else "enable")
                return Result("succeeded", "Product logging setting changed")

            if name == "repair":
                context = maintenance.Context.from_app(app, prompt=False)
                changes = None
                if repair.inspect(context) == repair.FailureType.FailedUpgrade:
                    target = self._latest_target(app)
                    changes = self._release_changes(app.conf, target)
                if changes is None:
                    applied = repair.repair_installation(app, require_target=True)
                else:
                    validator = partial(self._validate_release_document, app.conf, target)
                    with app.conf.validated_writes(validator):
                        applied = repair.repair_installation(app, configuration_changes=changes, require_target=True)
                return Result("cancelled" if applied is False else "succeeded", "Repair request settled")

            if name == "product_update":
                target = self._latest_target(app)
                changes = self._release_changes(app.conf, target)
                if not app.approve("Update Logos?", f"Latest stable release: {target}\n"
                                   f"Installation: {app.conf._raw.install_dir}"):
                    return Result("cancelled", "Update was not started")
                app.checkpoint()
                validator = partial(self._validate_release_document, app.conf, target)
                with app.conf.validated_writes(validator), maintenance.installing(app):
                    app.conf._change(**self._release_changes(app.conf, target))
                    self._validate_release_document(app.conf, target, app.conf._raw._saved)
                    installer.install(app)
                return Result("succeeded", f"Logos updated to {target}")

            if name == "runtime_update":
                raw = app.conf._raw
                appimage = (raw.wine_binary or "").lower().endswith(".appimage")
                recommended = raw.wine_binary in (constants.WINE_RECOMMENDED_SIGIL, constants.WINE_BETA_SIGIL)
                if raw.wine_binary_code not in ("AppImage", "Recommended") and not appimage and not recommended:
                    return Result("unknown", "Recommended Wine updates apply only to managed AppImage runtimes")

            actions = {
                "runtime_update": (utils.update_to_latest_recommended_appimage, "Runtime update completed"),
                "dependencies": (utils.install_dependencies, "Dependency installation completed"),
                "icu": (wine.enforce_icu_data_files, "ICU installation completed"),
                "catalog": (control.remove_library_catalog, "Library catalog removed"),
                "indexes": (control.remove_all_index_files, "Index files removed"),
                "uninstall": (control.uninstall, "Installation removed"),
                "edit_config": (lambda owner: control.edit_file(owner.conf.config_file_path), "Configuration opened"),
            }
            function, message = actions[name]
            if name in ("catalog", "indexes", "runtime_update", "icu", "dependencies"):
                if not app.approve("Continue with this maintenance action?", f"{name.replace('_', ' ').title()}\n"
                                   f"Installation: {app.conf._raw.install_dir}"):
                    return Result("cancelled", "Maintenance was not started")
            app.checkpoint()
            if name == "dependencies":
                app.conf.skip_install_system_dependencies = False
            value = function(app)
            if name in ("repair", "uninstall") and value is False:
                return Result("cancelled", "Maintenance was not started")
            return Result("succeeded", message)
        return self._settled(run)

    @staticmethod
    def _stable_changes(conf):
        changes = {"faithlife_product_release_channel": "stable", "app_release_channel": "stable"}
        document = config_store.apply_changes(conf._raw._saved, changes, conf.config_file_path)
        effective = PersistentConfiguration.from_document(document, conf._overrides.environment)
        if effective.faithlife_product_release_channel != "stable" or effective.app_release_channel != "stable":
            raise maintenance.MaintenanceError("An invocation override selects a non-stable release channel")
        return changes

    def select_runtime(self, path):
        def run(app, cancel):
            if not path or not Path(path).is_absolute() or not Path(path).is_file():
                raise ValueError("Choose an existing absolute AppImage file")
            if not app.approve("Change Wine runtime?", path):
                return Result("cancelled", "Runtime was not changed")
            app.checkpoint()
            utils.set_appimage_symlink(app, source=path)
            return Result("succeeded", "Wine runtime changed and saved")
        return self._settled(run)

    def discover(self, app, cancel):
        choices = utils.get_wine_options(app)
        app.checkpoint()
        with self._lock:
            self._snapshot = replace(self._snapshot, runtimes=tuple(choices))
        return Result("succeeded", "Wine runtimes discovered")

    def _settled(self, operation):
        def run(app, cancel):
            try:
                return operation(app, cancel)
            except (backup_store.CopyCancelled, UserExitedFromAsk):
                return Result("cancelled", "Operation cancelled at a safe boundary. Earlier completed steps remain")
            finally:
                try:
                    self.refresh(app, cancel)
                except Exception:
                    logging.exception("Cannot refresh installation after operation")
        return run

    def open_log(self, app, cancel):
        path = Path(app.conf.app_log_path)
        if not path.is_file():
            raise ValueError("No controller log exists yet")
        if not shutil.which("xdg-open"):
            raise ValueError("xdg-open is unavailable")
        control.edit_file(str(path))
        return Result("succeeded", "Controller log opened")
