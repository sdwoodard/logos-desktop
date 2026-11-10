"""Runner transactions use owned disposable stores and real accepted fixture bytes."""

from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from logos_desktop import artifacts, constants, maintenance, runtime_runners as runners, runtime_stacks
from tests.artifact_fixtures import appimage
from tests.test_artifacts import response


def interrupted_worker(store, runner, content):
    original = runners.paths.rename_no_replace

    def crash_after_rename(source, destination):
        original(source, destination)
        if destination.name.startswith("copy-"):
            os._exit(37)

    with (patch.object(artifacts.requests, "get", return_value=response(content)),
          patch.object(runners.paths, "rename_no_replace", side_effect=crash_after_rename)):
        store.download(runner)


def reconcile_worker(store, catalog):
    store.reconcile(catalog)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = patch.object(constants, "STATE_DIR", str(self.root / "state"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        def filesystem(path):
            info = Path(path).stat()
            return {"canonical_path": str(path), "filesystem_uuid": "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12,
                    "filesystem_root": str(path), "boot_id": "b" * 8 + "-bbbb-bbbb-bbbb-" + "b" * 12,
                    "device": info.st_dev, "inode": info.st_ino, "mount_id": 1, "owner_uid": os.geteuid()}

        self.filesystem = filesystem
        identity_patch = patch.object(runners.runtime_filesystem, "identity", side_effect=filesystem)
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        self.store = runners.RunnerStore(self.root / "store", self.root / "coordination")
        self.content = appimage()
        self.runner = replace(next(iter(runtime_stacks.bundled().runners.values())),
                              size=len(self.content), sha256=hashlib.sha256(self.content).hexdigest())

    def download(self, runner=None, **kwargs):
        with patch.object(artifacts.requests, "get", return_value=response(self.content)):
            return self.store.download(runner or self.runner, **kwargs)

    def test_download_has_no_prefix_config_or_wine_execution_and_counts_physical_keys(self):
        prefix = self.root / "prefix"
        prefix.mkdir()
        data = prefix / "data"
        data.write_bytes(b"original")
        with patch("subprocess.Popen", side_effect=AssertionError("runtime execution")):
            operation = self.download()
        self.assertEqual(self.store.operation(operation)["result"], "succeeded")
        self.assertEqual(data.read_bytes(), b"original")
        copies = self.store.inventory()
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].path.read_bytes(), self.content)
        with patch.object(artifacts.requests, "get", side_effect=AssertionError("cached transfer used network")):
            self.store.download(self.runner)
        self.assertEqual(len(self.store.inventory()), 1)
        self.download(replace(self.runner, id="other", provider="other"))
        self.assertEqual(len(self.store.inventory()), 2)

    def test_read_only_missing_inventory_does_not_create_state(self):
        with self.assertRaises((OSError, runners.RunnerError)):
            self.store.inventory()
        self.assertFalse(self.store.root.exists())
        self.assertFalse(self.store.coordination.exists())

    def test_tamper_and_provider_replacement_never_publish(self):
        with patch.object(artifacts.requests, "get", return_value=response(self.content + b"replacement")):
            with self.assertRaises(artifacts.ArtifactError):
                self.store.download(self.runner)
        self.assertFalse(self.store.inventory())
        self.download()
        path = self.store.inventory()[0].path
        path.chmod(0o600)
        path.write_bytes(b"changed")
        with self.assertRaises(runners.RunnerError):
            self.store.inventory()

    def test_cancel_retry_and_late_cancel_publication_truth(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(runners.Cancelled):
            self.download(cancel=cancel)
        registry = self.store._registry()
        inventory = self.store._inventory(registry)
        operation = next(iter(inventory["operations"]))
        self.assertEqual(self.store.operation(operation)["result"], "cancelled")
        with patch.object(artifacts.requests, "get", return_value=response(self.content)):
            retried = self.store.retry(operation, self.runner)
        self.assertEqual(self.store.operation(retried)["result"], "succeeded")

        previous = runners.paths.rename_no_replace

        def publish(source, target):
            result = previous(source, target)
            if target.name.startswith("copy-"):
                cancel.set()
            return result

        cancel.clear()
        other = replace(self.runner, id="second", provider="second")
        with patch.object(runners.paths, "rename_no_replace", side_effect=publish):
            operation = self.download(other, cancel=cancel)
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.store.operation(operation)["result"], "succeeded")

    def test_enospc_leaves_selection_and_inventory_unchanged(self):
        with patch.object(runners.shutil, "disk_usage", return_value=Mock(free=0)):
            with self.assertRaisesRegex(runners.RunnerError, "space"):
                self.download()
        self.assertFalse(self.store.inventory())

    def test_removal_disabled_until_all_routes_integrate(self):
        self.download()
        copy = self.store.inventory()[0]
        with self.assertRaisesRegex(runners.RunnerError, "reference-integration-incomplete"):
            self.store.remove(copy.generation)
        self.assertTrue(copy.path.exists())

    def test_actual_unused_removal_preserves_external_and_dependencies(self):
        self.download()
        copy = self.store.inventory()[0]
        external = self.root / "external.AppImage"
        external.write_bytes(self.content)
        with patch.object(runners, "INTEGRATED_REFERENCE_KINDS", runners.REFERENCE_KINDS):
            operation = self.store.remove(copy.generation)
        self.assertEqual(self.store.operation(operation)["result"], "succeeded")
        self.assertFalse(copy.path.exists())
        self.assertEqual(external.read_bytes(), self.content)
        self.assertFalse(self.store.inventory())

    def test_cross_store_digest_pins_and_changed_owner_block_removal(self):
        self.download()
        copy = self.store.inventory()[0]
        owner = self.root / "owner.json"
        self.owner(owner, copy.generation)
        self.store.pin(copy.generation, kind="return-point", owner_path=owner,
                       owner_sha256=artifacts.regular_hash(owner))
        other = runners.RunnerStore(self.root / "other-store", self.store.coordination)
        with patch.object(artifacts.requests, "get", return_value=response(self.content)):
            other.download(replace(self.runner, provider="other", id="other"))
        other_copy = other.inventory()[0]
        with patch.object(runners, "INTEGRATED_REFERENCE_KINDS", runners.REFERENCE_KINDS):
            with self.assertRaisesRegex(runners.RunnerError, "return-point"):
                other.remove(other_copy.generation)
            owner.write_bytes(b"changed")
            with self.assertRaisesRegex(runners.RunnerError, "owner changed"):
                other.remove(other_copy.generation)

    def owner(self, path, generation):
        record = {"schema": "runtime-runner-owner-2", "record": "return-point", "id": str(uuid.uuid4()),
                  "revision": 1, "phase": "pending", "runner_generations": [generation]}
        path.write_bytes(runtime_stacks.canonical(record))
        path.chmod(0o600)
        return record

    def test_nonterminal_owner_change_cannot_release_pin(self):
        self.download()
        copy = self.store.inventory()[0]
        path = self.root / "owner.json"
        record = self.owner(path, copy.generation)
        pin = self.store.pin(copy.generation, kind="return-point", owner_path=path,
                             owner_sha256=artifacts.regular_hash(path))
        record.update(revision=2, phase="active")
        path.write_bytes(runtime_stacks.canonical(record))
        with self.assertRaisesRegex(runners.RunnerError, "terminal"):
            self.store.release_pin(pin, settled_owner_sha256=artifacts.regular_hash(path))
        record.update(phase="settled", runner_generations=[])
        path.write_bytes(runtime_stacks.canonical(record))
        self.store.release_pin(pin, settled_owner_sha256=artifacts.regular_hash(path))
        self.assertFalse(self.store._registry()["references"])

    def test_missing_reference_or_inventory_binding_cannot_become_unused(self):
        self.download()
        copy = self.store.inventory()[0]
        path = self.root / "owner.json"
        self.owner(path, copy.generation)
        self.store.pin(copy.generation, kind="return-point", owner_path=path,
                       owner_sha256=artifacts.regular_hash(path))
        registry_path = self.store.coordination / "registry.json"
        registry = runners._read(registry_path)
        registry["references"] = {}
        runners._write(registry_path, registry)
        with self.assertRaisesRegex(runners.RunnerError, "binding changed"):
            self.store.inventory()
        checkpoint_path = self.store.coordination / "checkpoint.json"
        checkpoint = runners._read(checkpoint_path)
        checkpoint["bindings"][str(registry_path)] = artifacts.regular_hash(registry_path)
        runners._write(checkpoint_path, checkpoint)
        with self.assertRaisesRegex(runners.RunnerError, "missing or duplicate pin"):
            self.store.inventory()

    def test_reboot_device_remap_keeps_stable_identity_but_new_filesystem_is_refused(self):
        self.download()
        def reboot(path):
            observed = self.filesystem(path)
            observed.update(boot_id=str(uuid.UUID(int=1)), device=observed["device"] + 1)
            return observed

        with patch.object(runners.runtime_filesystem, "identity", side_effect=reboot):
            self.assertEqual(len(self.store.inventory()), 1)
        def replaced(path):
            observed = reboot(path)
            observed["filesystem_uuid"] = str(uuid.UUID(int=2))
            return observed

        with patch.object(runners.runtime_filesystem, "identity", side_effect=replaced):
            with self.assertRaisesRegex(runners.RunnerError, "identity changed"):
                self.store.inventory()

    def test_missing_store_and_lost_registry_are_unknown(self):
        self.download()
        (self.store.coordination / "registry.json").unlink()
        with self.assertRaisesRegex(runners.RunnerError, "no registry"):
            self.store.initialize()

    def test_lost_registry_cannot_reset_checkpoint_through_new_alternate_store(self):
        self.download()
        checkpoint = self.store.coordination / "checkpoint.json"
        original = checkpoint.read_bytes()
        (self.store.coordination / "registry.json").unlink()
        alternate = runners.RunnerStore(self.root / "alternate-store", self.store.coordination)
        with self.assertRaisesRegex(runners.RunnerError, "no registry"):
            alternate.initialize()
        self.assertEqual(checkpoint.read_bytes(), original)
        self.assertFalse(alternate.root.exists())
        self.assertTrue(self.store.root.exists())

    def test_partial_bootstrap_resumes_only_the_recorded_initial_root(self):
        with patch.object(self.store, "_commit", side_effect=OSError("injected bootstrap interruption")):
            with self.assertRaises(OSError):
                self.store.initialize()
        alternate = runners.RunnerStore(self.root / "alternate-store", self.store.coordination)
        with self.assertRaisesRegex(runners.RunnerError, "no registry"):
            alternate.initialize()
        self.store.initialize()
        self.assertFalse(self.store.inventory())

    def test_two_terminal_owners_settle_together_and_release_is_idempotent(self):
        self.download()
        copy = self.store.inventory()[0]
        owners = []
        for index in range(2):
            path = self.root / f"owner-{index}.json"
            record = self.owner(path, copy.generation)
            pin = self.store.pin(copy.generation, kind="return-point", owner_path=path,
                                 owner_sha256=artifacts.regular_hash(path))
            owners.append((path, record, pin))
        for path, record, _ in owners:
            record.update(revision=2, phase="settled", runner_generations=[])
            path.write_bytes(runtime_stacks.canonical(record))
        path, _, pin = owners[0]
        self.assertTrue(self.store.release_pin(pin, settled_owner_sha256=artifacts.regular_hash(path)))
        path, _, pin = owners[1]
        self.assertFalse(self.store.release_pin(pin, settled_owner_sha256=artifacts.regular_hash(path)))
        self.assertFalse(self.store._registry()["references"])

    def test_explicit_settlement_preserves_progress_with_a_malformed_other_owner(self):
        self.download()
        copy = self.store.inventory()[0]
        first = self.root / "first.json"
        first_record = self.owner(first, copy.generation)
        first_pin = self.store.pin(copy.generation, kind="return-point", owner_path=first,
                                   owner_sha256=artifacts.regular_hash(first))
        second = self.root / "second.json"
        self.owner(second, copy.generation)
        second_pin = self.store.pin(copy.generation, kind="return-point", owner_path=second,
                                    owner_sha256=artifacts.regular_hash(second))
        first_record.update(revision=2, phase="settled", runner_generations=[])
        first.write_bytes(runtime_stacks.canonical(first_record))
        second.write_bytes(b"malformed")
        self.store.release_pin(first_pin, settled_owner_sha256=artifacts.regular_hash(first))
        registry = runners._read(self.store.coordination / "registry.json")
        self.assertNotIn(first_pin, registry["references"])
        self.assertIn(second_pin, registry["references"])
        with self.assertRaisesRegex(runners.RunnerError, "owner changed"):
            self.store.inventory()

    def interrupted_removal(self):
        self.download()
        copy = self.store.inventory()[0]
        with (patch.object(runners, "INTEGRATED_REFERENCE_KINDS", runners.REFERENCE_KINDS),
              patch.object(self.store, "_finish_remove", side_effect=OSError("injected stop")),
              self.assertRaises(OSError)):
            self.store.remove(copy.generation)
        return copy, self.store.root / (".removed-" + copy.generation) / copy.path.name

    def test_changed_same_inode_tombstone_is_preserved_on_reconcile(self):
        _, tombstone = self.interrupted_removal()
        original_inode = tombstone.stat().st_ino
        tombstone.chmod(0o600)
        changed = b"x" * len(self.content)
        tombstone.write_bytes(changed)
        with self.assertRaisesRegex(runners.RunnerError, "identity changed"):
            self.store.reconcile(runtime_stacks.bundled())
        self.assertEqual(tombstone.stat().st_ino, original_inode)
        self.assertEqual(tombstone.read_bytes(), changed)

    def test_changed_tombstone_subvolume_is_refused_and_reboot_remap_is_allowed(self):
        copy, tombstone = self.interrupted_removal()

        def wrong_subvolume(path):
            current = self.filesystem(path)
            if Path(path) == tombstone.parent:
                current["filesystem_root"] = "/other-subvolume/" + Path(path).name
            return current

        with patch.object(runners.runtime_filesystem, "identity", side_effect=wrong_subvolume):
            with self.assertRaisesRegex(runners.RunnerError, "directory changed"):
                self.store.reconcile(runtime_stacks.bundled())
        self.assertTrue(tombstone.exists())

        def reboot(path):
            current = self.filesystem(path)
            current.update(boot_id=str(uuid.UUID(int=1)), device=current["device"] + 1)
            return current

        with patch.object(runners.runtime_filesystem, "identity", side_effect=reboot):
            self.store.reconcile(runtime_stacks.bundled())
        self.assertFalse(tombstone.exists())
        self.assertFalse(copy.path.exists())

    def test_unknown_checkpoint_intent_refuses_before_any_publication(self):
        self.download()
        original_write = runners._write

        def interrupt(path, value):
            if path.name == "checkpoint.json":
                raise OSError("injected checkpoint failure")
            return original_write(path, value)

        with patch.object(runners, "_write", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.store.download(self.runner)
        intent_path = self.store.coordination / "transaction.json"
        intent = runners._read(intent_path)
        checkpoint = str(self.store.coordination / "checkpoint.json")
        intent["documents"][checkpoint]["schema"] = "unknown"
        runners._write(intent_path, intent)
        before = {path: Path(path).read_bytes() for path in intent["documents"]}
        with self.assertRaisesRegex(runners.RunnerError, "Unsupported checkpoint"):
            self.store.reconcile(runtime_stacks.bundled())
        self.assertTrue(intent_path.exists())
        self.assertEqual(before, {path: Path(path).read_bytes() for path in intent["documents"]})

    def test_reopened_transport_identity_and_space_failures_have_distinct_reasons(self):
        import requests

        with patch.object(artifacts.requests, "get", side_effect=requests.ConnectionError("private details")):
            with self.assertRaises(artifacts.ArtifactError):
                self.store.download(self.runner)
        inventory = self.store._inventory(self.store._registry())
        first = next(iter(inventory["operations"]))
        self.assertEqual(self.store.operation(first)["reason"], "transport-unavailable")
        with patch.object(artifacts.requests, "get", return_value=response(b"wrong")):
            with self.assertRaises(artifacts.ArtifactError):
                self.store.download(self.runner)
        with patch.object(runners.shutil, "disk_usage", return_value=Mock(free=0)):
            with self.assertRaises(runners.RunnerError):
                self.store.download(self.runner)
        operations = self.store._inventory(self.store._registry())["operations"].values()
        self.assertEqual({item["reason"] for item in operations},
                         {"transport-unavailable", "identity-mismatch", "insufficient-space"})

    def test_symlink_and_hardlink_generation_refused(self):
        self.download()
        copy = self.store.inventory()[0]
        alias = self.root / "alias"
        os.link(copy.path, alias)
        with self.assertRaises(runners.RunnerError):
            self.store.inventory()
        alias.unlink()
        copy.path.unlink()
        copy.path.symlink_to(self.root / "external")
        with self.assertRaises(runners.RunnerError):
            self.store.inventory()

    def test_busy_artifact_lease_blocks_removal(self):
        self.download()
        copy = self.store.inventory()[0]
        with (patch.object(runners, "INTEGRATED_REFERENCE_KINDS", runners.REFERENCE_KINDS),
              maintenance.artifact_use(), self.assertRaises(maintenance.MaintenanceBusy)):
            self.store.remove(copy.generation)

    def test_worker_lock_blocks_second_transfer_and_reconciliation(self):
        self.store.initialize()
        with self.store._worker():
            with self.assertRaises(maintenance.MaintenanceBusy):
                self.download()
            with self.assertRaises(maintenance.MaintenanceBusy):
                self.store.reconcile(runtime_stacks.bundled())

    def test_interrupted_publication_reconciles_exact_bytes(self):
        original = self.store._save

        def fail_after_publication(inventory):
            if inventory["copies"]:
                raise OSError("injected state publication failure")
            return original(inventory)

        with patch.object(self.store, "_save", side_effect=fail_after_publication):
            with self.assertRaises(OSError):
                self.download()
        catalog = runtime_stacks.bundled()
        catalog = replace(catalog, runners={self.runner.id: self.runner})
        self.store.reconcile(catalog)
        self.assertEqual(len(self.store.inventory()), 1)

    def test_interrupted_removal_reconciles_without_touching_other_bytes(self):
        self.download()
        copy = self.store.inventory()[0]
        with (patch.object(runners, "INTEGRATED_REFERENCE_KINDS", runners.REFERENCE_KINDS),
              patch.object(self.store, "_finish_remove", side_effect=OSError("injected stop")),
              self.assertRaises(OSError)):
            self.store.remove(copy.generation)
        self.store.reconcile(runtime_stacks.bundled())
        self.assertFalse(self.store.inventory())

    def test_fresh_process_reconciles_publication_after_worker_death(self):
        context = multiprocessing.get_context("fork")
        child = context.Process(target=interrupted_worker, args=(self.store, self.runner, self.content))
        child.start()
        child.join(10)
        self.assertEqual(child.exitcode, 37)
        catalog = replace(runtime_stacks.bundled(), runners={self.runner.id: self.runner})
        reconciler = context.Process(target=reconcile_worker, args=(self.store, catalog))
        reconciler.start()
        reconciler.join(10)
        self.assertEqual(reconciler.exitcode, 0)
        self.assertEqual(len(self.store.inventory()), 1)

    def test_checkpoint_write_failure_reconciles_exact_preimages(self):
        self.download()
        original = runners._write
        failed = False

        def interrupt(path, value):
            nonlocal failed
            if path.name == "checkpoint.json" and not failed:
                failed = True
                raise OSError(28, "injected checkpoint ENOSPC")
            return original(path, value)

        with patch.object(runners, "_write", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.store.download(self.runner)
        with self.assertRaisesRegex(runners.RunnerError, "transaction"):
            self.store.inventory()
        self.store.reconcile(runtime_stacks.bundled())
        self.assertEqual(len(self.store.inventory()), 1)

    def test_late_download_acknowledgement_failure_keeps_valid_settled_outcome(self):
        original = self.store._save
        failed = False

        def late_failure(inventory):
            nonlocal failed
            original(inventory)
            if not failed and any(item["result"] == "succeeded" for item in inventory["operations"].values()):
                failed = True
                raise OSError("injected final acknowledgement failure")

        with patch.object(self.store, "_save", side_effect=late_failure):
            with self.assertRaises(OSError):
                self.download()
        inventory = self.store._inventory(self.store._registry())
        operation = next(iter(inventory["operations"].values()))
        self.assertEqual((operation["phase"], operation["result"]), ("settled", "succeeded"))
        self.assertFalse((self.store.coordination / "transaction.json").exists())
        context = multiprocessing.get_context("fork")
        catalog = replace(runtime_stacks.bundled(), runners={self.runner.id: self.runner})
        child = context.Process(target=reconcile_worker, args=(self.store, catalog))
        child.start()
        child.join(10)
        self.assertEqual(child.exitcode, 0)
        self.assertEqual(len(self.store.inventory()), 1)

    def test_late_removal_acknowledgement_failure_preserves_completed_deletion(self):
        self.download()
        copy = self.store.inventory()[0]
        original = self.store._save
        failed = False

        def late_failure(inventory):
            nonlocal failed
            original(inventory)
            if not failed and any(item["kind"] == "remove" and item["result"] == "succeeded"
                                  for item in inventory["operations"].values()):
                failed = True
                raise OSError("injected final removal acknowledgement failure")

        with (patch.object(runners, "INTEGRATED_REFERENCE_KINDS", runners.REFERENCE_KINDS),
              patch.object(self.store, "_save", side_effect=late_failure), self.assertRaises(OSError)):
            self.store.remove(copy.generation)
        self.assertFalse(copy.path.exists())
        self.assertFalse((self.store.coordination / "transaction.json").exists())
        context = multiprocessing.get_context("fork")
        child = context.Process(target=reconcile_worker, args=(self.store, runtime_stacks.bundled()))
        child.start()
        child.join(10)
        self.assertEqual(child.exitcode, 0)
        self.assertFalse(self.store.inventory())

    def test_noncanonical_owner_rejection_leaves_checkpoint_usable(self):
        self.download()
        copy = self.store.inventory()[0]
        path = self.root / "owner.json"
        record = self.owner(path, copy.generation)
        path.write_text(json.dumps(record, indent=2))
        checkpoint = (self.store.coordination / "checkpoint.json").read_bytes()
        with self.assertRaisesRegex(runners.RunnerError, "canonical"):
            self.store.pin(copy.generation, kind="return-point", owner_path=path,
                           owner_sha256=artifacts.regular_hash(path))
        self.assertFalse((self.store.coordination / "transaction.json").exists())
        self.assertEqual((self.store.coordination / "checkpoint.json").read_bytes(), checkpoint)
        self.assertEqual(len(self.store.inventory()), 1)

    def test_invalid_proposal_is_rejected_before_creating_intent(self):
        self.download()
        inventory = self.store._inventory(self.store._registry())
        operation = next(iter(inventory["operations"].values()))
        operation["result"] = "unknown"
        with self.assertRaisesRegex(runners.RunnerError, "unsettled"):
            self.store._save(inventory)
        self.assertFalse((self.store.coordination / "transaction.json").exists())
        self.assertEqual(len(self.store.inventory()), 1)

    def test_reference_publication_cannot_race_past_remove_admission(self):
        self.download()
        copy = self.store.inventory()[0]
        path = self.root / "owner.json"
        self.owner(path, copy.generation)
        errors = []

        def try_pin():
            try:
                self.store.pin(copy.generation, kind="return-point", owner_path=path,
                               owner_sha256=artifacts.regular_hash(path))
            except Exception as error:
                errors.append(error)

        with self.store._admission():
            thread = threading.Thread(target=try_pin)
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], maintenance.MaintenanceBusy)
