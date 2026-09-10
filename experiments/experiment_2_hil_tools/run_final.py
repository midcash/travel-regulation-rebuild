"""Final executable entry point for experiment 2."""

from . import run_refined
from .refined_v2 import HilTools

run_refined.HilTools = HilTools


if __name__ == "__main__":
    run_refined.main()
