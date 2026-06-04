"""Schedule 2 Part II passthrough — closes reconciliation gaps for
"other taxes" the engine recognizes structurally but whose underlying
inputs aren't auto-extracted by the importer (e.g. excess Roth IRA
contribution excise, household employment taxes, recapture of credits).

The engine adds max(0, schedule_2_other_taxes_reported − engine-modeled
Part II) to total_tax as ``unmodeled_other_taxes``. Part I items
(AMT, APTC repayment) flow through 1040 line 17 instead and are not
included in the residual calculation.
"""

from decimal import Decimal

from taxlens.engine import compute
from taxlens.importers.pdf import _extract_fields
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def test_passthrough_adds_residual_when_reported_exceeds_engine():
    ret = Return(
        tax_year=2020,
        filing_status=FilingStatus.MFJ,
        wages=Decimal("176865"),
        interest_income=Decimal("481"),
        ordinary_dividends=Decimal("2223"),
        qualified_dividends=Decimal("1374"),
        long_term_capital_gains=Decimal("16383"),
        short_term_capital_gains=Decimal("20637"),
        unemployment_compensation=Decimal("26188"),
        qualified_reit_ptp_dividends=Decimal("778"),
        charitable_contributions_non_itemizer=Decimal("220"),
        schedule_2_other_taxes_reported=Decimal("720"),
    )
    res = compute(ret, load_rules(2020))
    assert res.unmodeled_other_taxes == Decimal("720.00")


def test_passthrough_zero_when_field_unset():
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.MFJ,
        wages=Decimal("100000"),
    )
    res = compute(ret, load_rules(2024))
    assert res.unmodeled_other_taxes == Decimal(0)


def test_passthrough_no_residual_when_reported_matches_engine():
    # Engine models SE tax fully — if the user reports exactly the
    # engine-computed Part II total, no residual passthrough.
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        schedule_2_other_taxes_reported=Decimal(0),
    )
    res = compute(ret, load_rules(2024))
    assert res.unmodeled_other_taxes == Decimal(0)


def test_passthrough_never_negative():
    # If engine over-models (rare but possible during dev), the
    # passthrough must clamp to zero — never subtract from total_tax.
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        schedule_2_other_taxes_reported=Decimal(-1000),
    )
    res = compute(ret, load_rules(2024))
    assert res.unmodeled_other_taxes == Decimal(0)


def test_extract_line_23_other_taxes_2020_phrasing():
    text = """\
1040 (2020)
23 Other taxes, including self-employment tax, from Schedule 2, line 10 . . . . . . . . . . . . . . . . 23 720
24 Add lines 22 and 23. This is your total tax . . . . . . . . . . . . . . . . . . . . . . . . . . . . 24 39,506
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("schedule_2_other_taxes_reported") == Decimal("720")


def test_extract_line_23_other_taxes_post_2021_phrasing():
    # Post-TY2021 the cross-reference points at Schedule 2 line 21.
    text = """\
1040 (2024)
23 Other taxes, including self-employment tax, from Schedule 2, line 21 . . . . . . . . . . . . . . . 23 1,234
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("schedule_2_other_taxes_reported") == Decimal("1234")
