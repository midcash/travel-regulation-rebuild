"""Compatibility launcher for the Durable MVP; supplier modules remain reusable."""
import os
from pathlib import Path
import subprocess
import sys

from .settings import load_env

if __name__ == "__main__":
    if "--mode=deepseek" in sys.argv or ("--mode" in sys.argv and "deepseek" in sys.argv):
        load_env()
    script = Path(__file__).resolve().parents[1] / "durable_mvp" / "server.mjs"
    raise SystemExit(subprocess.call(["node", str(script), *sys.argv[1:]], env=os.environ))
