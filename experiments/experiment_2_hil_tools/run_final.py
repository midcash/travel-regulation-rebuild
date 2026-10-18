"""Backward-compatible alias for the single experiment 2 runner."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.experiment_2_hil_tools.run import main


if __name__ == "__main__":
    main()
