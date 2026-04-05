#!/usr/bin/env python3
"""
Behavioral tests for Resources/bin/copilot.

All tests exercise observable runtime behavior via subprocess execution of the
actual script — no running cmux app required.  Fake `copilot` and `cmux`
binaries are created in temp directories for full isolation.

PTY idle-detection tests call the script with the internal `--pty-mode` flag
so the child binary can be supplied directly without needing it in PATH.

Run with:
    python3 tests/test_copilot_wrapper.py
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(REPO_ROOT, "Resources", "bin", "copilot")
WRAPPER_DIR = os.path.dirname(WRAPPER)

# Always include system bin dirs so fake scripts using #!/usr/bin/env bash work.
_SYSTEM_PATH = "/usr/bin:/bin"


def _safe_path(*extra_dirs: str) -> str:
    parts = list(extra_dirs) + [_SYSTEM_PATH]
    return ":".join(p for p in parts if p)


def _write_exe(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Bash wrapper tests
# ---------------------------------------------------------------------------

class TestWrapperPassthrough(unittest.TestCase):
    """The wrapper execs the real copilot unchanged outside cmux."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        _write_exe(os.path.join(self.tmp, "copilot"), textwrap.dedent("""\
            #!/usr/bin/env bash
            printf 'real-copilot args:%s\\n' "$*"
            exit 0
        """))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, extra_env: dict | None = None, args: list | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": _safe_path(WRAPPER_DIR, self.tmp)}
        env.pop("CMUX_SURFACE_ID", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [WRAPPER] + (args or ["hello"]),
            env=env, capture_output=True, text=True, timeout=10,
        )

    def test_execs_real_copilot_when_not_in_cmux(self) -> None:
        """Without CMUX_SURFACE_ID the wrapper execs the real binary unchanged."""
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("real-copilot args:hello", result.stdout)

    def test_exit_code_propagated_outside_cmux(self) -> None:
        """Exit code from the real copilot is preserved."""
        _write_exe(os.path.join(self.tmp, "copilot"), "#!/usr/bin/env bash\nexit 42\n")
        result = self._run()
        self.assertEqual(result.returncode, 42)

    def test_args_forwarded_outside_cmux(self) -> None:
        """All command-line arguments are forwarded to the real binary."""
        result = self._run(args=["foo", "bar", "--baz"])
        self.assertIn("foo bar --baz", result.stdout)


class TestWrapperFindRealCopilot(unittest.TestCase):
    """_find_real_copilot skips the wrapper's own directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        _write_exe(os.path.join(self.tmp, "copilot"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "found-real-copilot"
            exit 7
        """))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wrapper_dir_skipped_real_binary_found(self) -> None:
        """Wrapper never resolves to itself; finds real binary in a later PATH entry."""
        env = {**os.environ, "PATH": _safe_path(WRAPPER_DIR, self.tmp)}
        env.pop("CMUX_SURFACE_ID", None)
        result = subprocess.run(
            [WRAPPER], env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("found-real-copilot", result.stdout)

    def test_error_when_real_copilot_not_in_path(self) -> None:
        """Wrapper exits non-zero with a clear message when no real binary is found."""
        env = {**os.environ, "PATH": _safe_path(WRAPPER_DIR)}
        env.pop("CMUX_SURFACE_ID", None)
        result = subprocess.run(
            [WRAPPER], env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("copilot", result.stderr.lower())


# ---------------------------------------------------------------------------
# PTY helper tests — idle-detection based
# ---------------------------------------------------------------------------

class TestPtyHelperIdleDetection(unittest.TestCase):
    """
    Idle-detection loop: fires cmux notify after the child is silent for IDLE_THRESHOLD.

    Tests call the script with `--pty-mode <cmux_bin> <child_bin>` to exercise
    the PTY logic directly without needing a real `copilot` binary in PATH.
    """

    # Use a very short threshold so tests run fast.
    IDLE_THRESHOLD = "0.15"

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.notify_log = os.path.join(self.tmp, "notify.log")
        _write_exe(os.path.join(self.tmp, "cmux"), textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "$@" >> {self.notify_log}
        """))
        self.fake_cmux = os.path.join(self.tmp, "cmux")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_helper(
        self,
        child_script: str,
        *,
        extra_env: dict | None = None,
        timeout: float = 8.0,
    ) -> subprocess.CompletedProcess:
        child_bin = os.path.join(self.tmp, "fake-copilot")
        _write_exe(child_bin, child_script)
        env = {
            **os.environ,
            "CMUX_SURFACE_ID": "test-surface",
            "CMUX_COPILOT_IDLE_THRESHOLD": self.IDLE_THRESHOLD,
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, WRAPPER, "--pty-mode", self.fake_cmux, child_bin],
            env=env,
            capture_output=True,
            timeout=timeout,
        )

    def _wait_for_notify(self, *, wait: float = 3.0, poll: float = 0.05) -> str:
        """Poll until the notify log has content (Popen write is async)."""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                with open(self.notify_log) as f:
                    content = f.read()
                if content:
                    return content
            except FileNotFoundError:
                pass
            time.sleep(poll)
        try:
            with open(self.notify_log) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    # -- notify fires ---------------------------------------------------------

    def test_notify_fires_after_idle(self) -> None:
        """Child writes output then pauses — notify should fire once."""
        # Child prints something then sleeps longer than the idle threshold.
        idle_s = float(self.IDLE_THRESHOLD) * 4
        self._run_helper(
            f"#!/usr/bin/env bash\nprintf 'some output'\nsleep {idle_s:.2f}\nexit 0\n"
        )
        log = self._wait_for_notify()
        self.assertIn("notify", log, f"Expected notify after idle; log={log!r}")

    def test_notify_title_and_body_defaults(self) -> None:
        """Notify is called with the default --title Copilot --body text."""
        idle_s = float(self.IDLE_THRESHOLD) * 4
        self._run_helper(
            f"#!/usr/bin/env bash\nprintf 'copilot output line'\nsleep {idle_s:.2f}\nexit 0\n"
        )
        log = self._wait_for_notify()
        self.assertIn("Copilot", log)
        self.assertIn("Waiting for your input", log)

    def test_custom_title_and_body_env(self) -> None:
        """CMUX_COPILOT_NOTIFY_TITLE and _BODY override the defaults."""
        idle_s = float(self.IDLE_THRESHOLD) * 4
        self._run_helper(
            f"#!/usr/bin/env bash\nprintf 'copilot output line'\nsleep {idle_s:.2f}\nexit 0\n",
            extra_env={
                "CMUX_COPILOT_NOTIFY_TITLE": "MyTitle",
                "CMUX_COPILOT_NOTIFY_BODY": "MyBody",
            },
        )
        log = self._wait_for_notify()
        self.assertIn("MyTitle", log)
        self.assertIn("MyBody", log)

    def test_notify_fires_once_per_idle_period(self) -> None:
        """Continued silence only triggers one notify, not repeated ones."""
        idle_s = float(self.IDLE_THRESHOLD) * 6
        self._run_helper(
            f"#!/usr/bin/env bash\nprintf 'some output'\nsleep {idle_s:.2f}\nexit 0\n"
        )
        log = self._wait_for_notify()
        notify_lines = [l for l in log.splitlines() if "notify" in l]
        self.assertEqual(len(notify_lines), 1, f"Expected one notify; log={log!r}")

    # -- notify does NOT fire -------------------------------------------------

    def test_no_notify_when_child_exits_immediately(self) -> None:
        """Child with no output that exits instantly should not trigger notify."""
        self._run_helper("#!/usr/bin/env bash\nexit 0\n")
        # Give async notify a moment to arrive if it was wrongly fired.
        time.sleep(float(self.IDLE_THRESHOLD) * 3)
        log = self._wait_for_notify(wait=0.1)
        self.assertEqual(log, "", f"Expected no notify; log={log!r}")

    def test_no_notify_before_idle_threshold(self) -> None:
        """Child that exits before IDLE_THRESHOLD elapses should not notify."""
        # Child writes output and exits in less than the idle threshold.
        fast_exit_s = float(self.IDLE_THRESHOLD) * 0.3
        self._run_helper(
            f"#!/usr/bin/env bash\nprintf 'quick output'\nsleep {fast_exit_s:.3f}\nexit 0\n"
        )
        time.sleep(float(self.IDLE_THRESHOLD) * 0.5)
        log = self._wait_for_notify(wait=0.1)
        self.assertEqual(log, "", f"Expected no notify for fast exit; log={log!r}")

    # -- exit code and output passthrough ------------------------------------

    def test_exit_code_zero_propagated(self) -> None:
        """Helper propagates exit code 0 from the child."""
        result = self._run_helper("#!/usr/bin/env bash\nexit 0\n")
        self.assertEqual(result.returncode, 0)

    def test_exit_code_nonzero_propagated(self) -> None:
        """Helper propagates a non-zero exit code from the child."""
        result = self._run_helper("#!/usr/bin/env bash\nexit 7\n")
        self.assertEqual(result.returncode, 7)

    def test_output_forwarded_to_stdout(self) -> None:
        """Child stdout is passed through to the caller unchanged."""
        result = self._run_helper(
            "#!/usr/bin/env bash\nprintf 'hello-output'\nexit 0\n"
        )
        self.assertIn(b"hello-output", result.stdout)

    # -- notify resets after each idle period --------------------------------

    def test_notify_resets_after_new_output(self) -> None:
        """After user presses Enter, a second idle period triggers another notify."""
        import pty as _pty_mod
        import threading

        idle_s = float(self.IDLE_THRESHOLD) * 4

        # Child outputs a burst, then waits for a line on stdin (simulates waiting
        # for the user), then outputs a second burst and goes idle again.
        child_bin = os.path.join(self.tmp, "fake-copilot-two-bursts")
        _write_exe(child_bin, textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf 'first output burst'
            read -r _input
            printf 'second output burst'
            sleep {idle_s:.2f}
            exit 0
        """))

        # Give the helper a real PTY as stdin so stdin_is_tty=True and
        # Enter-detection code runs.
        master_stdin_fd, slave_stdin_fd = _pty_mod.openpty()
        env = {
            **os.environ,
            "CMUX_SURFACE_ID": "test-surface",
            "CMUX_COPILOT_IDLE_THRESHOLD": self.IDLE_THRESHOLD,
        }

        proc = subprocess.Popen(
            [sys.executable, WRAPPER, "--pty-mode", self.fake_cmux, child_bin],
            stdin=slave_stdin_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        os.close(slave_stdin_fd)

        # After the first idle fires (threshold * 4 sleep + threshold for idle),
        # write Enter so the helper resets notified and the child continues.
        def _send_enter() -> None:
            time.sleep(float(self.IDLE_THRESHOLD) * 6)
            try:
                os.write(master_stdin_fd, b"\r\n")
            except OSError:
                pass

        t = threading.Thread(target=_send_enter, daemon=True)
        t.start()

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        finally:
            try:
                os.close(master_stdin_fd)
            except OSError:
                pass
            proc.stdout and proc.stdout.close()
            proc.stderr and proc.stderr.close()
        t.join(timeout=2)

        log = self._wait_for_notify(wait=3.0)
        notify_lines = [l for l in log.splitlines() if "notify" in l]
        self.assertEqual(len(notify_lines), 2, f"Expected two notifies; log={log!r}")

    def test_no_renotify_while_typing(self) -> None:
        """Typing characters (without Enter) does not re-fire the notification."""
        idle_s = float(self.IDLE_THRESHOLD) * 4
        # Child: output, then go idle (notify fires). Then echo keystrokes without
        # newline — these should NOT reset notified.
        self._run_helper(
            f"#!/usr/bin/env bash\n"
            f"printf 'copilot asks something'\n"
            f"sleep {idle_s:.2f}\n"
            # Simulate typing characters (no Enter) — child writes them to TTY as echo
            f"printf 'abc'\n"
            f"sleep {idle_s:.2f}\n"
            f"exit 0\n"
        )
        log = self._wait_for_notify(wait=5.0)
        notify_lines = [l for l in log.splitlines() if "notify" in l]
        self.assertEqual(len(notify_lines), 1, f"Expected one notify; got {len(notify_lines)}; log={log!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
