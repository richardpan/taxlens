"""Field-source provenance — the PDF importer tags each extracted
field with the stream it came from so the UI can flag layout-only
extractions for manual review.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from taxlens.importers.pdf import import_pdf
from tests.synthetic_pdf import make_1040_pdf


def test_pdf_import_populates_field_sources(tmp_path: Path) -> None:
    pdf = tmp_path / "p.pdf"
    make_1040_pdf(
        pdf,
        tax_year=2024,
        filing_status_label="Single",
        wages=Decimal("75000"),
        interest=Decimal("250"),
        total_tax_reported=Decimal("8500"),
    )
    imp = import_pdf(pdf)
    assert imp.field_sources is not None
    # Every field stored on the Return that was actually extracted from
    # the PDF should have a provenance entry.
    assert "wages" in imp.field_sources
    assert "interest_income" in imp.field_sources
    # Sources are one of the known tags.
    valid = {"acroform", "default", "layout", "merged", "zero-backfill"}
    for fname, src in imp.field_sources.items():
        assert src in valid, f"unexpected source {src!r} for {fname}"


def test_field_sources_does_not_contain_internal_helpers(tmp_path: Path) -> None:
    """Internal extraction helpers (e.g. ``total_tax_reported``,
    ``_form1040_line8_total``) get popped from ``fields`` before the
    Return is built; provenance must be popped along with them so the
    UI doesn't display sources for fields that don't exist on the
    final Return."""
    pdf = tmp_path / "p.pdf"
    make_1040_pdf(
        pdf,
        tax_year=2024,
        filing_status_label="Single",
        wages=Decimal("100000"),
        total_tax_reported=Decimal("13841"),
    )
    imp = import_pdf(pdf)
    assert imp.field_sources is not None
    assert "total_tax_reported" not in imp.field_sources
    assert "_form1040_line8_total" not in imp.field_sources
