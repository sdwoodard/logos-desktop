"""Completed snapshots and recoverable replacement of Logos user data."""

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import secrets
import shutil
import threading

from ou_dedetai import backup_store as store
from ou_dedetai import constants, maintenance, paths
from ou_dedetai.app import UserExitedFromAsk


recover_pending_restore = store.recover_pending_restore


class BackupBase:
    def __init__(self, app, *, backup_dir=None, source=None, stop=None):
        self.app = app
        self.backup_dir = backup_dir
        self.selected_source = source
        self.stop = stop or threading.Event()
        self.copied = 0

    def _live_path(self, context):
        value = self.app.conf._logos_appdata_dir
        if not isinstance(value, (str, Path)) or not str(value):
            raise store.BackupError("Cannot maintain data before the product is installed")
        live = Path(value).expanduser()
        if not live.is_absolute() or live.is_symlink() or not live.resolve().is_relative_to(context.prefix):
            raise store.BackupError("Product data is outside the selected Wine prefix or is linked")
        return live.resolve(strict=True)

    def _backup_path(self, context, live):
        value = self.backup_dir or self.app.conf._raw.backup_dir
        if value and self.backup_dir is None:
            if not self.app.approve(f"Use backup folder {value}?"):
                value = None
        if not value:
            value = self.app.ask("Choose an existing backup folder:", [constants.PROMPT_OPTION_DIRECTORY])
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise UserExitedFromAsk
        root = Path(value).expanduser()
        if not root.is_absolute():
            root = paths.absolute_from_install(root, context.install)
        if root.is_symlink() or not paths.disjoint(root, live):
            raise store.BackupError("Backup folder and live product data must be disjoint, unlinked directories")
        if not root.is_dir():
            raise store.BackupError("Backup folder is missing. Reconnect its disk or choose another existing folder")
        return root.resolve(strict=True)

    def _progress(self, count):
        self.copied += count
        self.app.status(f"Copying data: {self.copied} bytes", None)


class BackupTask(BackupBase):
    def run(self):
        try:
            with maintenance.maintain(self.app) as context:
                live_path = self._live_path(context)
                root_path = self._backup_path(context, live_path)
                with store.Directory(live_path) as live, store.Directory(root_path) as root:
                    store.bind_root(root, context, self.app)
                    manifest = store.new_manifest(live.path, context.product, self.stop)
                    store.verify_capacity(root, manifest)
                    name = f"{context.product}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(12)}"
                    os.mkdir(root.path / name, mode=0o700)
                    with store.Directory(root.path / name) as container:
                        try:
                            os.mkdir(container.path / ".incomplete", mode=0o700)
                            store.copy_payload(
                                live.path, container.path / ".incomplete", manifest, self.stop, self._progress,
                            )
                            store.verify_payload(live.path, manifest, self.stop)
                            live.validate()
                            root.validate()
                            maintenance.assert_quiescent(context)
                            store.write_json(container.path / ".incomplete", "manifest.json", manifest)
                            paths.rename_no_replace(container.path / ".incomplete", container.path / "complete")
                            store.sync_directory(container.path)
                            store.sync_directory(root.path)
                        except BaseException:
                            try:
                                shutil.rmtree(root.path / name)
                            except OSError:
                                logging.exception("Incomplete backup remains at %s", root_path / name)
                            raise
                result = root_path / name
                self.app.status(f"Finished backup. Verified snapshot: {result}", 100)
                return result
        except (OSError, ValueError) as error:
            raise store.BackupError(f"Backup failed: {error}") from error


def completed_snapshots(root, product):
    completed = []
    for path in sorted(root.iterdir(), reverse=True):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            with store.Directory(path) as container, store.Directory(container.path / "complete") as payload:
                manifest = store.read_json(payload.path / "manifest.json")
                store.validate_manifest(manifest, product)
                store.verify_payload(payload.path, manifest)
            completed.append(path.name)
        except (OSError, ValueError, maintenance.MaintenanceError):
            continue
    return completed


class RestoreTask(BackupBase):
    def run(self):
        try:
            with maintenance.maintain(self.app) as context:
                live_path = self._live_path(context)
                root_path = self._backup_path(context, live_path)
                with store.Directory(root_path) as root:
                    store.bind_root(root, context, self.app)
                    candidates = completed_snapshots(root.path, context.product)
                    if not candidates:
                        raise store.BackupError(
                            "No completed backups in this folder. Legacy and incomplete folders remain untouched. "
                            "Choose another backup folder"
                        )
                    selected = self.selected_source or self.app.ask("Choose completed backup to restore:", candidates)
                    if not isinstance(selected, (str, Path)) or not str(selected):
                        raise UserExitedFromAsk
                    selected = Path(selected).name if self.selected_source is not None else str(selected)
                    if selected not in candidates:
                        raise store.BackupError("Selected backup is no longer available")
                    with (
                        store.Directory(root.path / selected) as snapshot,
                        store.Directory(snapshot.path / "complete") as payload,
                    ):
                        manifest = store.read_json(payload.path / "manifest.json")
                        store.validate_manifest(manifest, context.product)
                        store.verify_payload(payload.path, manifest, self.stop)
                        root.validate()
                        snapshot.validate()
                        payload.validate()
                        def precommit():
                            root.validate()
                            snapshot.validate()
                            payload.validate()
                            if store.read_json(payload.path / "manifest.json") != manifest:
                                raise store.BackupError("Selected backup metadata changed during copying")

                        warning = store.restore_payload(
                            context, live_path, payload.path, manifest, self.stop, self._progress, precommit,
                        )
                if warning:
                    self.app.status(warning, 100)
                else:
                    self.app.status("Finished restore. All selected data verified", 100)
        except (OSError, ValueError) as error:
            raise store.BackupError(f"Restore failed: {error}") from error


def backup(app):
    return BackupTask(app).run()


def restore(app):
    return RestoreTask(app).run()
