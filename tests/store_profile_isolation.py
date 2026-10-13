"""Store profile isolation for tests, mirroring test_scan_troubleshooting.py.

First-run database creation is explicit since the reliability refactor: importing
``store``/``server`` no longer creates a database, and business routes answer 503
until one exists. Tests written against the old auto-create behaviour therefore
have to point the store at a throwaway profile and create its database themselves.

The legacy paths need redirecting as well. ``LEGACY_DB_FILE`` is derived from the
repository directory rather than from ``BILI_FAV_ORGANIZER_DATA_DIR``, so a stale
``<repo>/data/library.sqlite3`` would otherwise be migrated into the temporary
profile - and correctly refused by the lifecycle guards, because an old database
does not satisfy the current table contract.

Nothing here relaxes a production guard: the isolation only makes each test own a
genuine, explicitly initialized database.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import store


def stop_task_scheduler() -> None:
    """Release the scheduler bound to the current (usually about-to-go) profile.

    A worker left over from another test keeps calling into ``store`` and would
    otherwise touch a profile that is being rebuilt or removed.
    """
    server = sys.modules.get("server")
    if server is None:
        return
    scheduler = getattr(server, "_TASK_SCHEDULER", None)
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=True, timeout=15, pause_running=False)
    except Exception:
        # A worker that will not stop must not turn test teardown into a failure;
        # the next test rekeys the scheduler anyway.
        pass
    server._TASK_SCHEDULER = None
    server._TASK_SCHEDULER_KEY = None


def isolated_store_profile(stack, *, prefix: str = "bfo-store-profile-") -> Path:
    """Bind ``store`` to a fresh temporary profile and initialize its database.

    ``stack`` is an ``ExitStack`` owned by the caller; the original paths are
    restored and the temporary directory removed when it closes.
    """
    root = Path(tempfile.mkdtemp(prefix=prefix))
    # A scheduler left over from another test is bound to another database path
    # and would report it missing; rekey happens only through _ensure_task_scheduler.
    stop_task_scheduler()
    # Cleanup is LIFO: stop the scheduler before its database disappears.
    stack.callback(shutil.rmtree, root, True)
    stack.callback(stop_task_scheduler)
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    legacy_dir = root / "legacy"
    for name, value in (
        ("DATA_DIR", data_dir),
        ("DB_FILE", data_dir / "library.sqlite3"),
        ("LEGACY_DATA_DIR", legacy_dir),
        ("LEGACY_DB_FILE", legacy_dir / "library.sqlite3"),
        ("_LEGACY_FILES", {}),
    ):
        stack.enter_context(patch.object(store, name, value))
    store.initialize_new_database()
    return data_dir
