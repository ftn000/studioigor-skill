"""Tests for scripts/ostools.py using real child processes (no mocks). Stdlib only.

    python -m unittest tests/test_ostools.py -v
"""
import ast
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ostools  # noqa: E402

IS_WIN = ostools.IS_WIN
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def wait_until(pred, timeout):
    """Poll pred() until it is true or timeout seconds pass; return its last value."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return bool(pred())


def gone(pid):
    return not ostools.process_alive(pid)


class ProcessAliveTests(unittest.TestCase):
    def test_self_is_alive(self):
        """Our own pid is alive."""
        self.assertTrue(ostools.process_alive(os.getpid()))

    def test_invalid_pids(self):
        """Falsy and wrongly typed pids are not alive."""
        for bad in (0, None, "123", True):
            with self.subTest(pid=bad):
                self.assertFalse(ostools.process_alive(bad))

    def test_exited_child(self):
        """A child that already exited is not alive."""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=30)
        self.assertFalse(ostools.process_alive(p.pid))

    def test_sleeping_child_and_kill(self):
        """A sleeping child is alive, and kill_tree(force) stops it."""
        p = subprocess.Popen(SLEEPER)
        try:
            self.assertTrue(ostools.process_alive(p.pid))
            ostools.kill_tree(p.pid, force=True)
            self.assertTrue(wait_until(lambda: gone(p.pid), 10))
        finally:
            p.kill()
            p.wait(timeout=10)


class CmdlineTests(unittest.TestCase):
    def test_cmdline(self):
        """process_cmdline sees the child's command line; a dead pid gives ''."""
        p = subprocess.Popen(SLEEPER)
        try:
            self.assertIn("time.sleep(120)", ostools.process_cmdline(p.pid))
        finally:
            p.kill()
            p.wait(timeout=10)
        self.assertEqual(ostools.process_cmdline(p.pid), "")


class SpawnAndKillTests(unittest.TestCase):
    def test_spawn_detached_and_kill(self):
        """spawn_detached starts a live child; kill_tree stops it."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.devnull, "w") as out, open(os.devnull, "w") as err:
                p = ostools.spawn_detached(SLEEPER, cwd=Path(tmp), env=os.environ.copy(),
                                           stdout=out, stderr=err)
            try:
                self.assertTrue(ostools.process_alive(p.pid))
                ostools.kill_tree(p.pid)
                stopped = wait_until(lambda: gone(p.pid), 10)
                if not stopped:
                    # Expected on Windows: plain `taskkill` (no /F) only sends WM_CLOSE, which a
                    # console process in its own process group can ignore. Forced kill must work.
                    self.assertTrue(IS_WIN, "polite kill_tree should stop the child on POSIX")
                    ostools.kill_tree(p.pid, force=True)
                    self.assertTrue(wait_until(lambda: gone(p.pid), 10))
            finally:
                p.kill()
                p.wait(timeout=10)

    def test_kill_tree_kills_grandchild(self):
        """kill_tree(force) takes down the whole tree, not just the root."""
        parent_code = (
            "import subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "open(sys.argv[1], 'w').write(str(g.pid))\n"
            "time.sleep(120)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "grandchild.pid"
            with open(os.devnull, "w") as out, open(os.devnull, "w") as err:
                p = ostools.spawn_detached([sys.executable, "-c", parent_code, str(pidfile)],
                                           cwd=Path(tmp), env=os.environ.copy(),
                                           stdout=out, stderr=err)
            gpid = None
            try:
                self.assertTrue(wait_until(
                    lambda: pidfile.exists() and pidfile.read_text().strip().isdigit(), 20))
                gpid = int(pidfile.read_text().strip())
                self.assertTrue(ostools.process_alive(gpid))
                ostools.kill_tree(p.pid, force=True)
                self.assertTrue(wait_until(lambda: gone(p.pid), 10), "parent still alive")
                self.assertTrue(wait_until(lambda: gone(gpid), 10), "grandchild still alive")
            finally:
                p.kill()
                p.wait(timeout=10)
                if gpid:
                    ostools.kill_tree(gpid, force=True)

    def test_kill_tree_dead_or_falsy(self):
        """kill_tree on falsy or dead pids does not raise."""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=30)
        for pid in (0, None, p.pid):
            with self.subTest(pid=pid):
                ostools.kill_tree(pid)
                ostools.kill_tree(pid, force=True)


class LockTests(unittest.TestCase):
    def test_try_lock(self):
        """Second lock fails while the first is held; works again after release."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "x.lock"
            handles = []
            try:
                first = ostools.try_lock(path)
                handles.append(first)
                self.assertIsNotNone(first)
                self.assertTrue(hasattr(first, "fileno"))
                self.assertIsNone(ostools.try_lock(path))
                first.close()
                third = ostools.try_lock(path)
                handles.append(third)
                self.assertIsNotNone(third)
                self.assertTrue(hasattr(third, "fileno"))
            finally:
                for h in handles:
                    if h:
                        h.close()


@unittest.skipUnless(IS_WIN, "keep_awake keeper process is Windows-specific here")
class KeepAwakeTests(unittest.TestCase):
    def test_keep_awake_seconds(self):
        """The keeper exits by itself after `seconds`."""
        pid = ostools.keep_awake(seconds=2)
        try:
            self.assertIsInstance(pid, int)
            self.assertTrue(ostools.process_alive(pid))
            time.sleep(4)
            self.assertFalse(ostools.process_alive(pid))
        finally:
            ostools.kill_tree(pid, force=True)

    def test_keep_awake_watch_pid(self):
        """The keeper exits once the watched pid dies (its loop wakes every 60 s, so allow 75 s)."""
        child = subprocess.Popen(SLEEPER)
        pid = None
        try:
            pid = ostools.keep_awake(watch_pid=child.pid)
            self.assertIsInstance(pid, int)
            self.assertTrue(ostools.process_alive(pid))
            child.kill()
            child.wait(timeout=10)
            self.assertTrue(wait_until(lambda: gone(pid), 75))
        finally:
            child.kill()
            child.wait(timeout=10)
            ostools.kill_tree(pid, force=True)


class SourceGuardTests(unittest.TestCase):
    def test_single_os_kill_in_posix_branch(self):
        """The only os.kill() call is in process_alive, after its `if IS_WIN` branch."""
        text = (SCRIPTS / "ostools.py").read_text(encoding="utf-8")
        calls = [n for n in ast.walk(ast.parse(text))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "kill" and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == "os"]
        self.assertEqual(len(calls), 1)
        lines = text.splitlines()
        guard = [i + 1 for i, ln in enumerate(lines) if ln.strip() == "if IS_WIN:"]
        self.assertTrue(any(g < calls[0].lineno for g in guard))
        # the call must come after the Windows branch has returned in process_alive
        start = max(g for g in guard if g < calls[0].lineno)
        self.assertIn("return", "\n".join(lines[start:calls[0].lineno - 1]))


if __name__ == "__main__":
    unittest.main()
