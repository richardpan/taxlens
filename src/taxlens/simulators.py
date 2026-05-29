"""Planning simulators: Roth conversion and Tax-Loss Harvesting (TLH).

Both are thin wrappers around the regular `compute()` engine that translate a
high-level scenario into Return field overrides, run the engine, and report
the marginal cost / benefit. They are pure functions for testability — the
service layer threads in the stored Return.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from taxlens.engine import compute
from taxlens.models import Return, TaxResult


ZERO = Decimal("0")


@dataclass(frozen=True)
class SimResult:
    """Result of a single-year planning scenario."""
    original: TaxResult
    scenario: TaxResult
    scenario_label: str
    inputs: dict[str, Any]

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
        }


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
    return SimResult(
        original=compute(base),
        scenario=compute(scenario_return),
        scenario_label=f"Roth conversion: ${amount:,.0f}",
        inputs={"amount": amount, "kind": "roth_conversion"},
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
    )
