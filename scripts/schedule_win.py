"""Windows scheduler backend for autopilot: Task Scheduler through `schtasks`.

Notes:
- Task Scheduler runs the job only while the user is logged in and the PC is
  awake. That is a known limitation of this backend.
- The task runs as the current user with the default settings.
- `schtasks /TR` is limited to 261 characters, so the real command lives in a
  wrapper batch file and only its quoted path is passed to /TR.
- The wrapper is written as UTF-8 (no BOM). cmd.exe may not parse non-ASCII
  paths in it correctly; use ASCII-safe paths where possible.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def win_install(label: str, argv: list[str], every_minutes: int, state_dir: Path) -> str:
    """Create or replace the task. Returns "" on success, else an error message."""
    try:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        wrapper = state_dir / f"{label}.cmd"
        body = "@echo off\n" + subprocess.list2cmdline(argv) + "\n"
        with wrapper.open("w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        if every_minutes < 60:
            sched = ["/SC", "MINUTE", "/MO", str(max(1, every_minutes))]
        else:
            sched = ["/SC", "HOURLY", "/MO", str(min(23, max(1, round(every_minutes / 60))))]
        cmd = ["schtasks", "/Create", "/TN", label, "/TR", f'"{wrapper}"', *sched, "/F"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return f"schtasks /Create failed: {e}"
    if p.returncode == 0:
        return ""
    return "schtasks /Create failed: " + (p.stderr or p.stdout).strip()


def win_uninstall(label: str) -> None:
    """Delete the task; the result is ignored."""
    try:
        subprocess.run(["schtasks", "/Delete", "/TN", label, "/F"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def win_installed(label: str) -> bool:
    """True if the task exists."""
    try:
        p = subprocess.run(["schtasks", "/Query", "/TN", label], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0
