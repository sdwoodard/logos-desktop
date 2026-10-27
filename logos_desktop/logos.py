import os
from pathlib import Path
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
import logging
import psutil
import threading

from logos_desktop import constants, database, maintenance, runtime
from logos_desktop.app import App
from logos_desktop.config import get_logos_user_id

from . import wine


# Linux comm exposes at most 15 bytes of a Windows executable's process name.
INDEXER_PROCESS_NAMES = {"logosindexer.exe"}
INDEXER_PROCESS_NAMES |= {name[:15] for name in INDEXER_PROCESS_NAMES}
_CURRENT_POLICY_TARGET = object()


class State(Enum):
    RUNNING = 1
    STOPPED = 2
    STARTING = 3
    STOPPING = 4


@dataclass
class RunResult:
    outcome: str
    message: str
    returncode: int | None = None


@dataclass
class RunPolicies:
    configuration: object
    target: tuple[Path, str] | None = None
    paths: tuple[Path, ...] = ()
    pending: bool = True
    error: str = ""


@dataclass
class ManagedRun:
    context: maintenance.Context
    process: subprocess.Popen
    lease: maintenance.Lease
    started: float = field(default_factory=time.monotonic)
    empty_since: float | None = None
    stopped: bool = False
    handed_off: bool = False
    members: dict = field(default_factory=dict)
    direct_worker: bool = False
    result: RunResult | None = None
    policies: RunPolicies | None = None


