#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Standalone executable launcher for Taildrop to Mac."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from taildrop_gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
