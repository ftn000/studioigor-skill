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

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Optional

IS_WIN = os.name == "nt"


def try_lock(path: Path) -> Optional[IO]:
    """Non-blocking exclusive lock on `path` (created if missing).

    Returns an open file object that HOLDS the lock, or None if another process holds it.
    The lock is released when the returned object is closed or the process exits.
    POSIX: fcntl.flock(LOCK_EX | LOCK_NB). Windows: msvcrt.locking(LK_NBLCK) on byte 0.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if IS_WIN:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        fh.close()
        return None
    return fh


def process_alive(pid) -> bool:
    """True if a process with this PID exists right now. Must never signal it.
    Falsy or invalid pid -> False.
    POSIX: os.kill(pid, 0) is allowed here ONLY on POSIX (signal 0 is a probe there).
    Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess == STILL_ACTIVE (259)
    via ctypes.
    """
    if not pid or isinstance(pid, bool) or not isinstance(pid, int):
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_cmdline(pid) -> str:
    """Full command line of the process, or "" if it is gone or unreadable.
    POSIX: `ps -o command= -p <pid>`.
    Windows: powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>').CommandLine"
    Use a timeout of 15 s. Parse only the integer pid; never interpolate other input into the command.
    """
    if not process_alive(pid):
        return ""
    pid = int(pid)
    if IS_WIN:
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"]
    else:
        cmd = ["ps", "-o", "command=", "-p", str(pid)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out or "").strip()


def spawn_detached(argv: list[str], *, cwd: Path, env: dict, stdout: IO, stderr: IO,
                   stdin=subprocess.DEVNULL) -> subprocess.Popen:
    """Start a child in its own process group, so kill_tree() can stop it and its children.
    POSIX: start_new_session=True. Windows: creationflags=CREATE_NEW_PROCESS_GROUP (0x200).
    Returns the Popen; proc.pid is the root of the tree.
    """
    argv = list(argv)
    kwargs: dict = {}
    if IS_WIN:
        argv[0] = shutil.which(argv[0]) or argv[0]
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=stdout, stderr=stderr,
                            stdin=stdin, **kwargs)


def kill_tree(pid, *, force: bool = False) -> None:
    """Stop the process `pid` and all its children. Best effort: never raises for a dead pid.
    force=False: polite stop (POSIX SIGTERM to the group; Windows `taskkill /T /PID`).
    force=True: hard stop (POSIX SIGKILL to the group; Windows `taskkill /T /F /PID`).
    Callers that want "graceful, then hard" call it twice with a short wait in between.
    """
    if not pid:
        return
    try:
        if IS_WIN:
            cmd = ["taskkill", "/T"] + (["/F"] if force else []) + ["/PID", str(int(pid))]
            subprocess.run(cmd, capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(int(pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass


def keep_awake(seconds: Optional[int] = None, watch_pid: Optional[int] = None) -> Optional[int]:
    """Stop the machine from sleeping while the work runs, even after the caller exits.

    Returns the PID of a keeper process, or None when nothing could be started (then the caller
    just runs without keep-awake; it must not fail). Stop it with kill_tree(pid).
    The keeper exits on its own when `seconds` elapse (if given) or `watch_pid` is no longer
    alive (if given).
    macOS: `caffeinate -i -s` (with -t / -w as needed).
    Windows: a detached `ostools.py keep-awake` process that calls
    SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) every 60 s.
    Linux: returns None.
    """
    try:
        if sys.platform == "darwin":
            cmd = ["caffeinate", "-i", "-s"]
            if seconds:
                cmd += ["-t", str(int(seconds))]
            elif watch_pid:
                cmd += ["-w", str(int(watch_pid))]
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            return proc.pid
        if IS_WIN:
            cmd = [sys.executable, os.path.abspath(__file__), "keep-awake",
                   "--seconds", str(int(seconds or 0)), "--watch-pid", str(int(watch_pid or 0))]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x8)
            return proc.pid
    except Exception:
        return None
    return None


def _keep_awake_loop(seconds: int, watch_pid: int) -> int:
    """Foreground keep-awake loop (Windows). Returns when the deadline passes or watch_pid dies."""
    import ctypes
    es_continuous, es_system_required = 0x80000000, 0x00000001
    set_state = ctypes.windll.kernel32.SetThreadExecutionState
    deadline = time.monotonic() + seconds if seconds else None
    try:
        while True:
            set_state(es_continuous | es_system_required)
            time.sleep(60 if deadline is None else max(0.0, min(60.0, deadline - time.monotonic())))
            if deadline is not None and time.monotonic() >= deadline:
                break
            if watch_pid and not process_alive(watch_pid):
                break
    finally:
        set_state(es_continuous)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ostools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ka = sub.add_parser("keep-awake")
    ka.add_argument("--seconds", type=int, default=0)
    ka.add_argument("--watch-pid", type=int, default=0)
    args = parser.parse_args(argv)
    return _keep_awake_loop(args.seconds, args.watch_pid)


if __name__ == "__main__":
    sys.exit(main())
