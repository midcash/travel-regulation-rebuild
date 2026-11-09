"""White-box unit tests for scripts/check.py documentation/repository assertions.

Uses temporary directories only; never touches the real README.md or
durable_mvp/server.mjs. Exercises the exact cases that were previously missed
(fenced code blocks, multi-line static imports) plus false-positive guards.
"""
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check  # noqa: E402


class DocsCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _problems(self):
        return check._find_problems(self.root)

    # --- assertion 1: required runtime files -------------------------------
    def test_missing_runtime_file_is_reported(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        # calendar.mjs intentionally absent
        problems = self._problems()
        self.assertTrue(any("calendar.mjs" in p for p in problems), problems)

    def test_all_runtime_files_present_is_green(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self.assertEqual(self._problems(), [])

    # --- assertion 2: README references exist ------------------------------
    def test_missing_readme_reference_is_reported(self):
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write("README.md", "see `docs/ghost.md`\n")
        problems = self._problems()
        self.assertTrue(any("docs/ghost.md" in p for p in problems), problems)

    def test_valid_readme_reference_is_green(self):
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write("docs/product-behavior.md", "# pb\n")
        self._write("README.md", "see [x](docs/product-behavior.md)\n")
        self.assertEqual(self._problems(), [])

    def test_reference_with_anchor_and_query_is_stripped(self):
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write("docs/product-behavior.md", "# pb\n")
        # Anchor / query must be stripped, leaving an existing path -> green.
        self._write(
            "README.md",
            "see [a](docs/product-behavior.md#S01) and [b](docs/product-behavior.md?x=1)\n",
        )
        self.assertEqual(self._problems(), [])

    def test_anchor_to_missing_file_still_reported(self):
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write("README.md", "see [a](docs/ghost.md#nope)\n")
        problems = self._problems()
        self.assertTrue(any("docs/ghost.md" in p for p in problems), problems)

    # --- assertion 2 / cubic #3: fenced code blocks -----------------------
    def test_reference_after_fenced_block_is_still_detected(self):
        # A fenced block with odd backticks used to desynchronise inline-code
        # pairing and hide the real reference that follows it. Must be caught.
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write(
            "README.md",
            "before\n```\ncode with ` stray backtick\n```\nsee `docs/ghost.md` here\n",
        )
        problems = self._problems()
        self.assertTrue(any("docs/ghost.md" in p for p in problems), problems)

    def test_fenced_block_contents_are_not_treated_as_references(self):
        # Paths mentioned only inside a fenced block must NOT be checked.
        self._write("durable_mvp/server.mjs", "import './calendar.mjs'\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write(
            "README.md",
            "```\nsee docs/does-not-exist.md\n```\n# clean body\n",
        )
        self.assertEqual(self._problems(), [])

    # --- assertion 3 / cubic #1: travel_agent import detection ------------
    def test_multiline_static_import_is_detected(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write(
            "durable_mvp/server.mjs",
            "import {\n  a,\n  b,\n} from '../travel_agent/leak.mjs';\n",
        )
        problems = self._problems()
        self.assertTrue(any("travel_agent" in p for p in problems), problems)

    def test_specifier_mention_in_comment_is_not_false_positive(self):
        # Merely mentioning travel_agent in a comment/string must not trip.
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write(
            "durable_mvp/server.mjs",
            "// do not import travel_agent here\n"
            "const note = 'travel_agent is frozen';\n"
            "import './calendar.mjs';\n",
        )
        self.assertEqual(self._problems(), [])

    def test_dynamic_import_is_not_flagged(self):
        # import() is a call, not a static declaration -> not our constraint.
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write(
            "durable_mvp/server.mjs",
            "const m = await import('./travel_agent/leak.mjs');\n",
        )
        self.assertEqual(self._problems(), [])

    def test_normal_relative_import_not_flagged(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.mjs", "export {}\n")
        self._write(
            "durable_mvp/server.mjs",
            "import {Calendar} from './calendar.mjs';\n",
        )
        self.assertEqual(self._problems(), [])

    # --- pure-function level checks (no filesystem) -----------------------
    def test_specifier_matcher(self):
        self.assertTrue(check._specifier_is_travel_agent("travel_agent"))
        self.assertTrue(check._specifier_is_travel_agent("./travel_agent"))
        self.assertTrue(check._specifier_is_travel_agent("../travel_agent/leak.mjs"))
        self.assertTrue(check._specifier_is_travel_agent("travel_agent.foo"))
        self.assertFalse(check._specifier_is_travel_agent("./calendar.mjs"))
        self.assertFalse(check._specifier_is_travel_agent("my_travel_agent"))
        self.assertFalse(check._specifier_is_travel_agent("travel_agent_utils"))


if __name__ == "__main__":
    unittest.main()
