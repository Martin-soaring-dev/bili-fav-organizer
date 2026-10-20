from __future__ import annotations

import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path

from core.database.profile_lock import ProfileLockTimeoutError
import server


def _hold_server_instance_lease(root: str, acquired, release) -> None:
    lease = server._acquire_server_instance_lease(Path(root))
    acquired.set()
    try:
        if not release.wait(10):
            raise TimeoutError("test parent did not release the server instance lock")
    finally:
        lease.close()


class ServerInstanceLockTests(unittest.TestCase):
    def test_second_process_cannot_share_a_vault_profile(self):
        with tempfile.TemporaryDirectory(prefix="bfo-server-instance-") as directory:
            context = multiprocessing.get_context("spawn")
            acquired = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_server_instance_lease,
                args=(directory, acquired, release),
            )
            process.start()
            try:
                self.assertTrue(acquired.wait(10), "first service process did not acquire its lease")
                with self.assertRaises(ProfileLockTimeoutError):
                    server._acquire_server_instance_lease(Path(directory))
            finally:
                release.set()
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            self.assertEqual(0, process.exitcode)
            lease = server._acquire_server_instance_lease(Path(directory))
            lease.close()

    def test_server_entrypoint_holds_and_releases_profile_lease(self):
        project_root = Path(server.__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="bfo-server-entrypoint-") as directory:
            root = Path(directory)
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(root / "local")
            env["USERPROFILE"] = str(root / "user")
            env["HOME"] = str(root / "home")
            env["TEMP"] = str(root / "temp")
            env["TMP"] = str(root / "temp")
            env.pop("BILI_FAV_ORGANIZER_DATA_DIR", None)
            env["BFO_PARENT_PIPE"] = "1"
            command = [
                sys.executable, str(project_root / "server.py"),
                "--port", "0", "--no-browser", "--handshake",
            ]

            def start_server():
                process = subprocess.Popen(
                    command,
                    cwd=project_root,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                lines: queue.Queue[str | None] = queue.Queue()

                def collect_output():
                    assert process.stdout is not None
                    for line in process.stdout:
                        lines.put(line)
                    lines.put(None)

                reader = threading.Thread(target=collect_output, daemon=True)
                reader.start()
                return process, lines, reader

            def stop_server(process, reader):
                if process.stdin is not None and not process.stdin.closed:
                    # The supported parent-pipe shutdown path runs main()'s
                    # finally block, including closing the profile lease.
                    process.stdin.close()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                reader.join(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                self.assertEqual(0, process.returncode)

            first, first_lines, first_reader = start_server()

            def wait_until_ready(process, lines):
                seen = []
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=max(0.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if line is None:
                        break
                    seen.append(line)
                    if line.startswith("BFO_READY "):
                        return
                    if process.poll() is not None:
                        break
                self.fail("server.py did not announce readiness: " + "".join(seen))

            try:
                wait_until_ready(first, first_lines)
                second = subprocess.run(
                    command,
                    cwd=project_root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                self.assertEqual(2, second.returncode, second.stdout)
                self.assertIn("已有服务实例运行", second.stdout)
            finally:
                stop_server(first, first_reader)

            third, third_lines, third_reader = start_server()
            try:
                wait_until_ready(third, third_lines)
            finally:
                stop_server(third, third_reader)


if __name__ == "__main__":
    unittest.main()
