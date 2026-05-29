"""Tests for Schedule E MACRS depreciation + §1250 recapture on disposal."""
from decimal import Decimal

import pytest

from taxlens.depreciation import compute_property_year
from taxlens.engine import compute
from taxlens.models import FilingStatus, RentalProperty, Return


def _ret(props, year=2024, **extras):
    return Return(
        tax_year=year,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(120_000),
        rental_properties=props,
        **extras,
    )


# ── unit-level MACRS math ────────────────────────────────────────────────────

def test_residential_mid_month_first_year_january():
    p = RentalProperty(
        id="p1", property_type="residential",
        cost_basis=Decimal(275_000),
        in_service_year=2024, in_service_month=1,
    )
    r = compute_property_year(p, 2024)
    # 275000 / 27.5 = 10,000 annual SL; mid-month Jan = (12.5 - 1)/12 = 11.5/12
    expected = Decimal(10_000) * Decimal("11.5") / Decimal(12)
    assert r.current_year_deduction == expected.quantize(Decimal("0.01"))


def test_residential_mid_month_first_year_july():
    p = RentalProperty(
        id="p1", property_type="residential",
        cost_basis=Decimal(275_000),
        in_service_year=2024, in_service_month=7,
    )
    r = compute_property_year(p, 2024)
    # (12.5 - 7)/12 = 5.5/12
    expected = Decimal(10_000) * Decimal("5.5") / Decimal(12)
    assert r.current_year_deduction == expected.quantize(Decimal("0.01"))


def test_residential_full_year_in_middle():
    p = RentalProperty(
        id="p1", property_type="residential",
        cost_basis=Decimal(275_000),
        in_service_year=2020, in_service_month=6,
        prior_accumulated_depreciation=Decimal("35000"),
    )
    r = compute_property_year(p, 2024)
    # Full SL year = 10,000
    assert r.current_year_deduction == Decimal("10000.00")
    assert r.accumulated_after == Decimal("45000.00")


def test_nonresidential_39_year_full_year():
    p = RentalProperty(
        id="p1", property_type="nonresidential",
        cost_basis=Decimal(390_000),
        in_service_year=2020, in_service_month=1,
    )
    r = compute_property_year(p, 2024)
    # 390000 / 39 = 10,000 SL/year
    assert r.current_year_deduction == Decimal("10000.00")


def test_personal_5y_table():
    p = RentalProperty(
        id="p1", property_type="personal_5y",
        cost_basis=Decimal(10_000),
        in_service_year=2024, in_service_month=1,
    )
    # Year 0 of 5y HY table: 20.00%
    r = compute_property_year(p, 2024)
    assert r.current_year_deduction == Decimal("2000.00")
    # Year 1: 32.00%
    r2 = compute_property_year(p, 2025)
    assert r2.current_year_deduction == Decimal("3200.00")


# ── disposal: §1250 unrecaptured-gain recapture ─────────────────────────────

def test_disposal_recapture_caps_at_accumulated_depreciation():
    p = RentalProperty(
        id="p1", property_type="residential",
        cost_basis=Decimal(275_000),
        in_service_year=2015, in_service_month=1,
        prior_accumulated_depreciation=Decimal("90000"),
        disposed_year=2024, disposed_month=12,
        sale_price=Decimal(400_000),
    )
    r = compute_property_year(p, 2024)
    # Exit-year mid-month for Dec: (12 - 0.5)/12 = 11.5/12 of 10000 = 9583.33
    expected_dep = (Decimal(10_000) * Decimal("11.5") / Decimal(12)).quantize(Decimal("0.01"))
    assert r.current_year_deduction == expected_dep
    accum = Decimal("90000") + expected_dep
    adj_basis = Decimal(275_000) - accum
    expected_gain = Decimal(400_000) - adj_basis
    assert r.sale_total_gain == expected_gain.quantize(Decimal("0.01"))
    # Recapture = min(gain, accumulated dep)
    assert r.sale_recapture_1250 == min(expected_gain, accum).quantize(Decimal("0.01"))


# ── full engine integration ─────────────────────────────────────────────────

def test_engine_subtracts_depreciation_from_rental_net():
    """Property with $0 net cash rental but $10k depreciation produces a $10k
    passive loss that gets absorbed by the active-participation allowance."""
    prop = RentalProperty(
        id="house", property_type="residential",
        cost_basis=Decimal(275_000),
        in_service_year=2020, in_service_month=1,
        prior_accumulated_depreciation=Decimal("40000"),
    )
    ret = _ret([prop], is_active_real_estate_participant=True,
               rental_net_income=Decimal(0))
    result = compute(ret)
    # $10k depreciation → $10k loss → fully allowed under $25k allowance.
    assert result.depreciation_current_year == Decimal("10000.00")
    assert result.schedule_e_income == Decimal("-10000.00")
    assert result.passive_loss_disallowed == Decimal("0")
    assert result.depreciation_accumulated_out["house"] == Decimal("50000.00")


def test_engine_disposal_feeds_unrecaptured_1250_at_25pct():
    """Selling a depreciated rental at a gain pushes accumulated dep into
    the 25%-rate bucket."""
    prop = RentalProperty(
        id="house", property_type="residential",
        cost_basis=Decimal(275_000),
        in_service_year=2010, in_service_month=1,
        prior_accumulated_depreciation=Decimal("130000"),
        disposed_year=2024, disposed_month=6,
        sale_price=Decimal(500_000),
    )
    ret = _ret([prop], is_active_real_estate_participant=True,
               rental_net_income=Decimal(0))
    result = compute(ret)
    # There should be a non-zero unrecaptured §1250 tax component.
    assert result.unrecaptured_1250_tax > Decimal("0")
    # And the long-term cap-gain bucket got the excess (any gain above
    # accumulated depreciation).


def test_multiple_properties_sum_correctly():
    p1 = RentalProperty(
        id="a", property_type="residential",
        cost_basis=Decimal(275_000),
        in_service_year=2020, in_service_month=1,
    )
    p2 = RentalProperty(
        id="b", property_type="residential",
        cost_basis=Decimal(550_000),
        in_service_year=2020, in_service_month=1,
    )
    ret = _ret([p1, p2], is_active_real_estate_participant=True,
               rental_net_income=Decimal(20_000))
    result = compute(ret)
    # 10k + 20k = 30k depreciation; rental net 20k → -10k loss → allowed.
    assert result.depreciation_current_year == Decimal("30000.00")


def test_no_properties_is_no_op():
    ret = _ret([], rental_net_income=Decimal(5_000))
    result = compute(ret)
    assert result.depreciation_current_year == Decimal("0")
    assert result.depreciation_accumulated_out == {}
