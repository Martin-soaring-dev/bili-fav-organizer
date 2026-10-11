"""Launch a disposable unpacked Electron package with an isolated Windows profile."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from run_isolated_tests import ROOT


def isolated_env(workspace: Path, profile_name: str) -> dict[str, str]:
    paths = {
        "LOCALAPPDATA": workspace / profile_name,
        "APPDATA": workspace / profile_name / "AppData",
        "USERPROFILE": workspace / profile_name / "User",
        "HOME": workspace / f"{profile_name}-home",
        "TEMP": workspace / f"{profile_name}-temp",
        "TMP": workspace / "launch-temp",
        "TMPDIR": workspace / "launch-temp",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({key: str(value) for key, value in paths.items()})
    env.update({
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "all_proxy": "http://127.0.0.1:9",
    })
    env.pop("BILI_FAV_ORGANIZER_DATA_DIR", None)
    return env


def run_node(node: str, script: Path, args: list[str], *, cwd: Path,
             env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    command = [node, str(script), *args]
    print("$ " + subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8",
                            errors="replace", capture_output=True, timeout=timeout,
                            check=False)
    # Windows GitHub runners often default stdout to cp1252; Node/CDP returns
    # Unicode (Chinese UI text). Never let logging abort a valid smoke test.
    if result.stdout:
        sys.stdout.buffer.write(result.stdout.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    if result.stderr:
        sys.stderr.buffer.write(result.stderr.encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()
    return result


def wait_for_app_page(debug_port: int, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    endpoint = f"http://127.0.0.1:{debug_port}/json/list"
    while time.monotonic() < deadline:
        try:
            with urlopen(endpoint, timeout=1) as response:
                targets = json.loads(response.read().decode("utf-8"))
            page = next((target for target in targets if target.get("type") == "page"
                         and target.get("url", "").startswith("http://127.0.0.1:")), None)
            if page:
                return page["url"]
        except Exception:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"Electron renderer did not navigate to its local backend within {timeout}s.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path,
                        help="a retained .bfo-package-smoke-* workspace")
    parser.add_argument("--profile-name", default="launch-profile",
                        help="isolated profile directory name under the package workspace")
    parser.add_argument("--ready-timeout", type=int, default=90)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("This packaged shell smoke requires Windows.")

    workspace = args.workspace.resolve(strict=True)
    root = ROOT.resolve()
    if workspace.parent != root or not workspace.name.startswith(".bfo-package-smoke-"):
        raise SystemExit(f"Refusing a workspace outside this repository: {workspace}")
    checkout = workspace / "checkout"
    package = checkout / "dist" / "electron" / "win-unpacked"
    executable = package / "BiliFav Organizer.exe"
    if not executable.is_file() or not (package / "resources" / "app.asar").is_file():
        raise SystemExit(f"Unpacked Electron package is incomplete: {package}")

    node = shutil_which("node")
    if not node:
        raise SystemExit("Node.js is required for the shell smoke scripts.")
    if not args.profile_name.startswith("launch-profile") or ".." in args.profile_name:
        raise SystemExit("Profile name must start with 'launch-profile' and stay local.")
    env = isolated_env(workspace, args.profile_name)
    log_path = Path(env["LOCALAPPDATA"]) / "BiliFavOrganizer" / "shell.log"
    stdout_path = workspace / "launch.stdout.log"
    stderr_path = workspace / "launch.stderr.log"
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    args_for_app = [
        str(executable),
        "--remote-debugging-port=19222",
        "--inspect=19229",
    ]
    print("$ " + subprocess.list2cmdline(args_for_app), flush=True)
    with stdout_path.open("w", encoding="utf-8") as stdout, \
            stderr_path.open("w", encoding="utf-8") as stderr:
        app = subprocess.Popen(args_for_app, cwd=package, env=env,
                               stdout=stdout, stderr=stderr,
                               startupinfo=startup, creationflags=flags)

    smoke_result: subprocess.CompletedProcess[str] | None = None
    quit_result: subprocess.CompletedProcess[str] | None = None
    cleanup_confirmed = False
    try:
        deadline = time.monotonic() + args.ready_timeout
        while time.monotonic() < deadline:
            if app.poll() is not None:
                raise RuntimeError(f"Electron exited before readiness (code {app.returncode}).")
            try:
                log = log_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                log = ""
            if "后端就绪 pid=" in log:
                break
            time.sleep(0.25)
        else:
            raise TimeoutError(f"Backend did not become ready within {args.ready_timeout}s.")

        page_url = wait_for_app_page(19222, args.ready_timeout)
        print(f"PACKAGED_RENDERER_URL={page_url}", flush=True)
        smoke_result = run_node(node, ROOT / "tools" / "shell-smoke.mjs",
                                ["19222", "19229"], cwd=checkout, env=env, timeout=90)
    except Exception as exc:
        print(f"PACKAGED_SMOKE_ERROR={type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        if app.poll() is None:
            try:
                quit_result = run_node(node, ROOT / "tools" / "shell-quit.mjs",
                                       ["19229"], cwd=checkout, env=env, timeout=35)
            except Exception as exc:
                print(f"PACKAGED_QUIT_ERROR={type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
            if app.poll() is None:
                try:
                    app.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    app.terminate()
                    try:
                        app.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        killed = subprocess.run(
                            ["taskkill", "/PID", str(app.pid), "/T", "/F"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            check=False)
                        print(f"TASKKILL_EXIT_CODE={killed.returncode}", flush=True)
                        try:
                            app.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            print("PACKAGED_PROCESS_EXIT_UNCONFIRMED=true", file=sys.stderr,
                                  flush=True)
            cleanup_confirmed = app.poll() is not None
        else:
            app.wait()
            cleanup_confirmed = True

    profile_root = Path(env["LOCALAPPDATA"])
    evidence = {
        "workspace": str(workspace),
        "isolated_localappdata": env["LOCALAPPDATA"],
        "shell_log": str(log_path),
        "electron_exit_code": app.returncode,
        "electron_exit_confirmed": cleanup_confirmed,
        "shell_smoke_exit_code": None if smoke_result is None else smoke_result.returncode,
        "shell_quit_exit_code": None if quit_result is None else quit_result.returncode,
        "profile_file_count": sum(1 for p in profile_root.rglob("*") if p.is_file()),
        "sqlite_created": (profile_root / "BiliFavOrganizer" / "data" / "library.sqlite3").is_file(),
        "config_created": (profile_root / "BiliFavOrganizer" / "config.json").is_file(),
    }
    print("PACKAGED_SMOKE_EVIDENCE=" + json.dumps(evidence, ensure_ascii=False), flush=True)
    return 0 if (smoke_result is not None and smoke_result.returncode == 0
                 and quit_result is not None and quit_result.returncode == 0
                 and cleanup_confirmed and app.returncode == 0) else 1


def shutil_which(program: str) -> str | None:
    from shutil import which
    return which(program)


if __name__ == "__main__":
    raise SystemExit(main())
