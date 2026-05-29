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


class RentalProperty(BaseModel):
    """One Schedule E rental real-estate property with MACRS depreciation.

    Land basis must be excluded from `cost_basis` (land is not depreciable).
    We support the two big real-property classes (mid-month SL):
      - "residential"     → 27.5-year straight-line
      - "nonresidential"  → 39-year straight-line
    Personal-property classes (5y appliances, 15y land improvements) use a
    half-year convention with the 200/150% DB tables; if needed, set
    `property_type` to one of "personal_5y" / "personal_15y".
    """
    model_config = ConfigDict(frozen=True)

    id: str
    property_type: str = "residential"
    cost_basis: Decimal = Decimal(0)            # depreciable basis (no land)
    in_service_year: int = 0
    in_service_month: int = 1                   # 1..12
    prior_accumulated_depreciation: Decimal = Decimal(0)
    # Disposition. When disposed_year == tax_year, depreciation prorates
    # via mid-month on the way out and any unrecaptured §1250 gain is
    # added to the year's unrecaptured_1250 stack.
    disposed_year: int | None = None
    disposed_month: int | None = None
    sale_price: Decimal = Decimal(0)            # gross sale proceeds


class Return(BaseModel):
    """Inputs for one tax year. All values default to 0 if absent."""
    model_config = ConfigDict(frozen=True)

    tax_year: int
    filing_status: FilingStatus
    qualifying_children: int = 0
    # Other dependents (qualifying relatives or kids who age out of CTC).
    # Each eligible for the $500 nonrefundable Credit for Other Dependents (ODC).
    other_dependents: int = 0

    # Income
    wages: Decimal = Decimal(0)                  # 1040 line 1
    interest_income: Decimal = Decimal(0)        # line 2b (taxable)
    ordinary_dividends: Decimal = Decimal(0)     # line 3b
    qualified_dividends: Decimal = Decimal(0)    # line 3a (subset of 3b)
    long_term_capital_gains: Decimal = Decimal(0)
    short_term_capital_gains: Decimal = Decimal(0)
    # Schedule D worksheet items — special max rates apply (28% and 25% respectively).
    # Treat as a subset of total long-term gains for income/AGI purposes.
    collectibles_gains: Decimal = Decimal(0)          # 28%-rate gain (Sch D wksht)
    unrecaptured_1250_gains: Decimal = Decimal(0)     # 25%-rate gain (Sch D wksht)
    se_income: Decimal = Decimal(0)              # Schedule C net profit
    other_ordinary_income: Decimal = Decimal(0)

    # Schedule E (rentals + royalties + passthrough)
    rental_net_income: Decimal = Decimal(0)        # net of expenses & depreciation; can be < 0
    royalty_income: Decimal = Decimal(0)
    is_active_real_estate_participant: bool = False  # gates the $25k PAL allowance
    suspended_passive_losses_carryforward: Decimal = Decimal(0)

    # Per-property MACRS depreciation. When non-empty, the engine subtracts
    # the computed current-year depreciation from rental_net_income BEFORE
    # running the Schedule E passive-loss logic. Disposals also feed
    # unrecaptured §1250 gain into the cap-gains stack.
    rental_properties: list[RentalProperty] = Field(default_factory=list)

    # K-1 passthroughs (1065, 1120-S, 1041) — aggregated; engine treats by character
    k1_ordinary_business_income: Decimal = Decimal(0)
    k1_interest: Decimal = Decimal(0)
    k1_ordinary_dividends: Decimal = Decimal(0)
    k1_qualified_dividends: Decimal = Decimal(0)
    k1_long_term_gains: Decimal = Decimal(0)
    k1_short_term_gains: Decimal = Decimal(0)
    k1_section_199a_qbi: Decimal = Decimal(0)      # for QBI deduction (Form 8995)
    k1_is_sstb: bool = False                       # specified service trade/business flag

    # Retirement / health contributions — used both for AGI math and the advisor
    traditional_401k_contributions: Decimal = Decimal(0)   # already excluded from W-2 box 1
    roth_401k_contributions: Decimal = Decimal(0)
    traditional_ira_contributions: Decimal = Decimal(0)    # above-the-line if deductible
    roth_ira_contributions: Decimal = Decimal(0)
    hsa_contributions: Decimal = Decimal(0)                # employee + employer (info)
    charitable_contributions: Decimal = Decimal(0)         # for itemize/bunch advisor
    mortgage_interest: Decimal = Decimal(0)                # for itemize advisor
    salt_paid: Decimal = Decimal(0)                        # state+local taxes, $10k SALT cap

    # ISO exercise (bargain element) — feeds AMT preferences & the advisor
    iso_bargain_element: Decimal = Decimal(0)

    # Multi-year capital-loss carryforward (§1212(b)). Set by the user (or auto-
    # computed by the service from a prior year's TaxResult.capital_loss_carryforward_out).
    capital_loss_carryforward_in: Decimal = Decimal(0)   # positive number = available loss

    # Other multi-year carryforwards (also auto-reflowed by the service).
    nol_carryforward_in: Decimal = Decimal(0)             # §172 NOL (positive = available)
    amt_credit_carryforward_in: Decimal = Decimal(0)      # Form 8801 prior-year min tax credit
    ftc_carryforward_in: Decimal = Decimal(0)             # §904 unused FTC (10yr)
    charitable_carryover_in: Decimal = Decimal(0)         # §170(d) 5yr carryover

    # Foreign taxes paid (for FTC) and itemized charitable already above.
    foreign_taxes_paid: Decimal = Decimal(0)

    # Education credits (Form 8863).
    #   - aotc_qualified_expenses: one entry per qualifying student (max 4),
    #     each amount up to $4,000 used. Refundable portion = 40%.
    #   - llc_qualified_expenses: aggregate per return (max $10,000 used).
    aotc_qualified_expenses: list[Decimal] = Field(default_factory=list)
    llc_qualified_expenses: Decimal = Decimal(0)

    # Above-the-line adjustments (Schedule 1 Part II), excluding ½ SE tax (engine adds it)
    hsa_deduction: Decimal = Decimal(0)
    other_adjustments: Decimal = Decimal(0)

    # Deduction choice
    itemized_deductions: Decimal | None = None   # None → use standard deduction

    # AMT (Form 6251) preference/adjustment add-backs. Most filers leave at 0.
    amt_preferences: Decimal = Decimal(0)        # e.g. private activity bond interest
    amt_adjustments: Decimal = Decimal(0)        # e.g. ISO bargain element

    # State (optional). When set, the engine also produces a `state_result` slot.
    state: str | None = None                     # ISO-3166-2 subdivision, e.g. "CA"
    # Optional sub-state locality (currently: NYC, YONKERS). Layered on top of state.
    locality: str | None = None

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
    collectibles_tax: Decimal = Decimal(0)        # 28%-cap rate
    unrecaptured_1250_tax: Decimal = Decimal(0)   # 25%-cap rate
    se_tax: Decimal
    additional_medicare_tax: Decimal
    niit: Decimal
    amt: Decimal = Decimal(0)                     # excess of tentative AMT over regular tax
    qbi_deduction: Decimal = Decimal(0)           # Form 8995 / 8995-A
    schedule_e_income: Decimal = Decimal(0)       # net rental + royalty + K-1 passthrough (post-PAL)
    passive_loss_disallowed: Decimal = Decimal(0) # losses parked on Form 8582 carryforward
    depreciation_current_year: Decimal = Decimal(0)        # total MACRS deduction this year
    depreciation_accumulated_out: dict[str, Decimal] = Field(default_factory=dict)  # per-property running total
    eitc: Decimal = Decimal(0)                             # Schedule EIC (refundable)
    aotc_nonrefundable: Decimal = Decimal(0)               # Form 8863 line 19
    aotc_refundable: Decimal = Decimal(0)                  # Form 8863 line 8 (40%)
    llc_credit: Decimal = Decimal(0)                       # Form 8863 line 19 (LLC piece)
    capital_loss_carryforward_out: Decimal = Decimal(0)  # §1212(b) — to use in a future year
    nol_carryforward_out: Decimal = Decimal(0)           # §172 — to use in a future year
    amt_credit_carryforward_out: Decimal = Decimal(0)    # Form 8801 — to use in a future year
    ftc_carryforward_out: Decimal = Decimal(0)           # §904 — to use in a future year (10y)
    charitable_carryover_out: Decimal = Decimal(0)       # §170(d) — to use in a future year (5y)
    credits: Decimal
    total_tax: Decimal

    refund_or_owed: Decimal               # positive = refund, negative = owed

    ordinary_bracket_fills: list[BracketFill]
    qualified_bracket_fills: list[BracketFill]
    steps: list[ComputationStep]

    # Optional state computation (populated when Return.state is set).
    state_result: "StateResult | None" = None

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
    # AMT (Form 6251) — optional so older year files without it still load.
    amt: dict[str, Any] | None = None
    # Schedule D worksheet cap rates — optional; defaults applied in engine.
    collectibles_rate: Decimal = Decimal("0.28")
    unrecaptured_1250_rate: Decimal = Decimal("0.25")
    # QBI deduction (Section 199A) — optional, defaults below if absent.
    qbi: dict[str, Any] | None = None
    # 401(k) / IRA / HSA limits used by the Advisor.
    contribution_limits: dict[str, Any] | None = None
    # EITC (Schedule EIC) parameters — optional, defaults to no-EITC if absent.
    eitc: dict[str, Any] | None = None
    # Education credits (Form 8863) — optional.
    education_credits: dict[str, Any] | None = None


