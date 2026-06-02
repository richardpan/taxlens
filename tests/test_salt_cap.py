"""SALT cap (§164(b)(6)) tests covering TCJA $10k flat cap (2018-2024)
and OBBB-era $40k+ cap with 30% phaseout (2025-2029).

Verifies:
  * `apply_salt_cap` helper math (cap, phaseout, floor).
  * Engine auto-derives itemized from components when
    ``itemized_deductions`` is None and applies the cap.
  * Pre-2018 years (no `salt_cap` in rules) are no-op pass-through.
"""
from decimal import Decimal

from taxlens.engine import apply_salt_cap, compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


# --- Helper: apply_salt_cap math ---------------------------------------

def test_apply_salt_cap_tcja_2024_under_cap() -> None:
    rules = load_rules(2024)
    capped, eff_cap, reduction = apply_salt_cap(
        Decimal("8000"), Decimal("150000"), "single", rules,
    )
    assert capped == Decimal("8000")
    assert eff_cap == Decimal("10000")
    assert reduction == Decimal("0")


def test_apply_salt_cap_tcja_2024_over_cap() -> None:
    rules = load_rules(2024)
    capped, eff_cap, reduction = apply_salt_cap(
        Decimal("25000"), Decimal("150000"), "mfj", rules,
    )
    assert capped == Decimal("10000")
    assert eff_cap == Decimal("10000")
    assert reduction == Decimal("15000")


def test_apply_salt_cap_tcja_2024_mfs_lower_cap() -> None:
    rules = load_rules(2024)
    capped, _eff, _red = apply_salt_cap(
        Decimal("8000"), Decimal("150000"), "mfs", rules,
    )
    assert capped == Decimal("5000")


def test_apply_salt_cap_obbb_2025_no_phaseout() -> None:
    """MAGI well under $500k phaseout — full $40k cap applies."""
    rules = load_rules(2025)
    capped, eff_cap, _red = apply_salt_cap(
        Decimal("35000"), Decimal("250000"), "single", rules,
    )
    assert capped == Decimal("35000")
    assert eff_cap == Decimal("40000")


def test_apply_salt_cap_obbb_2025_phaseout_partial() -> None:
    """MAGI $520k single → cap reduced by 30% × ($520k − $500k) = $6k →
    effective cap = $34k. Filer paid $50k → deducts $34k."""
    rules = load_rules(2025)
    capped, eff_cap, reduction = apply_salt_cap(
        Decimal("50000"), Decimal("520000"), "single", rules,
    )
    assert eff_cap == Decimal("34000")
    assert capped == Decimal("34000")
    assert reduction == Decimal("16000")


def test_apply_salt_cap_obbb_2025_phaseout_hits_floor() -> None:
    """MAGI very high ($700k) → reduction would drive cap below $10k floor;
    pinned at floor. Filer paid $50k → deducts $10k."""
    rules = load_rules(2025)
    capped, eff_cap, _red = apply_salt_cap(
        Decimal("50000"), Decimal("700000"), "single", rules,
    )
    assert eff_cap == Decimal("10000")
    assert capped == Decimal("10000")


def test_apply_salt_cap_obbb_2026_indexed_values() -> None:
    """OBBB indexes the cap +1% per year: $40,000 (2025) → $40,400 (2026).
    Phaseout threshold also bumps from $500k to $505k."""
    rules = load_rules(2026)
    capped, eff_cap, _red = apply_salt_cap(
        Decimal("45000"), Decimal("250000"), "mfj", rules,
    )
    assert eff_cap == Decimal("40400")
    assert capped == Decimal("40400")


def test_apply_salt_cap_pre_tcja_no_op() -> None:
    """Pre-2018 years have no salt_cap in rules — pass-through."""
    rules = load_rules(2017)
    capped, eff_cap, reduction = apply_salt_cap(
        Decimal("25000"), Decimal("150000"), "single", rules,
    )
    assert capped == Decimal("25000")
    assert eff_cap is None
    assert reduction == Decimal("0")


# --- Engine integration: auto-compose itemized + apply cap ------------

def test_engine_auto_composes_itemized_with_salt_cap_2024() -> None:
    """User supplies components (no itemized_deductions). Engine should
    auto-compose: $20k mortgage + $5k charity + min($25k SALT, $10k cap)
    = $35k itemized — beats the $14,600 single std deduction."""
    r = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("250000"),
        mortgage_interest=Decimal("20000"),
        charitable_contributions=Decimal("5000"),
        salt_paid=Decimal("25000"),
    )
    result = compute(r)
    assert result.deduction_kind == "itemized"
    assert result.deduction_used == Decimal("35000.00")


def test_engine_obbb_high_earner_2025_phaseout_bites() -> None:
    """MFJ filer with $700k AGI and $60k SALT paid: phaseout drives cap
    down to floor ($10k). Itemized = $20k mortgage + $5k charity +
    $10k SALT = $35k."""
    r = Return(
        tax_year=2025,
        filing_status=FilingStatus.MFJ,
        wages=Decimal("700000"),
        mortgage_interest=Decimal("20000"),
        charitable_contributions=Decimal("5000"),
        salt_paid=Decimal("60000"),
    )
    result = compute(r)
    assert result.deduction_kind == "itemized"
    assert result.deduction_used == Decimal("35000.00")


def test_engine_user_supplied_itemized_takes_precedence() -> None:
    """When the caller supplies ``itemized_deductions`` directly (PDF
    importer / Schedule A line 17), it's trusted as already-capped and
    the engine does NOT re-cap salt_paid."""
    r = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("250000"),
        itemized_deductions=Decimal("28000"),
        # If we naively re-capped, we'd compose $20k + $5k + $10k = $35k
        # and overwrite the user's $28k. We must not.
        mortgage_interest=Decimal("20000"),
        charitable_contributions=Decimal("5000"),
        salt_paid=Decimal("25000"),
    )
    result = compute(r)
    assert result.deduction_kind == "itemized"
    assert result.deduction_used == Decimal("28000.00")
