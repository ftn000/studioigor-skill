"""Smoke tests for the studioigor scripts. Stdlib only.

    python -m unittest discover -s tests -v

They cover what broke on Windows before the portability pass (imports of Unix-only modules,
python3 hard-coded in messages, lsof, console encoding) and the project-state core of studio.py.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))


def run_script(name, *args, cwd=None):
    env = dict(os.environ, PYTHONUTF8="1")
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=120, env=env)


class ImportTests(unittest.TestCase):
    """Modules must import on every platform; Unix-only pieces are guarded, not fatal."""

    def test_modules_import(self):
        for mod in ["refs", "board", "envcheck", "learn", "scope"]:
            with self.subTest(module=mod):
                __import__(mod)

    def test_port_listening_is_false_for_unused_port(self):
        import envcheck
        # bind a free port, close it, and check nothing listens there
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertFalse(envcheck.port_listening(port))

    def test_lock_helper_takes_and_releases(self):
        import refs
        with tempfile.TemporaryFile("w+") as fh:
            refs._lock(fh)  # must not raise on Windows (msvcrt) or POSIX (fcntl)

    def test_autopilot_help_runs_on_this_platform(self):
        r = run_script("autopilot.py", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)


class CliTests(unittest.TestCase):
    def test_help_for_each_cli(self):
        for name in ["studio.py", "scope.py", "learn.py", "refs.py", "board.py"]:
            with self.subTest(script=name):
                r = run_script(name, "--help")
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_studio_project_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = run_script("studio.py", "init", str(root), "--name", "Smoke Game")
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertTrue((root / ".studioigor").is_dir())

            r = run_script("studio.py", "decide", "smoke decision", "--by", "user", "--why", "test",
                           cwd=root)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

            r = run_script("studio.py", "status", cwd=root)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertIn("Smoke Game", r.stdout)

    def test_studio_prints_unicode_without_crashing(self):
        # the em dash in the status header used to fail on cp1251/cp437 consoles
        with tempfile.TemporaryDirectory() as tmp:
            run_script("studio.py", "init", tmp, "--name", "Юникод — тест")
            r = run_script("studio.py", "status", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Юникод", r.stdout)


class StateTests(unittest.TestCase):
    def test_init_writes_pipeline_and_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_script("studio.py", "init", tmp, "--name", "State Game")
            st = Path(tmp) / ".studioigor"
            for f in ["PIPELINE.md", "DECISIONS.md", "STATE.md"]:
                self.assertTrue((st / f).exists(), f)


class AutopilotGoalTests(unittest.TestCase):
    """Goals cover every stage, audit closes by its report, untracked user files stay out of commits."""

    def test_goals_cover_every_stage_and_audit(self):
        import autopilot
        for g in ["audit", "idea", "style", "stack", "assets", "mechanics", "slice", "content", "polish", "build"]:
            with self.subTest(goal=g):
                self.assertIn(g, autopilot.GOALS)

    def test_audit_is_closed_by_its_report(self):
        import autopilot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".studioigor" / "autopilot").mkdir(parents=True)
            self.assertFalse(autopilot.goal_closed(root, "audit"))
            (root / ".studioigor" / "autopilot" / "AUDIT.md").write_text("# audit\n", encoding="utf-8")
            self.assertTrue(autopilot.goal_closed(root, "audit"))

    def test_user_untracked_files_are_excluded_from_add_all(self):
        import autopilot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "user_notes").mkdir()
            (root / "user_notes" / "a.txt").write_text("mine", encoding="utf-8")
            (root / "game.txt").write_text("code", encoding="utf-8")
            (root / ".studioigor").mkdir()
            (root / ".studioigor" / "STATE.md").write_text("state", encoding="utf-8")
            hidden = autopilot.hide_user_untracked(root)
            self.assertIn("user_notes", hidden)
            self.assertNotIn(".studioigor", hidden)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            staged = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--name-only"],
                                    capture_output=True, text=True).stdout.split()
            self.assertNotIn("user_notes/a.txt", staged)
            self.assertIn(".studioigor/STATE.md", staged)


if __name__ == "__main__":
    unittest.main()
