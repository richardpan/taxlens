"""Typed data model for tax returns and computation results.

Money is represented as Decimal end-to-end. Floats are forbidden in any
computation that produces a dollar amount.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FilingStatus(str, Enum):
    SINGLE = "single"
    MFJ = "mfj"     # married filing jointly
    MFS = "mfs"     # married filing separately
    HOH = "hoh"     # head of household
    QSS = "qss"     # qualifying surviving spouse


class Return(BaseModel):
    """Inputs for one tax year. All values default to 0 if absent."""
    model_config = ConfigDict(frozen=True)

    tax_year: int
    filing_status: FilingStatus
    qualifying_children: int = 0

    # Income
    wages: Decimal = Decimal(0)                  # 1040 line 1
    interest_income: Decimal = Decimal(0)        # line 2b (taxable)
    ordinary_dividends: Decimal = Decimal(0)     # line 3b
    qualified_dividends: Decimal = Decimal(0)    # line 3a (subset of 3b)
    long_term_capital_gains: Decimal = Decimal(0)
    short_term_capital_gains: Decimal = Decimal(0)
    se_income: Decimal = Decimal(0)              # Schedule C net profit
    other_ordinary_income: Decimal = Decimal(0)

    # Above-the-line adjustments (Schedule 1 Part II), excluding ½ SE tax (engine adds it)
    hsa_deduction: Decimal = Decimal(0)
    other_adjustments: Decimal = Decimal(0)

    # Deduction choice
    itemized_deductions: Decimal | None = None   # None → use standard deduction

    # Withholding & estimated payments (for refund/owed calc)
    federal_withholding: Decimal = Decimal(0)
    estimated_payments: Decimal = Decimal(0)

    # Reconciliation reference: the value printed on the PDF's line 24 (total tax).
    # If provided, the engine surfaces the delta but never overrides its own computation.
    reported_total_tax: Decimal | None = None


class ComputationStep(BaseModel):
    """One audit-trail entry produced by the engine."""
    model_config = ConfigDict(frozen=True)

    index: int
    label: str
    formula: str                          # human-readable formula
    inputs: dict[str, Any]                # substituted values, for the UI
    output: Decimal

    def __str__(self) -> str:
        return f"[{self.index}] {self.label}: {self.formula} = {self.output}"


class BracketFill(BaseModel):
    """One row of a bracket-walk audit trail."""
    model_config = ConfigDict(frozen=True)

    lower: Decimal
    upper: Decimal | None                 # None = no upper bound
    rate: Decimal
    amount_in_bracket: Decimal
    tax_in_bracket: Decimal


class TaxResult(BaseModel):
    """Full output of a tax computation."""
    model_config = ConfigDict(frozen=True)

    tax_year: int
    filing_status: FilingStatus

    agi: Decimal
    taxable_income: Decimal
    deduction_used: Decimal
    deduction_kind: str                   # "standard" or "itemized"

    ordinary_tax: Decimal
    qualified_tax: Decimal
    se_tax: Decimal
    additional_medicare_tax: Decimal
    niit: Decimal
    credits: Decimal
    total_tax: Decimal

    refund_or_owed: Decimal               # positive = refund, negative = owed

    ordinary_bracket_fills: list[BracketFill]
    qualified_bracket_fills: list[BracketFill]
    steps: list[ComputationStep]

    # Reconciliation
    reported_total_tax: Decimal | None = None
    reconciliation_delta: Decimal | None = None  # computed − reported

    def reconciled(self, tolerance: Decimal = Decimal("1.00")) -> bool:
        """True iff a reported value is present and within tolerance of the computed value."""
        if self.reconciliation_delta is None:
            return False
        return abs(self.reconciliation_delta) <= tolerance


class Rules(BaseModel):
    """Parsed contents of a `tax_rules/federal/{year}.yaml` file."""
    model_config = ConfigDict(frozen=True)

    year: int
    standard_deduction: dict[str, Decimal]
    ordinary_brackets: dict[str, list[tuple[Decimal, Decimal]]]
    qualified_brackets: dict[str, list[tuple[Decimal, Decimal]]]
    se_tax: dict[str, Decimal]
    additional_medicare: dict[str, Any]
    niit: dict[str, Any]
    ctc: dict[str, Any]
