"""Tests for §469 Passive Activity Loss depth additions (v0.30.0).

Covers:
  - §469(c)(7) Real estate professional: unlimited rental loss deduction
  - §469(g) Complete disposition: full release of suspended losses
  - §469(i)(3)(F) MAGI: phaseout uses gross-income proxy, not just wages
"""
from decimal import Decimal

from taxlens import compute
from taxlens.models import FilingStatus, RentalProperty, Return
from taxlens.service import TaxLensService
from taxlens.db import make_sessionmaker


# ────────────────────────── §469(c)(7) Real estate professional ──────────────────────────


def test_re_pro_deducts_full_rental_loss_at_high_income():
    """A real estate professional gets the full deduction even at $300k AGI."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(300_000),
        rental_net_income=Decimal(-40_000),
        is_real_estate_professional=True,
    )
    r = compute(ret)
    # No phaseout, no $25k cap. Full $40k loss.
    assert r.schedule_e_income == Decimal("-40000.00")
    assert r.passive_loss_disallowed == 0


def test_re_pro_releases_suspended_losses():
    """Activating RE-pro status releases all prior suspended PALs."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        rental_net_income=Decimal(-10_000),
        suspended_passive_losses_carryforward=Decimal(60_000),
        is_real_estate_professional=True,
    )
    r = compute(ret)
    # Released: prior $60k + current $10k = $70k full deduction.
    assert r.schedule_e_income == Decimal("-70000.00")
    assert r.passive_loss_disallowed == 0
    assert r.passive_loss_released_on_disposition == Decimal("60000.00")


# ────────────────────────── §469(g) Complete disposition ──────────────────────────


def test_complete_disposition_releases_suspended_losses():
    """Selling a rental in a taxable transaction releases all suspended PALs."""
    prop = RentalProperty(
        id="rental1",
        property_type="residential",
        cost_basis=Decimal(200_000),
        in_service_year=2018,
        in_service_month=1,
        prior_accumulated_depreciation=Decimal(45_000),
        disposed_year=2024,
        disposed_month=6,
        sale_price=Decimal(180_000),  # loss sale; no recapture concerns
    )
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        rental_net_income=Decimal(-5_000),
        suspended_passive_losses_carryforward=Decimal(40_000),
        is_active_real_estate_participant=True,
        rental_properties=[prop],
    )
    r = compute(ret)
    # All $40k suspended + this year's net rental loss become fully deductible.
    # No PAL carryforward.
    assert r.passive_loss_disallowed == 0
    assert r.passive_loss_released_on_disposition == Decimal("40000.00")


def test_no_disposition_keeps_passive_rules():
    """Sanity: without disposition, the $25k cap still applies."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(80_000),
        rental_net_income=Decimal(-15_000),
        suspended_passive_losses_carryforward=Decimal(40_000),
        is_active_real_estate_participant=True,
    )
    r = compute(ret)
    # Combined loss = $55k. Allowance at $80k MAGI = $25k. Disallowed = $30k.
    assert r.schedule_e_income == Decimal("-25000.00")
    assert r.passive_loss_disallowed == Decimal("30000.00")
    assert r.passive_loss_released_on_disposition == 0


# ────────────────────────── §469(i)(3)(F) MAGI ──────────────────────────


def test_magi_includes_interest_and_dividends_for_phaseout():
    """Pre-v0.30 only counted wages+k1_obi; now interest/divs trigger phaseout too."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(80_000),
        interest_income=Decimal(10_000),
        ordinary_dividends=Decimal(20_000),
        long_term_capital_gains=Decimal(30_000),  # pushes MAGI to $140k
        rental_net_income=Decimal(-30_000),
        is_active_real_estate_participant=True,
    )
    r = compute(ret)
    # MAGI proxy = 80 + 10 + 20 + 30 = 140k. Allowance = 25k − (40k × 0.5) = 5k.
    # Allowed loss = 5k; disallowed = 25k.
    assert r.schedule_e_income == Decimal("-5000.00")
    assert r.passive_loss_disallowed == Decimal("25000.00")


def test_magi_fully_phases_out_allowance():
    """MAGI ≥ $150k → zero allowance even with active participation."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(120_000),
        interest_income=Decimal(40_000),  # MAGI proxy = 160k
        rental_net_income=Decimal(-20_000),
        is_active_real_estate_participant=True,
    )
    r = compute(ret)
    assert r.schedule_e_income == 0
    assert r.passive_loss_disallowed == Decimal("20000.00")


# ────────────────────────── service threading ──────────────────────────


def test_suspended_loss_released_by_service_on_disposition(tmp_path):
    """End-to-end: year 1 builds up suspended PAL, year 2 disposes → freed."""
    db_path = tmp_path / "pal.db"
    svc = TaxLensService(make_sessionmaker(db_path))

    # Year 1: high income blocks the loss; carries forward.
    y1 = Return(
        tax_year=2023, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        rental_net_income=Decimal(-30_000),
        is_active_real_estate_participant=True,
    )
    svc.import_return(y1, source_hash="y1")
    r1 = svc.get_by_year(2023)["result"]
    assert Decimal(r1["passive_loss_disallowed"]) == Decimal("30000.00")

    # Year 2: dispose. All $30k suspended releases against any income.
    prop = RentalProperty(
        id="r1", property_type="residential",
        cost_basis=Decimal(150_000), in_service_year=2018,
        prior_accumulated_depreciation=Decimal(30_000),
        disposed_year=2024, disposed_month=12,
        sale_price=Decimal(140_000),
    )
    y2 = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        rental_net_income=Decimal(0),
        is_active_real_estate_participant=True,
        rental_properties=[prop],
    )
    svc.import_return(y2, source_hash="y2")
    r2 = svc.get_by_year(2024)["result"]
    assert Decimal(r2["passive_loss_released_on_disposition"]) == Decimal("30000.00")
    assert Decimal(r2["passive_loss_disallowed"]) == 0
