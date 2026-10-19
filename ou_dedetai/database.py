import abc
import contextlib
from dataclasses import dataclass, field
import logging
import os
import select
import sqlite3
import struct
import threading
import time
import inotify.calls
import inotify.constants
from pathlib import Path
from typing import Any, Optional


class FaithlifeDatabase(contextlib.AbstractContextManager):
    """Class for interacting with internal Faithlife databases.
    
    Use with python's context manager"""

    logos_app_dir: Path
    logos_user_id: str
    _db: Optional[sqlite3.Connection]

    def __init__(
        self,
        logos_app_dir: Path,
        logos_user_id: str
    ):
        self.logos_app_dir = logos_app_dir
        self.logos_user_id = logos_user_id
        self._db = None

    @abc.abstractmethod
    def _database_path(self) -> Path:
        """Path to the database"""
        pass

    def execute_sql(
        self,
        sql_statement: str,
        parameters: list[str] = list(frozenset())
    ) -> list[Any]:
        return self.database.execute(sql_statement, parameters).fetchall()
        
    def fetch_one(
        self,
        sql_statement: str,
        parameters: list[str] = list(frozenset())
    ) -> Optional[Any]:
        contents = self.execute_sql(sql_statement, parameters)
        if contents and len(contents) > 0:
            # Content comes in as a tuple, trailing , unpacks first argument
            content, = contents[0]
            return content
        return None

    @property
    def database(self) -> sqlite3.Connection:
        if not self._db:
            self._db = self._connect()
        return self._db

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._database_path()), autocommit=True)

    def close(self):
        if self._db:
            self._db.close()
            self._db = None

    def __enter__(self):
        self.database
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._db:
                self._db.__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class LocalUserPreferencesManager(FaithlifeDatabase):
    def _database_path(self):
        return self.logos_app_dir / "Documents" / self.logos_user_id / "LocalUserPreferences" / "PreferencesManager.db" 
    
    @property
    def app_local_preferences(self) -> Optional[str]:
        return self.fetch_one(
            "SELECT Data FROM Preferences WHERE `Type`='AppLocalPreferences' LIMIT 1" 
        )
    
    @app_local_preferences.setter
    def app_local_preferences(self, value: str):
        self.execute_sql(
            "UPDATE Preferences SET Data= ? WHERE `Type`='AppLocalPreferences'", 
            [value]
        )
    
    # Need to override __enter__ to return the proper type.
    def __enter__(self):
        super().__enter__()
        return self


@dataclass
class WatcherHealth:
    ready: threading.Event = field(default_factory=threading.Event)
    error: str = ""


def watch_db(path: str, sql_statements: list[str], stop: threading.Event, health=None):
    try:
        _watch_db(path, sql_statements, stop, health)
    except Exception as error:
        if health is None:
            raise
        health.error = str(error)
        health.ready.set()


def _watch_db(path, sql_statements, stop, health):
    """Reconcile conditional SQL after events without guessing which writer sent them."""
    path = Path(path).absolute()
    descriptor = inotify.calls.inotify_init()
    connection = None
    try:
        os.set_blocking(descriptor, False)
        if stop.is_set():
            return
        flags = inotify.constants
        mask = (flags.IN_MODIFY | flags.IN_CLOSE_WRITE | flags.IN_CREATE | flags.IN_DELETE
                | flags.IN_MOVED_TO | flags.IN_MOVED_FROM | flags.IN_DELETE_SELF | flags.IN_MOVE_SELF)
        inotify.calls.inotify_add_watch(descriptor, os.fsencode(path.parent), mask)
        names = {path.name, path.name + "-wal", path.name + "-shm"}
        identity = None
        next_check = 0
        last_error = None

        while not stop.is_set():
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + 5
                try:
                    info = path.stat()
                    current = (info.st_dev, info.st_ino)
                    if identity != current:
                        if connection is not None:
                            connection.close()
                            connection = None
                        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                                     isolation_level=None, timeout=0.2)
                        identity = current
                    connection.execute("BEGIN IMMEDIATE").close()
                    try:
                        for statement in sql_statements:
                            if stop.is_set():
                                break
                            connection.execute(statement).close()
                        fresh = path.stat()
                        if stop.is_set() or (fresh.st_dev, fresh.st_ino) != identity:
                            connection.execute("ROLLBACK").close()
                            identity = None
                            next_check = 0
                            continue
                        else:
                            connection.execute("COMMIT").close()
                    except BaseException:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK").close()
                        raise
                    last_error = None
                    if health is not None:
                        health.error = ""
                        health.ready.set()
                except (OSError, sqlite3.Error) as error:
                    if connection is not None:
                        connection.close()
                        connection = None
                    identity = None
                    key = (type(error), str(error))
                    if key != last_error:
                        logging.warning("Database enforcement will retry: %s", error)
                        last_error = key
                    next_check = time.monotonic() + 1
                    if health is not None:
                        health.error = str(error)

            ready, _, _ = select.select([descriptor], [], [], 0.2)
            if ready:
                events = os.read(descriptor, 65536)
                offset = 0
                changed = False
                while offset < len(events):
                    _, event_mask, _, length = struct.unpack_from("iIII", events, offset)
                    name = os.fsdecode(events[offset + 16:offset + 16 + length].rstrip(b"\0"))
                    if event_mask & (flags.IN_IGNORED | flags.IN_DELETE_SELF | flags.IN_MOVE_SELF):
                        raise RuntimeError("Database directory changed. Restart the managed application")
                    changed |= name in names or bool(event_mask & flags.IN_Q_OVERFLOW)
                    offset += 16 + length
                if changed:
                    # A fixed debounce bounds work during both external bursts and self-events.
                    next_check = min(next_check, time.monotonic() + 0.2)
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            os.close(descriptor)
