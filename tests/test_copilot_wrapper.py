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

    def _make_timestamped_cmux(self) -> str:
        """Create a fake cmux that prepends a millisecond timestamp to each log line.

        Used by tests that need to compare when the notification fired against
        when a child-side event occurred.
        """
        path = os.path.join(self.tmp, "cmux-ts")
        _write_exe(path, textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "$(python3 -c 'import time; print(int(time.time()*1000))') $@" >> {self.notify_log}
        """))
        return path

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

    # -- streaming / false positive edge cases --------------------------------

    def test_streaming_sub_threshold_gaps_fires_once(self) -> None:
        """Many output bursts separated by short gaps fire exactly one notification.

        Copilot CLI streams tokens as they arrive from the API.  As long as each
        inter-token gap stays below IDLE_THRESHOLD, we should not notify until the
        full response has been delivered.
        """
        short_gap = float(self.IDLE_THRESHOLD) * 0.4   # well below threshold
        idle_s = float(self.IDLE_THRESHOLD) * 4
        self._run_helper(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            for i in 1 2 3 4 5; do
                printf "token $i "
                sleep {short_gap:.3f}
            done
            sleep {idle_s:.2f}
            exit 0
        """))
        log = self._wait_for_notify(wait=5.0)
        notify_lines = [l for l in log.splitlines() if "notify" in l]
        self.assertEqual(len(notify_lines), 1,
            f"Expected one notify after streaming; log={log!r}")

    def test_slow_streaming_gap_fires_during_response(self) -> None:
        """A >THRESHOLD pause mid-response fires the notification too early.

        KNOWN LIMITATION: when the LLM stalls mid-response for longer than
        IDLE_THRESHOLD (slow network, large code block), idle detection
        fires during the response rather than after it.  Because notified
        stays True for the rest of the session turn, the user never gets the
        "done waiting" notification — they get one notification at the wrong time.

        This test documents the current (imperfect) behaviour: exactly one
        notification fires but it fires during the gap between output bursts,
        not after the response completes.
        """
        long_gap = float(self.IDLE_THRESHOLD) * 4   # exceeds threshold → fires
        idle_s = float(self.IDLE_THRESHOLD) * 4
        flag_file = os.path.join(self.tmp, "second_burst_started")
        self._run_helper(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf 'first part of response'
            sleep {long_gap:.2f}        # notification fires here (false positive)
            touch {flag_file}
            printf 'rest of response'
            sleep {idle_s:.2f}          # idle: notified=True → no second fire
            exit 0
        """), timeout=12.0)
        log = self._wait_for_notify(wait=5.0)
        notify_lines = [l for l in log.splitlines() if "notify" in l]
        # Exactly one notification — but fired during the gap, before second_burst_started.
        self.assertEqual(len(notify_lines), 1,
            f"Expected one (premature) notify; log={log!r}")
        # Verify the timing: notification must have fired BEFORE the second burst.
        # The second_burst_started flag is created long after the threshold elapses,
        # so if both files exist, the notification arrived while the gap was ongoing.
        self.assertTrue(
            os.path.exists(flag_file),
            "Child should have completed both bursts",
        )
        notify_mtime = os.path.getmtime(self.notify_log)
        flag_mtime = os.path.getmtime(flag_file)
        self.assertLess(
            notify_mtime, flag_mtime,
            "Notification should have fired during the gap (before second burst started)",
        )

    # -- stdin key classification: Enter vs non-Enter -------------------------

    def test_non_enter_keys_in_stdin_do_not_reset_notification(self) -> None:
        """Arrow keys and other non-Enter keys must NOT reset notified.

        In copilot's select-list TUI the user presses up/down arrows to choose
        an option.  Each arrow key causes copilot to redraw the list (output on
        master_fd) and then go quiet again.  If a non-Enter keystroke reset
        notified, we would re-fire the notification on every arrow-key press.

        Scenario: notification fires during first idle, we send an arrow key,
        child outputs a display update and goes quiet again — assert no second
        notification fires.
        """
        import pty as _pty_mod
        import threading

        idle_s = float(self.IDLE_THRESHOLD) * 4

        child_bin = os.path.join(self.tmp, "fake-copilot-tui")
        _write_exe(child_bin, textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf 'copilot response rendered'
            sleep {idle_s:.2f}              # notification fires here
            printf 'tui redraws after key'  # simulates arrow-key triggered redraw
            sleep {idle_s:.2f}              # should NOT notify again
            exit 0
        """))

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

        # Send an up-arrow escape sequence after the first notification fires.
        # It arrives in the stdin branch; must NOT reset notified.
        def _send_arrow_key() -> None:
            time.sleep(float(self.IDLE_THRESHOLD) * 2)  # after first idle
            try:
                os.write(master_stdin_fd, b"\x1b[A")
            except OSError:
                pass

        t = threading.Thread(target=_send_arrow_key, daemon=True)
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
        self.assertEqual(len(notify_lines), 1,
            f"Arrow key must not reset notified; got {len(notify_lines)} notifies; log={log!r}")

    def test_enter_echo_race_fires_before_response(self) -> None:
        """After Enter, the 2nd notification fires AFTER copilot starts responding.

        When the user presses Enter, the wrapper resets output_since_enter to 0
        and notified to False.  The PTY line discipline immediately echoes \\r\\n
        (~2 bytes) back on master_fd, which re-arms last_output_at.  Without the
        fix this echo alone is enough to make the idle timer fire — the 2nd
        notification would arrive during the startup gap before copilot has
        produced a single real output byte.

        The fix: idle is suppressed until output_since_enter >= _MIN_RESPONSE_OUTPUT
        (5 bytes in this test).  The echo is only ~2 bytes, so idle cannot fire
        until copilot starts streaming real content.

        Timeline
        --------
        t=0          child outputs "first prompt" → idle fires at t≈threshold
        t=6*thr      thread sends Enter; output_since_enter resets to 0
        t=6*thr      PTY echo arrives (~2 bytes); output_since_enter=2 < 5 → no fire
        t=6*thr+gap  child touches child_output_flag, outputs response (>5 bytes)
                     output_since_enter crosses threshold; idle arms
        t=…+thr      2nd notification fires — AFTER the flag was created

        Without the fix the 2nd notification fires at t≈6*thr+threshold (well
        before the flag at t=6*thr+gap).
        """
        import pty as _pty_mod
        import threading

        startup_sleep = float(self.IDLE_THRESHOLD) * 4   # > threshold: clear gap
        response_sleep = float(self.IDLE_THRESHOLD) * 4
        child_output_flag = os.path.join(self.tmp, "child_output_flag")

        child_bin = os.path.join(self.tmp, "fake-copilot-enter-echo")
        _write_exe(child_bin, textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf 'copilot first prompt'
            read -r _input
            sleep {startup_sleep:.2f}
            touch {child_output_flag}
            printf 'copilot response content here'
            sleep {response_sleep:.2f}
            exit 0
        """))

        # Timestamped fake cmux so we can compare notification vs flag times.
        timestamped_cmux = self._make_timestamped_cmux()

        master_stdin_fd, slave_stdin_fd = _pty_mod.openpty()
        env = {
            **os.environ,
            "CMUX_SURFACE_ID": "test-surface",
            "CMUX_COPILOT_IDLE_THRESHOLD": self.IDLE_THRESHOLD,
            # 5 > PTY echo (~2 bytes) but << actual response content (29 bytes)
            "CMUX_COPILOT_MIN_RESPONSE_OUTPUT": "5",
        }

        proc = subprocess.Popen(
            [sys.executable, WRAPPER, "--pty-mode", timestamped_cmux, child_bin],
            stdin=slave_stdin_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        os.close(slave_stdin_fd)

        # Send Enter after the first notification fires (same timing as
        # test_notify_resets_after_new_output).  This is the realistic scenario:
        # the wrapper is running, the child is at its read-r prompt, the first
        # idle already fired, and the user now submits their reply.
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
        self.assertGreaterEqual(
            len(notify_lines), 2,
            f"Expected at least 2 notifications (first prompt + response); log={log!r}",
        )
        self.assertTrue(
            os.path.exists(child_output_flag),
            "child_output_flag not created — child did not reach the response phase",
        )

        # The 2nd notification must fire AFTER the child touched the flag.
        # Without the fix: fires at Enter+threshold (~150ms), flag at Enter+startup_sleep (~600ms).
        # With the fix: fires after startup_sleep + response_sleep + threshold (~1.35s after Enter).
        notify2_ms = int(notify_lines[1].split()[0])
        flag_mtime_ms = int(os.path.getmtime(child_output_flag) * 1000)

        self.assertGreater(
            notify2_ms, flag_mtime_ms,
            f"2nd notification fired before copilot produced any output "
            f"(notify at {notify2_ms}ms, child output at {flag_mtime_ms}ms) — "
            f"Enter-echo race condition not fixed",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
