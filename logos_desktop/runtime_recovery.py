"""Complete local generations and fail-closed recovery publication.

These points are independent of user-data backups. A returned prefix stays behind
the launch guard until reviewed offline inspection and explicit online resumption.
"""

import base64
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from . import artifacts, config_store, constants, maintenance, paths
from . import runtime_copies, runtime_filesystem, runtime_runners, runtime_stacks
from . import runtime_boundary, runtime_trials


MAX_RECORD = 4 * 1024 * 1024
CAPABILITY_MAX_AGE_NS = 5 * 60 * 1_000_000_000
# Resume requires a bound offline inspection and explicit retained-work acknowledgment.
ONLINE_RESUME_SUPPORTED = True
DEMONSTRATED_EXPORT_SUFFIXES = frozenset((".txt",))
_local = threading.local()
PHASES = (
    "preparing", "point-verified", "apply-intent", "selection-pending",
    "recipe-pending", "applied", "return-staging", "return-ready",
    "resume-pending", "withdraw-intent", "withdrawn", "publish-intent",
    "published", "config-pending", "returned", "conflict", "kept-current", "resume-intent", "resumed",
)


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise maintenance.RecoveryRequired("Invalid complete recovery identity")
    return value


def sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_mount_id(descriptor):
    fields = Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines()
    return next(int(line.split()[1]) for line in fields if line.startswith("mnt_id:"))


def check_export_directory(path, descriptor, identity):
    runtime_copies.checked_directory(path)
    current = runtime_filesystem.identity(path)
    held = os.fstat(descriptor)
    if (current != identity or os.readlink(f"/proc/self/fd/{descriptor}") != str(path)
            or (held.st_dev, held.st_ino, held.st_uid) !=
            (identity["device"], identity["inode"], identity["owner_uid"])
            or export_mount_id(descriptor) != identity["mount_id"]):
        raise maintenance.RecoveryRequired("Export directory changed. Preserve the copy and recheck its location")


@contextmanager
def export_directory(path):
    """Hold no-follow ancestry and bind mount facts to the opened directory."""
    path = runtime_copies.checked_directory(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            shared_temporary = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if (info.st_uid not in (0, os.geteuid())
                    or (info.st_mode & 0o022 and not shared_temporary)):
                raise maintenance.RecoveryRequired("Export ancestry is writable by another account")
        identity = runtime_filesystem.identity(path)
        check_export_directory(path, descriptor, identity)
        yield descriptor, identity
    finally:
        os.close(descriptor)


def write(path, value, *, checkpoint=lambda boundary: None):
    """Publish a single durable record, leaving ambiguous outcomes guarded."""
    content = runtime_stacks.canonical(value)
    if len(content) > MAX_RECORD:
        raise maintenance.RecoveryRequired("Complete recovery record exceeds its bound")
    path = Path(path)
    runtime_copies.checked_directory(path.parent)
    checkpoint("before-record:" + path.name)
    descriptor, name = tempfile.mkstemp(prefix=".recovery-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            checkpoint("before-file-fsync:" + path.name)
            os.fsync(output.fileno())
            checkpoint("after-file-fsync:" + path.name)
        checkpoint("before-record-rename:" + path.name)
        os.replace(temporary, path)
        checkpoint("after-record-rename:" + path.name)
        sync(path.parent)
        checkpoint("after-record:" + path.name)
    finally:
        temporary.unlink(missing_ok=True)


def read(path):
    path = Path(path)
    runtime_copies.checked_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise maintenance.RecoveryRequired("Unsafe complete recovery record")
        return runtime_stacks.decode(source.read(MAX_RECORD + 1), MAX_RECORD)


def registration_path(context):
    key = hashlib.sha256(os.fsencode(context.prefix)).hexdigest()
    return Path(constants.COORDINATION_DIR) / "operations" / key / "complete-recovery.json"


def registration(context):
    path = registration_path(context)
    if not os.path.lexists(path):
        return None
    value = read(path)
    runtime_stacks.fields(value, "schema prefix install root root_identity id secondary")
    if value["schema"] != "complete-recovery-registration-1" or value["prefix"] != str(context.prefix):
        raise maintenance.RecoveryRequired("Foreign complete recovery registration")
    if (not isinstance(value["install"], str) or not Path(value["install"]).is_absolute()
            or context.prefix.parent != Path(value["install"]) / "data"
            or (context.install is not None and value["install"] != str(context.install))):
        raise maintenance.RecoveryRequired("Complete recovery installation identity does not match")
    identifier(value["id"])
    root = Path(value["root"])
    if not root.is_absolute() or root != root.resolve() or not isinstance(value["secondary"], bool):
        raise maintenance.RecoveryRequired("Invalid complete recovery location")
    runtime_copies.checked_directory(root)
    if not runtime_filesystem.same_retained_root(value["root_identity"], runtime_filesystem.identity(root)):
        raise maintenance.RecoveryRequired("Complete recovery storage changed. Preserve it and recheck its mount")
    return value


def check_admission(context):
    """Protect every supported entry point, including callers using recovery=True."""
    value = registration(context)
    if value is None:
        return
    allowed = getattr(_local, "registration", None)
    if allowed == value["id"] and not value["secondary"]:
        return
    if value["secondary"]:
        raise maintenance.RecoveryRequired("Retained newer generation. Inspect an offline copy through recovery")
    journal = read(Path(value["root"]) / "journal.json")
    validate_journal(journal, value)
    if journal["phase"] not in ("applied", "kept-current", "resumed"):
        raise maintenance.RecoveryRequired(
            "Complete recovery or offline inspection is pending. Use recovery status before launching"
        )
    if journal["phase"] == "resumed":
        marker = read(Path(value["root"]) / "marker.json")
        if os.path.lexists(marker["path"]):
            raise maintenance.RecoveryRequired("Online resume needs durable marker cleanup. Recheck resume status")


def owns_marker(path, context):
    """Allow only the exact operation owner to traverse its compatibility marker."""
    value = registration(context)
    if value is None or getattr(_local, "registration", None) != value["id"]:
        return False
    try:
        marker = read(path)
    except FileNotFoundError:
        return False
    return marker == {"schema": "runtime-admission-1", "operation_id": value["id"],
                      "directory": value["root"]}


def operation_owned(context):
    value = registration(context)
    return value is not None and not value["secondary"] and getattr(_local, "registration", None) == value["id"]


def owns_activation(path, context):
    if not operation_owned(context):
        return False
    service = Recovery(context)
    journal = service.status()
    point = service.verify_point(journal["point"])
    service.validate_publications(journal, point)
    activation = journal["activation"]
    return (activation is not None and activation["path"] == str(path)
            and read(path) == activation["record"])


def inspection_facts(boundary):
    worker = Path(runtime_trials.__file__).with_name("runtime_trial_worker.py")
    wayland = boundary.wayland.stat()
    server = shutil.which("Xwayland")
    if server is None:
        raise maintenance.RecoveryRequired("Restore XWayland before rechecking recovery inspection")
    return {"worker": artifacts.regular_hash(worker), "boundary": artifacts.regular_hash(runtime_boundary.__file__),
            "controller": artifacts.regular_hash(__file__), "bwrap": artifacts.regular_hash(boundary.bwrap),
            "xwayland": artifacts.regular_hash(server), "dependencies": [list(item) for item in boundary.dependencies],
            "boot": runtime_trials.identity(os.getpid())["boot_id"], "kernel": os.uname().release,
            "display": [str(boundary.wayland), wayland.st_dev, wayland.st_ino]}


def validate_journal(value, registered):
    runtime_stacks.fields(value, "schema id revision phase result reason point stage newer stage_identity "
                          "newer_identity gate configs selection stages activation runners inspection "
                          "resume capability")
    if (value["schema"] != "complete-recovery-journal-1" or value["id"] != registered["id"]
            or value["phase"] not in PHASES or type(value["revision"]) is not int or value["revision"] < 1
            or value["result"] not in ("pending", "succeeded", "failed", "unknown")):
        raise maintenance.RecoveryRequired("Unknown complete recovery journal")
    if value["point"] is not None:
        identifier(value["point"])
    if type(value["configs"]) is not list or len(value["configs"]) > 8:
        raise maintenance.RecoveryRequired("Invalid configuration publication graph")
    if value["phase"] in ("apply-intent", "selection-pending", "recipe-pending", "applied"):
        if value["point"] is None or value["capability"] is None or len(value["runners"]) < 2:
            raise maintenance.RecoveryRequired("Apply journal has no complete recovery or runner graph")
    if value["phase"] in ("recipe-pending", "applied") and (
        value["activation"] is None or not value["selection"]
    ):
        raise maintenance.RecoveryRequired("Recipe execution has no recorded selection publication")
    prefix = Path(registered["prefix"])
    if value["newer"] is not None and value["newer"] != str(prefix.with_name(".runtime-newer-" + value["point"])):
        raise maintenance.RecoveryRequired("Foreign return publication path")
    if type(value["stages"]) is not list or len(value["stages"]) > 64:
        raise maintenance.RecoveryRequired("Invalid return staging history")
    for stage in value["stages"]:
        path = Path(stage)
        start = ".runtime-old-" + value["point"] + "-"
        if path.parent != prefix.parent or not path.name.startswith(start):
            raise maintenance.RecoveryRequired("Foreign staging path")
        identifier(path.name[len(start):])
    if value["stage"] is not None and value["stage"] not in value["stages"]:
        raise maintenance.RecoveryRequired("Unregistered return staging path")
    if value["phase"] in ("resume-pending", "withdraw-intent", "withdrawn", "publish-intent",
                           "published", "config-pending", "returned") and value["gate"] != "online-resume-pending":
        raise maintenance.RecoveryRequired("Restored generation has no durable online guard")
    if value["phase"] in ("applied", "kept-current") and (
        value["result"] != "succeeded" or value["gate"] is not None
    ):
        raise maintenance.RecoveryRequired("Contradictory ordinary launch state")
    if value["phase"] == "resumed" and (value["result"] != "succeeded" or value["gate"] != "online-resumed"
                                          or value["resume"] is None or value["inspection"] is None):
        raise maintenance.RecoveryRequired("Online resume has no verified confirmation")
    if value["phase"] in ("resume-intent", "resumed"):
        record = value["resume"]
        if not isinstance(record, dict):
            raise maintenance.RecoveryRequired("Online resume intent has no acknowledged record")
        try:
            runtime_stacks.fields(record, "confirmation retained_work_acknowledged confirmed")
            confirmation = record["confirmation"]
            if not isinstance(confirmation, dict):
                raise maintenance.RecoveryRequired("Online resume confirmation is invalid")
            runtime_stacks.fields(confirmation, "point active retained inspection message")
            confirmed = datetime.fromisoformat(record["confirmed"])
            runtime_stacks.fields(value["inspection"], "id sha256")
            identifier(value["inspection"]["id"])
            runtime_stacks.digest(value["inspection"]["sha256"])
        except (TypeError, ValueError) as error:
            raise maintenance.RecoveryRequired("Online resume acknowledgment record is invalid") from error
        gate = "online-resumed" if value["phase"] == "resumed" else "online-resume-pending"
        if (record["retained_work_acknowledged"] is not True or confirmed.tzinfo is None or value["gate"] != gate
                or confirmation["point"] != value["point"] or confirmation["active"] != registered["prefix"]
                or confirmation["retained"] != value["newer"] or confirmation["inspection"] != value["inspection"]
                or value["inspection"] is None or not isinstance(confirmation["message"], str)
                or not confirmation["message"]):
            raise maintenance.RecoveryRequired("Online resume acknowledgment does not match this generation")
    elif value["resume"] is not None:
        raise maintenance.RecoveryRequired("Unexpected online resume record before intent")


def snapshot(path):
    path = Path(path)
    runtime_copies.checked_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "kind": "absent", "content": None, "mode": None}
    if info.st_uid != os.geteuid():
        raise maintenance.RecoveryRequired("Recovery target belongs to another account")
    if stat.S_ISLNK(info.st_mode):
        return {"path": str(path), "kind": "link", "content": os.readlink(path), "mode": None}
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024
            or info.st_mode & 0o022):
        raise maintenance.RecoveryRequired("Unsupported recovery configuration target")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        content = source.read(1024 * 1024 + 1)
        if (runtime_copies._signature(before) != runtime_copies._signature(os.fstat(source.fileno()))
                or runtime_copies._signature(before) != runtime_copies._signature(info)):
            raise maintenance.RecoveryRequired("Recovery configuration changed while reading")
    return {"path": str(path), "kind": "file", "content": base64.b64encode(content).decode("ascii"),
            "mode": stat.S_IMODE(info.st_mode)}


