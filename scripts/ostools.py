"""Platform helpers for autopilot.py: locks, process checks, process trees, keep-awake.

Stdlib only. Works on macOS, Linux and native Windows. Every public function has the same
contract on all platforms; only the implementation differs.

HARD RULES
- Never call os.kill() with a signal in this module. On Windows, os.kill(pid, 0) and any other
  signal except CTRL events call TerminateProcess and kill the process. Use process_alive().
- POSIX-only names (fcntl, signal.SIGHUP, signal.SIGKILL, os.killpg, os.getuid) appear only
  inside `if IS_WIN` / `if not IS_WIN` branches, never at import time.
- Windows process inspection uses PowerShell (Get-CimInstance Win32_Process), not psutil.
- Every subprocess call has a timeout.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import IO, Callable, Optional

IS_WIN = os.name == "nt"


def try_lock(path: Path) -> Optional[IO]:
    """Non-blocking exclusive lock on `path` (created if missing).

    Returns an open file object that HOLDS the lock, or None if another process holds it.
    The lock is released when the returned object is closed or the process exits.
    POSIX: fcntl.flock(LOCK_EX | LOCK_NB). Windows: msvcrt.locking(LK_NBLCK) on byte 0.
    """
    raise NotImplementedError


def process_alive(pid) -> bool:
    """True if a process with this PID exists right now. Must never signal it.
    Falsy or invalid pid -> False.
    POSIX: os.kill(pid, 0) is allowed here ONLY on POSIX (signal 0 is a probe there).
    Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess == STILL_ACTIVE (259)
    via ctypes.
    """
    raise NotImplementedError


def process_cmdline(pid) -> str:
    """Full command line of the process, or "" if it is gone or unreadable.
    POSIX: `ps -o command= -p <pid>`.
    Windows: powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>').CommandLine"
    Use a timeout of 15 s. Parse only the integer pid; never interpolate other input into the command.
    """
    raise NotImplementedError


def spawn_detached(argv: list[str], *, cwd: Path, env: dict, stdout: IO, stderr: IO,
                   stdin=subprocess.DEVNULL) -> subprocess.Popen:
    """Start a child in its own process group, so kill_tree() can stop it and its children.
    POSIX: start_new_session=True. Windows: creationflags=CREATE_NEW_PROCESS_GROUP (0x200).
    Returns the Popen; proc.pid is the root of the tree.
    """
    raise NotImplementedError


def kill_tree(pid, *, force: bool = False) -> None:
    """Stop the process `pid` and all its children. Best effort: never raises for a dead pid.
    force=False: polite stop (POSIX SIGTERM to the group; Windows `taskkill /T /PID`).
    force=True: hard stop (POSIX SIGKILL to the group; Windows `taskkill /T /F /PID`).
    Callers that want "graceful, then hard" call it twice with a short wait in between.
    """
    raise NotImplementedError


def keep_awake(seconds: Optional[int] = None, watch_pid: Optional[int] = None) -> Optional[Callable[[], None]]:
    """Stop the machine from sleeping while the work runs.

    Returns a release() function, or None when nothing could be started (then the caller just runs
    without keep-awake; it must not fail).
    Stops when: release() is called, `seconds` elapse (if given), or `watch_pid` is no longer alive
    (if given).
    macOS: `caffeinate -i -s` (with -t / -w as needed).
    Windows: a daemon thread that calls SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    every 60 s and clears it (ES_CONTINUOUS alone) on release. The thread must stay alive.
    Linux: returns None.
    """
    raise NotImplementedError
