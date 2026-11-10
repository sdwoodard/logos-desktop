"""Detached ownership of verified offline trial copies and source admission."""

import hmac
import hashlib
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

import psutil

from . import artifacts, config_store, constants, instance, maintenance, paths, runtime_boundary, runtime_copies
from . import runtime_recipes, runtime_runners, runtime_stacks


MAX_RECORD = 65536
PHASES = ("resolved", "copying", "verifying", "ready", "launching", "running", "ending", "ended", "conflict")


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(runtime_stacks.canonical(value))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def read(path):
    for _ in range(8):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink not in (0, 1) or stat.S_IMODE(info.st_mode) & 0o077):
                raise maintenance.RecoveryRequired("Unsafe trial operation record")
            # An atomic publisher may withdraw the opened generation before fstat.
            if info.st_nlink == 0:
                continue
            return runtime_stacks.decode(source.read(MAX_RECORD + 1), MAX_RECORD)
    raise maintenance.MaintenanceBusy("Trial status is changing. Retry status")


def identity(pid):
    root = Path(f"/proc/{pid}")
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "start_ticks": int(fields[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "namespaces": {name: os.readlink(root / "ns" / name) for name in ("mnt", "net", "pid", "ipc")}}


def _record_status(directory):
    directory = runtime_copies.checked_directory(directory)
    record = read(directory / "operation.json")
    runtime_stacks.fields(record, "schema id revision phase result reason supervisor source copy worker progress "
                         "job_sha256 budget running_acknowledged cancel_requested failure_reason")
    if (record["schema"] != 1 or type(record["schema"]) is not int
            or str(uuid.UUID(record["id"])) != record["id"]
            or record["phase"] not in PHASES
            or record["result"] not in ("pending", "succeeded", "failed", "cancelled", "unknown")):
        raise maintenance.RecoveryRequired("Unsupported trial operation")
    runtime_stacks.positive(record["revision"])
    if type(record["running_acknowledged"]) is not bool or type(record["cancel_requested"]) is not bool:
        raise maintenance.RecoveryRequired("Invalid running ownership acknowledgment")
    if not isinstance(record["failure_reason"], str) or len(record["failure_reason"]) > 4096:
        raise maintenance.RecoveryRequired("Invalid trial failure history")
    runtime_stacks.digest(record["job_sha256"])
    if artifacts.regular_hash(directory / "job.json") != record["job_sha256"]:
        raise maintenance.RecoveryRequired("Trial immutable plan changed")
    if ((record["phase"] == "ended" and record["result"] not in ("succeeded", "failed", "cancelled"))
            or (record["phase"] == "conflict" and record["result"] != "unknown")
            or (record["phase"] not in ("ended", "conflict") and record["result"] != "pending")):
        raise maintenance.RecoveryRequired("Contradictory trial result")
    validate_identity(record["supervisor"])
    if record["copy"] is not None:
        receipt = record["copy"]
        runtime_stacks.fields(receipt, "manifest source copy methods")
        for name in ("source", "copy"):
            runtime_runners._directory_identity(receipt[name])
        if (receipt["source"]["canonical_path"] != record["source"]
                or receipt["copy"]["canonical_path"] != str(directory / "prefix")):
            raise maintenance.RecoveryRequired("Trial copy receipt belongs to different roots")
        runtime_stacks.fields(receipt["manifest"],
                             "schema format sha256 entries logical_bytes allocated_bytes observation_sha256")
        if (type(receipt["manifest"]["schema"]) is not int or receipt["manifest"]["schema"] != 1
                or receipt["manifest"]["format"] != "posix-base64-jsonl-v1"):
            raise maintenance.RecoveryRequired("Unsupported copy manifest format")
        for name in ("sha256", "observation_sha256"):
            runtime_stacks.digest(receipt["manifest"][name])
        runtime_stacks.fields(receipt["methods"], "reflink full")
        for value in [receipt["manifest"][name] for name in ("entries", "logical_bytes", "allocated_bytes")]:
            if type(value) is not int or value < 0:
                raise maintenance.RecoveryRequired("Invalid manifest size")
    if record["progress"] is not None:
        runtime_stacks.fields(record["progress"], "phase bytes total")
        progress = record["progress"]
        if (progress["phase"] not in ("copying", "verifying")
                or type(progress["bytes"]) is not int or type(progress["total"]) is not int
                or not 0 <= progress["bytes"] <= progress["total"]):
            raise maintenance.RecoveryRequired("Invalid copy progress")
    if not isinstance(record["source"], str) or not Path(record["source"]).is_absolute():
        raise maintenance.RecoveryRequired("Invalid trial source")
    if record["worker"] is not None:
        validate_worker(record["worker"], record["id"], host=True)
    if record["phase"] in ("launching", "running", "ending") and (record["worker"] is None or record["copy"] is None):
        raise maintenance.RecoveryRequired("Running trial lacks verified ownership")
    if record["phase"] == "running" and not record["running_acknowledged"]:
        raise maintenance.RecoveryRequired("Running trial lacks application acknowledgment")
    if record["phase"] == "running" and record["worker"]["phase"] != "running":
        raise maintenance.RecoveryRequired("Running trial has no running worker acknowledgment")
    if record["result"] == "succeeded":
        if (not record["running_acknowledged"] or record["copy"] is None or record["worker"] is None
                or record["worker"]["phase"] != "ended" or record["worker"]["reason"] != "processes-settled"
                or record["worker"]["returncode"] != 0 or record["cancel_requested"] or record["failure_reason"]):
            raise maintenance.RecoveryRequired("Successful trial lacks accepted execution evidence")
    return record


def status(directory):
    record = _record_status(Path(directory))
    if record["phase"] not in ("ended", "conflict"):
        try:
            live = identity(record["supervisor"]["pid"])
        except OSError:
            live = None
        if live != record["supervisor"]:
            record = dict(record, phase="conflict", result="unknown", reason="supervisor-unavailable")
            record["failure_reason"] = record["failure_reason"] or "supervisor-unavailable"
    return record


def validate_identity(value):
    runtime_stacks.fields(value, "pid start_ticks boot_id namespaces")
    runtime_stacks.positive(value["pid"])
    runtime_stacks.positive(value["start_ticks"])
    if str(uuid.UUID(value["boot_id"])) != value["boot_id"]:
        raise maintenance.RecoveryRequired("Invalid trial boot identity")
    runtime_stacks.fields(value["namespaces"], "mnt net pid ipc")
    for name, namespace in value["namespaces"].items():
        if not isinstance(namespace, str) or not namespace.startswith(name + ":[") or not namespace.endswith("]"):
            raise maintenance.RecoveryRequired("Invalid namespace identity")


def validate_worker(value, operation_id, *, host=False):
    fields = "schema operation phase identity reason returncode" + (" host_identity" if host else "")
    runtime_stacks.fields(value, fields)
    if (type(value["schema"]) is not int or value["schema"] != 1
            or value["phase"] not in ("preparing", "prepared", "running", "unknown", "ended")
            or value["operation"] != operation_id):
        raise maintenance.RecoveryRequired("Invalid trial worker status")
    worker = value["identity"]
    runtime_stacks.fields(worker, "pid start_ticks namespaces prefix_device prefix_inode")
    for key in ("pid", "start_ticks", "prefix_device", "prefix_inode"):
        runtime_stacks.positive(worker[key])
    runtime_stacks.fields(worker["namespaces"], "mnt net pid ipc")
    if not isinstance(value["reason"], str) or len(value["reason"]) > 4096:
        raise maintenance.RecoveryRequired("Invalid worker reason")
    if value["phase"] == "ended":
        if type(value["returncode"]) is not int:
            raise maintenance.RecoveryRequired("Worker did not report its launcher result")
    elif value["returncode"] is not None:
        raise maintenance.RecoveryRequired("Live worker has a terminal result")
    if host:
        validate_identity(value["host_identity"])
        if (value["host_identity"]["start_ticks"] != worker["start_ticks"]
                or value["host_identity"]["namespaces"] != worker["namespaces"]):
            raise maintenance.RecoveryRequired("Worker host mapping changed")


def worker_status(private, child, logical_prefix, copied, receipt, operation_id):
    value = read(private / "worker-status.json")
    validate_worker(value, operation_id)
    worker = value["identity"]
    expected = copied.stat()
    if not runtime_copies.runtime_filesystem.same_live_root(receipt["copy"], copied):
        raise maintenance.RecoveryRequired("Verified copy root was replaced")
    for process in [psutil.Process(child.pid), *psutil.Process(child.pid).children(recursive=True)]:
        try:
            root = Path(f"/proc/{process.pid}")
            inside_pid = next(int(line.split()[-1]) for line in (root / "status").read_text().splitlines()
                              if line.startswith("NSpid:"))
            observed = identity(process.pid)
            if (inside_pid != worker["pid"] or observed["start_ticks"] != worker["start_ticks"]
                    or observed["namespaces"] != worker["namespaces"]):
                continue
            prefix = (root / "root" / str(logical_prefix).lstrip("/")).stat()
            if (prefix.st_dev, prefix.st_ino) != (expected.st_dev, expected.st_ino):
                raise maintenance.RecoveryRequired("Worker does not own the copied prefix")
            if (worker["prefix_device"], worker["prefix_inode"]) != (expected.st_dev, expected.st_ino):
                raise maintenance.RecoveryRequired("Worker reported a different prefix")
            value["host_identity"] = observed
            return value
        except (FileNotFoundError, ProcessLookupError, psutil.NoSuchProcess):
            continue
    raise maintenance.RecoveryRequired("Cannot prove copied namespace ownership")


def request(directory, action):
    if action not in ("status", "cancel", "stop"):
        raise ValueError("Unknown trial command")
    directory = runtime_copies.checked_directory(directory)
    record = status(directory)
    try:
        live = identity(record["supervisor"]["pid"])
    except (OSError, KeyError):
        raise maintenance.RecoveryRequired("Trial owner is unavailable. Preserve its reservation") from None
    if live != record["supervisor"]:
        raise maintenance.RecoveryRequired("Trial owner identity changed. Preserve its reservation")
    capability = read(directory / "capability.json")["capability"]
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(3)
        peer.connect(str(endpoint(record["id"])))
        credentials = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if credentials[:2] != (live["pid"], os.getuid()):
            raise maintenance.RecoveryRequired("Trial endpoint does not match its owner")
        peer.sendall(runtime_stacks.canonical({"action": action, "capability": capability}) + b"\n")
        response = peer.makefile("rb").readline(MAX_RECORD + 1)
    return runtime_stacks.decode(response, MAX_RECORD)


def endpoint(operation_id):
    if str(uuid.UUID(operation_id)) != operation_id:
        raise maintenance.MaintenanceError("Invalid trial endpoint identity")
    path = Path(constants.COORDINATION_DIR) / "trials" / (operation_id.replace("-", "")[:24] + ".sock")
    if len(os.fsencode(path)) > 100:
        raise maintenance.MaintenanceError("Stable trial coordination path exceeds Unix socket limits")
    return path


def profile_snapshot(path):
    path = Path(path)
    runtime_copies.checked_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise maintenance.MaintenanceError("Trial profile must be an owned regular file")
        content = source.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise maintenance.MaintenanceError("Trial profile exceeds its size bound")
    document = config_store.canonical(config_store.decode(content, path), path)
    install = document.get("install_dir")
    prefix = document.get("WINEPREFIX") or (str(Path(install) / "data/wine64_bottle") if install else None)
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
            "install": install, "prefix": prefix, "product": document.get("faithlife_product")}


