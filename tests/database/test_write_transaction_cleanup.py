from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import store
from core.database import DatabaseState
from core.scheduler.store import SQLiteTaskStore


class _FailingBeginConnection(sqlite3.Connection):
    """Keep test references alive so only explicit close releases each handle."""

    def __init__(self, *args, **kwargs):
        kwargs.pop("factory", None)
        super().__init__(*args, **kwargs)
        self.close_calls = 0
        self.is_closed = False

    def execute(self, sql, *args, **kwargs):
        if str(sql).strip().upper() == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return super().execute(sql, *args, **kwargs)

    def close(self):
        self.close_calls += 1
        self.is_closed = True
        return super().close()


class _FakeProfileReadLease:
    def close(self):
        return None


class _FakeLifecycle:
    def __init__(self, opened):
        self.opened = opened

    def acquire_profile_read_lease(self):
        # _ProfileAwareRLock takes a shared lease before its process-local lock.
        # This test isolates transaction retry cleanup, so a no-op lease is enough.
        return _FakeProfileReadLease()

    @contextmanager
    def connection_gate(self):
        yield

    @property
    def active_connection_count(self):
        return sum(not connection.is_closed for connection in self.opened)


def _connect_failing_begin(opened):
    connection = sqlite3.connect(":memory:", factory=_FailingBeginConnection)
    # Keep a strong reference so garbage collection cannot hide a missing close.
    opened.append(connection)
    return connection


class WriteTransactionCleanupTests(unittest.TestCase):
    def test_store_transaction_closes_both_connections_if_retry_fails(self):
        opened = []
        lifecycle = _FakeLifecycle(opened)

        def connect():
            return _connect_failing_begin(opened)

        with patch.object(store, "_lifecycle", return_value=lifecycle), \
             patch.object(store, "_connect", side_effect=connect), \
             patch.object(store, "database_state", return_value=DatabaseState.READY.value):
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly database"):
                with store._transaction():
                    self.fail("transaction should not start")

        self.assertEqual(2, len(opened))
        self.assertTrue(all(connection.close_calls == 1 for connection in opened))
        self.assertEqual(0, lifecycle.active_connection_count)

    def test_scheduler_store_closes_both_connections_if_retry_fails(self):
        opened = []

        def connect():
            return _connect_failing_begin(opened)

        task_store = SQLiteTaskStore(
            connection_factory=connect,
            database_exists=lambda: True,
        )
        with patch.object(task_store, "_connect_ready", side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly database"):
                task_store._open_write_transaction()

        self.assertEqual(2, len(opened))
        self.assertTrue(all(connection.close_calls == 1 for connection in opened))
        self.assertEqual(0, sum(not connection.is_closed for connection in opened))


if __name__ == "__main__":
    unittest.main()