def validate_snapshot(value):
    runtime_stacks.fields(value, "path kind content mode")
    path = Path(value["path"])
    if not path.is_absolute() or ".." in path.parts:
        raise maintenance.RecoveryRequired("Invalid recovery configuration path")
    if value["kind"] == "file":
        if type(value["mode"]) is not int or not 0 <= value["mode"] <= 0o777 or value["mode"] & 0o022:
            raise maintenance.RecoveryRequired("Invalid recovery file mode")
        content = base64.b64decode(value["content"], validate=True)
        if len(content) > 1024 * 1024 or base64.b64encode(content).decode("ascii") != value["content"]:
            raise maintenance.RecoveryRequired("Invalid recovery file preimage")
    elif value["kind"] == "link":
        if not isinstance(value["content"], str) or not value["content"] or "\0" in value["content"]:
            raise maintenance.RecoveryRequired("Invalid recovery link preimage")
        if value["mode"] is not None:
            raise maintenance.RecoveryRequired("Invalid recovery link metadata")
    elif value["kind"] != "absent" or value["content"] is not None or value["mode"] is not None:
        raise maintenance.RecoveryRequired("Unknown recovery preimage kind")


def activation_snapshot(value):
    if "link" in value:
        runtime_stacks.fields(value, "path link")
        return {"path": value["path"], "kind": "link", "content": value["link"], "mode": None}
    if value.get("absent") is True:
        runtime_stacks.fields(value, "path absent")
        return {"path": value["path"], "kind": "absent", "content": None, "mode": None}
    runtime_stacks.fields(value, "path content mode")
    return {"path": value["path"], "kind": "file", "content": value["content"], "mode": value["mode"]}


def runtime_preimage(document, install):
    for key in ("WINESERVER_EXE", "CUSTOMBINPATH", "WINEDLLOVERRIDES", "SELECTED_APPIMAGE_FILENAME"):
        if document.get(key) is not None:
            raise maintenance.RecoveryRequired("Runtime overrides need a modeled complete recovery layout")
    bindir = config_store.absolute_path(document.get("APPDIR_BINDIR") or install / "data/bin", base=install)
    link = document.get("APPIMAGE_LINK_SELECTION_NAME") or "selected_wine.AppImage"
    if bindir != install / "data/bin" or link != "selected_wine.AppImage":
        raise maintenance.RecoveryRequired("Custom companion layouts need a modeled complete recovery layout")
    binary = config_store.absolute_path(document["wine_binary"], base=install).resolve(strict=True)
    return {"binary": str(binary), "code": document.get("wine_binary_code"),
            "bindir": str(bindir), "link": link}


def restored_snapshot(saved, published, current):
    if current == saved:
        return saved
    if saved["kind"] == published["kind"] == current["kind"] == "file":
        before = config_store.decode(base64.b64decode(saved["content"]), saved["path"])
        after = config_store.decode(base64.b64decode(published["content"]), saved["path"])
        stored = config_store.decode(base64.b64decode(current["content"]), saved["path"])
        changed = {key for key in before.keys() | after.keys()
                   if (key in before) != (key in after) or before.get(key) != after.get(key)}
        for modern, legacy in config_store.ALIASES.items():
            if modern in changed or legacy in changed:
                changed.update((modern, legacy))
        restored, conflicts = config_store._restore_fields(
            stored, {key: before[key] for key in changed if key in before},
            {key: after[key] for key in changed if key in after},
        )
        if conflicts:
            raise maintenance.RecoveryRequired("Later runtime configuration conflicts: " + ", ".join(conflicts))
        return dict(current, content=base64.b64encode(config_store.encode(restored)).decode("ascii"))
    if current == published:
        return saved
    raise maintenance.RecoveryRequired("Later runtime link conflicts with complete return")


def publish_snapshot(expected, proposed):
    path = Path(expected["path"])
    if proposed["path"] != expected["path"] or snapshot(path) != expected:
        raise maintenance.RecoveryRequired(f"Later configuration preserved at {path}. Resolve the conflict and recheck")
    if proposed["kind"] == "file":
        with config_store.Target(path) as target:
            target.stage(base64.b64decode(proposed["content"], validate=True), mode=proposed["mode"])
            target.publish()
    elif proposed["kind"] == "link":
        temporary = path.with_name(".recovery-link-" + str(uuid.uuid4()))
        try:
            temporary.symlink_to(proposed["content"])
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    elif proposed["kind"] == "absent":
        path.unlink(missing_ok=True)
    else:
        raise maintenance.RecoveryRequired("Unknown recovery target kind")
    sync(path.parent)
    if snapshot(path) != proposed:
        raise maintenance.RecoveryRequired("Published configuration did not verify")


