"""Renderer race unit checks; actual browser acceptance follows implementation."""
from pathlib import Path
import subprocess
import unittest


class WebSynchronization(unittest.TestCase):
    def check_case(self, name):
        result = subprocess.run(["node", str(Path(__file__).with_name("web_sync_checks.mjs")), name],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_A4_older_sse_revision_does_not_roll_back_page(self):
        self.check_case("stale-sse")

    def test_A4_delayed_initial_fetch_does_not_overwrite_newer_sse(self):
        self.check_case("late-fetch")

    def test_A4_retired_session_event_does_not_undo_explicit_reset(self):
        self.check_case("retired-session")
