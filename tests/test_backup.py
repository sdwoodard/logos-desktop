import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ou_dedetai import backup, backup_store as store, constants, maintenance
from ou_dedetai.app import UserExitedFromAsk


def fixture_app(root):
    install = root / "Installation 文"
    prefix = install / "data/wine64_bottle"
    live = prefix / "drive_c/users/fixture/AppData/Local/Logos"
    conf = SimpleNamespace(
        _raw=SimpleNamespace(
            install_dir=str(install), faithlife_product="Logos", faithlife_product_version="10",
            backup_dir=str(root / "backups"),
        ),
        _overrides=SimpleNamespace(
            install_dir=None, wine_prefix=None, assume_yes=False, config_path=str(root / "selected.json"),
        ),
        _logos_appdata_dir=str(live),
    )
    return SimpleNamespace(conf=conf, approve=Mock(return_value=True), ask=Mock(), status=Mock(), logos=None)


def hashes(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


class TestBackupTransactions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(constants, "STATE_DIR", str(self.root / "state/FaithLife-Community")).start()
        self.app = fixture_app(self.root)
        self.context = maintenance.Context.from_app(self.app)
        self.live = Path(self.app.conf._logos_appdata_dir)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        for name in store.ROOTS:
            (self.live / name).mkdir(parents=True)
            (self.live / name / "sentinel").write_bytes((name + " current").encode())
        (self.live / "System").mkdir()
        (self.live / "System/unchanged.exe").write_bytes(b"executable sentinel")
        self.before = hashes(self.live)

    def snapshot(self):
        return backup.BackupTask(self.app).run()

    def mutate(self):
        for name in store.ROOTS:
            (self.live / name / "sentinel").write_bytes((name + " newer").encode())
        self.before = hashes(self.live)

    def assert_preserved(self):
        self.assertEqual(hashes(self.live), self.before)

    def restore(self, snapshot, **kwargs):
        return backup.RestoreTask(self.app, source=snapshot, **kwargs).run()

    def test_round_trip_real_sqlite_wal_and_absent_empty_roots(self):
        database = self.live / "Data/library.db"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE notes(value TEXT)")
        connection.execute("INSERT INTO notes VALUES ('disposable note')")
        connection.commit()
        self.assertGreater(Path(str(database) + "-wal").stat().st_size, 0)
        (self.live / "Documents/empty").mkdir()
        snapshot = self.snapshot()
        expected = hashes(snapshot / "complete")
        expected.pop("manifest.json")
        connection.close()
        self.mutate()
        self.restore(snapshot)
        restored = hashes(self.live)
        restored.pop("System/unchanged.exe")
        self.assertEqual(restored, expected)
        self.assertTrue((self.live / "Documents/empty").is_dir())
        with sqlite3.connect(database) as restored_db:
            self.assertEqual(restored_db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(restored_db.execute("SELECT value FROM notes").fetchall(), [("disposable note",)])
        self.assertFalse(self.context.pending.exists())
        self.assertFalse(list(self.live.parent.glob(".logos-desktop-restore-*")))

    def test_preflight_and_partial_copy_failures_preserve_every_current_root(self):
        snapshot = self.snapshot()
        original_snapshot = hashes(snapshot)
        self.mutate()
        copy = store._hash_file
        for failure in ("capacity", "disk-full", "permission", "io", "cancel"):
            with self.subTest(failure=failure):
                self.app.status.reset_mock()
                count = 0
                stop = threading.Event()

                def interrupted(path, stop=None, target=None, progress=None):
                    nonlocal count
                    result = copy(path, stop, target, progress)
                    if target is not None:
                        count += 1
                        if count == 2:
                            if failure == "cancel":
                                raise store.CopyCancelled("fixture cancellation after copying")
                            code = {"disk-full": errno.ENOSPC, "permission": errno.EACCES, "io": errno.EIO}[failure]
                            raise OSError(code, "injected copy failure")
                    return result

                boundary = (
                    patch.object(store, "verify_capacity", side_effect=store.BackupError("injected no space"))
                    if failure == "capacity" else patch.object(store, "_hash_file", side_effect=interrupted)
                )
                with boundary, self.assertRaises(maintenance.MaintenanceError):
                    self.restore(snapshot, stop=stop)
                if failure != "capacity":
                    self.assertEqual(count, 2)
                self.assert_preserved()
                self.assertEqual(hashes(snapshot), original_snapshot)
                self.assertFalse(self.context.pending.exists())
                self.assertFalse(any("Finished restore" in call.args[0] for call in self.app.status.call_args_list))

    def test_each_replacement_boundary_rolls_back_even_after_a_real_rename(self):
        snapshot = self.snapshot()
        self.mutate()
        move = store._move
        for position in range(1, 7):
            for after in (False, True):
                with self.subTest(position=position, after=after):
                    count = 0

                    def fail_once(source, destination):
                        nonlocal count
                        count += 1
                        if count == position and not after:
                            raise OSError("before replacement")
                        move(source, destination)
                        if count == position and after:
                            raise OSError("after replacement")

                    with patch.object(store, "_move", side_effect=fail_once), self.assertRaises(store.BackupError):
                        self.restore(snapshot)
                    self.assert_preserved()
                    self.assertFalse(self.context.pending.exists())

    def test_backup_copy_failure_never_publishes_and_preserves_prior_snapshot(self):
        first = self.snapshot()
        before = hashes(first)
        copy = store._hash_file
        copied = []

        def disk_full(path, stop=None, target=None, progress=None):
            result = copy(path, stop, target, progress)
            if target is not None:
                copied.append(path)
                raise OSError(errno.ENOSPC, "injected disk-full after real write")
            return result

        with patch.object(store, "_hash_file", side_effect=disk_full), self.assertRaises(store.BackupError):
            self.snapshot()
        self.assertTrue(copied)
        self.assertEqual(backup.completed_snapshots(self.backups, "Logos"), [first.name])
        self.assertEqual(hashes(first), before)
        self.assert_preserved()

    def test_unique_names_and_completed_discovery(self):
        first = self.snapshot()
        before = hashes(first)
        second = self.snapshot()
        self.assertNotEqual(first, second)
        (self.backups / "Logos-legacy").mkdir()
        (self.backups / "Logos-partial/complete").mkdir(parents=True)
        self.assertEqual(set(backup.completed_snapshots(self.backups, "Logos")), {first.name, second.name})
        self.assertEqual(hashes(first), before)

    def test_metadata_listing_reports_size_roots_and_unknown_legacy_source_without_hashing(self):
        snapshot = self.snapshot()
        manifest_path = snapshot / "complete/manifest.json"
        manifest = store.read_json(manifest_path)
        with patch.object(store, "_hash_file", side_effect=AssertionError("Listing must not hash data")):
            info, = backup.list_snapshots(self.backups, "Logos")
        self.assertEqual(info.name, snapshot.name)
        self.assertEqual(info.created, manifest["created"])
        self.assertEqual(info.source, str(self.live))
        self.assertEqual(info.roots, store.ROOTS)
        self.assertEqual(info.size, sum(record.get("size", 0) for record in manifest["entries"].values()))
        del manifest["source"]
        store.write_json(manifest_path.parent, manifest_path.name, manifest)
        self.assertIsNone(backup.list_snapshots(self.backups, "Logos")[0].source)

    def test_restore_verifies_only_selected_snapshot_then_confirms_exact_paths(self):
        selected = self.snapshot()
        other = self.snapshot()
        (other / "complete/Data/sentinel").write_bytes(b"corrupt unselected snapshot")
        self.mutate()
        verified = []
        verify = store.verify_payload

        def record(path, manifest, stop=None):
            verify(path, manifest, stop)
            verified.append(Path(path).resolve())

        def approve(question, context=None):
            if question.startswith("Restore this verified"):
                self.assertEqual(verified, [selected / "complete"])
                self.assertIn(f"Snapshot: {selected}", context)
                self.assertIn(f"Target: {self.live}", context)
                return False
            return True

        self.app.approve.side_effect = approve
        with patch.object(store, "verify_payload", side_effect=record), self.assertRaises(UserExitedFromAsk):
            backup.restore(self.app, snapshot=selected)
        self.assert_preserved()
        self.assertFalse(self.context.pending.exists())

    def test_snapshot_verification_cancellation_after_real_block_preserves_live_data(self):
        (self.live / "Data/large").write_bytes(b"x" * (3 * 1024 * 1024))
        snapshot = self.snapshot()
        self.mutate()
        cancellation = threading.Event()
        hash_file = store._hash_file
        received = []

        def interrupt(path, stop=None, target=None, progress=None):
            def after_block(count):
                received.append(count)
                cancellation.set()
            return hash_file(path, stop, target, after_block)

        with patch.object(store, "_hash_file", side_effect=interrupt), self.assertRaises(store.CopyCancelled):
            self.restore(snapshot, stop=cancellation)
        self.assertTrue(received)
        self.assert_preserved()
        self.assertFalse(self.context.pending.exists())

    def test_cancel_directory_only_scan_and_metadata_listing(self):
        self.snapshot()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(store.CopyCancelled):
            backup.list_snapshots(self.backups, "Logos", stop)
        for root in store.ROOTS:
            (self.live / root / "sentinel").unlink()
        with self.assertRaises(store.CopyCancelled):
            store.inventory(self.live, stop)

    def test_explicit_snapshot_outside_selected_backup_folder_is_rejected(self):
        selected = self.snapshot()
        alias = self.root / selected.name
        alias.mkdir()
        with self.assertRaises(store.BackupError):
            backup.restore(self.app, snapshot=alias, backup_dir=self.backups)
        self.assert_preserved()

    def test_late_cancellation_during_restore_replacement_keeps_verified_committed_result(self):
        snapshot = self.snapshot()
        self.mutate()
        stop = threading.Event()
        move = store._move

        def cancel_after_move(source, destination):
            move(source, destination)
            stop.set()

        with patch.object(store, "_move", side_effect=cancel_after_move):
            self.restore(snapshot, stop=stop)
        self.assertTrue(stop.is_set())
        self.assertEqual((self.live / "Data/sentinel").read_bytes(), b"Data current")
        self.assertFalse(self.context.pending.exists())
        self.assertTrue(any("Finished restore" in call.args[0] for call in self.app.status.call_args_list))

    def test_missing_empty_unbound_and_invalid_locations_do_not_create_or_delete_data(self):
        self.app.conf._raw.backup_dir = str(self.root / "missing/disk/backups")
        with self.assertRaises(store.BackupError):
            backup.RestoreTask(self.app).run()
        self.assertFalse((self.root / "missing").exists())
        self.app.conf._raw.backup_dir = str(self.backups)
        with self.assertRaisesRegex(store.BackupError, "No completed backups"):
            backup.RestoreTask(self.app).run()
        self.assert_preserved()
        snapshot = self.snapshot()
        saved = self.root / "unmounted-disk"
        self.backups.rename(saved)
        self.backups.mkdir()
        with self.assertRaisesRegex(store.BackupError, "binding is missing"):
            backup.BackupTask(self.app).run()
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertTrue((saved / snapshot.name).is_dir())
        self.assert_preserved()

    def test_overlap_links_and_incomplete_snapshot_fail_before_mutation(self):
        for root in (self.live, self.live / "Data", self.live.parent):
            with self.subTest(root=root), self.assertRaises(store.BackupError):
                backup.BackupTask(self.app, backup_dir=root).run()
            self.assert_preserved()
        alias = self.root / "alias"
        alias.symlink_to(self.live, target_is_directory=True)
        with self.assertRaises(store.BackupError):
            backup.BackupTask(self.app, backup_dir=alias).run()
        snapshot = self.snapshot()
        self.mutate()
        (snapshot / "complete/Users/sentinel").unlink()
        with self.assertRaises(store.BackupError):
            self.restore(snapshot)
        self.assert_preserved()

    def test_source_links_and_manifest_traversal_are_rejected(self):
        outside = self.root / "outside"
        outside.write_bytes(b"unrelated")
        (self.live / "Data/link").symlink_to(outside)
        with self.assertRaises(store.BackupError):
            self.snapshot()
        (self.live / "Data/link").unlink()
        snapshot = self.snapshot()
        manifest_path = snapshot / "complete/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["entries"]["Data/../../outside"] = {"kind": "directory"}
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(store.BackupError):
            self.restore(snapshot)
        self.assertEqual(outside.read_bytes(), b"unrelated")
        self.assert_preserved()

    def test_rollback_failure_preserves_original_bytes_and_blocks_writers_until_recovery(self):
        snapshot = self.snapshot()
        self.mutate()
        move = store._move
        count = 0

        def fail(source, destination):
            nonlocal count
            count += 1
            if count >= 3:
                raise OSError("injected commit and rollback failure")
            move(source, destination)

        with patch.object(store, "_move", side_effect=fail), self.assertRaises(maintenance.RecoveryRequired):
            self.restore(snapshot)
        self.assertTrue(self.context.pending.exists())
        with self.assertRaises(maintenance.RecoveryRequired), maintenance.use(self.context):
            pass
        preserved = hashes(self.live)
        transaction = next(self.live.parent.glob(".logos-desktop-restore-*"))
        for key, value in hashes(transaction / "old").items():
            preserved[key] = value
        for key, value in self.before.items():
            self.assertEqual(preserved[key], value)
        store.recover_pending_restore(self.context)
        store.recover_pending_restore(self.context)
        self.assert_preserved()

    def test_fresh_process_recovery_after_each_commit_rename(self):
        snapshot = self.snapshot()
        self.mutate()
        program = """
import os,sys
from pathlib import Path
from ou_dedetai import backup,backup_store as store,constants
from tests.test_backup import fixture_app
root=Path(sys.argv[1])
constants.STATE_DIR=sys.argv[2]
move=store._move
count=0
def interrupt(source,destination):
    global count
    move(source,destination)
    count+=1
    if count==int(sys.argv[4]):
        os._exit(44)
store._move=interrupt
backup.RestoreTask(fixture_app(root),source=sys.argv[3]).run()
"""
        recovery = """
import sys
from pathlib import Path
from ou_dedetai import backup_store,constants,maintenance
from tests.test_backup import fixture_app
constants.STATE_DIR=sys.argv[2]
context=maintenance.Context.from_app(fixture_app(Path(sys.argv[1])))
backup_store.recover_pending_restore(context)
backup_store.recover_pending_restore(context)
"""
        for position in range(1, 7):
            with self.subTest(position=position):
                command = [
                    sys.executable, "-B", "-c", program, str(self.root), constants.STATE_DIR,
                    str(snapshot), str(position),
                ]
                self.assertEqual(subprocess.run(command, timeout=15).returncode, 44)
                self.assertTrue(self.context.pending.exists())
                subprocess.run(
                    [sys.executable, "-B", "-c", recovery, str(self.root), constants.STATE_DIR], check=True, timeout=15,
                )
                self.assert_preserved()
                self.assertFalse(self.context.pending.exists())

    def test_malformed_recovery_state_fails_closed(self):
        self.context.pending.write_text("pending")
        (self.context.state / "restore.json").write_text("{")
        with self.assertRaises(maintenance.RecoveryRequired):
            store.recover_pending_restore(self.context)
        self.assert_preserved()

    def test_absent_root_and_committed_cleanup_retry(self):
        (self.live / "Users/sentinel").unlink()
        (self.live / "Users").rmdir()
        snapshot = self.snapshot()
        (self.live / "Users").mkdir()
        (self.live / "Users/newer").write_bytes(b"newer discarded by selected snapshot")
        remove = store.shutil.rmtree
        failed = False

        def interrupted(path, *args, **kwargs):
            nonlocal failed
            remove(path, *args, **kwargs)
            if Path(path).name == "old" and not failed:
                failed = True
                raise OSError("injected cleanup interruption after removing old storage")

        with patch.object(store.shutil, "rmtree", side_effect=interrupted):
            self.restore(snapshot)
        self.assertTrue(self.context.pending.exists())
        self.assertFalse((self.live / "Users").exists())
        self.assertTrue(any("Restore committed" in call.args[0] for call in self.app.status.call_args_list))
        store.recover_pending_restore(self.context)
        store.recover_pending_restore(self.context)
        self.assertFalse(self.context.pending.exists())
        self.assertFalse((self.live / "Users").exists())

    def test_fresh_process_recovery_during_rollback_and_after_commit(self):
        snapshot = self.snapshot()
        self.mutate()
        program = """
import os,sys
from pathlib import Path
from ou_dedetai import backup,backup_store as store,constants
from tests.test_backup import fixture_app
constants.STATE_DIR=sys.argv[2]
move=store._move
count=0
def interrupt(source,destination):
    global count
    move(source,destination)
    count+=1
    if count==6:
        raise OSError('injected failed final move')
    if count==int(sys.argv[4]):
        os._exit(44)
if sys.argv[4]=='committed':
    journal=store._journal
    def committed(context,record):
        journal(context,record)
        if record['phase']=='committed':
            os._exit(44)
    store._journal=committed
else:
    store._move=interrupt
backup.RestoreTask(fixture_app(Path(sys.argv[1])),source=sys.argv[3]).run()
"""
        recovery = """
import sys
from pathlib import Path
from ou_dedetai import backup_store,constants,maintenance
from tests.test_backup import fixture_app
constants.STATE_DIR=sys.argv[2]
context=maintenance.Context.from_app(fixture_app(Path(sys.argv[1])))
backup_store.recover_pending_restore(context)
backup_store.recover_pending_restore(context)
"""
        for position in (*range(7, 13), "committed"):
            with self.subTest(position=position):
                command = [
                    sys.executable, "-B", "-c", program, str(self.root), constants.STATE_DIR,
                    str(snapshot), str(position),
                ]
                self.assertEqual(subprocess.run(command, timeout=15).returncode, 44)
                subprocess.run(
                    [sys.executable, "-B", "-c", recovery, str(self.root), constants.STATE_DIR], check=True, timeout=15,
                )
                if position == "committed":
                    self.assertEqual((self.live / "Data/sentinel").read_bytes(), b"Data current")
                else:
                    self.assert_preserved()

    def test_mount_replacement_before_commit_preserves_old_active_data(self):
        snapshot = self.snapshot()
        self.mutate()
        copy = store.copy_payload
        saved = self.root / "detached-disk"

        def detach(*args, **kwargs):
            copy(*args, **kwargs)
            self.backups.rename(saved)
            self.backups.mkdir()

        with patch.object(store, "copy_payload", side_effect=detach), self.assertRaises(store.BackupError):
            self.restore(snapshot)
        self.assert_preserved()
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertTrue((saved / snapshot.name).is_dir())

    def test_uncertain_commit_fsync_is_recovery_required_without_success_or_cleanup(self):
        snapshot = self.snapshot()
        self.mutate()
        sync = store.sync_directory
        failed = False

        def fail_commit(directory):
            nonlocal failed
            journal = self.context.state / "restore.json"
            if (
                Path(directory) == self.context.state and journal.exists()
                and store.read_json(journal)["phase"] == "committed" and not failed
            ):
                failed = True
                raise OSError(errno.EIO, "injected commit directory fsync failure")
            sync(directory)

        self.app.status.reset_mock()
        with patch.object(store, "sync_directory", side_effect=fail_commit):
            with self.assertRaises(maintenance.RecoveryRequired):
                self.restore(snapshot)
        self.assertTrue(failed)
        transaction = next(self.live.parent.glob(".logos-desktop-restore-*"))
        for name in store.ROOTS:
            self.assertEqual((transaction / "old" / name / "sentinel").read_bytes(), (name + " newer").encode())
        self.assertFalse(any(call.args[1:] == (100,) for call in self.app.status.call_args_list))
        with self.assertRaises(maintenance.RecoveryRequired), maintenance.use(self.context):
            pass
        store.recover_pending_restore(self.context)
        self.assertEqual((self.live / "Data/sentinel").read_bytes(), b"Data current")

    def test_recovery_hierarchy_is_flushed_before_any_live_rename(self):
        snapshot = self.snapshot()
        self.mutate()
        synced = set()
        sync = store.sync_directory
        move = store._move

        def recorded(directory):
            synced.add(Path(directory).resolve())
            sync(directory)

        def checked(source, destination):
            transaction = next(self.live.parent.glob(".logos-desktop-restore-*"))
            for directory in (transaction.parent, transaction, transaction / "old", transaction / "new"):
                self.assertIn(directory, synced)
            move(source, destination)

        with (
            patch.object(store, "sync_directory", side_effect=recorded),
            patch.object(store, "_move", side_effect=checked),
        ):
            self.restore(snapshot)

    def test_journal_without_marker_and_fifo_metadata_do_not_bypass_or_block_admission(self):
        journal = self.context.state / "restore.json"
        journal.write_text("{")
        with self.assertRaises(maintenance.RecoveryRequired), maintenance.use(self.context):
            pass
        with self.assertRaises(maintenance.RecoveryRequired):
            store.recover_pending_restore(self.context)
        journal.unlink()
        os.mkfifo(journal)
        with self.assertRaises(maintenance.RecoveryRequired):
            store.recover_pending_restore(self.context)
        self.assert_preserved()

    def test_prepared_journal_only_interruption_recovers_in_fresh_process(self):
        snapshot = self.snapshot()
        self.mutate()
        program = """
import os,sys
from pathlib import Path
from ou_dedetai import backup,backup_store as store,constants
from tests.test_backup import fixture_app
constants.STATE_DIR=sys.argv[2]
journal=store._journal
def interrupt(context,record):
    journal(context,record)
    if record['phase']=='prepared':
        os._exit(44)
store._journal=interrupt
backup.RestoreTask(fixture_app(Path(sys.argv[1])),source=sys.argv[3]).run()
"""
        command = [sys.executable, "-B", "-c", program, str(self.root), constants.STATE_DIR, str(snapshot)]
        self.assertEqual(subprocess.run(command, timeout=15).returncode, 44)
        self.assertFalse(self.context.pending.exists())
        with self.assertRaises(maintenance.RecoveryRequired), maintenance.use(self.context):
            pass
        store.recover_pending_restore(self.context)
        store.recover_pending_restore(self.context)
        self.assert_preserved()

    def test_cleanup_marker_interruption_reconciles_remaining_committed_journal(self):
        snapshot = self.snapshot()
        self.mutate()
        unlink = Path.unlink
        interrupted = False

        def fail_after_unlink(path, *args, **kwargs):
            nonlocal interrupted
            unlink(path, *args, **kwargs)
            if path == self.context.pending and not interrupted:
                interrupted = True
                raise OSError("injected failure after removing recovery marker")

        with patch.object(Path, "unlink", fail_after_unlink):
            self.restore(snapshot)
        self.assertTrue(interrupted)
        self.assertFalse(self.context.pending.exists())
        self.assertTrue((self.context.state / "restore.json").exists())
        with self.assertRaises(maintenance.RecoveryRequired), maintenance.use(self.context):
            pass
        sync = store.sync_directory
        order = []

        def record_unlink(path, *args, **kwargs):
            if path.parent == self.context.state:
                order.append(path.name)
            unlink(path, *args, **kwargs)

        def record_sync(path):
            if Path(path) == self.context.state:
                order.append("sync")
            sync(path)

        with patch.object(Path, "unlink", record_unlink), patch.object(store, "sync_directory", record_sync):
            store.recover_pending_restore(self.context)
        self.assertEqual(order[-2:], ["restore.json", "sync"])
        store.recover_pending_restore(self.context)
        self.assertEqual((self.live / "Data/sentinel").read_bytes(), b"Data current")
        self.assertFalse(self.context.needs_recovery())

    def test_recovery_identity_substitution_preserves_both_original_and_unknown_data(self):
        snapshot = self.snapshot()
        self.mutate()
        move = store._move
        count = 0

        def failed(source, destination):
            nonlocal count
            count += 1
            if count > 2:
                raise OSError("injected rollback failure")
            move(source, destination)

        with patch.object(store, "_move", side_effect=failed), self.assertRaises(maintenance.RecoveryRequired):
            self.restore(snapshot)
        transaction = next(self.live.parent.glob(".logos-desktop-restore-*"))
        original = transaction / "preserved-original-Data"
        (transaction / "old/Data").rename(original)
        (transaction / "old/Data").mkdir()
        unknown = transaction / "old/Data/unknown"
        unknown.write_bytes(b"unrelated replacement")
        with self.assertRaises(maintenance.RecoveryRequired):
            store.recover_pending_restore(self.context)
        self.assertEqual((original / "sentinel").read_bytes(), b"Data newer")
        self.assertEqual(unknown.read_bytes(), b"unrelated replacement")
        self.assertTrue(self.context.needs_recovery())