class Recovery:
    """One selected installation's complete recovery store and phase journal."""

    def __init__(self, context, *, checkpoint=lambda boundary: None):
        self.context = context
        self.checkpoint = checkpoint
        self.registered = registration(context)
        if self.registered is not None:
            self.root = Path(self.registered["root"])
            if context.install is None and not self.registered["secondary"]:
                self.context = maintenance.Context(context.prefix, Path(self.registered["install"]),
                                                   context.product or "Logos")
        else:
            self.root = None

    @classmethod
    def create(cls, context, root, *, checkpoint=lambda boundary: None):
        if context.install is None or context.prefix != context.install / "data/wine64_bottle":
            raise runtime_copies.CopyError("Complete recovery requires a managed prefix", "unsupported-external-prefix")
        runtime_copies.checked_directory(context.install)
        runtime_copies.layout(context.prefix)
        root = Path(root)
        runtime_copies.checked_directory(root.parent)
        if not paths.disjoint(root, context.install):
            raise maintenance.RecoveryRequired("Complete recovery storage overlaps the installation")
        instance = cls(context, checkpoint=checkpoint)
        if instance.registered is not None:
            raise maintenance.RecoveryRequired("Complete recovery already registered. Inspect its status")
        with maintenance.Lease(context, exclusive=True, prefix_only=True).entered():
            maintenance.assert_quiescent(context)
            root.mkdir(mode=0o700)
            sync(root.parent)
            runtime_copies.layout(root)
            if not runtime_copies.physical_disjoint(runtime_filesystem.identity(root),
                                                   runtime_filesystem.identity(context.install)):
                raise maintenance.RecoveryRequired("Physical recovery storage overlaps the installation")
            operation_id = str(uuid.uuid4())
            registered = {"schema": "complete-recovery-registration-1", "prefix": str(context.prefix),
                          "install": str(context.install), "root": str(root),
                          "root_identity": runtime_filesystem.identity(root), "id": operation_id, "secondary": False}
            journal = {"schema": "complete-recovery-journal-1", "id": operation_id, "revision": 1,
                       "phase": "preparing", "result": "pending", "reason": "", "point": None,
                       "stage": None, "newer": None, "stage_identity": None, "newer_identity": None,
                       "gate": None, "configs": [], "selection": [], "stages": [], "activation": None,
                       "runners": [], "inspection": None, "resume": None, "capability": None}
            write(root / "journal.json", journal, checkpoint=checkpoint)
            # Publish reconciliation inputs before the stable admission pointer.
            marker = {"schema": "runtime-admission-1", "operation_id": operation_id, "directory": str(root)}
            write(root / "marker.json", {"path": str(context.pending), "marker": marker}, checkpoint=checkpoint)
            write(registration_path(context), registered, checkpoint=checkpoint)
            instance.root, instance.registered = root, registered
            descriptor = os.open(context.pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(runtime_stacks.canonical(marker))
                output.flush()
                os.fsync(output.fileno())
            sync(context.state)
        return instance

    def status(self):
        if self.registered is None:
            raise maintenance.RecoveryRequired("No complete recovery store is registered")
        current = registration(self.context)
        if current != self.registered or current["secondary"]:
            raise maintenance.RecoveryRequired("Selected complete recovery registration changed")
        value = read(self.root / "journal.json")
        validate_journal(value, current)
        return value

    def phase(self, value, phase, *, result="pending", reason=""):
        self.checkpoint("before-phase:" + phase)
        value.update(phase=phase, result=result, reason=reason, revision=value["revision"] + 1)
        validate_journal(value, self.registered)
        write(self.root / "journal.json", value, checkpoint=self.checkpoint)
        self.checkpoint("after-phase:" + phase)

    @contextmanager
    def admitted(self):
        self.status()
        if getattr(_local, "registration", None) is not None:
            raise maintenance.MaintenanceBusy("A complete recovery operation is already active")
        _local.registration = self.registered["id"]
        try:
            with maintenance.Lease(self.context, exclusive=True, recovery=True, prefix_only=True).entered():
                self.status()
                if os.path.lexists(self.context.state_path / "restore.json"):
                    raise maintenance.RecoveryRequired(
                        "A foreign user-data restore journal needs separate reconciliation"
                    )
                from . import runtime

                activation = runtime.selection_state(self.context.install, create=False) / "runtime.json"
                if os.path.lexists(activation) and not owns_activation(activation, self.context):
                    raise maintenance.RecoveryRequired("A foreign runtime activation journal remains protected")
                maintenance.assert_quiescent(self.context)
                yield
        finally:
            _local.registration = None

    def pin(self, store, generation, kind):
        """Use separate immutable owner records for each physical dependency."""
        owner_id = str(uuid.uuid4())
        path = self.root / ("owner-" + owner_id + ".json")
        owner = {"schema": "runtime-runner-owner-2", "record": kind, "id": owner_id,
                 "revision": 1, "phase": "pending", "runner_generations": [generation]}
        write(path, owner, checkpoint=self.checkpoint)
        reference = store.pin(generation, kind=kind, owner_path=path, owner_sha256=artifacts.regular_hash(path))
        return {"store": str(store.root), "coordination": str(store.coordination), "generation": generation,
                "owner": str(path), "reference": reference}

    def verify_pin(self, reference):
        runtime_stacks.fields(reference, "store coordination generation owner reference")
        identifier(reference["generation"])
        identifier(reference["reference"])
        store = runtime_runners.RunnerStore(reference["store"], reference["coordination"])
        registry = store._registry()
        pin = registry["references"].get(reference["reference"])
        if pin is None or pin["generation"] != reference["generation"]:
            raise maintenance.RecoveryRequired("Complete recovery runner pin is missing")
        owner = registry["owners"][pin["owner_id"]]
        if owner["path"] != reference["owner"]:
            raise maintenance.RecoveryRequired("Complete recovery runner owner changed")
        return next(item for item in store.inventory() if item.generation == reference["generation"])

    def capture(self, *, store, generation, profile, portable, links, product_build, effective):
        """Capture old bytes without executing Wine or changing runtime selection."""
        with self.admitted():
            journal = self.status()
            if journal["phase"] in ("kept-current", "applied", "resumed"):
                index = self.point_index()
                if journal["point"] is not None:
                    history_path = self.root / ("history-" + journal["point"] + ".json")
                    write(history_path, journal, checkpoint=self.checkpoint)
                    index["points"][journal["point"]]["history"] = artifacts.regular_hash(history_path)
                    write(self.root / "points.json", index, checkpoint=self.checkpoint)
                journal.update(point=None, stage=None, newer=None, stage_identity=None, newer_identity=None,
                               gate=None, configs=[], selection=[], stages=[], activation=None, runners=[],
                               inspection=None, resume=None, capability=None)
                self.phase(journal, "preparing")
                self._ensure_marker()
            if journal["phase"] != "preparing" or journal["point"] is not None:
                raise maintenance.RecoveryRequired("Inspect the existing complete recovery preparation")
            runtime_stacks.text(product_build)
            from . import utils

            executable = utils.find_installed_product("Logos", str(self.context.prefix))
            if (executable is None or not Path(executable).resolve().is_relative_to(self.context.prefix)
                    or utils.get_current_logos_version(str(Path(executable).parent)) != product_build):
                raise maintenance.RecoveryRequired("Installed product identity does not match the recovery point")
            targets = [Path(profile), Path(portable), *map(Path, links)]
            expected_portable = self.context.install / constants.PORTABLE_CONFIG_NAME
            expected_links = self.context.install / "data/bin"
            if (Path(portable) != expected_portable or len(set(targets)) != len(targets)
                    or set(map(Path, links)) != {expected_links / name for name in
                                                ("selected_wine.AppImage", *artifacts.COMPANIONS)}):
                raise maintenance.RecoveryRequired("Complete recovery configuration layout is unsupported")
            preimages = [snapshot(path) for path in targets]
            if any(saved["kind"] != "file" for saved in preimages[:2]):
                raise maintenance.RecoveryRequired("Recovery needs saved profile and portable configuration files")
            document = config_store.canonical(config_store.decode(base64.b64decode(preimages[0]["content"]), profile),
                                              profile)
            if config_store.context(document) != self.context or config_store.context(effective) != self.context:
                raise maintenance.RecoveryRequired("Saved and effective recovery contexts differ")
            profile_identity = runtime_trials.profile_snapshot(profile)
            runtime_trials.verify_profile(profile_identity, self.context)
            runtime_trials.register_profile(profile_identity, self.context)
            selected = next(item for item in store.inventory() if item.generation == generation)
            selected_path = config_store.absolute_path(document["wine_binary"], base=self.context.install)
            if selected_path.resolve(strict=True) != selected.path:
                raise maintenance.RecoveryRequired("Old selection must bind the exact managed runner")
            resolved_effective = config_store.canonical(effective, profile)
            if runtime_preimage(resolved_effective, self.context.install) != runtime_preimage(
                document, self.context.install,
            ):
                raise maintenance.RecoveryRequired("Effective runtime differs from the saved managed selection")
            # Diagnostic trials permit private empty Documents mappings. Recovery
            # refuses every external mutable link instead of omitting its data.
            for item in runtime_copies.entries(self.context.prefix):
                if item["kind"] != "symlink":
                    continue
                relative = os.fsdecode(base64.b64decode(item["path"]))
                target = os.fsdecode(base64.b64decode(item["target"]))
                resolved = Path(os.path.normpath(self.context.prefix / Path(relative).parent / target))
                if not resolved.is_relative_to(self.context.prefix) and (relative, target) != ("dosdevices/z:", "/"):
                    raise runtime_copies.CopyError("Recovery cannot omit external mutable folders",
                                                   "unsupported-external-layout")
            index = self.point_index()
            if not os.path.lexists(self.root / "points.json"):
                write(self.root / "points.json", index, checkpoint=self.checkpoint)
            point_id = str(uuid.uuid4())
            directory = self.root / ("point-" + point_id)
            directory.mkdir(mode=0o700)
            sync(self.root)
            reference = self.pin(store, generation, "return-point")
            receipt = runtime_copies.copy(self.context.prefix, directory / "prefix",
                                          checkpoint=lambda: self.checkpoint("point-copy"))
            if preimages != [snapshot(path) for path in targets]:
                raise maintenance.RecoveryRequired("Configuration changed during complete recovery capture")
            point = {"schema": "complete-prefix-point-1", "id": point_id, "operation": self.registered["id"],
                     "created": datetime.now(timezone.utc).isoformat(), "product_build": product_build,
                     "source": receipt["source"], "copy": receipt["copy"], "manifest": receipt["manifest"],
                     "preimages": preimages, "effective": resolved_effective, "runtime": reference,
                     "runner_sha256": selected.sha256, "wine_user": runtime_trials.source_account(self.context.prefix)}
            write(directory / "point.json", point, checkpoint=self.checkpoint)
            journal["point"] = point_id
            index = self.point_index()
            index["points"][point_id] = {"sha256": artifacts.regular_hash(directory / "point.json"),
                                         "history": None, "cleanup": None}
            write(self.root / "points.json", index, checkpoint=self.checkpoint)
            self.verify_point(point_id)
            self.phase(journal, "point-verified", result="succeeded")
            return point_id

    def point_index(self):
        path = self.root / "points.json"
        if not os.path.lexists(path):
            if any(self.root.glob("point-*")):
                raise maintenance.RecoveryRequired("Complete point index is missing. Preserve all point directories")
            return {"schema": "complete-point-index-1", "points": {}, "retired": {}}
        value = read(path)
        runtime_stacks.fields(value, "schema points retired")
        if (value["schema"] != "complete-point-index-1" or type(value["points"]) is not dict
                or type(value["retired"]) is not dict or len(value["retired"]) > 256):
            raise maintenance.RecoveryRequired("Unknown complete point index")
        if len(value["points"]) > 64:
            raise maintenance.RecoveryRequired("Too many complete points. Review eligible recovery cleanup")
        for point_id, item in value["points"].items():
            identifier(point_id)
            runtime_stacks.fields(item, "sha256 history cleanup")
            runtime_stacks.digest(item["sha256"])
            if item["history"] is not None:
                runtime_stacks.digest(item["history"])
                if artifacts.regular_hash(self.root / ("history-" + point_id + ".json")) != item["history"]:
                    raise maintenance.RecoveryRequired("Complete point history changed")
            if item["cleanup"] is not None:
                runtime_stacks.digest(item["cleanup"])
        for point_id, plan in value["retired"].items():
            identifier(point_id)
            runtime_stacks.digest(plan)
        return value

    def _point_record(self, point_id):
        identifier(point_id)
        directory = self.root / ("point-" + point_id)
        runtime_copies.checked_directory(directory)
        binding = self.point_index()["points"].get(point_id)
        if binding is None or artifacts.regular_hash(directory / "point.json") != binding["sha256"]:
            raise maintenance.RecoveryRequired("Complete recovery point binding changed")
        if binding["cleanup"] is not None:
            raise maintenance.RecoveryRequired("Complete recovery point cleanup is pending")
        point = read(directory / "point.json")
        runtime_stacks.fields(point, "schema id operation created product_build source copy manifest preimages "
                              "effective runtime runner_sha256 wine_user")
        if (point["schema"] != "complete-prefix-point-1" or point["id"] != point_id
                or point["operation"] != self.registered["id"]):
            raise maintenance.RecoveryRequired("Foreign complete recovery point")
        runtime_runners._directory_identity(point["source"])
        selected = config_store.context(point["effective"])
        if (point["source"]["canonical_path"] != str(self.context.prefix)
                or selected is None or selected.prefix != self.context.prefix
                or selected.install != self.context.install
                or selected.product != "Logos" or self.context.product not in (None, "Logos")):
            raise maintenance.RecoveryRequired("Complete point belongs to another installation or prefix")
        if not isinstance(point["preimages"], list) or not 2 <= len(point["preimages"]) <= 8:
            raise maintenance.RecoveryRequired("Invalid recovery preimage graph")
        for saved in point["preimages"]:
            validate_snapshot(saved)
        if len({saved["path"] for saved in point["preimages"]}) != len(point["preimages"]):
            raise maintenance.RecoveryRequired("Duplicate recovery preimage path")
        return point

    def verify_point(self, point_id):
        point = self._point_record(point_id)
        directory = self.root / ("point-" + point_id)
        current = runtime_copies.layout(directory / "prefix")
        if not runtime_filesystem.same_retained_root(point["copy"], current):
            raise maintenance.RecoveryRequired("Complete recovery prefix identity changed")
        manifest = runtime_copies.manifest(directory / "prefix", logical_root=self.context.prefix)
        if any(getattr(manifest, key) != point["manifest"][key] for key in
               ("sha256", "entries", "logical_bytes", "schema", "format")):
            raise maintenance.RecoveryRequired("Complete recovery prefix failed byte verification")
        if self.verify_pin(point["runtime"]).sha256 != point["runner_sha256"]:
            raise maintenance.RecoveryRequired("Complete recovery runtime changed")
        return point

    def original_prefix_identity(self):
        journal = self.status()
        if (not operation_owned(self.context) or maintenance.current_context() != self.context
                or not maintenance.installation_owned() or journal["phase"] != "recipe-pending"):
            raise maintenance.RecoveryRequired("Original mutation requires its admitted recipe owner")
        point = self._point_record(journal["point"])
        current = runtime_copies.layout(self.context.prefix)
        if not runtime_filesystem.same_retained_root(point["source"], current):
            raise maintenance.RecoveryRequired("Original prefix identity changed before recipe mutation")
        maintenance.assert_quiescent(self.context)
        return current

    def validate_publications(self, journal, point):
        allowed = {saved["path"]: saved for saved in point["preimages"]}
        selected = {}
        for item in journal["selection"]:
            validate_snapshot(item)
            if item["path"] not in allowed or item["path"] in selected:
                raise maintenance.RecoveryRequired("Foreign or duplicate runtime selection target")
            selected[item["path"]] = item
        if selected and set(selected) != set(allowed):
            raise maintenance.RecoveryRequired("Incomplete runtime publication graph")
        seen = set()
        for item in journal["configs"]:
            runtime_stacks.fields(item, "previous next")
            for saved in item.values():
                validate_snapshot(saved)
            path = item["previous"]["path"]
            if path not in allowed or path in seen or item["next"]["path"] != path:
                raise maintenance.RecoveryRequired("Foreign or duplicate return configuration target")
            expected = restored_snapshot(allowed[path], selected.get(path, allowed[path]), item["previous"])
            if item["next"] != expected:
                raise maintenance.RecoveryRequired("Return configuration projection changed")
            seen.add(path)
        if journal["stage"] is not None and seen != set(allowed):
            raise maintenance.RecoveryRequired("Incomplete return configuration projection")
        activation = journal["activation"]
        if activation is not None:
            runtime_stacks.fields(activation, "path record")
            key = hashlib.sha256(os.fsencode(self.context.install)).hexdigest()
            path = Path(activation["path"])
            if (not path.is_absolute() or path.parts[-3:] != ("runtime-selection", key, "runtime.json")
                    or path != path.resolve()):
                raise maintenance.RecoveryRequired("Foreign activation journal path")
            record = activation["record"]
            if (record.get("install") != str(self.context.install)
                    or {item["path"]: activation_snapshot(item) for item in record["previous"]} != allowed
                    or {item["path"]: activation_snapshot(item) for item in record["next"]} != selected
                    or len(record["previous"]) != len(allowed) or len(record["next"]) != len(allowed)):
                raise maintenance.RecoveryRequired("Foreign activation publication graph")

    def return_confirmation(self):
        journal = self.status()
        point = self.verify_point(journal["point"])
        newer = self.context.prefix.with_name(".runtime-newer-" + point["id"])
        return {
            "point": point["id"], "created": point["created"], "product_build": point["product_build"],
            "runtime_sha256": point["runner_sha256"], "retained_newer": str(newer),
            "message": (
                f"Restore the complete local Logos installation from {point['created']}? "
                f"All local application data returns to that point. Newer local work stays at {newer}. "
                "Cloud changes already synchronized are not undone. Inspect offline before separately resuming online."
            ),
        }

    def apply(self, app, prepared, *, store, generation, host, capability=None,
              without_successful_trial=False, experimental=False):
        """Apply accepted inputs to the original only after fresh old-stack proof."""
        from . import compatibility, runtime, runtime_recipes

        with self.admitted():
            journal = self.status()
            if journal["phase"] != "point-verified":
                raise maintenance.RecoveryRequired("Apply requires a freshly verified complete recovery point")
            if maintenance.Context.from_app(app, prompt=False) != self.context:
                raise maintenance.RecoveryRequired("Selected application context changed before apply")
            for name in ("wine_appimage_path", "wineserver_binary", "wine_dll_overrides", "custom_binary_path"):
                if getattr(app.conf._overrides, name, None) is not None:
                    raise maintenance.RecoveryRequired("Effective runtime overrides need a modeled recovery layout")
            prepared.check()
            point = self.verify_point(journal["point"])
            actual = config_store.apply_changes(
                app.conf._raw._as_dict(), getattr(app.conf._overrides, "runtime_selection", {}) or {},
                point["preimages"][0]["path"],
            )
            if runtime_preimage(actual, self.context.install) != runtime_preimage(
                point["effective"], self.context.install,
            ):
                raise maintenance.RecoveryRequired("Effective old runtime changed. Recheck the selected invocation")
            old_runner = next((item for item in prepared.catalog.runners.values()
                               if item.sha256 == point["runner_sha256"]), None)
            if old_runner is None:
                raise maintenance.RecoveryRequired(
                    "This old managed runtime needs a reviewed recoverable version model"
                )
            old_version = compatibility.parse_wine_version(dict(old_runner.companions)["wine"]).version
            selected_runner = prepared.catalog.runners[prepared.stack.runner_id]
            new_version = compatibility.parse_wine_version(dict(selected_runner.companions)["wine"]).version
            if new_version < old_version:
                raise maintenance.RecoveryRequired(
                    "Apply cannot downgrade Wine on this prefix. "
                    "Keep current or return a verified complete older generation"
                )
            current = runtime_copies.manifest(self.context.prefix)
            if (current.sha256 != point["manifest"]["sha256"]
                    or current.observation_sha256 != point["manifest"]["observation_sha256"]):
                raise maintenance.RecoveryRequired(
                    "Original changed after point capture. Create a fresh recovery point"
                )
            proof = journal["capability"]
            if proof is None or (capability is not None and capability != proof):
                raise maintenance.RecoveryRequired("Inspect an unmodified old-prefix copy before applying")
            self.verify_capability(proof, point)
            if without_successful_trial is not True:
                raise maintenance.RecoveryRequired(
                    "This apply service requires the explicit apply-without-successful-trial choice"
                )
            observed = runtime_recipes.observe_prefix_settings(self.context.prefix)
            prospective_settings = dict(observed)
            # Initialize only a missing global version. Existing overrides must match.
            initialize_windows = (type(observed.get("windows-version")) is str
                                  and observed["windows-version"] == "absent"
                                  and prepared.recipe.settings.get("windows-version") == "win10")
            if initialize_windows:
                prospective_settings["windows-version"] = "win10"
            if any(prospective_settings.get(key) != value or type(prospective_settings.get(key)) is not type(value)
                   for key, value in prepared.recipe.settings.items()):
                raise maintenance.RecoveryRequired("Custom Wine settings preserved. Choose a matching recipe")
            result = runtime_stacks.assess(
                prepared.catalog, prepared.stack, product=self.context.product, product_build=point["product_build"],
                host=host, settings=prospective_settings,
                dependencies={item.dependency.role: item.artifact.sha256 for item in prepared.dependencies},
                availability="cached", runner_sha256=prepared.runner.sha256,
            )
            allowed_experimental = experimental is True and result.evidence == "experimental"
            if not result.selectable and not allowed_experimental:
                raise maintenance.RecoveryRequired("Candidate apply refused: " + result.reason)
            selected = next(item for item in store.inventory() if item.generation == generation)
            if selected.path != prepared.runner.path or selected.sha256 != prepared.runner.sha256:
                raise maintenance.RecoveryRequired("Candidate must bind the exact managed runner copy")
            for saved in point["preimages"]:
                if snapshot(saved["path"]) != saved:
                    raise maintenance.RecoveryRequired(
                        "Configuration changed after recovery capture. Recheck before apply"
                    )
            runtime_copies.require_capacity(self.context.prefix.parent, current.logical_bytes, current.entries)
            journal["runners"] = [self.pin(store, generation, "apply"),
                                  self.pin(store, generation, "pending-journal")]
            initialization_reason = "Initialize absent global Windows version to win10" if initialize_windows else ""
            self.phase(journal, "apply-intent", reason=initialization_reason)
            try:
                with (maintenance.recovery_installation(self.context), maintenance.artifact_use(),
                      runtime.prepared_inputs(app, prepared, wine_user=point["wine_user"])):
                    self.phase(journal, "selection-pending")

                    def record_selection(record):
                        previous, proposed = record["previous"], record["next"]
                        expected = {saved["path"]: saved for saved in point["preimages"]}
                        if {item["path"] for item in proposed} != set(expected):
                            raise maintenance.RecoveryRequired("Activation targets differ from the complete point")
                        for item in previous:
                            if snapshot(item["path"]) != expected[item["path"]]:
                                raise maintenance.RecoveryRequired("Activation preimage changed")
                        journal["selection"] = [activation_snapshot(item) for item in proposed]
                        journal["activation"] = {
                            "path": str(runtime.selection_state(self.context.install) / "runtime.json"),
                            "record": record,
                        }
                        self.validate_publications(journal, point)
                        self.phase(journal, "selection-pending")

                    runtime.activate(app, prepared.runner, managed=True, before_publish=record_selection)
                    self.phase(journal, "recipe-pending")
                    self._run_recipe(prepared, point["wine_user"])
                    self.original_prefix_identity()
                reason = "awaiting-user-check"
                if initialize_windows:
                    reason += ". Initialized absent global Windows version to win10"
                self.phase(journal, "applied", result="succeeded", reason=reason)
                marker = read(self.root / "marker.json")
                if not owns_marker(Path(marker["path"]), self.context):
                    raise maintenance.RecoveryRequired("Apply compatibility marker changed")
                Path(marker["path"]).unlink()
                sync(Path(marker["path"]).parent)
            except BaseException:
                # The durable phase is the recovery instruction. Retain all pins
                # and original/newer bytes even if acknowledgment itself failed.
                try:
                    durable = self.status()
                    if durable["phase"] != "applied":
                        self.phase(durable, durable["phase"], result="unknown", reason="apply-step-failed-or-unknown")
                except (OSError, ValueError, maintenance.MaintenanceError):
                    pass
                raise

    def verify_capability(self, proof, point):
        runtime_stacks.fields(proof, "id sha256")
        identifier(proof["id"])
        directory = self.root / ("inspection-" + proof["id"])
        record_path = directory / "inspection.json"
        if artifacts.regular_hash(record_path) != proof["sha256"]:
            raise maintenance.RecoveryRequired("Old-prefix inspection receipt changed")
        record = self.inspection_record(proof["id"])
        completed = record["completed_monotonic_ns"]
        if (record["schema"] != "complete-recovery-inspection-1" or type(completed) is not int
                or not 0 <= time.monotonic_ns() - completed <= CAPABILITY_MAX_AGE_NS):
            raise maintenance.RecoveryRequired("Old-prefix inspection is stale. Inspect a fresh copy before apply")
        if (record["id"] != proof["id"] or record["point"] != point["id"]
                or record["generation"] != "old-capability" or record["result"] != "succeeded"
                or record["source"] != str(self.context.prefix) or record["baseline"] != point["manifest"]["sha256"]
                or record["source_observation"] != point["manifest"]["observation_sha256"]
                or record["restarts"] < 1
                or any(record["observations"].get(key) is not True
                       for key in ("library", "resource", "documents", "restart"))
                or any(record["runner"][key] != point["runtime"][key]
                       for key in ("store", "coordination", "generation"))):
            raise maintenance.RecoveryRequired("Recovery capability does not bind this complete old generation")
        runtime_trials.validate_worker(record["worker"], proof["id"], host=True)
        if (record["worker"]["phase"] != "ended" or record["worker"]["reason"] != "processes-settled"
                or record["worker"]["returncode"] != 0):
            raise maintenance.RecoveryRequired("Old-prefix inspection has no normal all-helper settlement")
        boundary = runtime_boundary.inspect((self.context.install, self.root))
        if inspection_facts(boundary) != record["boundary"]:
            raise maintenance.RecoveryRequired("Recovery inspection prerequisites changed. Repair and inspect again")
        runner = self.verify_pin(point["runtime"])
        runtime_boundary.probe(runtime_boundary.command(
            boundary, source=self.context.prefix, copied=directory / "prefix", private=directory / "private",
            runner=runner.path, worker=Path(runtime_trials.__file__).with_name("runtime_trial_worker.py"),
            wine_user=point["wine_user"], check=True,
        ))

    def inspection_record(self, inspection_id):
        identifier(inspection_id)
        directory = self.root / ("inspection-" + inspection_id)
        record = read(directory / "inspection.json")
        runtime_stacks.fields(record, "schema id generation point result reason boundary runner source baseline "
                              "source_observation process worker observations directory_identity "
                              "completed_monotonic_ns copy restarts")
        if (record["schema"] != "complete-recovery-inspection-1" or record["id"] != inspection_id
                or record["generation"] not in ("old-capability", "restored", "newer")
                or record["result"] not in ("pending", "unknown", "succeeded")):
            raise maintenance.RecoveryRequired("Unknown recovery inspection record")
        if type(record["restarts"]) is not int or not 0 <= record["restarts"] <= 999:
            raise maintenance.RecoveryRequired("Invalid inspection restart generation")
        identifier(record["point"])
        runtime_runners._directory_identity(record["directory_identity"])
        if record["directory_identity"]["canonical_path"] != str(directory):
            raise maintenance.RecoveryRequired("Foreign recovery inspection directory")
        if record["copy"] is not None:
            runtime_runners._directory_identity(record["copy"])
            if record["copy"]["canonical_path"] != str(directory / "prefix"):
                raise maintenance.RecoveryRequired("Foreign recovery inspection copy")
        runtime_stacks.fields(record["runner"], "store coordination generation owner reference")
        identifier(record["runner"]["generation"])
        identifier(record["runner"]["reference"])
        if record["process"] is not None:
            runtime_trials.validate_identity(record["process"])
        if record["worker"] is not None:
            runtime_trials.validate_worker(record["worker"], inspection_id, host=True)
        if record["result"] == "succeeded":
            if record["copy"] is None or record["worker"] is None:
                raise maintenance.RecoveryRequired("Successful inspection has no verified copy or worker")
            if (record["worker"]["phase"] != "ended" or record["worker"]["reason"] != "processes-settled"
                    or record["worker"]["returncode"] != 0 or record["restarts"] < 1):
                raise maintenance.RecoveryRequired(
                    "Successful inspection has a failed or incomplete application outcome"
                )
            runtime_stacks.fields(record["observations"], "library resource documents restart")
            if any(value is not True for value in record["observations"].values()):
                raise maintenance.RecoveryRequired("Successful inspection has incomplete observations")
        return record

    def _run_recipe(self, prepared, wine_user):
        """Mutate the admitted original inside the same restricted execution boundary."""
        prefix_identity = self.original_prefix_identity()
        boundary = runtime_boundary.inspect((self.context.install, self.root))
        directory = self.root / ("apply-" + str(uuid.uuid4()))
        directory.mkdir(mode=0o700)
        sync(self.root)
        private = directory / "private"
        runtime_boundary.prepare_private(private, wine_user=wine_user)
        dependencies = [{"id": item.dependency.id, "role": item.dependency.role, "sha256": item.artifact.sha256,
                         "path": str(item.artifact.path), "files": [list(entry) for entry in item.files]}
                        for item in prepared.dependencies]
        operation_id = str(uuid.uuid4())
        runtime_trials.write(private / "job.json", {
            "id": operation_id, "registry": runtime_trials.registry_plan(prepared.recipe.settings),
            "dependencies": dependencies,
            "companions": dict(prepared.catalog.runners[prepared.stack.runner_id].companions),
            "prefix_identity": {key: prefix_identity[key] for key in ("device", "inode")},
        })
        extraction = runtime_boundary.expansion_budget(prepared.runner.path)
        runtime_copies.require_capacity(directory, extraction["bytes"] + 512 * 1024 * 1024)
        arguments = dict(source=self.context.prefix, copied=self.context.prefix, private=private,
                         runner=prepared.runner.path, worker=Path(runtime_trials.__file__).with_name(
                             "runtime_trial_worker.py"), dependencies=dependencies, wine_user=wine_user, mode="apply")
        runtime_boundary.probe(runtime_boundary.command(boundary, **arguments, check=True))
        receipt = {"copy": runtime_filesystem.live_identity(self.context.prefix)}
        with (private / "wine.log").open("xb") as log:
            child = subprocess.Popen(runtime_boundary.command(boundary, **arguments), stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, close_fds=True)
            process_record = {"schema": "complete-recovery-helper-1", "id": operation_id,
                              "owner": runtime_trials.identity(child.pid),
                              "prefix": receipt["copy"], "worker": None}
            write(directory / "process.json", process_record, checkpoint=self.checkpoint)
            deadline = time.monotonic() + 300
            while child.poll() is None:
                if time.monotonic() > deadline:
                    raise maintenance.RecoveryRequired(
                        "Recipe outcome is unknown. Preserve the point and inspect helpers"
                    )
                try:
                    worker = runtime_trials.worker_status(private, child, self.context.prefix,
                                                         self.context.prefix, receipt, operation_id)
                except FileNotFoundError:
                    time.sleep(0.1)
                    continue
                if process_record["worker"] is None or process_record["worker"]["phase"] != worker["phase"]:
                    process_record["worker"] = worker
                    write(directory / "process.json", process_record, checkpoint=self.checkpoint)
                if worker["phase"] == "unknown":
                    raise maintenance.RecoveryRequired("Recipe helper settlement is unknown. Use complete recovery")
                if worker["phase"] == "ended":
                    if worker["returncode"] != 0 or worker["reason"] != "recipe-settled":
                        raise maintenance.RecoveryRequired("Recipe failed. Use complete recovery")
                    runtime_trials.write(private / "request.json", {"action": "settle"})
                    child.wait(timeout=10)
                    break
                time.sleep(0.1)
            if child.returncode != 0:
                raise maintenance.RecoveryRequired("Recipe execution failed or is unknown. Use complete recovery")
            terminal = runtime_trials.read(private / "worker-status.json")
            runtime_trials.validate_worker(terminal, operation_id)
            if terminal["phase"] != "ended" or terminal["reason"] != "recipe-settled":
                raise maintenance.RecoveryRequired("Recipe has no checked settlement receipt")

    def _restore_plan(self, journal, point):
        publications = []
        applied = {item["path"]: item for item in journal["selection"]}
        for saved in point["preimages"]:
            current = snapshot(saved["path"])
            published = applied.get(saved["path"], saved)
            try:
                proposed = restored_snapshot(saved, published, current)
            except maintenance.RecoveryRequired:
                write(self.root / "config-conflict.json", {"path": saved["path"], "current": current,
                                                          "old": saved, "applied": published})
                raise maintenance.RecoveryRequired(
                    f"Later runtime settings preserved at {saved['path']}. Inspect config-conflict.json and recheck"
                )
            publications.append({"previous": current, "next": proposed})
        return publications

    def return_prefix(self, confirmation):
        """Headless return needs verified bytes, never a working display or Wine."""
        with self.admitted():
            journal = self.status()
            if confirmation != self.return_confirmation():
                raise maintenance.RecoveryRequired("Confirm the exact complete local-data return scope")
            if journal["phase"] not in ("point-verified", "apply-intent", "selection-pending",
                                         "recipe-pending", "applied"):
                raise maintenance.RecoveryRequired("Return is already pending. Reconcile the recorded journal")
            point = self.verify_point(journal["point"])
            journal["configs"] = self._restore_plan(journal, point)
            prefix = self.context.prefix
            runtime_copies.layout(prefix)
            stage = prefix.with_name(".runtime-old-" + point["id"] + "-" + str(uuid.uuid4()))
            newer = prefix.with_name(".runtime-newer-" + point["id"])
            if os.path.lexists(stage) or os.path.lexists(newer):
                raise maintenance.RecoveryRequired("Return staging names already exist. Preserve and inspect them")
            journal.update(stage=str(stage), newer=str(newer), newer_identity=runtime_filesystem.identity(prefix))
            journal["stages"].append(str(stage))
            self.phase(journal, "return-staging")
            self._ensure_marker()
            self._finish_return(journal, point)

    def _register_secondary(self, newer):
        context = maintenance.Context(newer, self.context.install, self.context.product)
        descriptor = maintenance._operation_directory(context)
        os.close(descriptor)
        record = dict(self.registered, prefix=str(newer), secondary=True)
        path = registration_path(context)
        if os.path.lexists(path):
            if read(path) != record:
                raise maintenance.RecoveryRequired("Retained generation has a foreign guard")
        else:
            write(path, record, checkpoint=self.checkpoint)
        descriptor = maintenance._operation_directory(context)
        try:
            maintenance._known_recovery_locations(descriptor, context)
        finally:
            os.close(descriptor)
        marker = {"schema": "runtime-admission-1", "operation_id": self.registered["id"],
                  "directory": str(self.root)}
        self._create_marker(context.pending, marker)

    def _create_marker(self, path, marker):
        if os.path.lexists(path):
            if read(path) != marker:
                raise maintenance.RecoveryRequired("Complete recovery compatibility guard changed")
            return
        self.checkpoint("before-compatibility-marker")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(runtime_stacks.canonical(marker))
            output.flush()
            os.fsync(output.fileno())
        sync(path.parent)
        self.checkpoint("after-compatibility-marker")

    def _ensure_marker(self):
        marker = read(self.root / "marker.json")
        runtime_stacks.fields(marker, "path marker")
        expected = {"schema": "runtime-admission-1", "operation_id": self.registered["id"],
                    "directory": str(self.root)}
        if marker["marker"] != expected:
            raise maintenance.RecoveryRequired("Complete recovery marker binding changed")
        self._create_marker(Path(marker["path"]), expected)

    def _moved_identity(self, saved, path):
        if path.parent != Path(saved["canonical_path"]).parent:
            raise maintenance.RecoveryRequired("Return generation moved outside its recorded parent")
        current = runtime_copies.layout(path)
        relocated = dict(saved, canonical_path=str(path),
                         filesystem_root=str(Path(saved["filesystem_root"]).parent / path.name))
        if not runtime_filesystem.same_retained_root(relocated, current):
            raise maintenance.RecoveryRequired("Return generation identity changed")
        return current

    def _rename(self, source, destination):
        self.checkpoint("before-prefix-rename:" + source.name)
        maintenance.assert_quiescent(self.context)
        paths.rename_no_replace(source, destination)
        self.checkpoint("after-prefix-rename:" + source.name)
        sync(destination.parent)
        self.checkpoint("after-prefix-fsync:" + destination.name)

    def _finish_return(self, journal, point):
        self.validate_publications(journal, point)
        prefix = self.context.prefix
        stage, newer = Path(journal["stage"]), Path(journal["newer"])
        if stage.parent != prefix.parent or newer.parent != prefix.parent:
            raise maintenance.RecoveryRequired("Return paths do not belong to this prefix")
        if journal["phase"] == "return-staging":
            self._ensure_marker()
            if not runtime_filesystem.same_retained_root(journal["newer_identity"], runtime_copies.layout(prefix)):
                raise maintenance.RecoveryRequired("Active prefix changed during return staging")
            if not os.path.lexists(stage):
                if any(os.path.lexists(stage.with_name(stage.name + suffix))
                       for suffix in (".partial", ".manifest.partial", ".manifest")):
                    # Keep the interrupted attempt intact. A new recorded name
                    # permits progress without guessing which partial bytes to delete.
                    stage = prefix.with_name(".runtime-old-" + point["id"] + "-" + str(uuid.uuid4()))
                    journal["stage"] = str(stage)
                    journal["stages"].append(str(stage))
                    self.phase(journal, "return-staging")
                runtime_copies.copy(self.root / ("point-" + point["id"]) / "prefix", stage,
                                    checkpoint=lambda: self.checkpoint("return-copy"), logical_root=prefix)
            if runtime_copies.manifest(stage, logical_root=prefix).sha256 != point["manifest"]["sha256"]:
                raise maintenance.RecoveryRequired("Staged old prefix failed verification")
            journal["stage_identity"] = runtime_copies.layout(stage)
            sync(stage.parent)
            self.phase(journal, "return-ready")
        if journal["phase"] == "return-ready":
            journal["gate"] = "online-resume-pending"
            self.phase(journal, "resume-pending")
        if journal["phase"] in ("resume-pending", "withdraw-intent"):
            self._register_secondary(newer)
            if os.path.lexists(newer):
                self._moved_identity(journal["newer_identity"], newer)
                if os.path.lexists(prefix):
                    raise maintenance.RecoveryRequired("Both active and withdrawn prefixes exist before publication")
                sync(newer.parent)
            else:
                if not runtime_filesystem.same_retained_root(journal["newer_identity"], runtime_copies.layout(prefix)):
                    raise maintenance.RecoveryRequired("Active prefix changed before return")
                self.phase(journal, "withdraw-intent")
                self._rename(prefix, newer)
            self.phase(journal, "withdrawn")
        if journal["phase"] in ("withdrawn", "publish-intent"):
            self._moved_identity(journal["newer_identity"], newer)
            if os.path.lexists(prefix):
                if os.path.lexists(stage):
                    raise maintenance.RecoveryRequired("Both staged and active old prefixes exist")
                self._moved_identity(journal["stage_identity"], prefix)
                # A visible rename may precede a failed directory sync.
                sync(prefix.parent)
            else:
                if not runtime_filesystem.same_retained_root(journal["stage_identity"], runtime_copies.layout(stage)):
                    raise maintenance.RecoveryRequired("Staged prefix changed before publication")
                if runtime_copies.manifest(stage, logical_root=prefix).sha256 != point["manifest"]["sha256"]:
                    raise maintenance.RecoveryRequired("Staged prefix bytes changed before publication")
                self.phase(journal, "publish-intent")
                self._rename(stage, prefix)
            self.phase(journal, "published")
        if journal["phase"] in ("published", "config-pending"):
            self._moved_identity(journal["stage_identity"], prefix)
            if runtime_copies.manifest(prefix).sha256 != point["manifest"]["sha256"]:
                raise maintenance.RecoveryRequired("Published prefix bytes changed before configuration settlement")
            sync(prefix.parent)
            self.phase(journal, "config-pending")
            for publication in journal["configs"]:
                current = snapshot(publication["previous"]["path"])
                if current == publication["next"]:
                    sync(Path(publication["next"]["path"]).parent)
                    continue
                self.checkpoint("before-config:" + publication["previous"]["path"])
                publish_snapshot(publication["previous"], publication["next"])
                self.checkpoint("after-config:" + publication["previous"]["path"])
            from . import runtime

            activation = journal["activation"]
            legacy = (Path(activation["path"]) if activation else
                      runtime.selection_state(self.context.install, create=False) / "runtime.json")
            if os.path.lexists(legacy):
                pending = read(legacy)
                # The enclosing operation recorded intent before activation wrote
                # its own journal. Archive only that exact publication graph.
                if activation is None or pending != activation["record"]:
                    raise maintenance.RecoveryRequired("Foreign runtime activation journal retained")
                write(self.root / "returned-runtime-activation.json", pending, checkpoint=self.checkpoint)
                legacy.unlink()
                sync(legacy.parent)
            elif activation is not None:
                sync(legacy.parent)
            if any(snapshot(item["next"]["path"]) != item["next"] for item in journal["configs"]):
                raise maintenance.RecoveryRequired("Complete return configuration did not settle coherently")
            self.phase(journal, "returned", result="succeeded", reason="offline-inspection-required")

    def reconcile_return(self):
        with self.admitted():
            journal = self.status()
            if journal["phase"] not in ("return-staging", "return-ready", "resume-pending",
                                         "withdraw-intent", "withdrawn", "publish-intent",
                                         "published", "config-pending"):
                raise maintenance.RecoveryRequired("No admitted return publication can be reconciled")
            self._finish_return(journal, self.verify_point(journal["point"]))

    def keep_current(self):
        with self.admitted():
            journal = self.status()
            if journal["phase"] not in ("preparing", "point-verified"):
                raise maintenance.RecoveryRequired("Original mutation may have started. Use complete return")
            if journal["point"] is not None:
                point = self.verify_point(journal["point"])
                current = runtime_copies.manifest(self.context.prefix)
                if (current.sha256 != point["manifest"]["sha256"]
                        or current.observation_sha256 != point["manifest"]["observation_sha256"]
                        or any(snapshot(saved["path"]) != saved for saved in point["preimages"])):
                    raise maintenance.RecoveryRequired("Original changed. Recheck before releasing preparation")
            self.phase(journal, "kept-current", result="succeeded", reason="original-not-applied")
            self._remove_marker()

    def _remove_marker(self):
        marker = read(self.root / "marker.json")
        path = Path(marker["path"])
        if os.path.lexists(path):
            if not owns_marker(path, self.context):
                raise maintenance.RecoveryRequired("Complete recovery compatibility marker changed")
            self.checkpoint("before-marker-withdrawal")
            path.unlink()
            self.checkpoint("after-marker-withdrawal")
        sync(path.parent)
        self.checkpoint("after-marker-directory-sync")

    def _resume_configuration(self, journal, point):
        profiles = {item["path"] for item in point["preimages"][:2]}
        runner = self.verify_pin(point["runtime"])
        launch_aliases = ("install_dir", "faithlife_product", "faithlife_product_version",
                          "wine_binary", "wine_binary_code")
        overrides = ("wine_appimage_path", "wineserver_binary", "wine_dll_overrides", "custom_binary_path")
        for publication in journal["configs"]:
            expected = publication["next"]
            current = snapshot(expected["path"])
            if expected["path"] not in profiles:
                if current != expected:
                    raise maintenance.RecoveryRequired("Returned runtime alias changed. Recheck before online resume")
                continue
            if current["kind"] != "file" or expected["kind"] != "file":
                raise maintenance.RecoveryRequired("Returned launch configuration changed its layout")
            raw = config_store.decode(base64.b64decode(current["content"]), current["path"])
            for modern in launch_aliases:
                legacy = config_store.ALIASES[modern]
                if modern in raw and legacy in raw and raw[modern] != raw[legacy]:
                    raise maintenance.RecoveryRequired("Returned launch configuration aliases conflict")
            if any(raw.get(key) is not None for key in overrides):
                raise maintenance.RecoveryRequired("Effective runtime overrides need a matching offline inspection")
            actual = config_store.canonical(raw, current["path"])
            previous = config_store.canonical(
                config_store.decode(base64.b64decode(expected["content"]), expected["path"]), expected["path"],
            )
            actual_runtime = runtime_preimage(actual, self.context.install)
            expected_runtime = runtime_preimage(previous, self.context.install)
            if (config_store.context(actual) != self.context or config_store.context(previous) != self.context
                    or actual.get("faithlife_product_version") != previous.get("faithlife_product_version")
                    or actual_runtime != expected_runtime or actual_runtime["binary"] != str(runner.path)):
                raise maintenance.RecoveryRequired("Returned launch context or runtime changed. Recheck offline")

    def _resume_inspection(self, journal, point):
        proof = journal["inspection"]
        runtime_stacks.fields(proof, "id sha256")
        identifier(proof["id"])
        path = self.root / ("inspection-" + proof["id"]) / "inspection.json"
        if artifacts.regular_hash(path) != proof["sha256"]:
            raise maintenance.RecoveryRequired("Offline inspection receipt changed")
        inspected = self.inspection_record(proof["id"])
        current = runtime_copies.manifest(self.context.prefix)
        if (inspected["result"] != "succeeded" or inspected["generation"] != "restored"
                or inspected["point"] != point["id"] or inspected["source"] != str(self.context.prefix)
                or inspected["baseline"] != current.sha256
                or inspected["source_observation"] != current.observation_sha256
                or any(inspected["runner"][key] != point["runtime"][key]
                       for key in ("store", "coordination", "generation"))):
            raise maintenance.RecoveryRequired("Restored generation or runtime changed. Inspect a fresh offline copy")
        boundary = runtime_boundary.inspect((self.context.install, self.context.prefix, self.root))
        if inspection_facts(boundary) != inspected["boundary"]:
            raise maintenance.RecoveryRequired("Offline inspection implementation or prerequisites changed. Reinspect")

    def _resume_record(self, journal, confirmation):
        if journal["phase"] == "returned":
            if (journal["resume"] is not None or journal["gate"] != "online-resume-pending"
                    or journal["result"] != "succeeded"):
                raise maintenance.RecoveryRequired("Unexpected returned-generation resume record")
            return
        if journal["phase"] not in ("resume-intent", "resumed"):
            raise maintenance.RecoveryRequired("No returned generation is awaiting online resumption")
        if journal["resume"]["confirmation"] != confirmation:
            raise maintenance.RecoveryRequired("Online resume acknowledgment does not match this generation")

    def resume_confirmation(self):
        journal = self.status()
        if journal["phase"] not in ("returned", "resume-intent", "resumed"):
            raise maintenance.RecoveryRequired("No returned generation is awaiting online resumption")
        point = self.verify_point(journal["point"])
        return {"point": point["id"], "active": str(self.context.prefix), "retained": journal["newer"],
                "inspection": journal["inspection"],
                "message": "Resume this restored installation online? Current server account state will synchronize. "
                "Local return did not rewind cloud changes. Confirm that you reviewed and preserved needed newer work. "
                "The retained newer generation stays offline."}

    def resume_online(self, confirmation, *, retained_work_acknowledged=False):
        if not ONLINE_RESUME_SUPPORTED:
            raise maintenance.RecoveryRequired(
                "Online resume is unavailable until supported synthetic cloud outcomes have passed native review. "
                "Keep both generations offline and retain newer work"
            )
        with self.admitted():
            journal = self.status()
            if (retained_work_acknowledged is not True or confirmation != self.resume_confirmation()
                    or journal["inspection"] is None):
                raise maintenance.RecoveryRequired(
                    "Offline inspection and explicit retained-work acknowledgment are required"
                )
            self._resume_record(journal, confirmation)
            point = self.verify_point(journal["point"])
            self.validate_publications(journal, point)
            if len(journal["configs"]) != len(point["preimages"]):
                raise maintenance.RecoveryRequired("Returned configuration publication graph is incomplete")
            if journal["phase"] == "resumed":
                # A prior journal rename can be visible before its directory sync succeeded.
                write(self.root / "journal.json", journal, checkpoint=self.checkpoint)
                self._remove_marker()
                return
            self._resume_configuration(journal, point)
            self._resume_inspection(journal, point)
            self._moved_identity(journal["stage_identity"], self.context.prefix)
            self._moved_identity(journal["newer_identity"], Path(journal["newer"]))
            if journal["phase"] == "returned":
                journal["resume"] = {"confirmation": confirmation, "retained_work_acknowledged": True,
                                      "confirmed": datetime.now(timezone.utc).isoformat()}
                self.phase(journal, "resume-intent")
            journal["gate"] = "online-resumed"
            # Older controllers rely on the marker until this acknowledgment is durable.
            self.phase(journal, "resumed", result="succeeded", reason="explicit-online-resume")
            self._remove_marker()

    def point_removal_confirmation(self, point_id):
        point = self.verify_point(point_id)
        index = self.point_index()
        others = [key for key in index["points"] if key != point_id and index["points"][key]["cleanup"] is None]
        if not others:
            raise maintenance.RecoveryRequired("Keep the last valid complete recovery point and its referenced runtime")
        for other in others:
            self.verify_point(other)
        if self.status()["point"] == point_id:
            raise maintenance.RecoveryRequired("The current operation references this complete recovery point")
        history_hash = index["points"][point_id]["history"]
        if history_hash is not None:
            history = read(self.root / ("history-" + point_id + ".json"))
            if history["newer"] is not None or history["phase"] not in ("kept-current", "applied"):
                raise maintenance.RecoveryRequired("Retained generations or pending history still reference this point")
        directories = list(self.root.glob("inspection-*"))
        if len(directories) > 128:
            raise maintenance.RecoveryRequired("Review owned inspection retention before removing complete points")
        for directory in directories:
            inspection_id = identifier(directory.name.removeprefix("inspection-"))
            record = self.inspection_record(inspection_id)
            if record["point"] == point_id:
                raise maintenance.RecoveryRequired(
                    "Export needed inspection work and remove its copy before this point"
                )
        return {"point": point_id, "created": point["created"], "bytes": point["manifest"]["logical_bytes"],
                "runtime_sha256": point["runner_sha256"], "remaining_points": sorted(others),
                "message": "Remove this complete recovery point? Its saved local data will be deleted. "
                "Other verified points remain. The current installation and newer work are separate."}

    def _settle_reference(self, reference):
        store = runtime_runners.RunnerStore(reference["store"], reference["coordination"])
        owner = read(reference["owner"])
        if owner["phase"] != "settled":
            owner.update(phase="settled", revision=owner["revision"] + 1, runner_generations=[])
            write(Path(reference["owner"]), owner, checkpoint=self.checkpoint)
        store.release_pin(reference["reference"], settled_owner_sha256=artifacts.regular_hash(reference["owner"]))

    def remove_point(self, point_id, confirmation=None):
        identifier(point_id)
        with self.admitted():
            index = self.point_index()
            plan_path = self.root / ("point-cleanup-plan-" + point_id + ".json")
            state_path = self.root / ("point-cleanup-state-" + point_id + ".json")
            if os.path.lexists(plan_path):
                plan = read(plan_path)
                runtime_stacks.fields(plan, "schema id operation identity point_sha256 history "
                                      "runtime journal confirmation")
                if (plan["schema"] != "complete-point-cleanup-1" or plan["id"] != point_id
                        or plan["operation"] != self.registered["id"]):
                    raise maintenance.RecoveryRequired("Foreign complete point cleanup plan")
                plan_hash = artifacts.regular_hash(plan_path)
                entry = index["points"].get(point_id)
                bound = entry["cleanup"] if entry is not None else index["retired"].get(point_id)
                if bound is None:
                    point = self.verify_point(point_id)
                    expected = self.point_removal_confirmation(point_id)
                    if (plan["point_sha256"] != entry["sha256"] or plan["runtime"] != point["runtime"]
                            or confirmation != expected or plan["confirmation"] != expected):
                        raise maintenance.RecoveryRequired("Unpublished point cleanup needs explicit reconciliation")
                    self.verify_pin(plan["journal"])
                    if any(plan["journal"][key] != point["runtime"][key]
                           for key in ("store", "coordination", "generation")):
                        raise maintenance.RecoveryRequired("Cleanup journal runner dependency changed")
                    store = runtime_runners.RunnerStore(plan["journal"]["store"], plan["journal"]["coordination"])
                    if store._registry()["references"][plan["journal"]["reference"]]["kind"] != "pending-journal":
                        raise maintenance.RecoveryRequired("Cleanup journal has a foreign reservation kind")
                    entry["cleanup"] = plan_hash
                    write(self.root / "points.json", index, checkpoint=self.checkpoint)
                elif bound != plan_hash:
                    raise maintenance.RecoveryRequired("Point cleanup plan binding changed")
                if confirmation != plan["confirmation"]:
                    raise maintenance.RecoveryRequired("Confirm the exact saved-data cleanup scope")
            else:
                if len(index["retired"]) >= 256:
                    raise maintenance.RecoveryRequired("Review retired recovery metadata before further point cleanup")
                expected = self.point_removal_confirmation(point_id)
                if confirmation != expected:
                    raise maintenance.RecoveryRequired("Confirm the exact saved-data cleanup scope")
                point = self.verify_point(point_id)
                reference = point["runtime"]
                store = runtime_runners.RunnerStore(reference["store"], reference["coordination"])
                journal = self.pin(store, reference["generation"], "pending-journal")
                plan = {"schema": "complete-point-cleanup-1", "id": point_id,
                        "operation": self.registered["id"],
                        "identity": runtime_copies.layout(self.root / ("point-" + point_id)),
                        "point_sha256": index["points"][point_id]["sha256"],
                        "history": index["points"][point_id]["history"],
                        "runtime": reference, "journal": journal, "confirmation": expected}
                write(plan_path, plan, checkpoint=self.checkpoint)
                plan_hash = artifacts.regular_hash(plan_path)
                index["points"][point_id]["cleanup"] = plan_hash
                write(self.root / "points.json", index, checkpoint=self.checkpoint)
            if os.path.lexists(state_path):
                state = read(state_path)
                runtime_stacks.fields(state, "schema id plan phase")
                if (state["schema"] != "complete-point-cleanup-state-1" or state["id"] != point_id
                        or state["plan"] != plan_hash
                        or state["phase"] not in ("intent", "withdrawn", "removed", "settled")):
                    raise maintenance.RecoveryRequired("Unknown point cleanup phase")
            else:
                state = {"schema": "complete-point-cleanup-state-1", "id": point_id,
                         "plan": plan_hash, "phase": "intent"}
                write(state_path, state, checkpoint=self.checkpoint)
            directory = self.root / ("point-" + point_id)
            tombstone = self.root / (".removed-point-" + point_id)
            if state["phase"] in ("intent", "withdrawn"):
                others = [key for key, value in index["points"].items()
                          if key != point_id and value["cleanup"] is None]
                if not others:
                    raise maintenance.RecoveryRequired("Cleanup would remove the last valid complete point")
                for other in others:
                    self.verify_point(other)
            if state["phase"] == "intent":
                if os.path.lexists(directory):
                    if not runtime_filesystem.same_retained_root(plan["identity"], runtime_copies.layout(directory)):
                        raise maintenance.RecoveryRequired("Complete point directory changed before cleanup")
                    self._rename(directory, tombstone)
                else:
                    self._moved_identity(plan["identity"], tombstone)
                state["phase"] = "withdrawn"
                write(state_path, state, checkpoint=self.checkpoint)
            if state["phase"] == "withdrawn":
                for other in others:
                    self.verify_point(other)
                if os.path.lexists(tombstone):
                    self._moved_identity(plan["identity"], tombstone)
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise maintenance.RecoveryRequired("Safe descriptor-based cleanup is unavailable")
                    shutil.rmtree(tombstone)
                    sync(self.root)
                state["phase"] = "removed"
                write(state_path, state, checkpoint=self.checkpoint)
            if state["phase"] in ("removed", "settled"):
                index["points"].pop(point_id, None)
                index["retired"][point_id] = plan_hash
                write(self.root / "points.json", index, checkpoint=self.checkpoint)
                self._settle_reference(plan["runtime"])
                state["phase"] = "settled"
                write(state_path, state, checkpoint=self.checkpoint)
                self._settle_reference(plan["journal"])

    def helper_status(self, directory_name):
        """Inspect an owned runner without taking or releasing its prefix reservation."""
        if not isinstance(directory_name, str) or Path(directory_name).name != directory_name:
            raise maintenance.RecoveryRequired("Select an owned recovery helper")
        kind, separator, operation = directory_name.partition("-")
        if not separator or kind not in ("apply", "inspection"):
            raise maintenance.RecoveryRequired("Unknown recovery helper purpose")
        identifier(operation)
        self.status()
        directory = self.root / directory_name
        runtime_copies.checked_directory(directory)
        if kind == "apply":
            record = read(directory / "process.json")
            runtime_stacks.fields(record, "schema id owner prefix worker")
            if record["schema"] != "complete-recovery-helper-1":
                raise maintenance.RecoveryRequired("Unknown apply helper record")
            owner = record["owner"]
            receipt = {"copy": record["prefix"]}
            copied = self.context.prefix
        else:
            record = self.inspection_record(operation)
            owner = record["process"]
            receipt = {"copy": record.get("copy")}
            copied = directory / "prefix"
        result = {"directory": directory_name, "purpose": kind, "result": "unknown", "worker": None,
                  "reason": "Recheck helpers, then use complete return. Keep recovery data and runner pins"}
        if owner is None or receipt["copy"] is None:
            return result
        runtime_trials.validate_identity(owner)
        try:
            current = runtime_trials.identity(owner["pid"])
        except (FileNotFoundError, ProcessLookupError):
            return result
        if any(current[key] != owner[key] for key in ("pid", "boot_id", "start_ticks")):
            return result
        value = runtime_trials.worker_status(directory / "private", SimpleNamespace(pid=owner["pid"]),
                                            self.context.prefix, copied, receipt, record["id"])
        result.update(worker=value, result="running" if value["phase"] != "unknown" else "unknown",
                      reason=value["reason"] or "Owned offline helper is active")
        return result

    def stop_helper(self, directory_name):
        snapshot = self.helper_status(directory_name)
        worker = snapshot["worker"]
        if worker is None:
            raise maintenance.RecoveryRequired(snapshot["reason"])
        if snapshot["purpose"] == "apply" and worker["phase"] != "ended":
            raise maintenance.RecoveryRequired(
                "Recipe work must settle or remain unknown. Recheck status, then use complete return"
            )
        action = "settle" if worker["phase"] == "ended" else "stop"
        runtime_trials.write(self.root / directory_name / "private/request.json", {"action": action})
        return snapshot

    def restart_helper(self, directory_name):
        snapshot = self.helper_status(directory_name)
        if (snapshot["purpose"] != "inspection" or snapshot["worker"] is None
                or snapshot["worker"]["phase"] != "running"):
            raise maintenance.RecoveryRequired("Only a running private inspection copy can restart")
        runtime_trials.write(self.root / directory_name / "private/request.json", {"action": "restart"})
        return snapshot

    def export_file(self, inspection_id, name, destination):
        """Copy a user-exported document, never an application database."""
        identifier(inspection_id)
        if (not isinstance(name, str) or Path(name).name != name
                or Path(name).suffix.lower() not in DEMONSTRATED_EXPORT_SUFFIXES):
            raise maintenance.RecoveryRequired("Choose a document export format demonstrated by native recovery checks")
        with self.admitted(), ExitStack() as directories:
            directory = self.root / ("inspection-" + inspection_id)
            record = self.inspection_record(inspection_id)
            if record["id"] != inspection_id or record["result"] != "succeeded":
                raise maintenance.RecoveryRequired("Finish the owned offline inspection before copying its export")
            held = []
            for path, saved in ((directory, record["directory_identity"]),
                                (directory / "prefix", record["copy"])):
                descriptor, identity = directories.enter_context(export_directory(path))
                if not runtime_filesystem.same_retained_root(saved, identity):
                    raise maintenance.RecoveryRequired("Completed inspection copy identity changed")
                runtime_copies.layout(path)
                held.append((path, descriptor, identity))
            source = directory / "prefix/drive_c/M4Export" / name
            source_parent, source_identity = directories.enter_context(export_directory(source.parent))
            held.append((source.parent, source_parent, source_identity))
            if source_identity["mount_id"] != held[1][2]["mount_id"]:
                raise maintenance.RecoveryRequired("Export source crosses a mount boundary")
            destination = Path(destination)
            if not destination.is_absolute() or destination.name in ("", ".", ".."):
                raise maintenance.RecoveryRequired("Choose an absolute document export destination")
            destination_parent, destination_identity = directories.enter_context(export_directory(destination.parent))
            held.append((destination.parent, destination_parent, destination_identity))
            physical_target = Path(destination_identity["filesystem_root"]) / destination.name
            target_identity = dict(destination_identity, filesystem_root=str(physical_target))
            protected_roots = (self.root, self.context.install, Path(self.status()["newer"]))
            for protected in protected_roots:
                protected_identity = runtime_filesystem.identity(protected)
                if (not paths.disjoint(protected, destination)
                        or not runtime_copies.physical_disjoint(protected_identity, target_identity)):
                    raise maintenance.RecoveryRequired("Export destination overlaps a protected generation")
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
            with os.fdopen(descriptor, "rb") as input_file:
                info = os.fstat(input_file.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                        or info.st_size > 64 * 1024 * 1024
                        or export_mount_id(input_file.fileno()) != source_identity["mount_id"]):
                    raise maintenance.RecoveryRequired("Export is not a bounded owned document file")
                content = input_file.read(64 * 1024 * 1024 + 1)
                try:
                    if Path(name).suffix.lower() != ".txt" or b"\0" in content:
                        raise ValueError
                    content.decode("utf-8-sig")
                except (UnicodeError, ValueError) as error:
                    raise maintenance.RecoveryRequired("This export requires a plain UTF-8 text document") from error
                if len(content) != info.st_size:
                    raise maintenance.RecoveryRequired("Document export size changed during reading")
                runtime_copies.require_capacity(destination.parent, info.st_size)
                temporary = ".export-" + str(uuid.uuid4())
                staged = False
                try:
                    for path, parent, identity in held:
                        check_export_directory(path, parent, identity)
                    output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600, dir_fd=destination_parent)
                    staged = True
                    with os.fdopen(output_fd, "wb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    if runtime_copies._signature(os.fstat(input_file.fileno())) != runtime_copies._signature(info):
                        raise maintenance.RecoveryRequired("Document export changed during copying")
                    for path, parent, identity in held:
                        check_export_directory(path, parent, identity)
                    paths.rename_no_replace(Path(temporary), Path(destination.name),
                                            source_dir_fd=destination_parent, destination_dir_fd=destination_parent)
                    os.fsync(destination_parent)
                finally:
                    if staged:
                        try:
                            os.unlink(temporary, dir_fd=destination_parent)
                        except FileNotFoundError:
                            pass
            return destination

    def cleanup_inspection(self, inspection_id, *, discard_work_confirmed=False):
        identifier(inspection_id)
        with self.admitted():
            journal = self.status()
            if discard_work_confirmed is not True:
                raise maintenance.RecoveryRequired(
                    "Confirm that needed inspection work has been exported before cleanup"
                )
            if any(journal[key] is not None and journal[key]["id"] == inspection_id
                   for key in ("inspection", "capability")):
                raise maintenance.RecoveryRequired("Keep the offline inspection referenced by the resume gate")
            directory = self.root / ("inspection-" + inspection_id)
            tombstone = self.root / (".removed-inspection-" + inspection_id)
            cleanup_path = self.root / ("cleanup-" + inspection_id + ".json")
            if os.path.lexists(cleanup_path):
                cleanup = read(cleanup_path)
                runtime_stacks.fields(cleanup, "schema id identity phase")
                if cleanup["schema"] != "inspection-cleanup-1" or cleanup["id"] != inspection_id:
                    raise maintenance.RecoveryRequired("Foreign inspection cleanup journal")
                if cleanup["phase"] == "removed":
                    return
            else:
                record = self.inspection_record(inspection_id)
                if record["id"] != inspection_id or record["result"] != "succeeded":
                    raise maintenance.RecoveryRequired("Unknown or unfinished inspection remains protected")
                if not runtime_filesystem.same_retained_root(record["directory_identity"],
                                                             runtime_copies.layout(directory)):
                    raise maintenance.RecoveryRequired("Offline inspection directory changed")
                helper = record["runner"]
                store = runtime_runners.RunnerStore(helper["store"], helper["coordination"])
                if helper["reference"] in store._registry()["references"]:
                    raise maintenance.RecoveryRequired("Inspection helper still has a durable runner reservation")
                cleanup = {"schema": "inspection-cleanup-1", "id": inspection_id,
                           "identity": record["directory_identity"], "phase": "intent"}
                write(cleanup_path, cleanup, checkpoint=self.checkpoint)
            if cleanup["phase"] == "intent":
                if os.path.lexists(directory):
                    if not runtime_filesystem.same_retained_root(cleanup["identity"], runtime_copies.layout(directory)):
                        raise maintenance.RecoveryRequired("Inspection identity changed before cleanup")
                    self._rename(directory, tombstone)
                else:
                    self._moved_identity(cleanup["identity"], tombstone)
                cleanup["phase"] = "withdrawn"
                write(cleanup_path, cleanup, checkpoint=self.checkpoint)
            if cleanup["phase"] == "withdrawn":
                if os.path.lexists(tombstone):
                    self._moved_identity(cleanup["identity"], tombstone)
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise maintenance.RecoveryRequired("Safe descriptor-based cleanup is unavailable")
                    shutil.rmtree(tombstone)
                    sync(self.root)
                cleanup["phase"] = "removed"
                write(cleanup_path, cleanup, checkpoint=self.checkpoint)

    def inspect_generation(self, *, newer=False, resource_uri=None, observe, status=lambda value: None,
                           stop=None, timeout=3600):
        """Run an owned offline inspection copy while retaining durable admission.

        Call from an owned worker. The observer supplies actual user observations
        after normal process settlement. Unknown outcomes keep the helper pin.
        Timeout reports unknown through the receipt and status callback, without
        ending supervision. Status consumers must inspect result as well as worker
        phase. After launch, reporting failures retain admission until the child
        exits or verified settlement completes, then raise the original failure.
        """
        from . import utils

        with self.admitted():
            journal = self.status()
            before_apply = journal["phase"] == "point-verified"
            if before_apply and newer:
                raise maintenance.RecoveryRequired("There is no retained newer generation before apply")
            if not before_apply and (journal["phase"] != "returned" or journal["gate"] != "online-resume-pending"):
                raise maintenance.RecoveryRequired("Return and configuration must settle before offline inspection")
            if before_apply:
                journal["capability"] = None
                self.phase(journal, "point-verified", result="succeeded", reason="old-inspection-required")
            point = self.verify_point(journal["point"])
            source = Path(journal["newer"]) if newer else self.context.prefix
            source_context = maintenance.Context(source, self.context.install, self.context.product)
            maintenance.assert_quiescent(source_context)
            if before_apply:
                if not runtime_filesystem.same_retained_root(point["source"], runtime_copies.layout(source)):
                    raise maintenance.RecoveryRequired("Old prefix identity changed before inspection")
            else:
                saved_identity = journal["newer_identity"] if newer else journal["stage_identity"]
                self._moved_identity(saved_identity, source)
            baseline = runtime_copies.manifest(source, logical_root=self.context.prefix)
            if before_apply and (baseline.sha256 != point["manifest"]["sha256"]
                                 or baseline.observation_sha256 != point["manifest"]["observation_sha256"]):
                raise maintenance.RecoveryRequired("Old prefix changed after point capture. Capture a fresh point")
            reference = journal["runners"][0] if newer and journal["runners"] else point["runtime"]
            runner = self.verify_pin(reference)
            boundary = runtime_boundary.inspect((self.context.install, source, self.root))
            facts = inspection_facts(boundary)
            executable = utils.find_installed_product("Logos", str(source))
            if executable is None or not Path(executable).resolve().is_relative_to(source):
                raise maintenance.RecoveryRequired("No supported local Logos executable is available for inspection")
            executable = "C:\\" + str(Path(executable).relative_to(source / "drive_c")).replace("/", "\\")
            operation_id = str(uuid.uuid4())
            directory = self.root / ("inspection-" + operation_id)
            directory.mkdir(mode=0o700)
            sync(self.root)
            store = runtime_runners.RunnerStore(reference["store"], reference["coordination"])
            helper = self.pin(store, runner.generation, "helper")
            record = {"schema": "complete-recovery-inspection-1", "id": operation_id,
                      "generation": "old-capability" if before_apply else "newer" if newer else "restored",
                      "point": point["id"], "result": "pending", "reason": "", "boundary": facts,
                      "runner": helper, "source": str(source), "baseline": baseline.sha256,
                      "source_observation": baseline.observation_sha256,
                      "process": None, "worker": None, "observations": None,
                      "directory_identity": runtime_filesystem.identity(directory), "completed_monotonic_ns": None,
                      "copy": None, "restarts": 0}
            write(directory / "inspection.json", record, checkpoint=self.checkpoint)
            receipt = runtime_copies.copy(source, directory / "prefix", logical_root=self.context.prefix,
                                          checkpoint=lambda: self.checkpoint("inspection-copy"))
            record["copy"] = receipt["copy"]
            write(directory / "inspection.json", record, checkpoint=self.checkpoint)
            (directory / "prefix/drive_c/M4Export").mkdir(mode=0o700, exist_ok=True)
            account = runtime_trials.source_account(source)
            private = directory / "private"
            runtime_boundary.prepare_private(private, wine_user=account)
            runtime_trials.write(private / "job.json", {"id": operation_id, "executable": executable,
                                                       "resource_uri": runtime_trials.local_resource(resource_uri),
                                                       "registry": [], "dependencies": [], "allow_restart": True})
            worker_path = Path(runtime_trials.__file__).with_name("runtime_trial_worker.py")
            arguments = dict(source=self.context.prefix, copied=directory / "prefix", private=private,
                             runner=runner.path, worker=worker_path,
                             wine_user=account)
            runtime_boundary.probe(runtime_boundary.command(boundary, **arguments, check=True))
            running = False
            authorized = False
            deadline = time.monotonic() + timeout
            with (private / "wine.log").open("xb") as log:
                child = subprocess.Popen(runtime_boundary.command(boundary, **arguments), stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=log, close_fds=True)
                try:
                    record["process"] = runtime_trials.identity(child.pid)
                    write(directory / "inspection.json", record, checkpoint=self.checkpoint)
                    while child.poll() is None:
                        previous = (record["worker"], record["result"], record["reason"], record["restarts"])
                        if time.monotonic() > deadline and record["result"] != "unknown":
                            record.update(result="unknown", reason="inspection-timeout")
                            write(directory / "inspection.json", record, checkpoint=self.checkpoint)
                        try:
                            worker = runtime_trials.worker_status(private, child, self.context.prefix,
                                                                 directory / "prefix", receipt, operation_id)
                        except FileNotFoundError:
                            time.sleep(0.1)
                            continue
                        record["worker"] = worker
                        if worker["phase"] == "unknown" and record["result"] != "unknown":
                            record.update(result="unknown", reason="inspection-recheck-required")
                        if worker["phase"] == "prepared" and not authorized and record["result"] != "unknown":
                            runtime_trials.write(private / "request.json", {"action": "launch"})
                            authorized = True
                        if stop is not None and stop.is_set():
                            runtime_trials.write(private / "request.json", {"action": "stop"})
                        if worker["phase"] == "running":
                            running = True
                            restarted = re.fullmatch(r"restart-([1-9][0-9]{0,2})", worker["reason"])
                            if restarted:
                                record["restarts"] = max(record["restarts"], int(restarted[1]))
                        current = (record["worker"], record["result"], record["reason"], record["restarts"])
                        if current != previous:
                            write(directory / "inspection.json", record, checkpoint=self.checkpoint)
                        status(dict(record))
                        if worker["phase"] == "ended":
                            runtime_trials.write(private / "request.json", {"action": "settle"})
                            child.wait(timeout=10)
                            break
                        time.sleep(0.2)
                    terminal = runtime_trials.read(private / "worker-status.json")
                    runtime_trials.validate_worker(terminal, operation_id)
                    if (record["result"] == "unknown" or child.returncode != 0
                            or terminal["phase"] != "ended" or terminal["returncode"] != 0
                            or terminal["reason"] != "processes-settled" or not running):
                        raise maintenance.RecoveryRequired("Offline inspection did not settle successfully")
                    after = runtime_copies.manifest(source, logical_root=self.context.prefix)
                    if after != baseline or after.observation_sha256 != baseline.observation_sha256:
                        raise maintenance.RecoveryRequired("Inspection source changed. Both generations remain guarded")
                    if inspection_facts(boundary) != facts:
                        raise maintenance.RecoveryRequired("Inspection implementation or prerequisites changed")
                    record["completed_monotonic_ns"] = time.monotonic_ns()
                    observations = observe()
                    runtime_stacks.fields(observations, "library resource documents restart")
                    if any(value is not True for value in observations.values()):
                        raise maintenance.RecoveryRequired("Offline inspection observations are incomplete")
                    if record["restarts"] < 1:
                        raise maintenance.RecoveryRequired("Restart the same offline inspection copy before confirming")
                    record.update(result="succeeded", reason="offline-user-inspection", observations=observations)
                    write(directory / "inspection.json", record, checkpoint=self.checkpoint)
                    owner = read(helper["owner"])
                    owner.update(phase="settled", revision=owner["revision"] + 1, runner_generations=[])
                    write(Path(helper["owner"]), owner, checkpoint=self.checkpoint)
                    store.release_pin(helper["reference"], settled_owner_sha256=artifacts.regular_hash(helper["owner"]))
                    if before_apply:
                        journal["capability"] = {"id": operation_id,
                                                 "sha256": artifacts.regular_hash(directory / "inspection.json")}
                        self.phase(journal, "point-verified", result="succeeded", reason="old-inspection-complete")
                    elif not newer:
                        journal["inspection"] = {"id": operation_id,
                                                 "sha256": artifacts.regular_hash(directory / "inspection.json")}
                        self.phase(journal, "returned", result="succeeded", reason="offline-inspection-complete")
                    return record
                except BaseException:
                    record.update(result="unknown", reason="inspection-recheck-required")
                    # Reporting and storage failures must not end a live namespace through parent death.
                    while child.poll() is None:
                        try:
                            if record["process"] is None:
                                record["process"] = runtime_trials.identity(child.pid)
                            worker = runtime_trials.worker_status(private, child, self.context.prefix,
                                                                 directory / "prefix", receipt, operation_id)
                            record["worker"] = worker
                            if stop is not None and stop.is_set():
                                runtime_trials.write(private / "request.json", {"action": "stop"})
                            write(directory / "inspection.json", record)
                            status(dict(record))
                            if worker["phase"] == "ended":
                                runtime_trials.write(private / "request.json", {"action": "settle"})
                                child.wait(timeout=10)
                        except Exception:
                            # The existing lease and durable runner pin remain owned during refusal.
                            pass
                        time.sleep(0.2)
                    try:
                        write(directory / "inspection.json", record)
                    except Exception:
                        # Preserve the original failure after ownership has safely ended.
                        pass
                    raise
