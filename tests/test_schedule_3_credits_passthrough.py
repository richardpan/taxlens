"""Schedule 3 line 8 passthrough — closes reconciliation gaps for
nonrefundable credits the engine recognizes structurally but whose
underlying detail isn't auto-extracted by the importer (e.g. §25C
Energy Efficient Home Improvement Credit / Form 5695 Section B,
adoption credit, mortgage-interest credit, alternative fuel-vehicle
refueling property credit).

The engine adds max(0, schedule_3_line_8_reported − engine-modeled
Sch 3 line 8) to credits as ``unmodeled_sch3_credits``. CTC + ODC
are on 1040 line 19 (not Sch 3) and are excluded.
"""

from decimal import Decimal

from taxlens.engine import compute
from taxlens.importers.pdf import _extract_fields
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def test_passthrough_adds_residual_when_reported_exceeds_engine():
    # Engine models $0 of Sch 3 line 8 nonref credits here; reporter
    # claims $2,000 (e.g. unmodeled §25C Energy Efficient Home
    # Improvement Credit). Residual flows through as a credit.
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        schedule_3_line_8_reported=Decimal("2000"),
    )
    res = compute(ret, load_rules(2024))
    assert res.unmodeled_sch3_credits == Decimal("2000.00")


def test_passthrough_zero_when_field_unset():
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
    )
    res = compute(ret, load_rules(2024))
    assert res.unmodeled_sch3_credits == Decimal(0)


def test_passthrough_no_residual_when_reported_matches_engine():
    # Foreign tax credit fully accounts for Sch 3 line 8 — foreign
    # source income is required for §904(a) limit to allow the credit.
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        interest_income=Decimal("1000"),
        foreign_source_income=Decimal("1000"),
        foreign_taxes_paid=Decimal("100"),
        schedule_3_line_8_reported=Decimal("100"),
    )
    res = compute(ret, load_rules(2024))
    assert res.unmodeled_sch3_credits == Decimal(0)


def test_passthrough_never_negative_when_engine_exceeds_reported():
    # Engine models more credit than reported — never inflate or
    # subtract from credits.
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        interest_income=Decimal("5000"),
        foreign_source_income=Decimal("5000"),
        foreign_taxes_paid=Decimal("500"),
        schedule_3_line_8_reported=Decimal("100"),
    )
    res = compute(ret, load_rules(2024))
    assert res.unmodeled_sch3_credits == Decimal(0)


def test_passthrough_reduces_total_tax():
    # Direct check: passthrough acts as a credit, not an addition.
    base = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
    )
    with_credit = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        schedule_3_line_8_reported=Decimal("1500"),
    )
    rules = load_rules(2024)
    base_res = compute(base, rules)
    cred_res = compute(with_credit, rules)
    assert cred_res.total_tax == base_res.total_tax - Decimal("1500.00")


def test_extract_line_20_schedule_3_credits():
    text = """\
1040 (2023)
19 Child tax credit or credit for other dependents from Schedule 8812 . . . . . . 19 2,000
20 Amount from Schedule 3, line 8 . . . . . . . . . . . . . . . . . . . . . . . 20 2,005
21 Add lines 19 and 20 . . . . . . . . . . . . . . . . . . . . . . . . . . . . 21 4,005
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("schedule_3_line_8_reported") == Decimal("2005")
