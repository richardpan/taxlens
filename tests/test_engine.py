"""Engine tests against hand-computed golden fixtures.

A failure here means either the engine drifted or a rule table changed.
Both deserve investigation — the engine should never silently change a number.
"""
from decimal import Decimal

import pytest

from taxlens import compute

# Fixture name → tolerance per field (most are exact; SE rounding can be ±$0.01)
FIXTURE_NAMES = [
    "mfj_2024_basic",
    "mfj_2024_qualified",
    "single_2023_se",
]

CENT = Decimal("0.01")


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_matches_expected(fixture, name):
    ret, expected = fixture(name)
    result = compute(ret)

    for field, want in expected.items():
        got = getattr(result, field)
        assert got is not None, f"{name}: field {field} is None"
        diff = abs(Decimal(got) - Decimal(want))
        assert diff <= CENT, (
            f"{name}.{field}: expected {want}, got {got} (Δ {diff})"
        )


def test_audit_trail_is_populated(fixture):
    ret, _ = fixture("mfj_2024_qualified")
    result = compute(ret)
    labels = [s.label for s in result.steps]
    # Must end with the rollup steps users expect to see in the UI
    assert "Total tax" in labels
    assert "Refund (+) / owed (−)" in labels
    # And expose the bracket fill data for the visualization
    assert result.ordinary_bracket_fills
    assert result.qualified_bracket_fills


def test_reconciliation_within_tolerance(fixture):
    ret, _ = fixture("mfj_2024_qualified")
    result = compute(ret)
    assert result.reported_total_tax is not None
    assert result.reconciled(tolerance=Decimal("1.00"))


def test_reconciliation_delta_surfaces_mismatch(fixture):
    ret, _ = fixture("mfj_2024_qualified")
    # Pretend the PDF reported a different number; engine must not "fix" it silently.
    mutated = ret.model_copy(update={"reported_total_tax": Decimal("50000")})
    result = compute(mutated)
    assert result.reconciliation_delta is not None
    assert result.reconciliation_delta != 0
    assert not result.reconciled(tolerance=Decimal("1.00"))


def test_rules_year_mismatch_raises(fixture):
    from taxlens.rules import load_rules

    ret, _ = fixture("mfj_2024_basic")
    wrong_rules = load_rules(2023)
    with pytest.raises(ValueError, match="rules year"):
        compute(ret, rules=wrong_rules)
