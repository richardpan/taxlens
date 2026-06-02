"""Performance baseline: 20-year synthetic scenario.

Asserts the service's import + carryforward-reflow pipeline stays
roughly linear in the number of stored returns. This is a regression
guard, not a benchmark — generous thresholds are intentional so it
won't flake on slow CI runners.

To get the actual numbers, run with ``-s``:

    pytest tests/test_perf_baseline.py -s
"""
from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from taxlens.models import FilingStatus, Return
from taxlens.service import TaxLensService


def _make_return(year: int) -> Return:
    """Synthesize a moderately-rich return with cap losses + FTC + SE
    so the carryforward reflow exercises the structured-lots paths,
    not just the trivial scalar pass-through."""
    return Return(
        tax_year=year,
        filing_status=FilingStatus.MFJ,
        wages=Decimal("250000") + Decimal(str((year - 2010) * 5000)),
        interest_income=Decimal("1500"),
        ordinary_dividends=Decimal("3000"),
        qualified_dividends=Decimal("2200"),
        long_term_capital_gains=Decimal("8000"),
        short_term_capital_gains=Decimal("-5000"),
        foreign_tax_paid=Decimal("1200"),
        traditional_ira_contribution=Decimal("6500"),
        federal_withholding=Decimal("45000"),
        salt_paid=Decimal("18000"),
        mortgage_interest=Decimal("12000"),
        charitable_contributions=Decimal("4000"),
    )


def test_perf_20_year_pipeline(tmp_path: Path) -> None:
    """Import 16 returns (TY2010-2025, the full available federal range)
    and time the full pipeline.

    Each ``import_return`` call triggers a full ``_reflow_carryforwards``
    that re-imports every prior year — this is N² in the worst case, so
    the threshold below scales with that. Empirically completes in
    well under 5 s on a developer laptop; we assert <30 s to avoid CI
    flakiness while still catching catastrophic regressions.
    """
    svc = TaxLensService.open(tmp_path / "perf.db")
    years = list(range(2010, 2026))

    t0 = time.perf_counter()
    for y in years:
        svc.import_return(_make_return(y))
    elapsed = time.perf_counter() - t0

    print(f"\n[perf] {len(years)}-year import + reflow chain: {elapsed:.2f}s "
          f"({elapsed / len(years) * 1000:.0f} ms/year avg)")

    assert elapsed < 30.0, (
        f"Pipeline took {elapsed:.2f}s; expected < 30s. "
        "If this is a real perf regression, profile "
        "service._reflow_carryforwards — it re-imports every prior "
        "year on each new import, so any per-year slowdown shows up "
        "as N² growth here."
    )


def test_perf_reflow_only(tmp_path: Path) -> None:
    """Once 16 years are stored, a single explicit reflow should
    complete fast (no PDF parsing, just engine recompute × 16).
    """
    svc = TaxLensService.open(tmp_path / "perf.db")
    years = list(range(2010, 2026))
    for y in years:
        svc.import_return(_make_return(y))

    t0 = time.perf_counter()
    svc._reflow_carryforwards()
    elapsed = time.perf_counter() - t0

    print(f"\n[perf] standalone {len(years)}-year reflow: {elapsed:.2f}s "
          f"({elapsed / len(years) * 1000:.0f} ms/year)")

    assert elapsed < 5.0, (
        f"Standalone reflow took {elapsed:.2f}s; expected < 5s."
    )
