"""White-box unit tests for scripts/check.py documentation/repository assertions.

Uses temporary directories only; never touches the real README.md or
durable_mvp/server.ts. Exercises the exact cases that were previously missed
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
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        # calendar.ts intentionally absent
        problems = self._problems()
        self.assertTrue(any("calendar.ts" in p for p in problems), problems)

    def test_all_runtime_files_present_is_green(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self.assertEqual(self._problems(), [])

    # --- assertion 2: README references exist ------------------------------
    def test_missing_readme_reference_is_reported(self):
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write("README.md", "see `docs/ghost.md`\n")
        problems = self._problems()
        self.assertTrue(any("docs/ghost.md" in p for p in problems), problems)

    def test_valid_readme_reference_is_green(self):
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write("docs/product-behavior.md", "# pb\n")
        self._write("README.md", "see [x](docs/product-behavior.md)\n")
        self.assertEqual(self._problems(), [])

    def test_reference_with_anchor_and_query_is_stripped(self):
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write("docs/product-behavior.md", "# pb\n")
        # Anchor / query must be stripped, leaving an existing path -> green.
        self._write(
            "README.md",
            "see [a](docs/product-behavior.md#S01) and [b](docs/product-behavior.md?x=1)\n",
        )
        self.assertEqual(self._problems(), [])

    def test_anchor_to_missing_file_still_reported(self):
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write("README.md", "see [a](docs/ghost.md#nope)\n")
        problems = self._problems()
        self.assertTrue(any("docs/ghost.md" in p for p in problems), problems)

    # --- assertion 2 / cubic #3: fenced code blocks -----------------------
    def test_reference_after_fenced_block_is_still_detected(self):
        # A fenced block with odd backticks used to desynchronise inline-code
        # pairing and hide the real reference that follows it. Must be caught.
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "README.md",
            "before\n```\ncode with ` stray backtick\n```\nsee `docs/ghost.md` here\n",
        )
        problems = self._problems()
        self.assertTrue(any("docs/ghost.md" in p for p in problems), problems)

    def test_fenced_block_contents_are_not_treated_as_references(self):
        # Paths mentioned only inside a fenced block must NOT be checked.
        self._write("durable_mvp/server.ts", "import './calendar.ts'\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "README.md",
            "```\nsee docs/does-not-exist.md\n```\n# clean body\n",
        )
        self.assertEqual(self._problems(), [])

    # --- assertion 3 / cubic #1: travel_agent import detection ------------
    def test_multiline_static_import_is_detected(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "import {\n  a,\n  b,\n} from '../travel_agent/leak.mjs';\n",
        )
        problems = self._problems()
        self.assertTrue(any("travel_agent" in p for p in problems), problems)

    def test_specifier_mention_in_comment_is_not_false_positive(self):
        # Merely mentioning travel_agent in a comment/string must not trip.
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "// do not import travel_agent here\n"
            "const note = 'travel_agent is frozen';\n"
            "import './calendar.ts';\n",
        )
        self.assertEqual(self._problems(), [])

    def test_dynamic_import_is_not_flagged(self):
        # import() is a call, not a static declaration -> not our constraint.
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "const m = await import('./travel_agent/leak.mjs');\n",
        )
        self.assertEqual(self._problems(), [])

    def test_normal_relative_import_not_flagged(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "import {Calendar} from './calendar.ts';\n",
        )
        self.assertEqual(self._problems(), [])

    # --- pure-function level checks (no filesystem) -----------------------
    def test_specifier_matcher(self):
        self.assertTrue(check._specifier_is_travel_agent("travel_agent"))
        self.assertTrue(check._specifier_is_travel_agent("./travel_agent"))
        self.assertTrue(check._specifier_is_travel_agent("../travel_agent/leak.mjs"))
        self.assertTrue(check._specifier_is_travel_agent("travel_agent.foo"))
        self.assertFalse(check._specifier_is_travel_agent("./calendar.ts"))
        self.assertFalse(check._specifier_is_travel_agent("my_travel_agent"))
        self.assertFalse(check._specifier_is_travel_agent("travel_agent_utils"))

    # --- cubic K13M: comments / template literals must not cause false ----
    def test_block_comment_import_is_not_false_positive(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "/* import x from './travel_agent/leak.mjs' */\n"
            "import './calendar.ts';\n",
        )
        self.assertEqual(self._problems(), [])

    def test_template_literal_import_is_not_false_positive(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "const s = `\nimport y from '../travel_agent/leak.mjs'\n`;\n"
            "import './calendar.ts';\n",
        )
        self.assertEqual(self._problems(), [])

    def test_line_comment_import_is_not_false_positive(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "// import z from './travel_agent/leak.mjs'\nimport './calendar.ts';\n",
        )
        self.assertEqual(self._problems(), [])

    # Reverse case (boundary officer): stripping comments must NOT hide a real
    # import that sits right next to / after a comment.
    def test_real_import_after_comment_is_still_detected(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "/* a leading block comment */\n"
            "import x from '../travel_agent/leak.mjs'; // trailing comment\n",
        )
        problems = self._problems()
        self.assertTrue(any("travel_agent" in p for p in problems), problems)

    # --- cubic K13S: export ... from is a static dependency --------------
    def test_export_from_travel_agent_is_detected(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "export {x} from './travel_agent/y.mjs';\n",
        )
        problems = self._problems()
        self.assertTrue(any("travel_agent" in p for p in problems), problems)

    # Reverse case (boundary officer): `export const travel_agent = 1` is a
    # declaration, not an export-from; it must NOT be flagged.
    def test_export_const_named_travel_agent_is_not_flagged(self):
        self._write("README.md", "# fake\n")
        self._write("durable_mvp/calendar.ts", "export {}\n")
        self._write(
            "durable_mvp/server.ts",
            "export const travel_agent = 1;\nimport './calendar.ts';\n",
        )
        self.assertEqual(self._problems(), [])


if __name__ == "__main__":
    unittest.main()