class StateResult(BaseModel):
    """Output of a state-level tax computation."""
    model_config = ConfigDict(frozen=True)

    state: str
    state_agi: Decimal
    state_taxable_income: Decimal
    state_tax: Decimal
    state_bracket_fills: list[BracketFill]
    steps: list[ComputationStep]
    # Optional locality (NYC, Yonkers) on top of state tax.
    locality: str | None = None
    locality_tax: Decimal = Decimal(0)


class StateRules(BaseModel):
    """Parsed contents of a `tax_rules/state/{xx}/{year}.yaml` file."""
    model_config = ConfigDict(frozen=True)

    state: str
    year: int
    standard_deduction: dict[str, Decimal]
    ordinary_brackets: dict[str, list[tuple[Decimal, Decimal]]]
    # CA-style: capital gains taxed as ordinary income. Override per-state when needed.
    qualified_brackets: dict[str, list[tuple[Decimal, Decimal]]] | None = None
    # Optional state surcharges (e.g. CA Mental Health Services Tax).
    mental_health_services_tax: dict[str, Any] | None = None
    # Optional state-level long-term capital-gains excise tax (e.g. WA 7% over $262k).
    # Shape: {rate, threshold_by_status: {single, mfj, ...}, standard_deduction_by_status?}
    capital_gains_excise_tax: dict[str, Any] | None = None
    notes: str | None = None


# Forward-reference rebuild now that StateResult exists.
TaxResult.model_rebuild()
