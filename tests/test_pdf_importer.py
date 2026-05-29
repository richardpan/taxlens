"""End-to-end PDF round-trip: generate synthetic 1040 → extract → compute → reconcile."""
from decimal import Decimal
from pathlib import Path

import pytest

from taxlens import compute
from taxlens.importers.pdf import import_pdf
from tests.synthetic_pdf import make_1040_pdf


def test_pdf_basic_round_trip(tmp_path: Path):
    """An MFJ 2024 return with wages + interest should extract cleanly."""
    pdf = tmp_path / "ty2024_basic.pdf"
    # Expected total tax computed from the basic fixture: 34,117.00
    make_1040_pdf(
        pdf,
        tax_year=2024,
        filing_status_label="Married filing jointly",
        wages=Decimal(240000),
        interest=Decimal(6000),
        withholding=Decimal(40000),
        total_tax_reported=Decimal("34117.00"),
        qualifying_children=2,
    )
    imp = import_pdf(pdf)
    assert imp.ret.tax_year == 2024
    assert imp.ret.filing_status.value == "mfj"
    assert imp.ret.wages == Decimal(240000)
    assert imp.ret.interest_income == Decimal(6000)
    assert imp.ret.qualifying_children == 2
    assert imp.ret.reported_total_tax == Decimal("34117.00")

    result = compute(imp.ret)
    assert result.total_tax == Decimal("34117.00")
    assert result.reconciled(tolerance=Decimal("1.00"))


def test_pdf_with_qualified_income(tmp_path: Path):
    pdf = tmp_path / "ty2024_qual.pdf"
    make_1040_pdf(
        pdf,
        tax_year=2024,
        filing_status_label="Married filing jointly",
        wages=Decimal(240000),
        interest=Decimal(6000),
        qual_div=Decimal(18000),
        ord_div=Decimal(18000),
        long_term_capital_gains=Decimal(14000),
        withholding=Decimal(45000),
        total_tax_reported=Decimal("43981.00"),
    )
    imp = import_pdf(pdf)
    assert imp.ret.qualified_dividends == Decimal(18000)
    assert imp.ret.long_term_capital_gains == Decimal(14000)
    result = compute(imp.ret)
    assert result.reconciled(tolerance=Decimal("1.00"))


def test_pdf_unknown_year_raises(tmp_path: Path):
    p = tmp_path / "blank.pdf"
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import LETTER
    c = canvas.Canvas(str(p), pagesize=LETTER)
    c.drawString(50, 700, "some unrelated text")
    c.showPage(); c.save()
    with pytest.raises(ValueError, match="tax year"):
        import_pdf(p)
