"""Run all credential-free checks; missing runtimes/dependencies fail visibly."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "baseline", "recovery"), default="all")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    baseline = [
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "verification/pi-step0",
         "-p", "test_portable.py", "-v"],
        [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "verification/baseline", "-v"],
    ]
    recovery = [[sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover",
                 "-s", "verification/session-recovery", "-v"]]
    commands = {"all": baseline + recovery, "baseline": baseline, "recovery": recovery}[args.suite]
    for command in commands:
        print("Running: " + " ".join(command), flush=True)
        result = subprocess.run(command, cwd=ROOT, env=env)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
