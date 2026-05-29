"""Manual importer: load a Return directly from a JSON/YAML file.

Used for hand-entered returns, regression fixtures, and as the lowest-friction
escape hatch when the PDF extractor can't read a return.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from taxlens.importers import Imported, sha256_file
from taxlens.models import Return


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


def import_manual(path: Path) -> Imported:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        raw = yaml.safe_load(text)
    if isinstance(raw, dict):
        raw.pop("expected", None)
    ret = Return(**_decimalize(raw))
    return Imported(
        ret=ret,
        source="manual",
        source_hash=sha256_file(path),
        source_filename=path.name,
        warnings=[],
    )
