"""Managed runner bytes, durable reservations and conservative removal admission."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import stat
import threading
import uuid

from . import artifacts, constants, maintenance, paths, runtime_filesystem, runtime_stacks


REFERENCE_KINDS = frozenset((
    "profile-selection", "trial", "helper", "apply", "return-point", "pending-journal", "retained-generation",
))
# M4/M5 must integrate every writer before ordinary removal becomes available.
INTEGRATED_REFERENCE_KINDS = frozenset()
MAX_STATE_BYTES = 4 * 1024 * 1024


class RunnerError(maintenance.MaintenanceError):
    """A runner operation cannot settle with proven ownership."""

    def __init__(self, message, *, reason="identity-uncertain"):
        super().__init__(message)
        self.reason = reason


class Cancelled(RunnerError):
    """Cancellation won before immutable publication."""


def _failure_reason(error):
    if isinstance(error, Cancelled):
        return "cancelled-before-publication"
    if isinstance(error, OSError):
        return "insufficient-space" if error.errno == 28 else "storage-unavailable"
    reason = getattr(error, "reason", None)
    allowed = {"transport-unavailable", "identity-mismatch", "artifact-invalid", "insufficient-space",
               "storage-unavailable", "identity-uncertain"}
    return reason if reason in allowed else "operation-failed"


def _uuid(value):
    if not isinstance(value, str):
        raise RunnerError("Invalid runner UUID")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as error:
        raise RunnerError("Invalid runner UUID") from error
    return value


def _directory_identity(value):
    runtime_stacks.fields(value, "canonical_path filesystem_uuid filesystem_root boot_id "
                          "device inode mount_id owner_uid")
    for name in ("filesystem_uuid", "boot_id"):
        _uuid(value[name])
    for name in ("canonical_path", "filesystem_root"):
        text = runtime_stacks.text(value[name])
        if not Path(text).is_absolute() or ".." in Path(text).parts or "\0" in text:
            raise RunnerError("Invalid retained directory location")
    for name in ("inode", "mount_id"):
        runtime_stacks.positive(value[name])
    for name in ("device", "owner_uid"):
        if type(value[name]) is not int or value[name] < 0:
            raise RunnerError("Invalid retained directory observation")
    if value["owner_uid"] != os.geteuid():
        raise RunnerError("Retained directory is not owned by this account")


def _safe_directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise RunnerError("Runner root needs an absolute owned path")
    for parent in reversed((path, *path.parents[:-1])):
        if not parent.exists():
            if create:
                parent.mkdir(mode=0o700)
            else:
                raise RunnerError("Registered runner directory is missing")
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise RunnerError("Runner ancestry contains a link or non-directory")
        if parent == path and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise RunnerError("Runner directory must be private and owned")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise RunnerError("Runner ancestry is writable by another user")
    return path


def _read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or info.st_mode & 0o077):
            raise RunnerError("Unsafe runner record")
        return runtime_stacks.decode(source.read(MAX_STATE_BYTES + 1), MAX_STATE_BYTES)


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, value):
    content = runtime_stacks.canonical(value)
    if len(content) > MAX_STATE_BYTES:
        raise RunnerError("Runner state byte budget exceeded")
    temporary = path.parent / (".record-" + str(uuid.uuid4()))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class RunnerCopy:
    generation: str
    provider: str
    sha256: str
    path: Path
    size: int
    state: str

    @property
    def content_key(self):
        return (self.provider, "x86_64", "appimage2", self.sha256)


class RunnerStore:
    def __init__(self, root=None, coordination=None):
        self.root = Path(root or Path(constants.DATA_HOME) / "runners")
        self.coordination = Path(coordination or constants.COORDINATION_DIR / "runners")

    @contextmanager
    def _admission(self):
        _safe_directory(self.coordination, create=True)
        descriptor = os.open(self.coordination / "usage.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_mode & 0o077):
                raise RunnerError("Unsafe runner admission lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise maintenance.MaintenanceBusy("Another runner transaction is settling") from error
            yield
        finally:
            os.close(descriptor)

    def initialize(self):
        """Create a private store and durable account checkpoint together."""
        with self._admission():
            if (self.coordination / "transaction.json").exists():
                raise RunnerError("Runner checkpoint transaction needs reconciliation")
            registry_path = self.coordination / "registry.json"
            if registry_path.exists():
                registry = self._registry()
            else:
                if (self.coordination / "checkpoint.json").exists():
                    raise RunnerError("Existing account checkpoint has no registry")
                account_path = self.coordination / "account.json"
                if account_path.exists():
                    account = _read(account_path)
                    runtime_stacks.fields(account, "schema id initial_root initial_store initialized")
                    if (account["schema"] != "runtime-runner-account-2" or account["initialized"] is not False
                            or account["initial_root"] != str(self.root)):
                        raise RunnerError("Existing runner account has no registry")
                    _uuid(account["id"])
                    store_id = _uuid(account["initial_store"])
                else:
                    if self.root.exists() or set(item.name for item in self.coordination.iterdir()) != {"usage.lock"}:
                        raise RunnerError("Existing runner state has no registry")
                    store_id = str(uuid.uuid4())
                    account = {"schema": "runtime-runner-account-2", "id": str(uuid.uuid4()),
                               "initial_root": str(self.root), "initial_store": store_id, "initialized": False}
                    _write(account_path, account)
                registry = {"schema": "runtime-runner-registry-2", "stores": {}, "references": {}, "profiles": {},
                            "owners": {}}
                _safe_directory(self.root, create=True)
                inventory = {"schema": "runtime-runner-inventory-1", "id": store_id,
                             "copies": {}, "operations": {}}
                if set(item.name for item in self.root.iterdir()) - {"inventory.json"}:
                    raise RunnerError("Initial runner store contains unrecognized data")
                inventory_path = self.root / "inventory.json"
                if inventory_path.exists() and _read(inventory_path) != inventory:
                    raise RunnerError("Initial inventory changed before registration")
                _write(inventory_path, inventory)
                registry["stores"][str(self.root)] = {"id": store_id, "root": runtime_filesystem.identity(self.root)}
                self._commit(registry)
                account["initialized"] = True
                _write(account_path, account)
                return
            if str(self.root) in registry["stores"]:
                self._inventory(registry)
                return
            if self.root.exists():
                raise RunnerError("Unregistered runner store is unknown")
            # A crash in initialization leaves an unknown root, never a removable empty store.
            _safe_directory(self.root, create=True)
            store_id = str(uuid.uuid4())
            inventory = {"schema": "runtime-runner-inventory-1", "id": store_id, "copies": {}, "operations": {}}
            _write(self.root / "inventory.json", inventory)
            registry["stores"][str(self.root)] = {"id": store_id, "root": runtime_filesystem.identity(self.root)}
            self._commit(registry)

    def _registry(self, *, validate_owners=True):
        if (self.coordination / "transaction.json").exists():
            raise RunnerError("Runner checkpoint transaction needs reconciliation")
        self._verify_checkpoint()
        registry = _read(self.coordination / "registry.json")
        self._validate_registry_document(registry, observe_owners=validate_owners)
        for root, identity in registry["stores"].items():
            _safe_directory(Path(root))
            current = runtime_filesystem.identity(Path(root))
            if not runtime_filesystem.same_retained_root(identity["root"], current):
                raise RunnerError("Registered store identity changed")
        return registry

    def _validate_registry_document(self, registry, *, inventories=None, observe_owners=False):
        runtime_stacks.fields(registry, "schema stores references profiles owners")
        if registry["schema"] != "runtime-runner-registry-2":
            raise RunnerError("Unsupported runner registry")
        for key in ("stores", "references", "profiles", "owners"):
            if type(registry[key]) is not dict or len(registry[key]) > 4096:
                raise RunnerError("Invalid runner registry collection")
        if registry["profiles"]:
            raise RunnerError("Profile registrations require reference-integration reconciliation")
        store_ids = set()
        for root, registration in registry["stores"].items():
            runtime_stacks.fields(registration, "id root")
            _uuid(registration["id"])
            _directory_identity(registration["root"])
            if registration["root"]["canonical_path"] != root or registration["id"] in store_ids:
                raise RunnerError("Duplicate or contradictory store registration")
            store_ids.add(registration["id"])
            self._inventory(registry, root, document=None if inventories is None else inventories[str(root)])
        self._validate_owners(registry, inventories=inventories, observe=observe_owners)

    def _verify_checkpoint(self):
        checkpoint = _read(self.coordination / "checkpoint.json")
        runtime_stacks.fields(checkpoint, "schema id revision bindings")
        if checkpoint["schema"] != "runtime-runner-checkpoint-2":
            raise RunnerError("Unsupported runner checkpoint")
        _uuid(checkpoint["id"])
        account = _read(self.coordination / "account.json")
        runtime_stacks.fields(account, "schema id initial_root initial_store initialized")
        if account["schema"] != "runtime-runner-account-2" or checkpoint["id"] != account["id"]:
            raise RunnerError("Checkpoint account identity changed")
        runtime_stacks.positive(checkpoint["revision"])
        if type(checkpoint["bindings"]) is not dict:
            raise RunnerError("Unknown runner checkpoint completeness")
        registry_path = str(self.coordination / "registry.json")
        if registry_path not in checkpoint["bindings"]:
            raise RunnerError("Checkpoint has no registry binding")
        registry = _read(Path(registry_path))
        if type(registry.get("stores")) is not dict:
            raise RunnerError("Invalid checkpoint registry")
        expected = {registry_path, *(str(Path(root) / "inventory.json") for root in registry["stores"])}
        if set(checkpoint["bindings"]) != expected:
            raise RunnerError("Missing checkpoint inventory binding")
        for path, expected_hash in checkpoint["bindings"].items():
            runtime_stacks.digest(expected_hash)
            if artifacts.regular_hash(path) != expected_hash:
                raise RunnerError("Runner checkpoint binding changed")

    def _commit(self, registry, inventory=None):
        """Journal preimage hashes and intended documents for checked forward replay."""
        registry_path = self.coordination / "registry.json"
        checkpoint_path = self.coordination / "checkpoint.json"
        documents = {str(registry_path): registry}
        for root in registry["stores"]:
            path = Path(root) / "inventory.json"
            documents[str(path)] = inventory if Path(root) == self.root and inventory is not None else _read(path)
        previous = _read(checkpoint_path) if checkpoint_path.exists() else None
        checkpoint = {
            "schema": "runtime-runner-checkpoint-2", "id": _read(self.coordination / "account.json")["id"],
            "revision": previous["revision"] + 1 if previous else 1,
            "bindings": {path: runtime_stacks.fingerprint(value) for path, value in documents.items()},
        }
        documents[str(checkpoint_path)] = checkpoint
        intent = {
            "schema": "runtime-runner-transaction-2", "documents": documents,
            "previous": {path: artifacts.regular_hash(path) if Path(path).exists() else None for path in documents},
            "previous_revision": previous["revision"] if previous else 0,
        }
        previous_registry = _read(registry_path) if registry_path.exists() else None
        intent["previous_registry"] = previous_registry
        old_owners = previous_registry["owners"] if previous_registry is not None else {}
        intent["owner_transitions"] = {
            owner_id: owner["sha256"] for owner_id, owner in registry["owners"].items()
            if owner_id not in old_owners or old_owners[owner_id]["sha256"] != owner["sha256"]
        }
        self._validate_transaction(intent)
        _write(self.coordination / "transaction.json", intent)
        self._finish_transaction(intent)

    def _validate_transaction(self, intent):
        runtime_stacks.fields(intent, "schema documents previous previous_revision previous_registry owner_transitions")
        if intent["schema"] != "runtime-runner-transaction-2" or type(intent["documents"]) is not dict:
            raise RunnerError("Unknown runner checkpoint intent")
        documents = intent["documents"]
        if type(intent["previous"]) is not dict or set(documents) != set(intent["previous"]):
            raise RunnerError("Incomplete checkpoint preimages")
        registry_path = str(self.coordination / "registry.json")
        checkpoint_path = str(self.coordination / "checkpoint.json")
        registry = documents.get(registry_path)
        if not isinstance(registry, dict) or not isinstance(registry.get("stores"), dict):
            raise RunnerError("Checkpoint intent has no registry")
        allowed = {registry_path, checkpoint_path, *(str(Path(root) / "inventory.json") for root in registry["stores"])}
        if set(documents) != allowed:
            raise RunnerError("Unexpected checkpoint publication target")
        previous_registry = intent["previous_registry"]
        if (None if previous_registry is None else runtime_stacks.fingerprint(previous_registry)) != intent["previous"][
            registry_path
        ]:
            raise RunnerError("Registry preimage does not match its previous binding")
        old_stores = previous_registry["stores"] if previous_registry is not None else {}
        if any(registry["stores"].get(root) != registration for root, registration in old_stores.items()):
            raise RunnerError("Checkpoint cannot discard or replace registered stores")
        if set(registry["stores"]) - old_stores.keys() - {str(self.root)}:
            raise RunnerError("Checkpoint cannot introduce a foreign store")
        inventories = {root: documents[str(Path(root) / "inventory.json")] for root in registry["stores"]}
        self._validate_registry_document(registry, inventories=inventories)
        transitions = intent["owner_transitions"]
        old_owners = previous_registry["owners"] if previous_registry is not None else {}
        if old_owners.keys() - registry["owners"].keys():
            raise RunnerError("Checkpoint cannot discard registered owners")
        expected_transitions = {
            key: value["sha256"] for key, value in registry["owners"].items()
            if key not in old_owners or old_owners[key]["sha256"] != value["sha256"]
        }
        if type(transitions) is not dict or transitions != expected_transitions:
            raise RunnerError("Unresolved intended owner transition")
        for owner_id, expected_hash in transitions.items():
            owner = registry["owners"][owner_id]
            if owner_id in old_owners:
                old = old_owners[owner_id]
                if (owner["path"] != old["path"] or owner["revision"] != old["revision"] + 1
                        or owner["preimage"]["record"] != old["preimage"]["record"]
                        or owner["preimage"]["phase"] != "settled" or owner["reference_ids"]):
                    raise RunnerError("Unsupported owner settlement transition")
            if owner["sha256"] != expected_hash or artifacts.regular_hash(owner["path"]) != expected_hash:
                raise RunnerError("Owner changed during checkpoint settlement")
        for root, registration in registry["stores"].items():
            _safe_directory(Path(root))
            if not runtime_filesystem.same_retained_root(registration["root"], runtime_filesystem.identity(Path(root))):
                raise RunnerError("Checkpoint intent store changed")
        checkpoint = documents[checkpoint_path]
        runtime_stacks.fields(checkpoint, "schema id revision bindings")
        previous_revision = intent["previous_revision"]
        account = _read(self.coordination / "account.json")
        if (checkpoint["schema"] != "runtime-runner-checkpoint-2" or checkpoint["id"] != account["id"]
                or type(previous_revision) is not int or previous_revision < 0
                or type(checkpoint["revision"]) is not int or checkpoint["revision"] != previous_revision + 1):
            raise RunnerError("Unsupported checkpoint identity or revision transition")
        _uuid(checkpoint["id"])
        if Path(checkpoint_path).exists():
            current_checkpoint = _read(Path(checkpoint_path))
            if current_checkpoint["revision"] not in (previous_revision, checkpoint["revision"]):
                raise RunnerError("Checkpoint revision changed after intent")
        expected = {path: runtime_stacks.fingerprint(value) for path, value in documents.items()
                    if path != checkpoint_path}
        if checkpoint.get("bindings") != expected:
            raise RunnerError("Checkpoint intent hashes disagree")
        for path, value in documents.items():
            if intent["previous"][path] is not None:
                runtime_stacks.digest(intent["previous"][path])
            current = artifacts.regular_hash(path) if Path(path).exists() else None
            if current not in (intent["previous"][path], runtime_stacks.fingerprint(value)):
                raise RunnerError("Later runner state conflicts with interrupted publication")
        return documents

    def _finish_transaction(self, intent):
        documents = self._validate_transaction(intent)
        for path, value in documents.items():
            _write(Path(path), value)
        # Every intended document is durable now. A failed acknowledgement of
        # intent cleanup must not rewrite a settled outcome into an invalid state.
        (self.coordination / "transaction.json").unlink()
        _sync(self.coordination)

    def _validate_owners(self, registry, *, inventories=None, observe=True):
        expected_refs = set()
        stores = {item["id"]: root for root, item in registry["stores"].items()}
        for owner_id, owner in registry["owners"].items():
            _uuid(owner_id)
            runtime_stacks.fields(owner, "path sha256 revision preimage reference_ids")
            runtime_stacks.digest(owner["sha256"])
            runtime_stacks.positive(owner["revision"])
            if not isinstance(owner["path"], str) or not Path(owner["path"]).is_absolute():
                raise RunnerError("Owner location must be absolute")
            if observe and artifacts.regular_hash(owner["path"]) != owner["sha256"]:
                raise RunnerError("Runner owner changed. Reconcile references first")
            record = owner["preimage"]
            if runtime_stacks.fingerprint(record) != owner["sha256"]:
                raise RunnerError("Owner preimage does not match its binding")
            self._owner_record(record, owner_id)
            if record["revision"] != owner["revision"]:
                raise RunnerError("Runner owner revision changed")
            if (type(owner["reference_ids"]) is not list
                    or len(set(owner["reference_ids"])) != len(owner["reference_ids"])):
                raise RunnerError("Invalid owner reference list")
            for reference_id in owner["reference_ids"]:
                _uuid(reference_id)
                if reference_id in expected_refs or reference_id not in registry["references"]:
                    raise RunnerError("Owner has a missing or duplicate pin")
                expected_refs.add(reference_id)
                reference = registry["references"][reference_id]
                runtime_stacks.fields(reference, "store generation sha256 kind owner_id")
                _uuid(reference["store"])
                _uuid(reference["generation"])
                _uuid(reference["owner_id"])
                runtime_stacks.digest(reference["sha256"])
                if reference["kind"] != record["record"]:
                    raise RunnerError("Reference kind disagrees with owner")
                if reference["owner_id"] != owner_id or reference["store"] not in stores:
                    raise RunnerError("Reference owner or store does not resolve")
                root = stores[reference["store"]]
                inventory = self._inventory(registry, root,
                                            document=None if inventories is None else inventories[root])
                copy = inventory["copies"].get(reference["generation"])
                if copy is None or copy["sha256"] != reference["sha256"] or copy["state"] != "published":
                    raise RunnerError("Reference physical copy does not resolve")
                if reference["generation"] not in record["runner_generations"]:
                    raise RunnerError("Owner dependency does not match its pin")
            if set(record["runner_generations"]) != {
                registry["references"][key]["generation"] for key in owner["reference_ids"]
            }:
                raise RunnerError("Owner dependency has no durable pin")
        if set(registry["references"]) != expected_refs:
            raise RunnerError("Reference has no registered owner")

    @staticmethod
    def _owner_record(record, owner_id):
        runtime_stacks.fields(record, "schema record id revision phase runner_generations")
        if record["schema"] != "runtime-runner-owner-2" or record["record"] not in REFERENCE_KINDS:
            raise RunnerError("Unsupported runner owner record")
        if record["id"] != owner_id or record["phase"] not in ("pending", "active", "settled"):
            raise RunnerError("Owner identity or phase mismatch")
        runtime_stacks.positive(record["revision"])
        generations = record["runner_generations"]
        if type(generations) is not list or len(generations) > 32 or len(set(generations)) != len(generations):
            raise RunnerError("Invalid owner dependencies")
        for generation in generations:
            _uuid(generation)
        if record["phase"] == "settled" and generations:
            raise RunnerError("Settled owner retains runner dependencies")

    def _inventory(self, registry, root=None, *, document=None):
        root = self.root if root is None else Path(root)
        if str(root) not in registry["stores"]:
            raise RunnerError("Runner store is unregistered")
        inventory = _read(root / "inventory.json") if document is None else document
        runtime_stacks.fields(inventory, "schema id copies operations")
        if (inventory["schema"] != "runtime-runner-inventory-1"
                or inventory["id"] != registry["stores"][str(root)]["id"]):
            raise RunnerError("Runner inventory identity changed")
        for key in ("copies", "operations"):
            if type(inventory[key]) is not dict or len(inventory[key]) > 4096:
                raise RunnerError("Invalid runner inventory collection")
        for generation, copy in inventory["copies"].items():
            _uuid(generation)
            runtime_stacks.fields(copy, "provider sha256 name size device inode root withdrawn_root state")
            runtime_stacks.identifier(copy["provider"])
            runtime_stacks.digest(copy["sha256"])
            runtime_stacks.positive(copy["size"])
            runtime_stacks.positive(copy["inode"])
            if type(copy["device"]) is not int or copy["device"] < 0:
                raise RunnerError("Invalid copy device observation")
            _directory_identity(copy["root"])
            if copy["root"]["canonical_path"] != str(root / ("copy-" + generation)):
                raise RunnerError("Copy location contradicts its generation")
            if copy["withdrawn_root"] is not None:
                _directory_identity(copy["withdrawn_root"])
                if copy["withdrawn_root"]["canonical_path"] != str(root / (".removed-" + generation)):
                    raise RunnerError("Withdrawn location contradicts its generation")
                original = copy["root"]
                withdrawn = copy["withdrawn_root"]
                expected_root = str(Path(original["filesystem_root"]).with_name(".removed-" + generation))
                if (withdrawn["filesystem_root"] != expected_root
                        or any(withdrawn[key] != original[key] for key in ("filesystem_uuid", "inode", "owner_uid"))):
                    raise RunnerError("Withdrawn identity contradicts its original generation")
            elif copy["state"] != "published":
                raise RunnerError("Withdrawn copy has no retained location identity")
            if (not isinstance(copy["name"], str) or Path(copy["name"]).name != copy["name"]
                    or copy["name"] in ("", ".", "..") or copy["state"] not in (
                        "published", "tombstoned", "cleanup-pending",
                    )):
                raise RunnerError("Invalid runner copy")
        for operation, item in inventory["operations"].items():
            _uuid(operation)
            runtime_stacks.fields(item, "kind runner provider sha256 phase result reason generation")
            runtime_stacks.text(item["reason"])
            runtime_stacks.identifier(item["runner"])
            runtime_stacks.identifier(item["provider"])
            runtime_stacks.digest(item["sha256"])
            if item["generation"] is not None:
                _uuid(item["generation"])
            phases = {"download": ("resolved", "receiving", "verifying", "settled"),
                      "remove": ("resolved", "tombstoned", "cleanup-pending", "settled")}
            if (item["kind"] not in phases or item["phase"] not in phases[item["kind"]]
                    or item["result"] not in ("pending", "succeeded", "failed", "cancelled", "unknown")):
                raise RunnerError("Invalid runner operation phase")
            if item["result"] == "succeeded" and (item["phase"] != "settled" or item["generation"] is None):
                raise RunnerError("Successful runner operation has no settled generation")
            if item["kind"] == "remove" and item["generation"] is None:
                raise RunnerError("Removal has no physical generation")
            if item["phase"] == "settled" and item["result"] in ("pending", "unknown"):
                raise RunnerError("Settled operation has an unsettled result")
        for generation, copy in inventory["copies"].items():
            if copy["state"] != "published" and not any(
                item["kind"] == "remove" and item["generation"] == generation and item["phase"] != "settled"
                for item in inventory["operations"].values()
            ):
                raise RunnerError("Withdrawn copy has no owning removal intent")
        for item in inventory["operations"].values():
            if item["kind"] == "remove" and item["phase"] != "settled":
                if item["generation"] not in inventory["copies"]:
                    raise RunnerError("Pending removal has no retained copy")
            if item["kind"] == "download" and item["result"] == "succeeded":
                generation = item["generation"]
                if generation not in inventory["copies"] and not any(
                    other["kind"] == "remove" and other["generation"] == generation
                    and other["result"] == "succeeded" for other in inventory["operations"].values()
                ):
                    raise RunnerError("Published generation is missing without a settled removal")
        return inventory

    def _save(self, inventory):
        self._commit(self._registry(), inventory)

    def _copy_path(self, generation, copy, *, tombstone=False):
        _uuid(generation)
        directory = self.root / ((".removed-" if tombstone else "copy-") + generation)
        return directory / copy["name"]

    def _verify_copy(self, generation, copy):
        path = self._copy_path(generation, copy)
        _safe_directory(path.parent)
        current_root = runtime_filesystem.identity(path.parent)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or not runtime_filesystem.same_retained_root(copy["root"], current_root)
                or (info.st_ino, info.st_size) != (copy["inode"], copy["size"])
                or (copy["root"]["boot_id"] == current_root["boot_id"] and info.st_dev != copy["device"])
                or artifacts.regular_hash(path) != copy["sha256"]):
            raise RunnerError("Managed runner bytes or identity changed")
        artifacts.validate_format(path, "appimage")
        for role in artifacts.COMPANIONS:
            alias = path.parent / role
            if not alias.is_symlink() or os.readlink(alias) != path.name:
                raise RunnerError("Managed companion identity changed")
        return path

    @staticmethod
    def _copy_record(runner, path):
        info = path.lstat()
        return {
            "provider": runner.provider, "sha256": runner.sha256, "name": path.name, "size": info.st_size,
            "device": info.st_dev, "inode": info.st_ino, "root": runtime_filesystem.identity(path.parent),
            "withdrawn_root": None, "state": "published",
        }

    def inventory(self):
        """Inspect registered state without creating or repairing any files."""
        registry = self._registry()
        inventory = self._inventory(registry)
        result = []
        for generation, copy in inventory["copies"].items():
            if copy["state"] == "published":
                path = self._verify_copy(generation, copy)
            else:
                path = self._copy_path(generation, copy, tombstone=True)
            result.append(RunnerCopy(generation, copy["provider"], copy["sha256"], path,
                                     copy["size"], copy["state"]))
        return tuple(result)

    def operation(self, operation_id):
        _uuid(operation_id)
        registry = self._registry()
        return dict(self._inventory(registry)["operations"][operation_id])

    def _operation_phase(self, operation_id, phase, result="pending", generation=None, reason=""):
        with self._admission():
            inventory = self._inventory(self._registry())
            operation = inventory["operations"][operation_id]
            operation.update(phase=phase, result=result, generation=generation, reason=reason)
            self._save(inventory)

    @contextmanager
    def _worker(self):
        descriptor = os.open(self.root / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_mode & 0o077):
                raise RunnerError("Unsafe transfer owner lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise maintenance.MaintenanceBusy("Runner worker is still active") from error
            yield
        finally:
            os.close(descriptor)

    def download(self, runner, *, operation_id=None, cancel=None, status=None, candidates=()):
        """Download runner bytes only. Retry uses a new operation with the same pin."""
        self.initialize()
        with self._worker():
            return self._download(runner, operation_id=operation_id, cancel=cancel, status=status,
                                  candidates=candidates)

    def _download(self, runner, *, operation_id=None, cancel=None, status=None, candidates=()):
        operation_id = str(uuid.uuid4()) if operation_id is None else _uuid(operation_id)
        with self._admission():
            inventory = self._inventory(self._registry())
            if operation_id in inventory["operations"]:
                raise RunnerError("Operation already exists. Inspect or explicitly retry it")
            if any(item["result"] in ("pending", "unknown") for item in inventory["operations"].values()):
                raise maintenance.MaintenanceBusy("Reconcile or finish the existing runner operation")
            for generation, copy in inventory["copies"].items():
                if (copy["provider"] == runner.provider and copy["sha256"] == runner.sha256
                        and copy["state"] == "published"):
                    self._verify_copy(generation, copy)
                    inventory["operations"][operation_id] = {
                        "kind": "download", "runner": runner.id, "provider": runner.provider,
                        "sha256": runner.sha256, "phase": "settled", "result": "succeeded",
                        "generation": generation, "reason": "exact-cached-bytes",
                    }
                    self._save(inventory)
                    return operation_id
            inventory["operations"][operation_id] = {
                "kind": "download", "runner": runner.id, "provider": runner.provider, "sha256": runner.sha256,
                "phase": "resolved", "result": "pending", "generation": None, "reason": "",
            }
            self._save(inventory)

        def checkpoint():
            if cancel is not None and cancel.is_set():
                raise Cancelled("Runner download cancelled")

        def progress(message, fraction):
            if status:
                status(operation_id, message, fraction)

        staging = self.root / (".transfer-" + operation_id)
        try:
            checkpoint()
            if shutil.disk_usage(self.root).free < runner.size * 2 + 16 * 1024 * 1024:
                raise RunnerError("Insufficient space for staged runner publication", reason="insufficient-space")
            _safe_directory(staging, create=True)
            self._operation_phase(operation_id, "receiving")
            accepted = artifacts.acquire(runner.spec(), staging, candidates, checkpoint=checkpoint, status=progress)
            # The transport publication is private staging, not manager publication.
            checkpoint()
            self._operation_phase(operation_id, "verifying")
            accepted.check()
            with self._admission(), maintenance.artifact_use():
                inventory = self._inventory(self._registry())
                checkpoint()
                for generation, copy in inventory["copies"].items():
                    if (copy["provider"] == runner.provider and copy["sha256"] == runner.sha256
                            and copy["state"] == "published"):
                        self._verify_copy(generation, copy)
                        inventory["operations"][operation_id].update(
                            phase="settled", result="succeeded", generation=generation,
                        )
                        self._save(inventory)
                        return operation_id
                generation = str(uuid.uuid4())
                destination = self.root / ("copy-" + generation)
                operation = inventory["operations"][operation_id]
                operation["generation"] = generation
                self._save(inventory)
                # No cancellation after this boundary. A crash retains the publication intent.
                paths.rename_no_replace(accepted.path.parent, destination)
                _sync(self.root)
                path = destination / accepted.path.name
                inventory["copies"][generation] = self._copy_record(runner, path)
                operation.update(phase="settled", result="succeeded")
                self._save(inventory)
                return operation_id
        except BaseException as error:
            # A publication intent needs reconciliation, even if the last write failed.
            if (self.coordination / "transaction.json").exists():
                raise
            operation = self.operation(operation_id)
            if operation["phase"] == "settled":
                raise
            result = "unknown" if operation["generation"] is not None else (
                "cancelled" if isinstance(error, Cancelled) else "failed"
            )
            reason = "publication-uncertain" if result == "unknown" else _failure_reason(error)
            self._operation_phase(operation_id, operation["phase"], result, operation["generation"], reason)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def retry(self, operation_id, runner, **kwargs):
        previous = self.operation(operation_id)
        if (previous["kind"] != "download" or previous["result"] not in ("failed", "cancelled")
                or (previous["runner"], previous["provider"], previous["sha256"]) != (
                    runner.id, runner.provider, runner.sha256,
                )):
            raise RunnerError("Retry requires a settled failed transfer with the same exact identity")
        return self.download(runner, **kwargs)

    def reconcile(self, catalog):
        """Settle interrupted owned work after proving that no transfer worker remains."""
        with self._worker(), self._admission(), maintenance.artifact_use(exclusive=True):
            intent = self.coordination / "transaction.json"
            if intent.exists():
                self._finish_transaction(_read(intent))
            inventory = self._inventory(self._registry())
            for operation_id, operation in inventory["operations"].items():
                if operation["result"] not in ("pending", "unknown"):
                    continue
                generation = operation["generation"]
                if operation["kind"] == "remove":
                    if generation not in inventory["copies"]:
                        raise RunnerError("Interrupted removal has no copy record")
                    copy = inventory["copies"][generation]
                    source = self._copy_path(generation, copy).parent
                    withdrawn = self._copy_path(generation, copy, tombstone=True).parent
                    if source.exists() and not withdrawn.exists():
                        self._verify_copy(generation, copy)
                        copy["withdrawn_root"] = None
                        operation.update(phase="settled", result="failed", reason="interrupted-before-withdrawal")
                        self._save(inventory)
                    elif not source.exists():
                        self._finish_remove(inventory, operation_id)
                    else:
                        raise RunnerError("Ambiguous interrupted removal")
                    continue
                runner = catalog.runners.get(operation["runner"])
                if runner is None or (runner.provider, runner.sha256) != (
                    operation["provider"], operation["sha256"],
                ):
                    raise RunnerError("Interrupted transfer needs its original catalog identity")
                if generation is not None:
                    destination = self.root / ("copy-" + generation)
                    if destination.exists():
                        _safe_directory(destination)
                        path = destination / runner.spec().name
                        if artifacts.regular_hash(path) != runner.sha256 or path.stat().st_size != runner.size:
                            raise RunnerError("Interrupted publication bytes changed")
                        artifacts.validate_format(path, "appimage")
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise RunnerError("Interrupted publication identity changed")
                        inventory["copies"][generation] = self._copy_record(runner, path)
                        operation.update(phase="settled", result="succeeded", reason="verified-publication-reconciled")
                    else:
                        operation.update(phase="settled", result="failed", generation=None,
                                         reason="interrupted-before-publication")
                else:
                    operation.update(phase="settled", result="failed", reason="interrupted-transfer")
                self._save(inventory)
                staging = self.root / (".transfer-" + operation_id)
                if staging.exists():
                    _safe_directory(staging)
                    shutil.rmtree(staging)
                    _sync(self.root)

    def pin(self, generation, *, kind, owner_path, owner_sha256):
        """Persist a conservative pin before publishing the owning use."""
        if kind not in REFERENCE_KINDS:
            raise RunnerError("Unknown runner reference kind")
        runtime_stacks.digest(owner_sha256)
        owner_path = Path(owner_path)
        if not owner_path.is_absolute() or artifacts.regular_hash(owner_path) != owner_sha256:
            raise RunnerError("Reference owner preimage does not match")
        with self._admission():
            registry = self._registry()
            inventory = self._inventory(registry)
            record = _read(owner_path)
            if artifacts.regular_hash(owner_path) != owner_sha256:
                raise RunnerError("Reference owner changed before registration")
            if runtime_stacks.fingerprint(record) != owner_sha256:
                raise RunnerError("Owner records must use canonical JSON before registration")
            owner_id = _uuid(record.get("id"))
            self._owner_record(record, owner_id)
            if (record["record"] != kind or record["phase"] not in ("pending", "active")
                    or record["runner_generations"] != [generation] or owner_id in registry["owners"]):
                raise RunnerError("Owner must declare one new pending runner dependency")
            copy = inventory["copies"][generation]
            if copy["state"] != "published":
                raise RunnerError("Runner is withdrawn")
            self._verify_copy(generation, copy)
            reference_id = str(uuid.uuid4())
            registry["references"][reference_id] = {
                "store": inventory["id"], "generation": generation, "sha256": copy["sha256"],
                "kind": kind, "owner_id": owner_id,
            }
            registry["owners"][owner_id] = {
                "path": str(owner_path), "sha256": owner_sha256, "revision": record["revision"],
                "preimage": record, "reference_ids": [reference_id],
            }
            self._commit(registry)
            return reference_id

    def release_pin(self, reference_id, *, settled_owner_sha256):
        """Release only after the caller has durably settled and revalidated its owner."""
        _uuid(reference_id)
        runtime_stacks.digest(settled_owner_sha256)
        with self._admission():
            registry = self._registry(validate_owners=False)
            if reference_id not in registry["references"]:
                return False
            reference = registry["references"][reference_id]
            owner = registry["owners"][reference["owner_id"]]
            if artifacts.regular_hash(Path(owner["path"])) != settled_owner_sha256:
                raise RunnerError("Settled owner changed")
            self._settle_terminal_owners(registry, required_owner=reference["owner_id"])
            self._commit(registry)
            return True

    def _settle_terminal_owners(self, registry, *, required_owner=None):
        settled = []
        for owner_id, owner in registry["owners"].items():
            try:
                record = _read(Path(owner["path"]))
                actual_hash = artifacts.regular_hash(owner["path"])
                self._owner_record(record, owner_id)
                terminal = (record["phase"] == "settled" and not record["runner_generations"]
                            and record["revision"] == owner["revision"] + 1
                            and record["record"] == owner["preimage"]["record"]
                            and actual_hash == runtime_stacks.fingerprint(record))
                if not terminal:
                    if owner_id == required_owner:
                        raise RunnerError("Owner has no proven terminal settlement transition")
                    continue
            except (OSError, ValueError, RunnerError):
                if owner_id == required_owner:
                    raise
                # Uncertain owners keep their preimage and every pin. Other terminal
                # settlements can still make durable progress through this explicit path.
                continue
            for key in owner["reference_ids"]:
                del registry["references"][key]
            owner.update(sha256=actual_hash, revision=record["revision"], preimage=record, reference_ids=[])
            settled.append(owner_id)
        self._validate_owners(registry, observe=False)
        return tuple(settled)

    def reconcile_owners(self):
        """Settle verified terminal reservations together while retaining uncertain pins."""
        with self._admission():
            registry = self._registry(validate_owners=False)
            settled = self._settle_terminal_owners(registry)
            if settled:
                self._commit(registry)
            return settled

    def removal_reasons(self, sha256):
        runtime_stacks.digest(sha256)
        registry = self._registry()
        reasons = []
        if INTEGRATED_REFERENCE_KINDS != REFERENCE_KINDS:
            reasons.append("reference-integration-incomplete")
        for root in registry["stores"]:
            inventory = self._inventory(registry, root)
            if any(item["result"] in ("pending", "unknown") for item in inventory["operations"].values()):
                reasons.append("pending-operation")
        for reference_id, reference in registry["references"].items():
            _uuid(reference_id)
            runtime_stacks.fields(reference, "store generation sha256 kind owner_id")
            _uuid(reference["store"])
            _uuid(reference["generation"])
            runtime_stacks.digest(reference["sha256"])
            if reference["kind"] not in REFERENCE_KINDS:
                raise RunnerError("Unsupported reference")
            if reference["sha256"] == sha256:
                reasons.append(reference["kind"])
        # Registration integration owns profile reconciliation in M5.
        if registry["profiles"]:
            reasons.append("profile-reconciliation-required")
        return tuple(sorted(set(reasons)))

    def remove(self, generation):
        _uuid(generation)
        with self._admission(), maintenance.artifact_use(exclusive=True):
            inventory = self._inventory(self._registry())
            copy = inventory["copies"][generation]
            reasons = self.removal_reasons(copy["sha256"])
            if reasons:
                raise RunnerError("Remove download blocked: " + ", ".join(reasons))
            self._verify_copy(generation, copy)
            operation_id = str(uuid.uuid4())
            inventory["operations"][operation_id] = {
                "kind": "remove", "runner": "owned-download", "provider": copy["provider"],
                "sha256": copy["sha256"], "phase": "resolved", "result": "pending", "generation": generation,
                "reason": "",
            }
            self._save(inventory)
            source = self._copy_path(generation, copy).parent
            tombstone = self._copy_path(generation, copy, tombstone=True).parent
            withdrawn = dict(copy["root"])
            withdrawn["canonical_path"] = str(tombstone)
            withdrawn["filesystem_root"] = str(Path(withdrawn["filesystem_root"]).with_name(tombstone.name))
            copy["withdrawn_root"] = withdrawn
            self._save(inventory)
            paths.rename_no_replace(source, tombstone)
            _sync(self.root)
            copy["state"] = "tombstoned"
            inventory["operations"][operation_id]["phase"] = "tombstoned"
            self._save(inventory)
            try:
                self._finish_remove(inventory, operation_id)
            except Exception as error:
                if not (self.coordination / "transaction.json").exists():
                    authoritative = self._inventory(self._registry())
                    operation = authoritative["operations"][operation_id]
                    if operation["phase"] != "settled":
                        operation.update(result="unknown", reason=_failure_reason(error))
                        self._save(authoritative)
                raise
            return operation_id

    def _finish_remove(self, inventory, operation_id):
        operation = inventory["operations"][operation_id]
        generation = operation["generation"]
        copy = inventory["copies"][generation]
        tombstone = self._copy_path(generation, copy, tombstone=True).parent
        if not os.path.lexists(tombstone) and operation["phase"] != "cleanup-pending":
            raise RunnerError("Withdrawn generation disappeared before cleanup")
        copy["state"] = "cleanup-pending"
        operation["phase"] = "cleanup-pending"
        operation["reason"] = "cleanup-pending"
        self._save(inventory)
        if os.path.lexists(tombstone):
            _safe_directory(tombstone)
            current = runtime_filesystem.identity(tombstone)
            if copy["withdrawn_root"] is None or not runtime_filesystem.same_retained_root(
                copy["withdrawn_root"], current,
            ):
                raise RunnerError("Withdrawn generation directory changed")
            directory = os.open(tombstone, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(directory).st_ino != current["inode"]:
                    raise RunnerError("Withdrawn directory changed before cleanup admission")
                names = set(os.listdir(directory))
                if not names.issubset({copy["name"], *artifacts.COMPANIONS}):
                    raise RunnerError("Unexpected runner generation contents")
                for name in names:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if name == copy["name"]:
                        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        with os.fdopen(descriptor, "rb") as source:
                            before = os.fstat(source.fileno())
                            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                                    or before.st_uid != os.geteuid() or before.st_ino != copy["inode"]
                                    or before.st_size != copy["size"]
                                    or hashlib.file_digest(source, "sha256").hexdigest() != copy["sha256"]):
                                raise RunnerError("Withdrawn runner identity changed")
                            after = os.fstat(source.fileno())
                            if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                                raise RunnerError("Withdrawn runner changed while verifying")
                    elif not stat.S_ISLNK(info.st_mode) or os.readlink(name, dir_fd=directory) != copy["name"]:
                        raise RunnerError("Unexpected companion in withdrawn generation")
                for name in names:
                    os.unlink(name, dir_fd=directory)
                os.fsync(directory)
                if os.stat(tombstone.name, dir_fd=parent, follow_symlinks=False).st_ino != current["inode"]:
                    raise RunnerError("Withdrawn path changed during cleanup")
                os.rmdir(tombstone.name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
                os.close(directory)
        del inventory["copies"][generation]
        operation.update(phase="settled", result="succeeded", reason="removed-owned-bytes")
        self._save(inventory)


class DownloadTask:
    """One owned transfer with thread-safe cancellation and explicit join."""

    def __init__(self, store, runner, status=None):
        self.operation_id = str(uuid.uuid4())
        self.cancelled = threading.Event()
        self.error = None
        self.result = None

        def run():
            try:
                self.result = store.download(runner, operation_id=self.operation_id,
                                             cancel=self.cancelled, status=status)
            except Exception as error:
                self.error = error

        self.thread = threading.Thread(target=run, name="runner-download", daemon=False)

    def start(self):
        self.thread.start()
        return self

    def cancel(self):
        self.cancelled.set()

    def join(self, timeout=None):
        self.thread.join(timeout)
        return not self.thread.is_alive()
