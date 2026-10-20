"""Run all credential-free checks; missing runtimes/dependencies fail visibly."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    commands = [
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "verification/pi-step0",
         "-p", "test_portable.py", "-v"],
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "verification/baseline", "-v"],
    ]
    for command in commands:
        print("Running: " + " ".join(command), flush=True)
        result = subprocess.run(command, cwd=ROOT, env=env)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
