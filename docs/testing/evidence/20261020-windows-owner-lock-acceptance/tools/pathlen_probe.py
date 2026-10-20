"""Prove whether the full-run ERRORs were caused by a >260 char SQLite path."""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from core.database.lifecycle import sqlite_uri  # noqa: E402


def probe(total_target, label):
    base = Path(tempfile.mkdtemp(prefix="bfo-pathlen-"))
    tail = ".library.sqlite3." + "0" * 32 + ".initializing"
    filler = total_target - len(str(base)) - 1 - len(tail)
    assert filler > 0, filler
    directory = base / ("d" * filler)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / tail
    uri = sqlite_uri(target, "rwc")
    print(f"[{label}] path_len={len(str(target))} uri_len={len(uri)}")
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.execute("CREATE TABLE IF NOT EXISTS probe(x INTEGER)")
        connection.commit()
        connection.close()
        print(f"[{label}] connect=OK")
        return True
    except Exception as exc:
        print(f"[{label}] connect=FAIL {type(exc).__name__}: {exc}")
        return False


ok_long = probe(266, "266 chars (matches observed failing run)")
ok_short = probe(226, "226 chars (short clone root)")
print(f"RESULT long=266:{'OK' if ok_long else 'FAIL'} short=226:{'OK' if ok_short else 'FAIL'}")
