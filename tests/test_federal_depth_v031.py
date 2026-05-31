"""Tests for v0.31.0 federal-depth additions:

  - Roth IRA MAGI phaseout exposed on TaxResult
  - §172 NOL pre-TCJA 20-year vintage aging
  - §469 per-activity suspended-PAL tracking
"""
from decimal import Decimal

from taxlens import compute
from taxlens.models import FilingStatus, RentalProperty, Return
from taxlens.service import TaxLensService
from taxlens.db import make_sessionmaker


# ────────────────────────── Roth phaseout result fields ──────────────────────────


def test_roth_phaseout_disallows_contribution_at_high_magi():
    """Single filer at AGI 161k (2024) — fully phased out."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(161_000),
        roth_ira_contributions=Decimal(7_000),
        taxpayer_age=40,
    )
    r = compute(ret)
    assert r.roth_contribution_allowed == 0
    assert r.roth_contribution_disallowed == Decimal("7000.00")
    # And the §4973 excise should also kick in (6% × 7000 = 420)
    assert r.excess_ira_contribution_excise == Decimal("420.00")


def test_roth_allowed_in_full_below_phaseout():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(60_000),
        roth_ira_contributions=Decimal(7_000),
        taxpayer_age=40,
    )
    r = compute(ret)
    assert r.roth_contribution_allowed == Decimal("7000.00")
    assert r.roth_contribution_disallowed == 0
    assert r.excess_ira_contribution_excise == 0


# ────────────────────────── §172 NOL vintage aging ──────────────────────────


def test_nol_pre_tcja_vintage_expires_after_20_years():
    """A 2002 NOL surviving into 2023 (21 years) should be dropped."""
    ret = Return(
        tax_year=2023, filing_status=FilingStatus.SINGLE,
        wages=Decimal(80_000),
        nol_carryforward_lots_in=[{"year": 2002, "amount": "10000"}],
    )
    r = compute(ret)
    assert r.nol_expired_this_year == Decimal("10000.00")
    assert r.nol_carryforward_out == 0
    assert r.nol_carryforward_lots_out == []


def test_nol_post_tcja_vintage_never_expires():
    """A post-TCJA NOL is never aged out by the pre-TCJA 20-year rule."""
    ret = Return(
        tax_year=2025, filing_status=FilingStatus.SINGLE,
        wages=Decimal(80_000),
        nol_carryforward_lots_in=[{"year": 2018, "amount": "5000"}],
    )
    r = compute(ret)
    assert r.nol_expired_this_year == 0
    # Loaded as a single lot, consumed FIFO against 80% taxable-income cap.
    # Taxable income ≈ 80k − std_ded ≈ 65k, cap ≈ 52k, so the whole 5k gets used.
    assert r.nol_carryforward_out == 0


def test_nol_fifo_consumes_oldest_first():
    """Three lots; FIFO consumption uses oldest first."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(20_000),  # taxable income too small to consume all lots
        nol_carryforward_lots_in=[
            {"year": 2015, "amount": "5000"},
            {"year": 2020, "amount": "20000"},
            {"year": 2023, "amount": "15000"},
        ],
    )
    r = compute(ret)
    # Pre-TCJA 2015 lot consumed first (partially); 2020 and 2023 untouched.
    assert r.nol_expired_this_year == 0
    out_years = sorted(int(l["year"]) for l in r.nol_carryforward_lots_out)
    assert 2020 in out_years
    assert 2023 in out_years
    # 2015 should be partially consumed but a remainder survives.
    out_2015 = [Decimal(l["amount"]) for l in r.nol_carryforward_lots_out if int(l["year"]) == 2015]
    assert out_2015 and out_2015[0] < Decimal("5000")


def test_nol_lots_threaded_by_service(tmp_path):
    db_path = tmp_path / "nol.db"
    svc = TaxLensService(make_sessionmaker(db_path))

    # Year 1: huge SE loss creates an NOL.
    y1 = Return(
        tax_year=2023, filing_status=FilingStatus.SINGLE,
        wages=Decimal(30_000),
        nol_carryforward_in=Decimal(50_000),  # imported with prior NOL
    )
    svc.import_return(y1, source_hash="y1")

    # Year 2: should see the lot threaded forward.
    y2 = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(120_000),
    )
    svc.import_return(y2, source_hash="y2")
    r2 = svc.get_by_year(2024)["result"]
    # Some NOL should have been used against 120k wages (post-TCJA 80% cap).
    assert Decimal(r2["nol_carryforward_out"]) < Decimal("50000")


# ────────────────────────── §469 per-activity PAL tracking ──────────────────────────


