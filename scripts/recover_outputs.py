#!/usr/bin/env python3
"""Run structural recovery on saved model responses."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cinesubbench.cli import main


if __name__ == "__main__":
    sys.argv.insert(1, "recover")
    main()
