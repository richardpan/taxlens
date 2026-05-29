"""Pytest fixtures: load golden returns from YAML so tests stay readable."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from taxlens.models import Return

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "returns"


def _decimalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _decimalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimalize(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return Decimal(str(obj))
    return obj


def load_fixture(name: str) -> tuple[Return, dict[str, Decimal]]:
    """Load a fixture file → (Return, expected_values dict)."""
    path = FIXTURES_DIR / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = _decimalize(raw)
    expected = raw.pop("expected", {})
    return Return(**raw), expected


@pytest.fixture
def fixture():
    return load_fixture
