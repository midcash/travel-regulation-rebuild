"""PyCharm entry point. Default: local dashboard using the real DeepSeek provider."""
from pathlib import Path
import os
import sys

from travel_agent.cli import main

if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    if len(sys.argv) == 1:
        sys.argv.extend(["serve", "--provider", "deepseek", "--port", "8766"])
    raise SystemExit(main())