class LogosManager:
    def __init__(self, app: App):
        self.logos_state = State.STOPPED
        self.indexing_state = State.STOPPED
        self.app = app
        self.processes: dict[str, subprocess.Popen] = {}
        """These are sub-processes we started"""
        self.existing_processes: dict[str, list[psutil.Process]] = {}
        """These are processes we discovered already running"""
        self._watchers = {}
        self._watcher_lock = threading.Lock()
        self._watcher_stopping = False
        self._watcher_policies = {}
        self._watcher_health = {}
        self.enforcement_error = ""
        self._transition = threading.RLock()
        self._closed = threading.Event()
        self._monitor_thread = None
        self._runs = {}
        self._context = None
        self._expected = ()
        self._next_scan = 0
        self._login_checked = False
        self._login_notice = False
        self._snapshot = []

    def start_monitoring(self):
        with self._transition:
            if self._closed.is_set():
                raise maintenance.MaintenanceBusy("Controller is closing")
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._next_scan = 0

            def observe():
                while True:
                    # Wake at the scan deadline without delaying explicit transitions.
                    delay = min(1, max(0, self._next_scan - time.monotonic()))
                    if self._closed.wait(delay):
                        return
                    try:
                        self.monitor()
                    except Exception:
                        logging.exception("Could not refresh the selected installation state")

            self._monitor_thread = self.app.start_thread(observe, daemon_bool=True)

    def close(self, timeout=3):
        """Detach observations without signaling Logos or its indexer."""
        self._closed.set()
        thread = self._monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                return False
        if not self._transition.acquire(blocking=False):
            return False
        try:
            try:
                self.stop_watchers(timeout=timeout)
            except maintenance.MaintenanceBusy as error:
                self.enforcement_error = str(error)
                return False
            for run in self._runs.values():
                run.lease.close()
            self._runs.clear()
            self.processes.clear()
            self.existing_processes.clear()
            return True
        finally:
            self._transition.release()

    def stop_watchers(self, timeout=3):
        with self._watcher_lock:
            self._watcher_stopping = True
            handles = list(self._watchers.values())
            for stop, thread in handles:
                stop.set()
        deadline = time.monotonic() + timeout
        for stop, thread in handles:
            thread.join(timeout=max(0, deadline - time.monotonic()))
            if thread.is_alive():
                raise maintenance.MaintenanceBusy("A database watcher has not stopped")
        with self._watcher_lock:
            for path, handle in list(self._watchers.items()):
                if handle in handles:
                    del self._watchers[path]
                    self._watcher_policies.pop(path, None)
                    self._watcher_health.pop(path, None)
            self._watcher_stopping = False

    @maintenance.uses_owner
    def _watch_database(self, path, sql, *, wait_ready=True):
        path = Path(path).resolve()
        with self._watcher_lock:
            if (self._closed.is_set() or self._watcher_stopping
                    or getattr(self.app, "_closing", self._closed).is_set()):
                raise maintenance.MaintenanceBusy("Controller is closing")
            old = self._watchers.get(path)
            if old and old[1].is_alive():
                if self._watcher_policies.get(path) != tuple(sql):
                    raise maintenance.MaintenanceBusy("Stop the current database policy before replacing it")
                stop, thread = old
                health = self._watcher_health[path]
            else:
                stop = threading.Event()
                previous = self._watcher_health.get(path)
                health = database.WatcherHealth(error=previous.error if previous else "")
                thread = maintenance.spawn(self.app, database.watch_db, str(path), sql, stop, health)
                self._watchers[path] = (stop, thread)
                self._watcher_policies[path] = tuple(sql)
                self._watcher_health[path] = health
        if not wait_ready:
            return path
        deadline = time.monotonic() + 3
        while not health.ready.wait(0.1):
            if self._closed.is_set() or time.monotonic() >= deadline:
                stop.set()
                raise maintenance.MaintenanceBusy("Database protection was not ready. Launch cancelled")
        if health.error:
            raise maintenance.MaintenanceError(f"Database protection failed: {health.error}")
        if not thread.is_alive():
            raise maintenance.MaintenanceError("Database protection worker stopped before launch")
        return path

    def _policy_target(self, configuration):
        directory = configuration._logos_appdata_dir
        if directory is None:
            return None
        context = maintenance.Context.from_app(self.app, prompt=False)
        try:
            root = Path(directory).resolve()
            data = root / "Data"
            if (context.product != "Logos"
                    or not root.is_relative_to(context.prefix)
                    or not data.resolve().is_relative_to(context.prefix)):
                raise maintenance.MaintenanceError("Database protection needs the selected installation's data")
            identities = [path for path in data.iterdir() if path.is_dir()]
            if len(identities) > 1 or (identities and identities[0].resolve().parent != data.resolve()):
                raise maintenance.MaintenanceError("Database protection cannot select between data folders")
        except FileNotFoundError:
            return None
        except maintenance.MaintenanceError:
            raise
        except (OSError, RuntimeError):
            raise maintenance.MaintenanceError(
                "Database protection could not inspect the selected data folders",
            ) from None
        if not identities:
            return None
        return root, identities[0].name

    def _policy_paths(self, target):
        if target is None:
            return ()
        root, identity = target
        try:
            paths = ((root / "Data" / identity / "UpdateManager/Updates.db").resolve(),
                     (root / "Documents" / identity / "LocalUserPreferences/PreferencesManager.db").resolve())
        except (OSError, RuntimeError):
            raise maintenance.MaintenanceError(
                "Database protection could not inspect the selected database paths",
            ) from None
        prefix = maintenance.Context.from_app(self.app, prompt=False).prefix
        if any(not path.is_relative_to(prefix) for path in paths):
            raise maintenance.MaintenanceError("Database protection path is outside the selected installation")
        return paths

    def _policies_ready(self, policies):
        statements = self._policy_statements(False)
        return len(policies.paths) == 2 and all(
            path in self._watchers and self._watchers[path][1].is_alive()
            and self._watcher_health[path].ready.is_set() and not self._watcher_health[path].error
            and self._watcher_policies[path] == tuple(sql)
            for path, sql in zip(policies.paths, statements, strict=True))

    def _policy_status(self):
        run = self._runs.get("logos")
        if run is None or run.policies is None:
            return ""
        policies = run.policies
        try:
            current = (self.app.conf is policies.configuration
                       and maintenance.Context.from_app(self.app, prompt=False) == run.context)
        except maintenance.MaintenanceError:
            current = False
        if not current:
            return "Database protection paused. Installation selection changed"
        if policies.error:
            return policies.error
        if policies.pending or not self._policies_ready(policies):
            return "Database protection pending. Waiting for application data"
        return ""

    def _policy_run_current(self, run, policies):
        if (self._closed.is_set() or getattr(self.app, "_closing", self._closed).is_set()
                or self.app.logos is not self
                or self._runs.get("logos") is not run or run.policies is not policies
                or self.app.conf is not policies.configuration or run.stopped
                or run.empty_since is not None or self.logos_state != State.RUNNING):
            return False
        try:
            current_context = maintenance.Context.from_app(self.app, prompt=False)
        except maintenance.MaintenanceError:
            return False
        if current_context != run.context:
            return False
        return run.process.poll() is None or any(
            self._exclusive_member(process, "logos", run.context) for process in run.members.values())

    def _attach_run_policies(self):
        # Profile selection holds this lock while joining the observer. Never
        # acquire it while holding _transition, even for the initial run lookup.
        run = self._runs.get("logos")
        profile_lock = getattr(self.app, "_profile_lock", None)
        if run is None or run.policies is None or profile_lock is None:
            return
        if not profile_lock.acquire(blocking=False):
            return
        try:
            with self._transition:
                run = self._runs.get("logos")
                policies = run.policies if run is not None else None
                if policies is None or not self._policy_run_current(run, policies):
                    return
            preparation_error = ""
            try:
                target = self._policy_target(policies.configuration)
                paths = self._policy_paths(target)
                available = [path.parent.is_dir() and path.is_file() for path in paths]
            except (OSError, RuntimeError):
                preparation_error = "Database protection could not identify the selected data folders"

            with self._transition:
                if not self._policy_run_current(run, policies):
                    return
                was_pending = policies.pending
                policies.pending = True
                try:
                    if self._watcher_stopping:
                        self.stop_watchers(timeout=0)
                    if preparation_error:
                        self.stop_watchers(timeout=0)
                        policies.error = preparation_error
                        return
                    current_target = self._policy_target(policies.configuration)
                    if current_target != target or self._policy_paths(current_target) != paths:
                        self.stop_watchers(timeout=0)
                        policies.error = "Application data changed. Retrying database protection"
                        return
                    if (not was_pending and target == policies.target and paths == policies.paths and all(available)
                            and self._policies_ready(policies)):
                        policies.pending, policies.error = False, ""
                        return
                    if policies.target != target or policies.paths != paths:
                        self.stop_watchers(timeout=0)
                        policies.target, policies.paths, policies.error = target, paths, ""
                    if target is None:
                        return
                    for path, sql, exists in zip(paths, self._policy_statements(False), available, strict=True):
                        if exists:
                            self._watch_database(path, sql, wait_ready=False)
                    policies.pending = not all(available) or not self._policies_ready(policies)
                    if not policies.pending:
                        policies.error = ""
                except (OSError, RuntimeError):
                    policies.error = "Database protection could not attach. Retrying"
        finally:
            profile_lock.release()

    def get_logos_pids(self, context=None):
        context = context or maintenance.Context.from_app(self.app, prompt=False)
        conf = self.app.conf
        expected = (conf.logos_exe, conf.logos_system_exe_windows_path,
                    conf.logos_indexer_exe_windows_path, conf.logos_cef_exe_windows_path)
        groups = {exe: [] for exe in expected if exe}
        snapshot = []
        current_uid = os.geteuid()
        for process in psutil.process_iter():
            try:
                with process.oneshot():
                    # Unknown classification must still reach strict identity checks.
                    try:
                        if process.uids().real != current_uid:
                            continue
                    except psutil.AccessDenied:
                        pass
                    try:
                        if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                            continue
                    except psutil.AccessDenied:
                        pass
                    process.info = process.as_dict(attrs=["pid", "ppid", "cmdline"])
                snapshot.append(process)
            except psutil.NoSuchProcess:
                continue
        for process in snapshot:
            command = process.info.get("cmdline") or []
            matches = set(groups).intersection(command)
            if matches and self._matches_selection(process, context, expected, strict=True):
                for exe in matches:
                    groups[exe].append(process)
        self._context, self._expected = context, expected
        self.existing_processes = groups
        self._snapshot = snapshot
        return groups

    def _prefix_member(self, process, context):
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return False
            prefix = process.environ().get("WINEPREFIX")
            return (process.uids().real == os.geteuid() and prefix is not None
                    and Path(prefix).is_absolute() and Path(prefix).resolve() == context.prefix)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False

    def _role_members(self, role):
        indexer = self.app.conf.logos_indexer_exe_windows_path
        members = []
        for exe, group in self.existing_processes.items():
            if role == "index" and exe == indexer:
                members.extend(group)
            elif role == "logos" and exe != indexer:
                members.extend(group)
        return members

    def _exclusive_member(self, process, role, context):
        if not self._prefix_member(process, context):
            return False
        try:
            name = process.name().lower()
            if name in {"wineserver", "wineboot.exe", "services.exe", "winedevice.exe", "explorer.exe",
                        "rpcss.exe", "svchost.exe", "plugplay.exe", "conhost.exe"}:
                return False
            command = process.cmdline()
            if role == "index":
                other = (self.app.conf.logos_exe, self.app.conf.logos_system_exe_windows_path,
                         self.app.conf.logos_cef_exe_windows_path)
                if name in {"logos.exe", "logoscef.exe"}:
                    return False
            else:
                other = (self.app.conf.logos_indexer_exe_windows_path,)
                if name in INDEXER_PROCESS_NAMES:
                    return False
            return not (set(other) - {None}).intersection(command)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False

    def _run_members(self, role, run):
        known = {pid: process for pid, process in run.members.items()
                 if self._exclusive_member(process, role, run.context)}
        for process in self._role_members(role):
            known[process.pid] = process
        # Preserve identities after reparenting. Discover children before their
        # parent exits rather than treating an arbitrary prefix process as owned.
        changed = True
        while changed:
            changed = False
            for process in self._snapshot:
                if process.pid not in known and process.info.get("ppid") in known:
                    if self._exclusive_member(process, role, run.context):
                        known[process.pid] = process
                        changed = True
        run.members = known
        for process in known.values():
            try:
                name = process.name().lower()
                if (role == "index" and process.pid == run.process.pid
                        and name in INDEXER_PROCESS_NAMES):
                    run.direct_worker = True
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        return list(known.values())

    def _matches_selection(self, process, context, expected=None, *, strict=False):
        """A Windows argv match alone does not identify a Wine installation."""
        try:
            if not process.is_running() or process.uids().real != os.geteuid():
                return False
            prefix = process.environ().get("WINEPREFIX")
            if not prefix or not Path(prefix).is_absolute() or Path(prefix).resolve() != context.prefix:
                return False
            if expected is None:
                conf = self.app.conf
                expected = (conf.logos_exe, conf.logos_system_exe_windows_path,
                            conf.logos_indexer_exe_windows_path, conf.logos_cef_exe_windows_path)
            return (bool((set(expected) - {None}).intersection(process.cmdline()))
                    and process.is_running() and process.status() != psutil.STATUS_ZOMBIE)
        except psutil.AccessDenied as error:
            if strict:
                raise maintenance.MaintenanceBusy("Cannot verify a selected program's process identity") from error
            return False
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            logging.warning("Skipped a Stop candidate whose selected-prefix identity could not be verified")
            return False

    def monitor(self):
        notifications = []
        with self._transition:
            now = time.monotonic()
            if self._closed.is_set() or (self._context is not None and now < self._next_scan):
                return
            self._next_scan = now + 1
            before_states = self.logos_state, self.indexing_state
            before_runs = dict(self._runs)
            had_error = bool(self.enforcement_error)
            was_settling = any(run.empty_since is not None for run in self._runs.values())
            if not self.app.is_installed() and not self._runs:
                return
            self.get_logos_pids()
            for role, attribute in (("logos", "logos_state"), ("index", "indexing_state")):
                run = self._runs.get(role)
                members = self._role_members(role) if run is None else self._run_members(role, run)
                if run is not None:
                    code = run.process.poll()
                    if code is not None and members:
                        run.handed_off = True
                    if members or code is None:
                        run.empty_since = None
                        continue
                    if run.empty_since is None:
                        run.empty_since = time.monotonic()
                        continue
                    if time.monotonic() - run.empty_since < 2:
                        continue
                    run.lease.close()
                    del self._runs[role]
                    self.processes = {key: value for key, value in self.processes.items() if value is not run.process}
                    if role == "logos":
                        self.stop_watchers()
                    elif not run.stopped:
                        if code != 0:
                            run.result = RunResult("failed", f"Indexing process failed with exit code {code}", code)
                        elif run.handed_off or not run.direct_worker:
                            run.result = RunResult("unknown", "Indexing stopped. The worker exit status is unavailable")
                        else:
                            run.result = RunResult("succeeded", "Indexing process has finished", code)
                        notifications.append((run.result.message, 100 if run.result.outcome == "succeeded" else 0))
                setattr(self, attribute, State.RUNNING if members else State.STOPPED)
            login_notice = self.monitor_for_potential_issues()
            if login_notice:
                notifications.append((login_notice, None))
        self._attach_run_policies()
        with self._transition:
            if self._closed.is_set():
                return
            error = (next((health.error for health in self._watcher_health.values() if health.error), "")
                     or self._policy_status())
            if error != self.enforcement_error:
                self.enforcement_error = error
                message = f"Database protection unavailable: {error}" if error else "Database protection restored"
                notifications.append((message, 0))
            states = self.logos_state, self.indexing_state
            steady = (states == before_states and not had_error and not self.enforcement_error
                      and not was_settling
                      and len(before_runs) == len(self._runs)
                      and all(self._runs.get(role) is run for role, run in before_runs.items())
                      and not any(run.empty_since is not None for run in self._runs.values()))
            if steady and not self._runs and states == (State.STOPPED, State.STOPPED):
                # External starts appear at the next idle scan, up to five seconds later.
                self._next_scan = now + 5
            elif (steady and State.RUNNING in states
                  and all(state in (State.STOPPED, State.RUNNING) for state in states)
                  and getattr(self, "_published_state", None) == (*states, "")):
                # Keep transitions fast, then observe healthy running roles every four seconds.
                self._next_scan = now + 4
        for message, percent in notifications:
            observe = getattr(self.app, "observe_manager", None)
            if observe is not None:
                observe(self, message, percent)
            else:
                self.app.status(message, percent)
        state = self.logos_state, self.indexing_state, self.enforcement_error
        if state != getattr(self, "_published_state", None):
            self._published_state = state
            observe = getattr(self.app, "observe_manager", None)
            if observe is not None:
                observe(self)

    def monitor_for_potential_issues(self):
        """Check login from the owned observer without creating a per-launch worker."""
        run = self._runs.get("logos")
        if run is None or self._login_checked:
            return
        directory = self.app.conf._logos_appdata_dir
        if directory is None:
            return
        self._login_checked = get_logos_user_id(directory) is not None
        if not self._login_checked and not self._login_notice and time.monotonic() - run.started > 105:
            self._login_notice = True
            return f"Trouble signing in? Use Get Support in {constants.APP_NAME} for browser handoff help"


    @maintenance.uses_owner
    def start(self):
        return self._start("logos")

    def _start(self, role):
        with self._transition:
            if self._closed.is_set():
                raise maintenance.MaintenanceBusy("Controller is closing")
            self._next_scan = 0
            attribute = "logos_state" if role == "logos" else "indexing_state"
            if role in self._runs or getattr(self, attribute) != State.STOPPED:
                raise maintenance.MaintenanceBusy("The requested program is already starting or running")
            lease = maintenance.Lease(maintenance.current_context(), role=role)
            try:
                self.get_logos_pids()
                if self._role_members(role):
                    raise maintenance.MaintenanceBusy("The requested program is already running")
                setattr(self, attribute, State.STARTING)
                self._launch(role, lease)
                setattr(self, attribute, State.RUNNING)
                return self._runs[role]
            except BaseException:
                lease.close()
                setattr(self, attribute, State.STOPPED)
                if role == "logos":
                    self.stop_watchers()
                raise

    def _launch(self, role, lease):
        context = lease.context
        configuration = self.app.conf
        try:
            from . import repair

            if repair.inspect(maintenance.Context.from_app(self.app)) == repair.FailureType.FailedUpgrade:
                raise maintenance.RecoveryRequired("A failed upgrade requires explicit --repair before launch")
            selected_runtime = runtime.executable(self.app.conf.wine_binary, self.app)
            wine_release, _ = wine.get_wine_release(selected_runtime)
            good_wine, reason = wine.check_wine_rules(
                wine_release,
                self.app.conf.installed_faithlife_product_release,
                self.app.conf.faithlife_product_version,
            )
            if not good_wine:
                raise maintenance.MaintenanceError(reason or "The selected Wine runtime is incompatible")
            self.start_monitoring()
            exe = self.app.conf.logos_exe if role == "logos" else self.app.conf.logos_indexer_exe_windows_path
            if not exe:
                raise ValueError("Could not find the installed program")
            target, paths = None, ()
            if role == "logos":
                target = self._policy_target(configuration)
                paths = self._policy_paths(target)
                self.prevent_logos_updates(target=target)
                self.set_auto_updates(False, target=target)
                if target is not None and not self._policies_ready(RunPolicies(configuration, target, paths)):
                    raise maintenance.MaintenanceError("Database protection did not stay ready before launch")
                if self._policy_target(configuration) != target or self._policy_paths(target) != paths:
                    raise maintenance.MaintenanceBusy("Application data changed before launch. Retry Start")
            if (self._closed.is_set() or getattr(self.app, "_closing", self._closed).is_set()
                    or self.app.conf is not configuration
                    or maintenance.Context.from_app(self.app, prompt=False) != context):
                raise maintenance.MaintenanceBusy("Launch cancelled because the controller is closing")
            process = wine.run_wine_application(
                self.app,
                selected_runtime,
                exe=exe
            )
            if process is None:
                raise maintenance.MaintenanceError("Wine did not start Logos")
            self.processes[exe] = process
            run = self._runs[role] = ManagedRun(context, process, lease)
            if role == "logos":
                run.policies = RunPolicies(configuration, target, paths, pending=target is None)
                self.enforcement_error = self._policy_status()
            try:
                parent = psutil.Process(process.pid)
                pending = [parent]
                while pending:
                    member = pending.pop()
                    if self._exclusive_member(member, role, context):
                        run.members[member.pid] = member
                        pending.extend(member.children())
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                pass
        except BaseException:
            lease.close()
            raise
        if role == "logos":
            self._login_checked = self._login_notice = False

    @staticmethod
    def _policy_statements(val):
        """Keep SQL policy separate from the run's pinned database paths."""
        sql = (
            """UPDATE Preferences SET Data='<data """ +
            ('OptIn="true"' if val else 'OptIn="false"') +
            """ StartDownloadHour="0" StopDownloadHour="0" MarkNewResourcesAsCloud="true" />' """
            """ WHERE Type='UpdateManagerPreferences'"""
        )
        desired = ('<data ' + ('OptIn="true"' if val else 'OptIn="false"')
                   + ' StartDownloadHour="0" StopDownloadHour="0" MarkNewResourcesAsCloud="true" />')
        sql += " AND Data IS NOT '" + desired + "'"
        application = [
            "DELETE FROM UpdateUrls WHERE UpdateId IN " +
                "(SELECT UpdateId FROM Updates WHERE Source='Application Update')",
            # Preserve the existing orphan-relation repair from upstream issue #275.
            "DELETE FROM Updates WHERE UpdateId NOT IN "+
                "(SELECT UpdateId FROM UpdateUrls) OR "+
                "Source='Application Update'",
            "UPDATE Resources SET Status=1, UpdateId=NULL WHERE UpdateId IS NOT NULL "+
                "AND UpdateId NOT IN (SELECT UpdateId FROM Updates)"
        ]
        return application, [sql]

    @maintenance.uses_owner
    def set_auto_updates(self, val: bool, *, target=_CURRENT_POLICY_TARGET, wait_ready=True):
        """Enforce the existing automatic new-resource download preference."""
        if target is _CURRENT_POLICY_TARGET:
            target = self._policy_target(self.app.conf)
        if target is None:
            return
        return self._watch_database(self._policy_paths(target)[1], self._policy_statements(val)[1],
                                    wait_ready=wait_ready)

    @maintenance.uses_owner
    def prevent_logos_updates(self, *, target=_CURRENT_POLICY_TARGET, wait_ready=True):
        """Remove pending application updates using the existing repair policy."""
        if target is _CURRENT_POLICY_TARGET:
            target = self._policy_target(self.app.conf)
        if target is None:
            return
        return self._watch_database(self._policy_paths(target)[0], self._policy_statements(False)[0],
                                    wait_ready=wait_ready)

    # Also noticed if the database Data/*/CloudResourceManager/CloudResources.db 
    # table TransitionStates has a ResourceId that isn't registered in UpdateManager,
    # logos will crash on startup. It can be recovered by removing those TransitionState
    # entries.
    # Perhaps we should attempt to recover in this state.
    # Since there are no known user interaction steps to get into this state, leaving it
    # alone for now. Only found it when manually modifying the database.

    def stop(self):
        self._stop(index_only=False)

    def _stop(self, *, index_only):
        with self._transition:
            self._next_scan = 0
            context = maintenance.Context.from_app(self.app, prompt=False)
            roles = ("index",) if index_only else ("logos", "index")
            attributes = ["indexing_state" if role == "index" else "logos_state" for role in roles]
            previous = [getattr(self, attribute) for attribute in attributes]
            for attribute in attributes:
                setattr(self, attribute, State.STOPPING)
            try:
                self.get_logos_pids(context)
                expected = (self.app.conf.logos_indexer_exe_windows_path,) if index_only else self._expected
                candidates = {p.pid: p for exe, group in self.existing_processes.items()
                              if exe in expected for p in group}
                owned = {}
                for role in roles:
                    run = self._runs.get(role)
                    if run is not None:
                        owned.update({p.pid: p for p in self._run_members(role, run)})
                candidates.update(owned)
                for process in candidates.values():
                    verified = (any(self._exclusive_member(process, role, context) for role in roles)
                                if process.pid in owned
                                else self._matches_selection(process, context, expected, strict=True))
                    if verified:
                        try:
                            process.terminate()
                        except (psutil.NoSuchProcess, psutil.ZombieProcess):
                            pass
                alive = self._wait_selected(list(candidates.values()), 2)
                for process in alive:
                    verified = (any(self._exclusive_member(process, role, context) for role in roles)
                                if process.pid in owned
                                else self._matches_selection(process, context, expected, strict=True))
                    if verified:
                        try:
                            process.kill()
                        except (psutil.NoSuchProcess, psutil.ZombieProcess):
                            pass
                alive = self._wait_selected(alive, 1)
                if alive:
                    raise maintenance.MaintenanceBusy("Selected processes have not stopped")
                self.get_logos_pids(context)
                if any(group for exe, group in self.existing_processes.items() if exe in expected):
                    raise maintenance.MaintenanceBusy("A selected process is still running. Retry Stop")
                for role in roles:
                    run = self._runs.get(role)
                    if run is not None:
                        run.stopped = True
                        run.result = RunResult("cancelled", "Indexing stopped by request")
                        if run.process.poll() is None:
                            raise maintenance.MaintenanceBusy("The launch process is still running")
                        run.lease.close()
                        del self._runs[role]
                self.processes = {name: p for name, p in self.processes.items() if p.poll() is None}
                if not index_only:
                    self.stop_watchers()
                for attribute in attributes:
                    setattr(self, attribute, State.STOPPED)
            except BaseException:
                for attribute, state in zip(attributes, previous):
                    setattr(self, attribute, state)
                raise

    def _wait_selected(self, candidates, timeout):
        """Observe exit without stealing a Popen child's wait status."""
        deadline = time.monotonic() + timeout
        while candidates:
            alive = []
            for process in candidates:
                try:
                    if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                        alive.append(process)
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    pass
            candidates = alive
            if not candidates or time.monotonic() >= deadline:
                return candidates
            time.sleep(0.05)
        return []

    def end_processes(self):
        self.close()

    @maintenance.uses_owner
    def index(self):
        run = self._start("index")
        self.app.status("Indexing has begun", 0)
        return run

    def stop_indexing(self):
        self._stop(index_only=True)

    @maintenance.uses_owner
    def get_app_logging_state(self, init=False):
        state = 'DISABLED'
        try:
            current_value = wine.get_registry_value(
                'HKCU\\Software\\Logos4\\Logging',
                'Enabled',
                self.app
            )
        except maintenance.MaintenanceError:
            raise
        except Exception as e:
            logging.warning(f"Failed to determine if logging was enabled, assuming no: {e}") 
            current_value = None
        if current_value == '0x1':
            state = 'ENABLED'
        return state

    @maintenance.uses_owner
    def switch_logging(self, action=None):
        state_disabled = 'DISABLED'
        value_disabled = '0000'
        state_enabled = 'ENABLED'
        value_enabled = '0001'
        if action == 'disable':
            value = value_disabled
            state = state_disabled
        elif action == 'enable':
            value = value_enabled
            state = state_enabled
        else:
            current_state = self.get_app_logging_state()
            logging.debug(f"app logging {current_state=}")
            if current_state == state_enabled:
                value = value_disabled
                state = state_disabled
            else:
                value = value_enabled
                state = state_enabled

        logging.info(f"Setting app logging to '{state}'.")
        exe_args = [
            'add', 'HKCU\\Software\\Logos4\\Logging', '/v', 'Enabled',
            '/t', 'REG_DWORD', '/d', value, '/f'
        ]
        process = wine.run_wine_during_install(
            app=self.app,
            wine_binary=self.app.conf.wine_binary,
            exe='reg',
            exe_args=exe_args
        )
        if process is None:
            raise maintenance.MaintenanceError("Logging was not changed because Wine did not start")
        process.wait()
        wine.wineserver_wait(self.app)
        self.app.conf.faithlife_product_logging = state == state_enabled
