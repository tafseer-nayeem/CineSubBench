#!/usr/bin/env python3
"""Calculate the composite leaderboard from evaluation results."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cinesubbench.cli import main


if __name__ == "__main__":
    sys.argv.insert(1, "leaderboard")
    main()
