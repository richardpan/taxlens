"""Enforced reconciliation gate — synthetic PDFs across every
supported tax year must reconcile to <$2 rounding-only deltas.
Locks in the importer + engine round-trip so regressions in
either layer are caught at CI time. No real PDFs are involved.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from taxlens.engine import compute
from taxlens.importers.pdf import import_pdf
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules
from tests.synthetic_pdf import make_1040_pdf


# Supported tax years — keep in sync with the rules YAML directory.
SUPPORTED_YEARS = list(range(2010, 2027))

# Reconciliation tolerance. Synthetic fixtures use whole-dollar inputs
# rendered with thousands separators so the round-trip should be exact;
# we leave a $1 cushion for IRS-style cent-level rounding in the engine.
MAX_DELTA = Decimal("1.00")


@pytest.mark.parametrize("year", SUPPORTED_YEARS)
def test_synthetic_pdf_reconciles_within_tolerance(tmp_path: Path, year: int) -> None:
    """For every supported tax year, build a minimal 1040 PDF, compute
    the expected total tax with the engine, render the PDF with that
    rounded value as the reported total, then re-import it. The delta
    must be within ``MAX_DELTA``.

    This guards two layers at once:
    * the importer's text extraction (any LINE_PATTERNS regression that
      drops a field manifests as a delta > $0).
    * the engine's reproducibility year-over-year (re-running the same
      inputs through the engine must yield the same total tax that was
      used to mint the fixture).
    """
    # Same simple single-filer scenario every year so the test is
    # purely about the round-trip mechanic, not income or rule edge cases.
    base_kwargs = dict(
        tax_year=year,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
    )
    expected = compute(Return(**base_kwargs), load_rules(year))
    reported_rounded = expected.total_tax.quantize(Decimal("1"))

    pdf = tmp_path / f"synth_{year}.pdf"
    make_1040_pdf(
        pdf,
        tax_year=year,
        filing_status_label="Single",
        wages=Decimal("100000"),
        total_tax_reported=reported_rounded,
    )
    imp = import_pdf(pdf)
    res = compute(imp.ret, load_rules(year))

    assert res.reconciliation_delta is not None, (
        f"TY{year}: reported_total_tax was not re-extracted from the synthetic PDF"
    )
    assert abs(res.reconciliation_delta) <= MAX_DELTA, (
        f"TY{year}: |delta| = ${abs(res.reconciliation_delta)} > ${MAX_DELTA}; "
        f"computed=${res.total_tax}, reported=${res.reported_total_tax}"
    )
