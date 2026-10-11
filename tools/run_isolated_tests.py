"""Run the full unittest suite from a sanitized temporary checkout.

This protects the working tree's tests/.test-data and redirects all application
profile/temp paths before any application module can be imported.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_NAMES = {
    ".git", ".aws", ".codex", ".agents", ".private", ".venv", "venv",
    "node_modules", "dist", "build", "output", "data", ".test-data",
    "__pycache__", ".pytest_cache", ".bfo-test-dependencies",
}


def ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in IGNORED_NAMES or name.endswith(".pyc")
        or name.startswith(".bfo-isolated-tests-")
        or name.startswith(".bfo-package-smoke-")
        or name.startswith(".bfo-migration-evidence-")
        or name == ".env" or name.startswith(".env.")
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix=".bfo-isolated-tests-", dir=ROOT,
                                     ignore_cleanup_errors=True) as temp_name:
        workspace = Path(temp_name)
        checkout = workspace / "checkout"
        shutil.copytree(ROOT, checkout, ignore=ignore)
        isolated_profile = workspace / "profile"
        isolated_home = workspace / "home"
        isolated_temp = workspace / "temp"
        for path in (isolated_profile, isolated_home, isolated_temp):
            path.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env.update({
            "LOCALAPPDATA": str(isolated_profile),
            "USERPROFILE": str(isolated_profile),
            "HOME": str(isolated_home),
            "TEMP": str(isolated_temp),
            "TMP": str(isolated_temp),
            "TMPDIR": str(isolated_temp),
            "BFO_SOURCE_REPOSITORY": str(ROOT),
            "BFO_LEGACY_REF": env.get("BFO_LEGACY_REF", "v0.208"),
            "BFO_MIGRATION_TEST": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        })
        env.pop("BILI_FAV_ORGANIZER_DATA_DIR", None)
        print(f"Isolated checkout: {checkout}", flush=True)
        print(f"Isolated LOCALAPPDATA: {isolated_profile}", flush=True)
        extra_pythonpath = []
        if "--pythonpath" in sys.argv[1:]:
            index = sys.argv.index("--pythonpath")
            if index + 1 >= len(sys.argv):
                raise SystemExit("--pythonpath requires a directory")
            extra_pythonpath.append(str(Path(sys.argv[index + 1]).resolve()))
        env["PYTHONPATH"] = os.pathsep.join(
            [str(checkout / "tests"), *extra_pythonpath, env.get("PYTHONPATH", "")]
        )
        if "--test" in sys.argv[1:]:
            index = sys.argv.index("--test")
            if index + 1 >= len(sys.argv):
                raise SystemExit("--test requires a unittest test id")
            discovery = ["-m", "unittest", "-v", sys.argv[index + 1]]
        elif "--migration-only" in sys.argv[1:]:
            discovery = ["-m", "unittest", "discover", "-s", "tests/migration", "-v"]
        else:
            discovery = ["-m", "unittest", "discover", "-s", "tests", "-v"]
        command = [sys.executable, *discovery]
        if "--stack-dump-seconds" in sys.argv[1:]:
            index = sys.argv.index("--stack-dump-seconds")
            if index + 1 >= len(sys.argv):
                raise SystemExit("--stack-dump-seconds requires an integer")
            seconds = int(sys.argv[index + 1])
            args = discovery[2:] if discovery[:2] == ["-m", "unittest"] else []
            if not args:
                raise SystemExit("stack dumps are supported for a unittest test id")
            bootstrap = (
                "import faulthandler,runpy,sys; "
                f"faulthandler.dump_traceback_later({seconds},repeat=True); "
                "sys.argv=['unittest',*sys.argv[1:]]; "
                "runpy.run_module('unittest',run_name='__main__')"
            )
            command = [sys.executable, "-c", bootstrap, *args]
        return subprocess.run(
            command,
            cwd=checkout, env=env, check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