def test_per_activity_release_only_disposed_property():
    """Two rentals with per-activity buckets; only the sold one releases."""
    p1 = RentalProperty(
        id="prop_A", property_type="residential",
        cost_basis=Decimal(150_000), in_service_year=2018,
        prior_accumulated_depreciation=Decimal(30_000),
        suspended_loss_in=Decimal(15_000),
        disposed_year=2024, disposed_month=8,
        sale_price=Decimal(140_000),
    )
    p2 = RentalProperty(
        id="prop_B", property_type="residential",
        cost_basis=Decimal(200_000), in_service_year=2020,
        suspended_loss_in=Decimal(25_000),
    )
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        rental_net_income=Decimal(0),
        is_active_real_estate_participant=True,
        rental_properties=[p1, p2],
    )
    r = compute(ret)
    # Only prop_A's $15k released; prop_B's $25k still in the per-activity out.
    assert r.passive_loss_released_on_disposition == Decimal("15000.00")
    assert r.per_activity_suspended_pal_out == {"prop_B": Decimal("25000")}


def test_per_activity_back_compat_with_aggregate_model():
    """Without per-activity buckets, aggregate model still releases on any disposition."""
    p1 = RentalProperty(
        id="prop_X", property_type="residential",
        cost_basis=Decimal(150_000), in_service_year=2018,
        prior_accumulated_depreciation=Decimal(30_000),
        disposed_year=2024, disposed_month=8,
        sale_price=Decimal(160_000),
    )
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        rental_net_income=Decimal(0),
        suspended_passive_losses_carryforward=Decimal(40_000),
        is_active_real_estate_participant=True,
        rental_properties=[p1],
    )
    r = compute(ret)
    # Aggregate path releases the full $40k pool.
    assert r.passive_loss_released_on_disposition == Decimal("40000.00")
    assert r.per_activity_suspended_pal_out == {}


def test_per_activity_re_pro_releases_all_buckets():
    """RE pro flag releases per-activity buckets too."""
    p1 = RentalProperty(
        id="prop_A", property_type="residential",
        cost_basis=Decimal(150_000), in_service_year=2018,
        suspended_loss_in=Decimal(20_000),
    )
    p2 = RentalProperty(
        id="prop_B", property_type="residential",
        cost_basis=Decimal(200_000), in_service_year=2020,
        suspended_loss_in=Decimal(30_000),
    )
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(300_000),
        rental_net_income=Decimal(0),
        is_real_estate_professional=True,
        rental_properties=[p1, p2],
    )
    r = compute(ret)
    # All $50k of per-activity buckets released.
    assert r.passive_loss_released_on_disposition == Decimal("50000.00")


def test_per_activity_threaded_by_service(tmp_path):
    """Per-activity buckets thread forward across years."""
    db_path = tmp_path / "pal2.db"
    svc = TaxLensService(make_sessionmaker(db_path))

    # Year 1: two rentals with starting per-activity losses, neither disposed.
    p1_y1 = RentalProperty(
        id="prop_A", property_type="residential",
        cost_basis=Decimal(150_000), in_service_year=2018,
        suspended_loss_in=Decimal(15_000),
    )
    p2_y1 = RentalProperty(
        id="prop_B", property_type="residential",
        cost_basis=Decimal(200_000), in_service_year=2020,
        suspended_loss_in=Decimal(25_000),
    )
    y1 = Return(
        tax_year=2023, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        is_active_real_estate_participant=True,
        rental_properties=[p1_y1, p2_y1],
    )
    svc.import_return(y1, source_hash="y1")

    # Year 2: dispose prop_A. The service should have threaded both buckets
    # into year 2's input; engine releases only A's, retains B's.
    p1_y2 = RentalProperty(
        id="prop_A", property_type="residential",
        cost_basis=Decimal(150_000), in_service_year=2018,
        prior_accumulated_depreciation=Decimal(25_000),
        disposed_year=2024, disposed_month=10,
        sale_price=Decimal(170_000),
    )
    p2_y2 = RentalProperty(
        id="prop_B", property_type="residential",
        cost_basis=Decimal(200_000), in_service_year=2020,
    )
    y2 = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),
        is_active_real_estate_participant=True,
        rental_properties=[p1_y2, p2_y2],
    )
    svc.import_return(y2, source_hash="y2")
    r2 = svc.get_by_year(2024)["result"]
    assert Decimal(r2["passive_loss_released_on_disposition"]) == Decimal("15000.00")
    assert {k: Decimal(v) for k, v in r2["per_activity_suspended_pal_out"].items()} == {
        "prop_B": Decimal("25000.00")
    }
