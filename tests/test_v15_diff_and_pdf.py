"""v0.15.0 — Diff service tests + historical-year PDF round-trips."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from taxlens import compute
from taxlens.importers.pdf import import_pdf
from taxlens.models import FilingStatus, Return
from taxlens.service import TaxLensService

from tests.realistic_1040 import Realistic1040, make_realistic_1040


@pytest.fixture
def svc(tmp_path: Path) -> TaxLensService:
    return TaxLensService.open(tmp_path / "test.db")


def _store(svc: TaxLensService, ret: Return) -> int:
    """Insert a Return directly through the service db plumbing."""
    import json
    from taxlens.db import ComputationCache, StoredReturn, dumps
    result = compute(ret)
    with svc.sessionmaker_() as s:
        row = StoredReturn(
            tax_year=ret.tax_year,
            filing_status=ret.filing_status.value,
            source="manual",
            source_hash=f"test-{id(ret)}",
            return_json=dumps(ret.model_dump(mode="json")),
        )
        row.cache = ComputationCache(result_json=dumps(result.model_dump(mode="json")))
        s.add(row)
        s.commit()
        return row.id


def test_diff_attributes_wage_increase_to_wages_driver(svc: TaxLensService):
    """Pure income increase should attribute almost entirely to the wages driver."""
    l = _store(svc, Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                            wages=Decimal(80000)))
    r = _store(svc, Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                            wages=Decimal(120000)))
    out = svc.diff_returns(l, r)
    assert out is not None
    overall = Decimal(out["overall_tax_delta"])
    assert overall > 0  # more wages → more tax
    # Top driver should be wages.
    wage_driver = next(d for d in out["drivers"] if d["field"] == "wages")
    assert Decimal(wage_driver["attributed_tax"]) == pytest.approx(overall, abs=Decimal(1))


def test_diff_rule_change_attribution_2017_to_2018(svc: TaxLensService):
    """Same inputs, only the tax year changes (TCJA boundary). The big delta
    should be attributed to the 'Rule changes' driver."""
    common = dict(filing_status=FilingStatus.MFJ, wages=Decimal(150000),
                   qualifying_children=1)
    l = _store(svc, Return(tax_year=2017, **common))
    r = _store(svc, Return(tax_year=2018, **common))
    out = svc.diff_returns(l, r)
    assert out is not None
    overall = Decimal(out["overall_tax_delta"])
    # TCJA cut taxes substantially at this income.
    assert overall < Decimal(-3000)
    rule_driver = next(d for d in out["drivers"] if d["field"] == "_rules")
    # The bulk of the move is rule-driven (kid count unchanged, wages unchanged).
    assert Decimal(rule_driver["attributed_tax"]) < Decimal(-3000)


def test_diff_payments_swap_changes_refund_not_total_tax(svc: TaxLensService):
    """Withholding/estimated payments don't change total_tax but they swing refund."""
    l = _store(svc, Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                            wages=Decimal(100000), federal_withholding=Decimal(10000)))
    r = _store(svc, Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                            wages=Decimal(100000), federal_withholding=Decimal(15000)))
    out = svc.diff_returns(l, r)
    assert out is not None
    # total_tax shouldn't move
    assert Decimal(out["overall_tax_delta"]) == Decimal("0.00")
    wh = next(d for d in out["drivers"] if d["field"] == "federal_withholding")
    # Attribution for payment fields uses refund delta sign-flipped (more
    # withholding = positive refund movement = negative attributed_tax)
    assert Decimal(wh["attributed_tax"]) < 0


def test_diff_returns_none_when_id_missing(svc: TaxLensService):
    assert svc.diff_returns(999, 1000) is None


def test_realistic_pdf_round_trip_2018_first_tcja(tmp_path: Path):
    """First post-TCJA PDF: verify importer + engine produce sensible numbers
    for TY2018's lower brackets and doubled SD."""
    pdf = tmp_path / "ty2018_mfj.pdf"
    fixture = Realistic1040(
        tax_year=2018, filing_status_label="Married filing jointly",
        wages=Decimal("150000"),
        interest=Decimal("500"),
        qual_div=Decimal("3000"),
        ord_div=Decimal("3000"),
        withholding=Decimal("20000"),
        total_tax_reported=Decimal("23000.00"),
        qualifying_children=2,
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    assert imp.ret.tax_year == 2018
    assert imp.ret.wages == Decimal("150000")
    res = compute(imp.ret)
    # 2018 MFJ at $150k+$6k investment, std $24k → TI $132,500
    # tax = 1905 + (77400−19050)*.12 + (132500−77400)*.22 = 1905 + 7002 + 12122 = $21,029
    # Less qual_div lift (3000 in 0% LTCG bracket) → roughly similar
    # less $4,000 CTC = ~$17k
    assert Decimal(15000) < res.total_tax < Decimal(20000)


def test_realistic_pdf_round_trip_2017_pre_tcja(tmp_path: Path):
    """Pre-TCJA PDF: verify importer + engine pickup personal exemption."""
    pdf = tmp_path / "ty2017_single.pdf"
    fixture = Realistic1040(
        tax_year=2017, filing_status_label="Single",
        wages=Decimal("60000"),
        withholding=Decimal("8000"),
        total_tax_reported=Decimal("8500.00"),
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    assert imp.ret.tax_year == 2017
    assert imp.ret.wages == Decimal("60000")
    res = compute(imp.ret)
    # Verify pre-TCJA personal exemption activated.
    assert res.personal_exemption_used == Decimal("4050.00")


def test_realistic_pdf_round_trip_2021_arpa_ctc(tmp_path: Path):
    """ARPA-year PDF with kids: verify fully-refundable CTC flows through."""
    pdf = tmp_path / "ty2021_arpa.pdf"
    fixture = Realistic1040(
        tax_year=2021, filing_status_label="Head of household",
        wages=Decimal("35000"),
        withholding=Decimal("1500"),
        total_tax_reported=Decimal("0.00"),
        qualifying_children=2,
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    assert imp.ret.tax_year == 2021
    assert imp.ret.qualifying_children == 2
    res = compute(imp.ret)
    # ARPA: $3k × 2 = $6,000 total CTC, fully refundable. Some used to zero out
    # the ~$1,660 tax owed, rest flows refundable. Refund should be substantial.
    assert res.actc + res.credits >= Decimal("5500")  # CTC total reaches $6k
    assert res.refund_or_owed > Decimal("5000")
