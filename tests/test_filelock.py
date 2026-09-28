from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brainskit.infrastructure.filelock import file_lock

_CHILD = """
import sys
from pathlib import Path
from brainskit.infrastructure.filelock import file_lock

lock_path, attempted_path, acquired_path, shared = sys.argv[1:]
Path(attempted_path).write_text("attempted", encoding="utf-8")
with file_lock(Path(lock_path), shared=shared == "true"):
    Path(acquired_path).write_text("acquired", encoding="utf-8")
"""


class FileLockTest(unittest.TestCase):
    def _spawn(self, root: Path, *, shared: bool) -> tuple[subprocess.Popen[str], Path, Path]:
        attempted = root / "attempted"
        acquired = root / "acquired"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                str(root / "state.lock"),
                str(attempted),
                str(acquired),
                str(shared).lower(),
            ],
            text=True,
        )
        return process, attempted, acquired

    def _wait_for(self, path: Path, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process.poll() is not None:
                self.fail(f"lock child exited early with {process.returncode}")
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path.name}")

    def _finish(self, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            self.fail("lock child did not exit after releasing its lock")

    def test_shared_locks_coexist_across_processes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with file_lock(root / "state.lock", shared=True):
                process, attempted, acquired = self._spawn(root, shared=True)
                try:
                    self._wait_for(attempted, process)
                    self._wait_for(acquired, process)
                    self._finish(process)
                finally:
                    process.kill() if process.poll() is None else None
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)

    def test_exclusive_lock_blocks_reader_until_release(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with file_lock(root / "state.lock", shared=False):
                process, attempted, acquired = self._spawn(root, shared=True)
                self._wait_for(attempted, process)
                time.sleep(0.2)
                self.assertFalse(acquired.exists())
            try:
                self._wait_for(acquired, process)
                self._finish(process)
            finally:
                process.kill() if process.poll() is None else None
                process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)

    def test_shared_lock_blocks_writer_until_release(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with file_lock(root / "state.lock", shared=True):
                process, attempted, acquired = self._spawn(root, shared=False)
                self._wait_for(attempted, process)
                time.sleep(0.2)
                self.assertFalse(acquired.exists())
            try:
                self._wait_for(acquired, process)
                self._finish(process)
            finally:
                process.kill() if process.poll() is None else None
                process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
