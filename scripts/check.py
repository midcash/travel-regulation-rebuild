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
    """
    seen = []
    for match in re.finditer(r"\]\(([^)\s]+)\)", text):
        seen.append(match.group(1))
    for match in re.finditer(r"`([^`]+)`", text):
        seen.append(match.group(1))
    for raw in seen:
        token = _strip_anchor(raw).strip()
        if _is_repo_reference(token):
            yield token


def _server_imports_travel_agent(text):
    """Return offending import lines where server.mjs imports travel_agent.

    Kept as a pure function over text so it can be exercised on sample strings
    without touching the real file.
    """
    offenders = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("import") and "travel_agent" in stripped:
            offenders.append(stripped)
    return offenders


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
