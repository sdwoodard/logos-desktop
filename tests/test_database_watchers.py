from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ou_dedetai import database


class TestDatabaseWatchers(unittest.TestCase):
    @contextmanager
    def connect(self, path):
        with closing(sqlite3.connect(path)) as connection, connection:
            yield connection

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "policy.db"
        self.create(self.path)

    def create(self, path):
        with self.connect(path) as connection:
            connection.execute("CREATE TABLE policy (value INTEGER)")
            connection.execute("INSERT INTO policy VALUES (0)")

    def watch(self):
        stop = threading.Event()
        failures = []

        def run():
            try:
                database.watch_db(str(self.path), ["UPDATE policy SET value=0 WHERE value != 0"], stop)
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=run)
        worker.start()

        def close():
            stop.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
        self.addCleanup(close)
        return stop, worker

    def corrected(self, connection):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if connection.execute("SELECT value FROM policy").fetchone() == (0,):
                return
            time.sleep(0.02)
        self.fail("External database change was not reconciled")

    def test_noop_then_single_external_commit_and_wal_commits_settle_without_storms(self):
        for wal in (False, True):
            with self.subTest(wal=wal):
                connection = sqlite3.connect(self.path, isolation_level=None)
                if wal:
                    connection.execute("PRAGMA journal_mode=WAL")
                stop, worker = self.watch()
                try:
                    time.sleep(0.3)
                    connection.execute("UPDATE policy SET value=1")
                    self.corrected(connection)
                    time.sleep(0.5)
                    before = {p.name: p.stat().st_mtime_ns for p in self.root.iterdir()}
                    time.sleep(0.7)
                    self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in self.root.iterdir()})
                    if wal:
                        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        connection.execute("UPDATE policy SET value=2")
                        self.corrected(connection)
                finally:
                    stop.set()
                    worker.join(2)
                    connection.close()

    def test_lock_failure_retries_without_a_further_event_and_cancellation_is_bounded(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("UPDATE policy SET value=4")
        connection.execute("BEGIN IMMEDIATE")
        stop, worker = self.watch()
        time.sleep(0.4)
        connection.execute("ROLLBACK")
        self.corrected(connection)
        connection.execute("BEGIN IMMEDIATE")
        stop.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        connection.execute("ROLLBACK")

    def test_replaced_and_recreated_database_is_reopened_and_old_generation_is_preserved(self):
        stop, worker = self.watch()
        time.sleep(0.3)
        replacement = self.root / "replacement.db"
        self.create(replacement)
        with self.connect(replacement) as connection:
            connection.execute("UPDATE policy SET value=7")
        old = self.root / "old.db"
        os.link(self.path, old)
        os.replace(replacement, self.path)
        with self.connect(self.path) as connection:
            self.corrected(connection)
        with self.connect(old) as connection:
            connection.execute("UPDATE policy SET value=8")
        self.path.unlink()
        time.sleep(0.3)
        self.assertFalse(self.path.exists())
        self.create(self.path)
        with self.connect(self.path) as connection:
            connection.execute("UPDATE policy SET value=9")
            connection.commit()
            self.corrected(connection)
        with self.connect(old) as connection:
            self.assertEqual(connection.execute("SELECT value FROM policy").fetchone(), (8,))
        stop.set()
        worker.join(2)

    def test_repeated_watchers_release_file_descriptors(self):
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            stop, worker = self.watch()
            time.sleep(0.05)
            stop.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_replaced_initial_generation_is_not_ready_until_replacement_commits(self):
        replacement = self.root / "replacement.db"
        self.create(replacement)
        locker = sqlite3.connect(replacement, isolation_level=None)
        self.addCleanup(locker.close)
        locker.execute("UPDATE policy SET value=4")
        locker.execute("BEGIN IMMEDIATE")
        health, stop = database.WatcherHealth(), threading.Event()
        original_stat = Path.stat
        count = [0]
        replaced = threading.Event()

        def stat(path, *args, **kwargs):
            if path == self.path:
                count[0] += 1
                if count[0] == 2:
                    os.replace(replacement, self.path)
                    replaced.set()
            return original_stat(path, *args, **kwargs)

        worker = threading.Thread(target=database.watch_db, args=(
            str(self.path), ["UPDATE policy SET value=0 WHERE value != 0"], stop, health,
        ))
        with patch.object(Path, "stat", stat):
            worker.start()
            try:
                self.assertTrue(replaced.wait(2))
                self.assertFalse(health.ready.wait(0.5))
                locker.execute("ROLLBACK")
                self.assertTrue(health.ready.wait(3))
                self.assertFalse(health.error)
                self.corrected(locker)
            finally:
                stop.set()
                worker.join(2)
