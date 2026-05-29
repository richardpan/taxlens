"""TXF round-trip: write minimal TXF → parse → engine → reconciliation."""
from decimal import Decimal

import pytest

from taxlens import compute
from taxlens.importers.txf import import_txf


TXF_SAMPLE = """V042
A TaxLens test
D 02/01/2025
^
TD
N997
$2024
^
TD
N999
$2
^
TD
N998
$2
^
TD
N260
$240000.00
^
TD
N286
$6000.00
^
TD
N521
$40000.00
^
TD
N996
$34117.00
^
"""


def test_txf_basic(tmp_path):
    p = tmp_path / "sample.txf"
    p.write_text(TXF_SAMPLE, encoding="utf-8")
    imp = import_txf(p)
    assert imp.ret.tax_year == 2024
    assert imp.ret.filing_status.value == "mfj"
    assert imp.ret.qualifying_children == 2
    assert imp.ret.wages == Decimal(240000)
    assert imp.ret.federal_withholding == Decimal(40000)
    result = compute(imp.ret)
    assert result.reconciled(tolerance=Decimal("1.00"))


def test_txf_missing_year(tmp_path):
    p = tmp_path / "bad.txf"
    p.write_text("V042\nA test\n^\nTD\nN999\n$1\n^\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tax year"):
        import_txf(p)
