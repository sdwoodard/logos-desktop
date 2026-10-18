import abc
import contextlib
import logging
import os
import select
import sqlite3
import struct
import threading
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
        self._db = self._connect()
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


# FIXME: refactor into FaithlifeDatabase class
def watch_db(path: str, sql_statements: list[str], stop: threading.Event):
    """Apply preferences while owned, releasing every database and watch on stop."""
    descriptor = inotify.calls.inotify_init()
    os.set_blocking(descriptor, False)
    connection = None
    try:
        if stop.is_set():
            return
        mask = inotify.constants.IN_MODIFY | inotify.constants.IN_CLOSE_WRITE
        inotify.calls.inotify_add_watch(descriptor, os.fsencode(Path(path).parent), mask)
        connection = sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True, autocommit=True, timeout=0.2)

        def execute_sql():
            for statement in sql_statements:
                if stop.is_set():
                    return
                try:
                    cursor = connection.execute(statement)
                    cursor.close()
                except sqlite3.OperationalError:
                    logging.exception("Best-effort db update failed")

        execute_sql()
        swallow_one = True
        names = {Path(path).name, Path(path).name + "-wal", Path(path).name + "-shm"}
        while not stop.is_set():
            ready, _, _ = select.select([descriptor], [], [], 0.2)
            if not ready:
                continue
            events = os.read(descriptor, 65536)
            offset = 0
            changed = False
            while offset < len(events):
                _, _, _, length = struct.unpack_from("iIII", events, offset)
                name = os.fsdecode(events[offset + 16:offset + 16 + length].rstrip(b"\0"))
                changed |= name in names
                offset += 16 + length
            if changed:
                if not swallow_one:
                    execute_sql()
                swallow_one = not swallow_one
    finally:
        if connection is not None:
            connection.close()
        os.close(descriptor)
