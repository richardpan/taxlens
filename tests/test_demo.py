"""Tests for the demo loader."""
from __future__ import annotations

from taxlens.demo import demo_files
from taxlens.engine import compute
from taxlens.importers.manual import import_manual


def test_demo_files_present_and_compute_ok():
    files = demo_files()
    assert len(files) >= 2, "expected at least two bundled demo returns"
    for path in files:
        imported = import_manual(path)
        result = compute(imported.ret)
        assert result.total_tax > 0, f"{path.name}: expected positive total_tax"
