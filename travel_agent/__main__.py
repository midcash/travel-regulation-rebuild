"""Compatibility launcher for the Durable MVP; supplier modules remain reusable."""
import os
from pathlib import Path
import subprocess
import sys

from .settings import load_env


def wants_env(argv):
    """Return True when the effective --mode value selects the real-model host."""
    mode = "offline"
    for index, argument in enumerate(argv):
        if argument == "--mode" and index + 1 < len(argv):
            mode = argv[index + 1]
        elif argument.startswith("--mode="):
            mode = argument.split("=", 1)[1]
    return mode == "deepseek"


if __name__ == "__main__":
    if wants_env(sys.argv[1:]):
        load_env()
    script = Path(__file__).resolve().parents[1] / "durable_mvp" / "server.mjs"
    raise SystemExit(subprocess.call(["node", str(script), *sys.argv[1:]], env=os.environ))
