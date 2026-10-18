import os
from pathlib import Path
import signal
import subprocess
import time
from enum import Enum
import logging
import psutil
import threading

from ou_dedetai import constants, database, maintenance
from ou_dedetai.app import App
from ou_dedetai.config import get_logos_user_id

from . import system
from . import wine


class State(Enum):
    RUNNING = 1
    STOPPED = 2
    STARTING = 3
    STOPPING = 4


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

    def stop_watchers(self):
        with self._watcher_lock:
            handles = list(self._watchers.values())
            for stop, thread in handles:
                stop.set()
            for stop, thread in handles:
                thread.join(timeout=3)
                if thread.is_alive():
                    raise maintenance.MaintenanceBusy("A database watcher has not stopped")
            self._watchers.clear()

    @maintenance.uses_owner
    def _watch_database(self, path, sql):
        with self._watcher_lock:
            old = self._watchers.get(path)
            if old and old[1].is_alive():
                return
            stop = threading.Event()
            thread = maintenance.spawn(self.app, database.watch_db, str(path), sql, stop)
            self._watchers[path] = (stop, thread)

    def monitor_indexing(self):
        if self.app.conf.logos_indexer_exe_windows_path in self.existing_processes:
            indexer = self.existing_processes.get(self.app.conf.logos_indexer_exe_windows_path)
            if indexer and isinstance(indexer[0], psutil.Process) and indexer[0].is_running():
                self.indexing_state = State.RUNNING
            else:
                self.indexing_state = State.STOPPED

    def monitor_logos(self):
        splash = []
        login = []
        cef = []
        if self.app.conf.logos_exe:
            splash = self.existing_processes.get(self.app.conf.logos_exe, [])
        if self.app.conf.logos_system_exe_windows_path:
            login = self.existing_processes.get(self.app.conf.logos_system_exe_windows_path, [])
        if self.app.conf.logos_cef_exe_windows_path:
            cef = self.existing_processes.get(self.app.conf.logos_cef_exe_windows_path, [])

        splash_running = splash[0].is_running() if splash else False
        login_running = login[0].is_running() if login else False
        cef_running = cef[0].is_running() if cef else False
        # logging.debug(f"{self.logos_state=}")
        # logging.debug(f"{splash_running=}; {login_running=}; {cef_running=}")

        if self.logos_state == State.STARTING:
            if login_running or cef_running:
                self.logos_state = State.RUNNING
        elif self.logos_state == State.RUNNING:
            if not any((splash_running, login_running, cef_running)):
                self.stop()
        elif self.logos_state == State.STOPPING:
            pass
        elif self.logos_state == State.STOPPED:
            if splash_running:
                self.logos_state = State.STARTING
            if login_running:
                self.logos_state = State.RUNNING
            if cef_running:
                self.logos_state = State.RUNNING

    def get_logos_pids(self):
        app = self.app
        # FIXME: consider refactoring to make one call to get a system pids
        # Currently this gets all system pids 4 times
        for exe_path in [
            app.conf.logos_exe,
            app.conf.logos_system_exe_windows_path,
            app.conf.logos_indexer_exe_windows_path,
            app.conf.logos_cef_exe_windows_path
        ]:
            if exe_path:
                self.existing_processes[exe_path] = system.get_pids(exe_path)

    def monitor(self):
        if self.app.is_installed():
            self.get_logos_pids()
            try:
                self.monitor_indexing()
                self.monitor_logos()
            except Exception as e:
                # pass
                logging.error(e)
        else:
            # Useful if the install directory got deleted while executing
            self.logos_state = State.STOPPED

    def monitor_for_potential_issues(self):
        """A function to be run in parallel with running Logos to detect common issues and suggest workarounds"""
        # Make sure we have a logos appdata, without it we can't reliably do any checks
        logos_appdata_dir = self.app.conf._logos_appdata_dir
        if logos_appdata_dir is None:
            logging.warning("The user should have installed the Logos application by now and have this populated."
                            " Waiting until that is resolved…")
            while logos_appdata_dir is None:
                time.sleep(1)
                logos_appdata_dir = self.app.conf._logos_appdata_dir
        
        # See if the user is logged in, this may change which things we look for
        is_user_logged_in: bool = False
        if get_logos_user_id(logos_appdata_dir) is not None:
            is_user_logged_in = True

        sent_trouble_signing_in_message: bool = False

        time_logos_started = time.time()

        while True:
            # Now we wait...
            time.sleep(10)

            if not is_user_logged_in:
                if get_logos_user_id(logos_appdata_dir) is not None:
                    # User's now logged in - no fuss.
                    is_user_logged_in = True
                else:
                    # Took me about ~1.5 minutes to sign on while simulating a little dilly-dallying and a couple
                    # incorrect password attempts. Should still be longer than most people take to sign on, but not SO
                    # long that people close OD in frustration to try again - fine line of balance.
                    if not sent_trouble_signing_in_message and time.time() - time_logos_started > 60 * 1.75:
                        # It's been five minutes since the user launched Logos and yet they haven't logged in yet.
                        # Are they having trouble?
                        self.app.pop_up(
                            "Trouble Signing In?",
                            "If you're having trouble signing in, try these workarounds:\n\n" \
                            "If the browser isn't launching after you hit \"Sign In\" in the Logos application "
                            "consider installing firefox from your system's package manager and temporarily setting "
                            "that as your default browser.\n\n"
                            "If the browser opens and entering your credentials is successful but nothing happens: "
                            "on the Logos browser page try hitting the button for not being redirected automatically "
                            "then click on the link that shows up that suggests to click it if you're still having "
                            "trouble. That should cause you to sign in on the Logos application.\n\n"
                            f"If you still need help use \"Get Support\" on the main page of {constants.APP_NAME}"
                        )
                        # We only want to send this message once
                        sent_trouble_signing_in_message = True


    @maintenance.uses_owner
    def start(self):
        self.logos_state = State.STARTING
        try:
            wine_release, _ = wine.get_wine_release(self.app.conf.wine_binary)
            good_wine, reason = wine.check_wine_rules(
                wine_release,
                self.app.conf.installed_faithlife_product_release,
                self.app.conf.faithlife_product_version,
            )
            if not good_wine:
                raise maintenance.MaintenanceError(reason or "The selected Wine runtime is incompatible")
            wine.wineserver_kill(self.app)
            self.prevent_logos_updates()
            self.set_auto_updates(False)
            if not self.app.conf.logos_exe:
                raise ValueError("Could not find installed Logos EXE to run")
            process = wine.run_wine_application(
                self.app,
                self.app.conf.wine_binary,
                exe=self.app.conf.logos_exe
            )
            if process is None:
                raise maintenance.MaintenanceError("Wine did not start Logos")
            self.processes[self.app.conf.logos_exe] = process
        except BaseException:
            self.logos_state = State.STOPPED
            self.stop_watchers()
            raise
        self.logos_state = State.RUNNING
        def finished(returncode):
            self.logos_state = State.STOPPED

        maintenance.hold_process(self.app, process, finished)
        self.app.start_thread(self.monitor_for_potential_issues, daemon_bool=False)

    # For now enforce this all the time, ideally we wouldn't override the user's choice
    # But risks as they are, setting it to false is safer for now.
    # Feel free to revisit when there is less chances of it breaking an install
    @maintenance.uses_owner
    def set_auto_updates(self, val: bool):
        """Edits Logos' internal db entry corresponding to the option in:
        Program Settings -> Internet -> Automatically Download New Resources
        """
        if self.app.conf._logos_appdata_dir is None:
            return
        logos_appdata_dir = Path(self.app.conf._logos_appdata_dir)
        logos_user_id = self.app.conf._logos_user_id
        if not logos_user_id:
            return None
        db_path = logos_appdata_dir / "Documents" / logos_user_id / "LocalUserPreferences" / "PreferencesManager.db" 
        sql = (
            """UPDATE Preferences SET Data='<data """ +
            ('OptIn="true"' if val else 'OptIn="false"') +
            """ StartDownloadHour="0" StopDownloadHour="0" MarkNewResourcesAsCloud="true" />' """
            """ WHERE Type='UpdateManagerPreferences'""" 
        )
        self._watch_database(db_path, [sql])

    @maintenance.uses_owner
    def prevent_logos_updates(self):
        """Edits Logos' internal database to remove pending installers
        before it has a chance to apply them
        """
        if self.app.conf._logos_appdata_dir is None:
            return
        logos_appdata_dir = Path(self.app.conf._logos_appdata_dir)
        logos_user_id = self.app.conf._logos_user_id
        if logos_user_id is None:
            return None
        db_path = logos_appdata_dir / "Data" / logos_user_id / "UpdateManager" / "Updates.db"
        # FIXME: I wonder if we can use the result of these deletion using RETURNING
        # Then we could notify the user that there are updates.
        # If we do that we'd have to consider if their other resources are up to date
        # AND if their library is index and their library is prepared.
        # Logos probably should be off for this
        sql = [
            # "DELETE FROM Installers WHERE 1",
            # Cleanup the Update Ids that are associated with an application update
            "DELETE FROM UpdateUrls WHERE UpdateId IN " +
                "(SELECT UpdateId FROM Updates WHERE Source='Application Update')",
            # Cleanup database relations and removes Application Updates
            # Fixes corrupt DBs caused by an earlier
            # version of the software #275
            # If we don't do this, the application will crash when it tries to update.
            "DELETE FROM Updates WHERE UpdateId NOT IN "+
                "(SELECT UpdateId FROM UpdateUrls) OR "+
                "Source='Application Update'",
            # Also remove any UpdateId references that don't exist
            "UPDATE Resources SET Status=1, UpdateId=NULL WHERE UpdateId IS NOT NULL "+
                "AND UpdateId NOT IN (SELECT UpdateId FROM Updates)"
        ]
        self._watch_database(db_path, sql)

    # Also noticed if the database Data/*/CloudResourceManager/CloudResources.db 
    # table TransitionStates has a ResourceId that isn't registered in UpdateManager,
    # logos will crash on startup. It can be recovered by removing those TransitionState
    # entries.
    # Perhaps we should attempt to recover in this state.
    # Since there are no known user interaction steps to get into this state, leaving it
    # alone for now. Only found it when manually modifying the database.

    def stop(self):
        logging.debug("Stopping LogosManager.")
        self.logos_state = State.STOPPING
        if len(self.existing_processes) == 0:
            self.get_logos_pids()

        pids: list[str] = []
        for processes in self.processes.values():
            pids.append(str(processes.pid))

        for existing_processes in self.existing_processes.values():
            pids.extend(str(proc.pid) for proc in existing_processes)

        if pids:
            try:
                system.run_command(['kill', '-9'] + pids)
                logging.debug(f"Stopped Logos processes at PIDs {', '.join(pids)}.")
            except Exception as e:
                logging.debug(f"Error while stopping Logos processes: {e}.")
        else:
            logging.debug("No Logos processes to stop.")
        self.logos_state = State.STOPPED
        # The Logos process has exited, if we wait here it hangs
        # wine.wineserver_wait(self.app)

    def end_processes(self):
        for process_name, process in self.processes.items():
            if isinstance(process, subprocess.Popen):
                logging.debug(f"Found {process_name} in Processes. Attempting to close {process}.")
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    os.waitpid(-process.pid, 0)

    @maintenance.uses_owner
    def index(self):
        self.indexing_state = State.STARTING
        try:
            if not self.app.conf.logos_indexer_exe_windows_path:
                raise ValueError("Cannot find installed indexer")
            wine.wineserver_kill(self.app)
            process = wine.run_wine_application(
                app=self.app,
                wine_binary=self.app.conf.wine_binary,
                exe=self.app.conf.logos_indexer_exe_windows_path
            )
            if process is None:
                raise maintenance.MaintenanceError("Wine did not start the indexer")
            self.processes[self.app.conf.logos_indexer_exe_windows_path] = process
        except BaseException:
            self.indexing_state = State.STOPPED
            raise

        def wait_on_indexing(returncode):
            self.indexing_state = State.STOPPED
            if returncode == 0:
                self.app.status("Indexing process has finished", 100)
            else:
                self.app.status(f"Indexing process failed with exit code {returncode}", 0)

        self.indexing_state = State.RUNNING
        self.app.status("Indexing has begun", 0)
        maintenance.hold_process(self.app, process, wait_on_indexing)

    def stop_indexing(self):
        self.indexing_state = State.STOPPING
        if self.app:
            pids = []
            for process_name in [self.app.conf.logos_indexer_exe_windows_path]:
                if process_name is None:
                    continue
                process = self.processes.get(process_name)
                if process:
                    pids.append(str(process.pid))
                else:
                    logging.debug(f"No LogosIndexer processes found for {process_name}.")

            if pids:
                try:
                    system.run_command(['kill', '-9'] + pids)
                    self.indexing_state = State.STOPPED
                    self.app.status(f"Stopped LogosIndexer processes at PIDs {', '.join(pids)}.")
                except Exception as e:
                    logging.debug(f"Error while stopping LogosIndexer processes: {e}.")
            else:
                logging.debug("No LogosIndexer processes to stop.")
                self.indexing_state = State.STOPPED
        wine.wineserver_wait(app=self.app)

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
