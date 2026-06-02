"""Planning simulators: Roth conversion and Tax-Loss Harvesting (TLH).

Both are thin wrappers around the regular `compute()` engine that translate a
high-level scenario into Return field overrides, run the engine, and report
the marginal cost / benefit. They are pure functions for testability — the
service layer threads in the stored Return.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from taxlens.engine import compute
from taxlens.models import Return, TaxResult


ZERO = Decimal("0")


# ── IRMAA thresholds (Medicare Part B/D Income-Related Monthly Adjustment) ──
#
# IRMAA uses a 2-year lookback: the surcharge that applies in tax-year Y is
# determined by your MAGI in year (Y - 2). So a Roth conversion this year
# affects Medicare premiums 2 years later. Thresholds below are by the
# *premium* year (2025 = MAGI from 2023), MFJ-aware. Values from the SSA
# fact sheet "2025 Medicare Parts B and D Premium Amounts" (CMS-9999).
#
# Each entry: (max_magi_for_tier, monthly_part_b_surcharge_dollars).
# Last tier has Decimal('Infinity') — anything above lands there.
IRMAA_TIERS: dict[int, dict[str, list[tuple[Decimal, Decimal]]]] = {
    2025: {
        "single": [
            (Decimal("106000"),    Decimal("0")),
            (Decimal("133000"),    Decimal("74.00")),
            (Decimal("167000"),    Decimal("185.00")),
            (Decimal("200000"),    Decimal("295.90")),
            (Decimal("500000"),    Decimal("406.90")),
            (Decimal("Infinity"),  Decimal("443.90")),
        ],
        "mfj": [
            (Decimal("212000"),    Decimal("0")),
            (Decimal("266000"),    Decimal("74.00")),
            (Decimal("334000"),    Decimal("185.00")),
            (Decimal("400000"),    Decimal("295.90")),
            (Decimal("750000"),    Decimal("406.90")),
            (Decimal("Infinity"),  Decimal("443.90")),
        ],
        "mfs": [
            (Decimal("106000"),    Decimal("0")),
            (Decimal("394000"),    Decimal("406.90")),
            (Decimal("Infinity"),  Decimal("443.90")),
        ],
    },
}


def _irmaa_lookup(magi: Decimal, year: int, status: str) -> tuple[int, Decimal]:
    """Return ``(tier_index, monthly_surcharge)`` for the IRMAA tier this
    MAGI lands in. ``year`` is the *premium* year (i.e. the year MAGI
    determines surcharges for, two years after the income year). Falls
    back to the most recent year present in IRMAA_TIERS."""
    table_year = year if year in IRMAA_TIERS else max(IRMAA_TIERS)
    tiers = IRMAA_TIERS[table_year].get(status) or IRMAA_TIERS[table_year]["single"]
    for i, (cap, surcharge) in enumerate(tiers):
        if magi <= cap:
            return i, surcharge
    return len(tiers) - 1, tiers[-1][1]


@dataclass(frozen=True)
class SimResult:
    """Result of a single-year planning scenario."""
    original: TaxResult
    scenario: TaxResult
    scenario_label: str
    inputs: dict[str, Any]
    # Optional planning notes surfaced to the UI (IRMAA, wash-sale, etc.).
    notes: list[str] = field(default_factory=list)

    @property
    def tax_delta(self) -> Decimal:
        return self.scenario.total_tax - self.original.total_tax

    @property
    def federal_marginal_rate(self) -> Decimal:
        """Effective marginal rate of the scenario delta (cost / amount)."""
        amt = self.inputs.get("amount") or ZERO
        if not amt or amt == ZERO:
            return ZERO
        return (self.tax_delta / Decimal(amt)).quantize(Decimal("0.0001"))

    def to_json(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_label,
            "inputs": {k: str(v) for k, v in self.inputs.items()},
            "tax_delta": str(self.tax_delta),
            "federal_marginal_rate": str(self.federal_marginal_rate),
            "original": self.original.model_dump(mode="json"),
            "after": self.scenario.model_dump(mode="json"),
            "notes": list(self.notes),
        }


def _irmaa_notes_for(base_magi: Decimal, scenario_magi: Decimal,
                     income_year: int, status: str) -> list[str]:
    """Compare IRMAA tiers between original + scenario MAGI; emit a note
    if the conversion bumps the filer into a higher Medicare tier
    two years out (the IRMAA lookback window)."""
    premium_year = income_year + 2
    base_tier, base_surcharge = _irmaa_lookup(base_magi, premium_year, status)
    sc_tier, sc_surcharge = _irmaa_lookup(scenario_magi, premium_year, status)
    if sc_tier > base_tier:
        annual_extra = (sc_surcharge - base_surcharge) * 12
        return [
            f"IRMAA: this conversion raises {premium_year} Medicare Part B "
            f"premium by ~${sc_surcharge - base_surcharge:.2f}/mo "
            f"(${annual_extra:.0f}/yr) due to the 2-year MAGI lookback. "
            "Per-Medicare-enrollee — double for MFJ couples both on Medicare."
        ]
    return []


def simulate_roth_conversion(base: Return, amount: Decimal) -> SimResult:
    """Convert `amount` of traditional IRA/401(k) → Roth in the given year.

    Mechanic: the converted amount is treated as additional ordinary income
    (taxed at marginal rates). Future tax-free growth is *not* modeled here —
    this answers "how much tax do I owe this year if I convert X?"
    """
    amount = Decimal(amount or 0)
    if amount < 0:
        raise ValueError("Roth conversion amount must be non-negative")

    # Convert to a plain dict, bump wages-equivalent ordinary income, rebuild.
    data = base.model_dump()
    # Use a dedicated bucket if it exists, otherwise add to wages (the engine
    # treats both the same way for ordinary-rate purposes).
    if "roth_conversion_amount" in data:
        data["roth_conversion_amount"] = (data.get("roth_conversion_amount") or ZERO) + amount
    else:
        data["wages"] = (data.get("wages") or ZERO) + amount

    scenario_return = Return.model_validate(data)
    base_result = compute(base)
    scenario_result = compute(scenario_return)
    notes = _irmaa_notes_for(
        base_result.agi, scenario_result.agi,
        base.tax_year, base.filing_status.value,
    )
    return SimResult(
        original=base_result,
        scenario=scenario_result,
        scenario_label=f"Roth conversion: ${amount:,.0f}",
        inputs={"amount": amount, "kind": "roth_conversion"},
        notes=notes,
    )


@dataclass(frozen=True)
class LadderRung:
    """Single year of a multi-year Roth conversion ladder."""
    year: int
    amount: Decimal
    tax_delta: Decimal
    marginal_rate: Decimal
    irmaa_tier: int
    irmaa_surcharge_monthly: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "amount": str(self.amount),
            "tax_delta": str(self.tax_delta),
            "marginal_rate": str(self.marginal_rate),
            "irmaa_tier": self.irmaa_tier,
            "irmaa_surcharge_monthly": str(self.irmaa_surcharge_monthly),
        }


@dataclass(frozen=True)
class LadderResult:
    """Result of a multi-year Roth conversion ladder."""
    rungs: list[LadderRung]
    cumulative_tax: Decimal
    cumulative_converted: Decimal
    notes: list[str]

    @property
    def avg_marginal_rate(self) -> Decimal:
        if self.cumulative_converted == ZERO:
            return ZERO
        return (self.cumulative_tax / self.cumulative_converted).quantize(Decimal("0.0001"))

    def to_json(self) -> dict[str, Any]:
        return {
            "rungs": [r.to_json() for r in self.rungs],
            "cumulative_tax": str(self.cumulative_tax),
            "cumulative_converted": str(self.cumulative_converted),
            "avg_marginal_rate": str(self.avg_marginal_rate),
            "notes": list(self.notes),
        }


def simulate_roth_conversion_ladder(base: Return,
                                    schedule: list[Decimal] | list[float] | list[int],
                                    ) -> LadderResult:
    """Project a multi-year Roth conversion ladder.

    ``base`` is treated as the recurring template — every year in the
    schedule re-uses the same income picture, only swapping in
    ``base.tax_year + i`` and the per-year conversion amount. This is
    deliberately simplistic (real life has wage growth, RMDs, Social
    Security onset) — but it answers "if my financial picture stays
    static, what's the multi-year cost of this ladder?" which is the
    dominant question for early-retirement Roth ladders.

    Each rung reports per-year tax delta + IRMAA tier and the
    cumulative summary aggregates them.
    """
    if not schedule:
        raise ValueError("schedule must be non-empty")
    rungs: list[LadderRung] = []
    notes: list[str] = []
    cum_tax = ZERO
    cum_amt = ZERO
    status = base.filing_status.value
    for i, raw in enumerate(schedule):
        amount = Decimal(str(raw))
        if amount < 0:
            raise ValueError(f"schedule[{i}] is negative")
        year = base.tax_year + i
        # Replicate the base into year (i) — only year + conversion
        # bucket change; everything else is held constant.
        data = base.model_dump()
        data["tax_year"] = year
        if amount > 0:
            if "roth_conversion_amount" in data:
                data["roth_conversion_amount"] = amount
            else:
                data["wages"] = (data.get("wages") or ZERO) + amount
        rung_return = Return.model_validate(data)
        rung_result = compute(rung_return)
        # Baseline: same year, no conversion.
        baseline_data = base.model_dump()
        baseline_data["tax_year"] = year
        if "roth_conversion_amount" in baseline_data:
            baseline_data["roth_conversion_amount"] = ZERO
        baseline_result = compute(Return.model_validate(baseline_data))
        delta = rung_result.total_tax - baseline_result.total_tax
        marg = (delta / amount).quantize(Decimal("0.0001")) if amount > 0 else ZERO
        tier_idx, surcharge = _irmaa_lookup(rung_result.agi, year + 2, status)
        rungs.append(LadderRung(
            year=year, amount=amount, tax_delta=delta,
            marginal_rate=marg,
            irmaa_tier=tier_idx,
            irmaa_surcharge_monthly=surcharge,
        ))
        cum_tax += delta
        cum_amt += amount
    # If any rung crosses an IRMAA tier vs. its baseline-year self,
    # surface a planning note on the aggregate result.
    bumped = [r for r in rungs if r.irmaa_tier >= 1 and r.amount > 0]
    if bumped:
        first = bumped[0]
        notes.append(
            f"IRMAA: conversions in {first.year}+ land in IRMAA tier "
            f"{first.irmaa_tier} (~${first.irmaa_surcharge_monthly}/mo "
            "Part B surcharge two years later). Consider sizing rungs "
            "to stay below the next tier threshold."
        )
    return LadderResult(
        rungs=rungs, cumulative_tax=cum_tax, cumulative_converted=cum_amt,
        notes=notes,
    )


# ── Tax-loss harvesting ──────────────────────────────────────────────────

WASH_SALE_NOTE = (
    "Wash-sale rule (§1091): a loss is disallowed if you buy a "
    "substantially identical security within 30 days before or after the "
    "sale (61-day window total). The simulator can't verify this — "
    "double-check your trade history before harvesting."
)


def simulate_tax_loss_harvest(base: Return, loss_amount: Decimal) -> SimResult:
    """Realize `loss_amount` of long-term capital losses this year.

    Mechanic:
      - LT losses first offset LT gains; remainder offsets ST gains.
      - Up to $3,000 of net capital loss offsets ordinary income.
      - The rest carries forward (we report it via TaxResult but don't model
        future years here — that's roth/tlh-multi territory).
    """
    loss_amount = Decimal(loss_amount or 0)
    if loss_amount < 0:
        raise ValueError("Loss amount should be expressed as a positive number")

    data = base.model_dump()
    # Subtract from existing LT gains (engine already nets LT+ST and applies
    # the $3k ordinary cap), so reducing LT gains by `loss_amount` exactly
    # models harvesting a fresh LT loss of that size.
    current_lt = data.get("long_term_capital_gains") or ZERO
    data["long_term_capital_gains"] = current_lt - loss_amount

    scenario_return = Return.model_validate(data)
    return SimResult(
        original=compute(base),
        scenario=compute(scenario_return),
        scenario_label=f"Tax-loss harvest: ${loss_amount:,.0f} LT loss",
        inputs={"amount": loss_amount, "kind": "tax_loss_harvest"},
        notes=[WASH_SALE_NOTE],
    )


@dataclass(frozen=True)
class TLHCarryforwardYear:
    """Projected use of a TLH carryforward in a future year."""
    year: int
    carryforward_in: Decimal
    used_against_ordinary: Decimal   # capped at $3,000/yr
    carryforward_out: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "carryforward_in": str(self.carryforward_in),
            "used_against_ordinary": str(self.used_against_ordinary),
            "carryforward_out": str(self.carryforward_out),
        }


def project_tlh_carryforward(loss_amount: Decimal, start_year: int,
                             years: int = 10) -> list[TLHCarryforwardYear]:
    """Project the depletion of a tax-loss-harvest carryforward across
    ``years`` future years assuming no offsetting capital gains and the
    full $3,000/yr ordinary-income offset is taken each year.

    This is the worst-case "depletion only via the $3k cap" path. If the
    filer has gains in any future year they'll burn through the
    carryforward faster — the engine handles that automatically when
    real returns are imported. This projection is a UI aid for the
    "how long will this loss take to use up?" question.
    """
    loss_amount = Decimal(loss_amount or 0)
    if loss_amount <= 0:
        return []
    cap = Decimal("3000")
    out: list[TLHCarryforwardYear] = []
    remaining = loss_amount
    for i in range(years):
        if remaining <= 0:
            break
        used = min(cap, remaining)
        new_remaining = remaining - used
        out.append(TLHCarryforwardYear(
            year=start_year + i,
            carryforward_in=remaining,
            used_against_ordinary=used,
            carryforward_out=new_remaining,
        ))
        remaining = new_remaining
    return out

