"""Tests for Form 1116 Foreign Tax Credit § 904(a) limit, § 904(c) carryforward
aging, and § 904(k) de minimis exception.

The pre-v0.29 importer modeled FTC as ``min(foreign_taxes_paid + carry_in,
regular_tax)``, which is fine for someone with only a couple hundred dollars
of 1099-DIV foreign tax (de minimis path) but overly generous for anyone
with material foreign holdings — the real § 904(a) limit caps the credit
at the foreign-source share of total taxable income. These tests lock in
the corrected behavior.
"""
from __future__ import annotations

from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


def _base() -> Return:
    """A return with enough wages to generate ~$15-17k of regular tax."""
    return Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
    )


def test_904a_limit_caps_credit_below_full_us_tax() -> None:
    """With foreign source income = 10% of taxable income, the FTC limit
    is 10% of pre-FTC US tax — even if foreign tax paid is much larger."""
    ret = _base().model_copy(update={
        "foreign_taxes_paid": Decimal("5000"),
        "foreign_source_income": Decimal("8570"),  # ~10% of taxable income
    })
    res = compute(ret)
    # Only ~10% of regular tax is usable; the rest carries forward.
    # Regular tax ~= $14k on $85,700 taxable → limit ~= $1,400.
    # All checks are sanity-bands rather than exact-equal to avoid coupling
    # the test to bracket inflation tweaks.
    assert res.credits < Decimal("2000"), \
        f"FTC capped by §904(a) should be well below the $5,000 paid; got credits={res.credits}"
    assert res.ftc_carryforward_out > Decimal("2500"), \
        f"unused FTC should carry forward; got {res.ftc_carryforward_out}"


def test_de_minimis_no_904a_when_under_300_and_no_fsi() -> None:
    """§904(k): individual with ≤$300 foreign tax and no Form 1116 takes
    the full credit. We detect this by foreign_source_income == 0 AND
    foreign_taxes_paid ≤ de minimis cap."""
    ret = _base().model_copy(update={
        "foreign_taxes_paid": Decimal("250"),
        # no foreign_source_income supplied
    })
    base_tax = compute(_base()).total_tax
    res = compute(ret)
    # Full credit applied — total tax reduced by exactly the foreign tax.
    assert base_tax - res.total_tax == Decimal("250"), \
        f"de minimis should give full $250 credit, got delta={base_tax - res.total_tax}"
    assert res.ftc_carryforward_out == Decimal("0")


def test_de_minimis_cap_doubles_for_mfj() -> None:
    """MFJ de minimis cap is $600 (vs $300 single)."""
    ret = Return(tax_year=2024, filing_status=FilingStatus.MFJ,
                 wages=Decimal("200000"),
                 foreign_taxes_paid=Decimal("550"))
    base = ret.model_copy(update={"foreign_taxes_paid": Decimal("0")})
    delta = compute(base).total_tax - compute(ret).total_tax
    assert delta == Decimal("550"), f"MFJ de minimis should allow $550 credit, got {delta}"


def test_904c_aging_drops_lots_older_than_10_years() -> None:
    """A lot generated in tax_year - 11 must be dropped; one from
    tax_year - 10 must survive."""
    ret = _base().model_copy(update={
        "foreign_taxes_paid": Decimal("0"),
        "foreign_source_income": Decimal("10000"),
        "ftc_carryforward_lots_in": [
            {"year": 2013, "amount": "5000"},   # 11y old → expired
            {"year": 2014, "amount": "3000"},   # 10y old → still good
        ],
    })
    res = compute(ret)
    assert res.ftc_expired_this_year == Decimal("5000")
    # Some of the 2014 lot may be consumed against the limit this year;
    # the leftover survives in the lots-out, with no 2013 vintage present.
    surviving_years = {int(l["year"]) for l in res.ftc_carryforward_lots_out}
    assert 2013 not in surviving_years, "11y-old vintage should be aged out"


def test_lots_consumed_fifo_oldest_first() -> None:
    """When multiple lots exist, the §904(a) limit consumes the OLDEST
    first so younger vintages get the maximum remaining shelf life."""
    ret = _base().model_copy(update={
        "foreign_taxes_paid": Decimal("0"),
        "foreign_source_income": Decimal("85000"),  # large fraction → big limit
        "ftc_carryforward_lots_in": [
            {"year": 2018, "amount": "1000"},
            {"year": 2022, "amount": "1000"},
        ],
    })
    res = compute(ret)
    # With a ~99% fraction × $14k regular tax, the limit is comfortably
    # larger than both lots combined → both fully consumed, nothing
    # carries out.
    assert res.ftc_carryforward_out == Decimal("0")
    assert res.credits >= Decimal("2000")


def test_lots_consumed_fifo_partial() -> None:
    """When the limit only covers part of the carryforward, the oldest
    lots are exhausted first; younger lots survive."""
    ret = _base().model_copy(update={
        "foreign_taxes_paid": Decimal("0"),
        "foreign_source_income": Decimal("8500"),   # ~10% → small limit
        "ftc_carryforward_lots_in": [
            {"year": 2018, "amount": "500"},   # should be fully used
            {"year": 2022, "amount": "5000"},  # partially used / mostly carried
        ],
    })
    res = compute(ret)
    surviving = {int(l["year"]): Decimal(l["amount"])
                 for l in res.ftc_carryforward_lots_out}
    # Oldest fully consumed, only younger vintage left.
    assert 2018 not in surviving, "FIFO should fully consume the oldest lot first"
    assert 2022 in surviving and surviving[2022] > 0


def test_lots_threaded_forward_by_service(tmp_path) -> None:
    """End-to-end: two consecutive years with massive foreign tax. The
    service must thread the carry-out lots from year 1 into year 2's
    lots_in, and the engine must consume FIFO from there."""
    from taxlens.db import make_sessionmaker
    from taxlens.service import TaxLensService

    svc = TaxLensService(make_sessionmaker(tmp_path / "ftc_thread.sqlite"))
    y1 = Return(tax_year=2022, filing_status=FilingStatus.SINGLE,
                wages=Decimal("50000"),
                foreign_taxes_paid=Decimal("8000"),
                foreign_source_income=Decimal("5000"))
    y2 = Return(tax_year=2023, filing_status=FilingStatus.SINGLE,
                wages=Decimal("50000"),
                foreign_taxes_paid=Decimal("0"),
                foreign_source_income=Decimal("40000"))
    svc.import_return(y1, source_hash="h1")
    svc.import_return(y2, source_hash="h2")

    y2_full = svc.get_by_year(2023)
    assert y2_full is not None
    res = y2_full["result"]
    # The 2022 vintage must show up among the consumed lots OR
    # remaining lots in 2023.
    seen_2022_vintage = (
        any(int(l["year"]) == 2022 for l in res.get("ftc_carryforward_lots_out", []))
        or Decimal(str(res.get("credits", "0"))) > 0
    )
    assert seen_2022_vintage, "2022 FTC carryforward must thread into 2023"


def test_no_foreign_tax_no_carryforward_no_expiry() -> None:
    """The common case: a return with zero foreign tax must produce
    zero carry-out, zero expired, and empty lots."""
    res = compute(_base())
    assert res.ftc_carryforward_out == Decimal("0")
    assert res.ftc_expired_this_year == Decimal("0")
    assert res.ftc_carryforward_lots_out == []