def verify_profile(snapshot, context):
    current = profile_snapshot(snapshot["path"])
    if (current != snapshot or current["install"] != str(context.install)
            or current["prefix"] != str(context.prefix) or current["product"] != context.product):
        raise maintenance.RecoveryRequired("Selected trial profile changed")


def register_profile(snapshot, context):
    root = Path(constants.COORDINATION_DIR) / "trials"
    maintenance.private_directory(root)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock = maintenance._operation_lock(directory, "profiles.lock", exclusive=True)
        try:
            _register_profile(snapshot, context)
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def _register_profile(snapshot, context):
    registry_path = Path(constants.COORDINATION_DIR) / "trials/profiles.json"
    maintenance.private_directory(registry_path.parent)
    if registry_path.exists():
        registry = read(registry_path)
        runtime_stacks.fields(registry, "schema profiles")
        if type(registry["schema"]) is not int or registry["schema"] != 1 or type(registry["profiles"]) is not list:
            raise maintenance.RecoveryRequired("Unknown trial profile registration")
        known = set(registry["profiles"])
    else:
        operations = Path(constants.COORDINATION_DIR) / "operations"
        if any(operations.glob("*/trial-profile.json")):
            raise maintenance.RecoveryRequired("Known trial profile registration is incomplete")
        known = set()
    roots = {Path(constants.CONFIG_DIR), Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/logos-desktop"}
    for root in roots:
        if root.exists():
            runtime_copies.checked_directory(root)
            for path in root.glob("*.json"):
                known.add(str(path))
                if len(known) > 64:
                    raise maintenance.RecoveryRequired("Too many known saved profiles for an offline trial")
    known.add(snapshot["path"])
    for path in sorted(known):
        saved = profile_snapshot(path)
        if path != snapshot["path"] and saved["prefix"] is not None:
            other = Path(saved["prefix"])
            if other.resolve() == context.prefix or (other.exists() and other.samefile(context.prefix)):
                raise runtime_copies.CopyError("Prefix is shared by known saved profiles", "unsupported-shared-prefix")
    write(registry_path, {"schema": 1, "profiles": sorted(known)})
    descriptor = maintenance._operation_directory(context)
    try:
        location = Path(os.readlink(f"/proc/self/fd/{descriptor}")) / "trial-profile.json"
        if location.exists():
            registered = read(location)
            if registered != {"schema": 1, "profile": snapshot["path"], "prefix": str(context.prefix)}:
                raise runtime_copies.CopyError("Prefix is registered to another saved profile",
                                              "unsupported-shared-prefix")
        else:
            write(location, {"schema": 1, "profile": snapshot["path"], "prefix": str(context.prefix)})
    finally:
        os.close(descriptor)


def registry_plan(settings):
    for key, value in settings.items():
        if (key not in runtime_stacks.SETTING_VALUES
                or not any(type(value) is type(option) and value == option
                           for option in runtime_stacks.SETTING_VALUES[key])):
            raise maintenance.RecoveryRequired("Unsupported recipe setting")
    return [list(entry) for key, value in settings.items()
            for entry in runtime_recipes._setting_entries(key, value)]


def source_account(prefix):
    root = runtime_copies._open_root(prefix)
    try:
        directory = runtime_copies._open_parent(root, b"drive_c/users/.")
        try:
            users = []
            for name in os.listdir(directory):
                if name.lower() == "public":
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                    raise maintenance.MaintenanceError("Wine account directory ownership is unsupported")
                users.append(runtime_boundary.account_name(name))
        finally:
            os.close(directory)
    except OSError as error:
        raise maintenance.MaintenanceError("Cannot inspect the source Wine account without following links") from error
    finally:
        os.close(root)
    if len(users) != 1:
        raise maintenance.MaintenanceError("Offline trial requires one unambiguous source Wine account")
    return users[0]


def local_resource(value):
    if value is not None and (not isinstance(value, str) or re.fullmatch(
            r"logosres:[a-z0-9][a-z0-9._-]{0,95}(?:;ref=[A-Za-z0-9._-]{1,128})?", value) is None):
        raise maintenance.MaintenanceError("Offline inspection accepts only a bounded local resource reference")
    return value


def validate_job(job):
    runtime_stacks.fields(job, "schema id source install product runner sha256 store coordination "
                         "generation executable profile controller session_fd session_identity "
                         "accepted settings wine_user resource_uri")
    if type(job["schema"]) is not int or job["schema"] != 1 or job["product"] != "Logos":
        raise maintenance.RecoveryRequired("Unsupported trial delegation")
    for name in ("id", "generation"):
        if str(uuid.UUID(job[name])) != job[name]:
            raise maintenance.RecoveryRequired("Invalid trial identity")
    for name in ("source", "install", "runner", "store", "coordination"):
        if not isinstance(job[name], str) or not Path(job[name]).is_absolute():
            raise maintenance.RecoveryRequired("Invalid trial path")
    runtime_stacks.digest(job["sha256"])
    if not isinstance(job["wine_user"], str):
        raise maintenance.RecoveryRequired("Trial delegation needs an explicit Wine account")
    runtime_boundary.account_name(job["wine_user"])
    local_resource(job["resource_uri"])
    runtime_stacks.fields(job["profile"], "path sha256 install prefix product")
    runtime_stacks.digest(job["profile"]["sha256"])
    validate_identity(job["controller"])
    runtime_stacks.positive(job["session_fd"])
    if type(job["session_identity"]) is not list or len(job["session_identity"]) != 2:
        raise maintenance.RecoveryRequired("Invalid controller session identity")
    for value in job["session_identity"]:
        runtime_stacks.positive(value)
    runtime_stacks.fields(job["accepted"], "catalog stack recipe recipe_id recipe_revision dependencies")
    for name in ("catalog", "recipe"):
        runtime_stacks.digest(job["accepted"][name])
    runtime_stacks.identifier(job["accepted"]["stack"])
    catalog = runtime_stacks.bundled()
    stack = catalog.stacks.get(job["accepted"]["stack"])
    if catalog.sha256 != job["accepted"]["catalog"] or stack is None:
        raise maintenance.RecoveryRequired("Trial catalog or stack does not match this controller")
    recipe = catalog.recipes[stack.recipe_id]
    runner = catalog.runners[stack.runner_id]
    if (job["accepted"]["recipe"] != recipe.sha256 or job["accepted"]["recipe_id"] != recipe.id
            or type(job["accepted"]["recipe_revision"]) is not int
            or job["accepted"]["recipe_revision"] != recipe.revision or runner.sha256 != job["sha256"]):
        raise maintenance.RecoveryRequired("Trial does not bind the accepted recipe and runner")
    if type(job["settings"]) is not dict:
        raise maintenance.RecoveryRequired("Invalid recipe settings")
    registry_plan(job["settings"])
    if job["settings"] != dict(recipe.settings):
        raise maintenance.RecoveryRequired("Recipe binding changed")
    dependencies = job["accepted"]["dependencies"]
    if type(dependencies) is not list or len(dependencies) > 32:
        raise maintenance.RecoveryRequired("Invalid trial dependency list")
    expected = {item.id: item for item in recipe.dependencies}
    if len(dependencies) != len(expected):
        raise maintenance.RecoveryRequired("Trial dependency graph is incomplete")
    seen = set()
    for item in dependencies:
        runtime_stacks.fields(item, "id path sha256 role files version layout")
        runtime_stacks.digest(item["sha256"])
        original = expected.get(item["id"])
        if original is None or item["id"] in seen:
            raise maintenance.RecoveryRequired("Trial dependency graph changed")
        seen.add(item["id"])
        if any(item[key] != getattr(original, key) for key in ("role", "sha256", "version", "layout")):
            raise maintenance.RecoveryRequired("Trial dependency does not match its recipe")
        if (not isinstance(item["path"], str) or not Path(item["path"]).is_absolute()
                or item["role"] not in ("icu", "winetricks")):
            raise maintenance.RecoveryRequired("Unsupported trial dependency")
        if item["role"] == "icu":
            if item["files"] != [list(entry) for entry in runtime_recipes.inspect_icu(item["path"])]:
                raise maintenance.RecoveryRequired("Trial ICU observations changed")
        elif item["files"]:
            raise maintenance.RecoveryRequired("Unexpected dependency file mapping")
    executable = job["executable"]
    if (not isinstance(executable, str) or not executable.startswith("C:\\")
            or not executable.lower().endswith(".exe") or len(executable) > 4096
            or any(component in ("", ".", "..") for component in executable[3:].split("\\"))):
        raise maintenance.RecoveryRequired("Select an executable inside the copied C drive")


def start(context, prepared, *, parent, store, generation, executable, profile, session, resource_uri=None):
    """Delegate admitted preparation to a private detached supervisor."""
    if context.install is None or context.prefix != context.install / "data/wine64_bottle":
        raise runtime_copies.CopyError("Offline trials require a managed prefix", "unsupported-external-prefix")
    runtime_copies.checked_directory(context.install)
    runtime_copies.layout(context.prefix)
    parent = runtime_copies.checked_directory(parent)
    if not paths.disjoint(context.install, parent):
        raise runtime_copies.CopyError("Trial storage overlaps installation")
    if not runtime_copies.physical_disjoint(runtime_copies.runtime_filesystem.identity(context.install),
                                           runtime_copies.runtime_filesystem.identity(parent)):
        raise runtime_copies.CopyError("Trial storage physically overlaps installation")
    prepared.check()
    if (not isinstance(session, instance.Session) or session._descriptor is None
            or session.prefix != context.prefix):
        raise maintenance.MaintenanceError("Trial delegation needs the selected controller session")
    snapshot = profile_snapshot(profile)
    verify_profile(snapshot, context)
    wine_user = source_account(context.prefix)
    controller = identity(os.getpid())
    session_info = os.fstat(session._descriptor)
    selected = next((copy for copy in store.inventory() if copy.generation == generation), None)
    if selected is None or selected.path != prepared.runner.path or selected.sha256 != prepared.runner.sha256:
        raise maintenance.MaintenanceError("Trial runner must bind one verified managed generation")
    runtime_boundary.inspect((context.install, Path(snapshot["path"])))
    operation_id = str(uuid.uuid4())
    directory = parent / operation_id
    endpoint(operation_id)
    directory.mkdir(mode=0o700)
    capability = secrets.token_hex(32)
    write(directory / "capability.json", {"capability": capability})
    job = {"schema": 1, "id": operation_id, "source": str(context.prefix),
           "install": str(context.install), "product": context.product,
           "runner": str(prepared.runner.path), "sha256": prepared.runner.sha256,
           "store": str(store.root), "coordination": str(store.coordination),
           "generation": generation, "executable": executable, "profile": snapshot,
           "settings": dict(prepared.recipe.settings), "wine_user": wine_user,
           "resource_uri": local_resource(resource_uri),
           "controller": controller, "session_fd": session._descriptor,
           "session_identity": [session_info.st_dev, session_info.st_ino],
           "accepted": {"catalog": prepared.catalog_sha256, "stack": prepared.stack.id,
                        "recipe": prepared.recipe.sha256, "recipe_id": prepared.recipe.id,
                        "recipe_revision": prepared.recipe.revision,
                        "dependencies": [{"id": item.dependency.id, "path": str(item.artifact.path),
                                          "sha256": item.artifact.sha256,
                                          "role": item.dependency.role,
                                          "layout": item.dependency.layout, "version": item.dependency.version,
                                          "files": [list(entry) for entry in item.files]}
                                         for item in prepared.dependencies]}}
    validate_job(job)
    write(directory / "job.json", job)
    with (directory / "supervisor.log").open("xb") as log:
        child = subprocess.Popen([sys.executable, "-m", "logos_desktop.runtime_trials", str(directory)],
                                 stdin=subprocess.PIPE, stdout=log, stderr=log,
                                 close_fds=True, start_new_session=True)
        child.stdin.write(capability.encode("ascii") + b"\n")
        child.stdin.close()
    task = TrialTask(directory, child)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            record = task.snapshot()
            if record["phase"] != "resolved" or record["reason"] == "owned-inspection":
                return task
        except FileNotFoundError:
            pass
        if child.poll() is not None:
            raise maintenance.MaintenanceError(f"Trial preparation failed. Inspect private operation {operation_id}")
        time.sleep(0.05)
    try:
        task.cancel()
    except (OSError, maintenance.MaintenanceError):
        pass
    raise maintenance.RecoveryRequired(f"Trial ownership acknowledgment timed out. Preserve operation {operation_id}")


class TrialTask:
    def __init__(self, directory, process):
        self.directory = directory
        self.process = process

    def snapshot(self):
        return status(self.directory)

    def cancel(self):
        return request(self.directory, "cancel")

    def stop(self):
        return request(self.directory, "stop")

    def close(self, timeout=30):
        record = self.snapshot()
        if record["phase"] not in ("ended", "conflict") and not record.get("running_acknowledged", False):
            response = self.cancel()
            if not response.get("running_acknowledged", False):
                self.process.wait(timeout=timeout)


def supervise(directory, capability):
    os.umask(0o077)
    directory = runtime_copies.checked_directory(directory)
    if not hmac.compare_digest(read(directory / "capability.json")["capability"], capability):
        raise maintenance.MaintenanceError("Trial delegation was not authenticated")
    job = read(directory / "job.json")
    validate_job(job)
    if job["id"] != directory.name or type(job["schema"]) is not int or job["schema"] != 1:
        raise maintenance.MaintenanceError("Trial delegation identity changed")
    context = maintenance.Context(Path(job["source"]), Path(job["install"]), job["product"])
    if context.prefix != context.install / "data/wine64_bottle":
        raise maintenance.MaintenanceError("Trial delegation has an unsupported prefix")
    runtime_copies.checked_directory(context.install)
    runtime_copies.checked_directory(directory.parent)
    validate_identity(job["controller"])
    if job["controller"] != identity(os.getppid()):
        raise maintenance.MaintenanceError("Trial delegation does not belong to its selected parent controller")
    verify_profile(job["profile"], context)
    store = runtime_runners.RunnerStore(job["store"], job["coordination"])
    record = {"schema": 1, "id": job["id"], "revision": 1, "phase": "resolved", "result": "pending",
              "reason": "", "supervisor": identity(os.getpid()), "source": job["source"], "copy": None,
              "worker": None, "progress": None, "job_sha256": artifacts.regular_hash(directory / "job.json"),
              "budget": None, "running_acknowledged": False, "cancel_requested": False, "failure_reason": ""}
    write(directory / "operation.json", record)
    cancelled = False
    namespace_cancelled = False
    marker = {"schema": "runtime-admission-1", "operation_id": job["id"], "directory": str(directory)}
    reference = None
    child = None
    marker_owned = False
    listener = socket.socket(socket.AF_UNIX)
    control = endpoint(job["id"])
    maintenance.private_directory(control.parent)
    listener.bind(str(control))
    os.chmod(control, 0o600)
    listener.listen(4)
    listener.setblocking(False)

    def publish(phase, result="pending", reason=""):
        if phase == "conflict" and reason:
            record["failure_reason"] = record["failure_reason"] or reason
        record.update(phase=phase, result=result, reason=reason, revision=record["revision"] + 1)
        write(directory / "operation.json", record)

    def checkpoint():
        nonlocal cancelled, namespace_cancelled
        if (not record["running_acknowledged"] and not record["cancel_requested"]
                and record["phase"] not in ("conflict", "ended")):
            try:
                parent = identity(job["controller"]["pid"])
                session_info = Path(f'/proc/{parent["pid"]}/fd/{job["session_fd"]}').stat()
                if parent != job["controller"] or [session_info.st_dev, session_info.st_ino] != job["session_identity"]:
                    cancelled = True
            except OSError:
                cancelled = True
            if cancelled:
                record["cancel_requested"] = True
            if cancelled and child is not None:
                namespace_cancelled = True
                record["cancel_requested"] = True
                write(directory / "private/request.json", {"action": "cancel"})
                if record["worker"] is not None:
                    publish("ending")
                cancelled = False
        try:
            peer, _ = listener.accept()
        except (TimeoutError, BlockingIOError):
            if cancelled:
                raise runtime_copies.Cancelled()
            return
        with peer:
            peer.settimeout(0.5)
            credentials = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if credentials[1] != os.getuid():
                return
            try:
                command = runtime_stacks.decode(peer.makefile("rb").readline(4097), 4096)
                if not hmac.compare_digest(command.get("capability", ""), capability):
                    return
                if command.get("action") == "cancel" and child is None:
                    record["cancel_requested"] = True
                    cancelled = True
                elif command.get("action") == "stop" and child is None:
                    record["cancel_requested"] = True
                    cancelled = True
                elif command.get("action") == "cancel" and record["phase"] == "ready":
                    namespace_cancelled = True
                    record["cancel_requested"] = True
                    write(directory / "private/request.json", {"action": "cancel"})
                elif command.get("action") == "cancel" and record["phase"] == "launching":
                    namespace_cancelled = True
                    record["cancel_requested"] = True
                    write(directory / "private/request.json", {"action": "stop"})
                    publish("ending")
                elif command.get("action") == "stop" and child is not None:
                    if not record["running_acknowledged"]:
                        namespace_cancelled = True
                        record["cancel_requested"] = True
                    write(directory / "private/request.json", {"action": "stop"})
                    if record["worker"] is not None:
                        publish("ending")
                if command.get("action") in ("cancel", "stop"):
                    publish(record["phase"], record["result"], record["reason"])
                peer.sendall(runtime_stacks.canonical(record) + b"\n")
            except (ValueError, OSError, TypeError):
                pass
        if cancelled:
            raise runtime_copies.Cancelled()

    owner_path = directory / "runner-owner.json"
    baseline = None
    baseline_identity = None
    with listener, maintenance.Lease(context, exclusive=True, prefix_only=True).entered():
        try:
            publish("resolved", reason="owned-inspection")
            maintenance.assert_quiescent(context, controller=job["controller"])
            boundary = runtime_boundary.inspect((context.install, Path(job["profile"]["path"])), checkpoint=checkpoint)
            baseline_identity = runtime_copies.layout(context.prefix)
            verify_profile(job["profile"], context)
            if source_account(context.prefix) != job["wine_user"]:
                raise maintenance.RecoveryRequired("Source Wine account changed before trial admission")
            register_profile(job["profile"], context)
            baseline = runtime_copies.manifest(context.prefix, checkpoint=checkpoint)
            extraction = runtime_boundary.expansion_budget(job["runner"], checkpoint=checkpoint)
            private_budget = extraction["bytes"] + 512 * 1024 * 1024
            capacity = runtime_copies.require_capacity(directory, baseline.logical_bytes + private_budget,
                                                       baseline.entries)
            record["budget"] = {"filesystem": runtime_copies.runtime_filesystem.identity(directory),
                                "copy_bytes": baseline.logical_bytes, "entry_count": baseline.entries,
                                "extraction": extraction, "private_bytes": 512 * 1024 * 1024,
                                "capacity": capacity, "future_recovery_reserved": False}
            write(owner_path, {"schema": "runtime-runner-owner-2", "record": "trial", "id": job["id"],
                               "revision": 1, "phase": "pending", "runner_generations": [job["generation"]]})
            reference = store.pin(job["generation"], kind="trial", owner_path=owner_path,
                                  owner_sha256=artifacts.regular_hash(owner_path))
            with maintenance.artifact_use():
                if artifacts.regular_hash(job["runner"]) != job["sha256"]:
                    raise maintenance.MaintenanceError("Trial artifact changed")
                descriptor = os.open(context.pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    marker_owned = True
                    output.write(runtime_stacks.canonical(marker))
                    output.flush()
                    os.fsync(output.fileno())
                sync_directory(context.state)
                publish("copying")

                def progress(value):
                    record["progress"] = value
                    publish(value["phase"])

                receipt = runtime_copies.copy(context.prefix, directory / "prefix",
                                              checkpoint=checkpoint, status=progress)
                write(directory / "copy.json", receipt)
                record["copy"] = receipt
                private = directory / "private"
                runtime_boundary.prepare_private(private, wine_user=job["wine_user"])
                for item in job["accepted"]["dependencies"]:
                    if artifacts.regular_hash(item["path"]) != item["sha256"]:
                        raise maintenance.RecoveryRequired("Trial dependency changed")
                write(private / "job.json", {"id": job["id"], "executable": job["executable"],
                                             "resource_uri": job["resource_uri"],
                                             "registry": registry_plan(job["settings"]),
                                             "dependencies": job["accepted"]["dependencies"]})
                worker = Path(__file__).with_name("runtime_trial_worker.py")
                arguments = dict(source=context.prefix, copied=directory / "prefix", private=private,
                                 runner=job["runner"], worker=worker,
                                 dependencies=job["accepted"]["dependencies"], wine_user=job["wine_user"])
                runtime_boundary.probe(runtime_boundary.command(boundary, **arguments, check=True))
                runtime_copies.require_capacity(directory, private_budget)
                verify_profile(job["profile"], context)
                if artifacts.regular_hash(directory / "job.json") != record["job_sha256"]:
                    raise maintenance.RecoveryRequired("Trial immutable plan changed before launch")
                if artifacts.regular_hash(job["runner"], checkpoint=checkpoint) != job["sha256"]:
                    raise maintenance.RecoveryRequired("Pinned trial runner changed before launch")
                checkpoint()
                source_now = runtime_copies.manifest(context.prefix, checkpoint=checkpoint)
                copied_now = runtime_copies.manifest(directory / "prefix", logical_root=context.prefix,
                                                     checkpoint=checkpoint)
                if (source_now != baseline or source_now.observation_sha256 != baseline.observation_sha256
                        or copied_now.sha256 != receipt["manifest"]["sha256"]
                        or not runtime_copies.runtime_filesystem.same_retained_root(
                            receipt["copy"], runtime_copies.runtime_filesystem.identity(directory / "prefix"))):
                    raise runtime_copies.CopyError("Verified source or copy changed before launch", "source-changed")
                publish("ready")
                with (private / "wine.log").open("xb") as log:
                    child = subprocess.Popen(runtime_boundary.command(boundary, **arguments),
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
                    deadline = time.monotonic() + 30
                    acknowledged = False
                    launch_authorized = False
                    while child.poll() is None:
                        checkpoint()
                        try:
                            value = worker_status(private, child, context.prefix, directory / "prefix",
                                                  receipt, job["id"])
                            if value["phase"] in ("preparing", "prepared"):
                                acknowledged = True
                                previous = record["worker"]
                                record["worker"] = value
                                if previous is None or previous["phase"] != value["phase"]:
                                    publish(record["phase"], record["result"], record["reason"])
                                if value["phase"] == "prepared" and not namespace_cancelled and not launch_authorized:
                                    checkpoint()
                                    if not namespace_cancelled:
                                        write(private / "request.json", {"action": "launch"})
                                        launch_authorized = True
                                        publish("launching")
                            if value["phase"] == "ended":
                                record["worker"] = value
                                write(private / "request.json", {"action": "settle"})
                                child.wait(timeout=10)
                                break
                            if value["phase"] == "running":
                                record["worker"] = value
                                if not namespace_cancelled:
                                    record["running_acknowledged"] = True
                                    publish("running")
                                break
                            if value["phase"] == "unknown":
                                record["worker"] = value
                                publish("conflict", "unknown", value["reason"])
                                break
                        except FileNotFoundError:
                            pass
                        if not acknowledged and time.monotonic() > deadline:
                            raise maintenance.RecoveryRequired("Worker acknowledgment timed out")
                        time.sleep(0.1)
                    while child.poll() is None:
                        checkpoint()
                        value = worker_status(private, child, context.prefix, directory / "prefix", receipt, job["id"])
                        if value["phase"] == "ended":
                            record["worker"] = value
                            write(private / "request.json", {"action": "settle"})
                            child.wait(timeout=10)
                            break
                        if value["phase"] == "unknown":
                            publish("conflict", "unknown", value["reason"])
                        time.sleep(0.1)
                if child.returncode != 0:
                    publish("conflict", "unknown", "namespace-exit-unknown")
                    return
                terminal = read(private / "worker-status.json")
                validate_worker(terminal, job["id"])
                preparation_cancelled = terminal.get("reason") == "cancelled-before-launch"
                if (not record["worker"] or terminal["identity"] != record["worker"]["identity"]
                        or (preparation_cancelled and not namespace_cancelled)):
                    publish("conflict", "unknown", "terminal-worker-mismatch")
                    return
                if (terminal.get("phase") != "ended"
                        or (not record["worker"] and not preparation_cancelled)):
                    publish("conflict", "unknown", "worker-result-unaccepted")
                    return
                publish("ending", reason="verifying-source")
                final_source = runtime_copies.manifest(context.prefix, checkpoint=checkpoint)
                unchanged = (final_source == baseline
                             and final_source.observation_sha256 == baseline.observation_sha256
                             and runtime_copies.runtime_filesystem.same_retained_root(
                                 baseline_identity, runtime_copies.layout(context.prefix)))
                if not unchanged:
                    publish("conflict", "unknown", "source-changed")
                    return
                result = ("failed" if record["failure_reason"] else
                          "cancelled" if record["cancel_requested"] or preparation_cancelled else
                          "succeeded" if record["running_acknowledged"] and terminal["reason"] == "processes-settled"
                          and terminal["returncode"] == 0
                          else "failed")
                publish("ended", result, terminal["reason"])
        except BaseException as error:
            if child is not None:
                publish("conflict", "unknown", "supervision-interrupted")
                raise
            current = runtime_copies.manifest(context.prefix) if marker_owned else None
            if marker_owned and (baseline is None or current != baseline
                                 or current.observation_sha256 != baseline.observation_sha256
                                 or not runtime_copies.runtime_filesystem.same_retained_root(
                                     baseline_identity, runtime_copies.layout(context.prefix))):
                publish("conflict", "unknown", "source-changed")
            else:
                publish("ended", "cancelled" if isinstance(error, runtime_copies.Cancelled) else "failed",
                        getattr(error, "reason", "preparation-failed"))
        finally:
            listener.close()
            if record["phase"] == "ended":
                control.unlink()
                if marker_owned:
                    if read(context.pending) != marker:
                        raise maintenance.RecoveryRequired("Trial marker changed. Preserve operation")
                    context.pending.unlink()
                    sync_directory(context.state)
                if reference:
                    write(owner_path, {"schema": "runtime-runner-owner-2", "record": "trial", "id": job["id"],
                                       "revision": 2, "phase": "settled", "runner_generations": []})
                    store.release_pin(reference, settled_owner_sha256=artifacts.regular_hash(owner_path))


def run(directory, capability):
    try:
        supervise(directory, capability)
    except BaseException:
        path = directory / "operation.json"
        if path.exists():
            record = read(path)
            context = maintenance.Context(Path(record["source"]))
            if record["phase"] not in ("ended", "conflict"):
                guarded = context.needs_recovery()
                record.update(phase="conflict" if guarded else "ended", result="unknown" if guarded else "failed",
                              reason="supervisor-startup-failed", revision=record["revision"] + 1)
                write(path, record)
        raise


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.stdin.buffer.readline(128).decode("ascii").strip())
