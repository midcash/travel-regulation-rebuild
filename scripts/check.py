"""Run all credential-free checks; missing runtimes/dependencies fail visibly."""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Repo-relative path prefixes that README may reference and that must exist on
# disk. A reference under one of these prefixes is a claim about the repository;
# if the file is gone, the docs have drifted and the build must fail. Other
# tokens (http URLs, C:/ paths, .venv/ commands, module names like `travel_agent`)
# are deliberately ignored to avoid false positives.
REFERENCE_PREFIXES = ("docs/", "durable_mvp/", "travel_agent/", "verification/")

# Files that must exist because they are the single maintained Agent run path.
REQUIRED_RUNTIME_FILES = ("durable_mvp/server.mjs", "durable_mvp/calendar.mjs")


def _fail(problems):
    for problem in problems:
        print("[docs-check] FAIL: " + problem, flush=True)
    return len(problems)


def _read_text(path):
    # utf-8-sig drops a leading BOM if present so a stray BOM cannot silently
    # defeat line-prefix checks (e.g. startswith("import") in server.mjs).
    return path.read_text(encoding="utf-8-sig")


def _strip_anchor(target):
    # Drop a trailing "#anchor" or "?query" so we test the file path only.
    return re.split(r"[#?]", target, maxsplit=1)[0]


def _is_repo_reference(token):
    if not token:
        return False
    if token.startswith(("http://", "https://", "mailto:")):
        return False
    # Windows drive paths (C:/...) or backslash paths are commands, not repo files.
    if re.match(r"^[A-Za-z]:", token) or "\\" in token:
        return False
    return token.startswith(REFERENCE_PREFIXES)


def _readme_references(text):
    """Yield repo-relative path tokens referenced by README.

    Sources: Markdown link targets ](target) and inline code spans `token`.
    Each token is stripped of anchors/queries and kept only if it falls under a
    known repo prefix, so command lines and anchors never cause false failures.

    Fenced code blocks (``` ... ```) are removed first: unmatched backticks
    inside a fence would otherwise desynchronise inline-code pairing and cause
    real references after the fence to be silently skipped (missed detection).
    """
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    seen = []
    for match in re.finditer(r"\]\(([^)\s]+)\)", text):
        seen.append(match.group(1))
    for match in re.finditer(r"`([^`]+)`", text):
        seen.append(match.group(1))
    for raw in seen:
        token = _strip_anchor(raw).strip()
        if _is_repo_reference(token):
            yield token


def _import_specifiers(text):
    """Yield the module specifier of each *static* import in `text`.

    Handles multi-line static imports:
        import x from './a.mjs'
        import {
            a,
            b,
        } from '../travel_agent/x.mjs'
        import './side-effect.mjs'
    Dynamic import() is a call expression, not a static declaration, so it is
    intentionally NOT treated as an import constraint here.
    """
    # Side-effect / bare: import '...'
    for match in re.finditer(r"(?m)^\s*import\s+(['\"])([^'\"]+)\1", text):
        yield match.group(2)
    # import ... from '...'; the part between "import" and "from" may span lines
    # and contain braces / identifiers, but no quotes (specifier is the quoted tail).
    for match in re.finditer(r"(?ms)^\s*import\s+[^'\"]*?\bfrom\s+(['\"])([^'\"]+)\1", text):
        yield match.group(2)


def _specifier_is_travel_agent(specifier):
    # A specifier points at travel_agent if it resolves to that package/dir:
    # './travel_agent', '../travel_agent/leak.mjs', 'travel_agent', 'travel_agent.x'.
    return re.match(r"^(?:\.{1,2}/)*travel_agent(?:\.|/|$)", specifier) is not None


def _server_imports_travel_agent(text):
    """Return offending import specifiers where server.mjs imports travel_agent.

    Matches the import's module *specifier* (not any substring on the line), so
    a line that merely mentions `travel_agent` in a comment/string is not a false
    positive, and multi-line static imports are still caught.
    """
    return [spec for spec in _import_specifiers(text) if _specifier_is_travel_agent(spec)]


def _find_problems(root):
    """Return the list of documentation/repository drift problems for root."""
    root = Path(root)
    problems = []

    # 1) The maintained Agent run path must exist.
    for rel in REQUIRED_RUNTIME_FILES:
        if not (root / rel).is_file():
            problems.append(f"required runtime file missing: {rel}")

    # 2) Paths referenced by README must exist on disk.
    readme = root / "README.md"
    if not readme.is_file():
        problems.append("README.md missing")
    else:
        checked = set()
        for token in _readme_references(_read_text(readme)):
            if token in checked:
                continue
            checked.add(token)
            if not (root / token).exists():
                problems.append(f"README references missing path: {token}")

    # 3) travel_agent must stay frozen: server.mjs must not import it.
    server = root / "durable_mvp/server.mjs"
    if server.is_file():
        for offender in _server_imports_travel_agent(_read_text(server)):
            problems.append(
                "durable_mvp/server.mjs must not import travel_agent "
                "(travel assets are frozen / not wired in): " + offender
            )

    return problems


def check_docs(root=ROOT):
    """Assert documentation claims match the repository. Returns failure count.

    root defaults to the repo root; passing a different root lets tests feed a
    synthetic tree without touching the real files.
    """
    return _fail(_find_problems(root))


def main():
    doc_problems = check_docs()
    if doc_problems:
        print(f"[docs-check] {doc_problems} documentation/repository drift error(s)", flush=True)
        return 1

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    commands = [
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "verification/durable-mvp", "-v"],
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "verification/durable-step0", "-v"],
    ]
    for command in commands:
        print("Running: " + " ".join(command), flush=True)
        result = subprocess.run(command, cwd=ROOT, env=env)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
