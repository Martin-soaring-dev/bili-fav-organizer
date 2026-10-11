"""Build a disposable Windows backend and Electron package without touching dist/."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from run_isolated_tests import ROOT, ignore


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("$ " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _prepare_environment(workspace: Path, *, allow_build_downloads: bool) -> dict[str, str]:
    profile = workspace / "profile"
    home = workspace / "home"
    temp = workspace / "temp"
    cache = workspace / "cache"
    for path in (profile, home, temp, cache):
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "LOCALAPPDATA": str(profile),
        "APPDATA": str(profile / "AppData"),
        "USERPROFILE": str(profile),
        "HOME": str(home),
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
        "ELECTRON_CACHE": str(cache / "electron"),
        "ELECTRON_BUILDER_CACHE": str(cache / "electron-builder"),
        "npm_config_cache": str(cache / "npm"),
        "CSC_IDENTITY_AUTO_DISCOVERY": "false",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "NO_PROXY": "127.0.0.1,localhost",
    })
    if allow_build_downloads:
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        env.pop("ALL_PROXY", None)
    else:
        env["HTTP_PROXY"] = "http://127.0.0.1:9"
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["ALL_PROXY"] = "http://127.0.0.1:9"
    env.pop("BILI_FAV_ORGANIZER_DATA_DIR", None)
    return env


def _junction(target: Path, source: Path, env: dict[str, str]) -> None:
    env = dict(env)
    env["BFO_JUNCTION_TARGET"] = str(target)
    env["BFO_JUNCTION_SOURCE"] = str(source)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "New-Item -ItemType Junction -Path $env:BFO_JUNCTION_TARGET "
         "-Target $env:BFO_JUNCTION_SOURCE | Out-Null"],
        cwd=ROOT, env=env, check=True,
    )


def _safe_remove(workspace: Path) -> None:
    root = ROOT.resolve()
    target = workspace.resolve()
    if target.parent != root or not target.name.startswith(".bfo-package-smoke-"):
        raise RuntimeError(f"Refusing to remove unexpected workspace path: {target}")
    junction = target / "checkout" / "node_modules"
    if junction.exists():
        # Removing the directory entry removes the junction; it does not traverse source node_modules.
        os.rmdir(junction)
    shutil.rmtree(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir-only", action="store_true",
                        help="build Electron's unpacked app directory, without an NSIS installer")
    parser.add_argument("--keep-workspace", action="store_true",
                        help="keep the disposable package for a later launch smoke test")
    parser.add_argument("--allow-build-downloads", action="store_true",
                        help="allow Electron Builder to fetch uncached packager tooling")
    parser.add_argument("--cleanup-workspace", type=Path,
                        help="remove a workspace previously printed with --keep-workspace")
    args = parser.parse_args()

    if args.cleanup_workspace:
        _safe_remove(args.cleanup_workspace)
        return 0
    if sys.platform != "win32":
        raise SystemExit("This package smoke builder requires Windows.")

    workspace = Path(tempfile.mkdtemp(prefix=".bfo-package-smoke-", dir=ROOT)).resolve()
    checkout = workspace / "checkout"
    env = _prepare_environment(workspace, allow_build_downloads=args.allow_build_downloads)
    try:
        shutil.copytree(ROOT, checkout, ignore=ignore)
        _junction(checkout / "node_modules", ROOT / "node_modules", env)

        dist = checkout / "dist"
        work = checkout / "build"
        _run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
              "--distpath", str(dist), "--workpath", str(work),
              "BiliFavOrganizer.spec"], cwd=checkout, env=env)
        builder_args = [shutil.which("node") or "node",
                        "node_modules/electron-builder/out/cli/cli.js",
                        "--config", "electron-builder.yml"]
        if args.dir_only:
            builder_args.extend([
                "--dir", "--win", "--x64", "--publish", "never",
                "-c.extraMetadata.version=0.209.0-beta.1",
                "-c.electronDist=node_modules/electron/dist",
            ])
        else:
            builder_args.extend([
                "--win", "nsis", "--x64", "--publish", "never",
                "-c.extraMetadata.version=0.209.0-beta.1",
            ])
        _run(builder_args, cwd=checkout, env=env)

        unpacked = dist / "electron" / "win-unpacked"
        exe = unpacked / "BiliFav Organizer.exe"
        installer = dist / "electron" / "BiliFavOrganizer-neo-Setup-0.209.0-beta.1.exe"
        if not exe.is_file() or (not args.dir_only and not installer.is_file()):
            raise RuntimeError(f"Expected package outputs missing under {dist / 'electron'}")
        report = {
            "workspace": str(workspace),
            "unpacked_exe": str(exe),
            "unpacked_bytes": exe.stat().st_size,
            "unpacked_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        }
        if installer.is_file():
            report.update({
                "installer": str(installer),
                "installer_bytes": installer.stat().st_size,
                "installer_sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
            })
        print("PACKAGE_SMOKE_OUTPUT=" + repr(report), flush=True)
        if not args.keep_workspace:
            _safe_remove(workspace)
        return 0
    except Exception:
        if not args.keep_workspace and workspace.exists():
            _safe_remove(workspace)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
