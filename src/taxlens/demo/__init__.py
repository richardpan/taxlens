"""Bundled demo returns. Used by `POST /api/demo/load`."""
from __future__ import annotations
from pathlib import Path

DEMO_DIR = Path(__file__).parent


def demo_files() -> list[Path]:
    return sorted(DEMO_DIR.glob("*.yaml"))
