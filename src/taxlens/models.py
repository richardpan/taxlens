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
    unemployment_compensation: Decimal = Decimal(0)  # 1099-G box 1 → Sch 1 line 7

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

    # Retirement income — 1099-R (pensions, IRA distributions) + SSA-1099.
    #   - pension_distributions_taxable: 1099-R box 2a for pensions/annuities
    #     (boxes 1 minus any non-taxable basis recovery). Flows directly into
    #     gross income on 1040 line 5b.
    #   - ira_distributions_taxable:    1099-R box 2a for IRA distributions
    #     (1040 line 4b). Tracked separately because the Roth/basis math
    #     differs and the Advisor uses it for RMD-style nudges.
    #   - social_security_benefits:     SSA-1099 box 5 (gross). Engine applies
    #     the 0%/50%/85% taxability formula (§86) using provisional income.
    #   - tax_exempt_interest:          1040 line 2a. Doesn't hit AGI directly
    #     but DOES count toward SS provisional income.
    #   - early_withdrawal_subject_to_penalty: taxable portion of distributions
    #     taken before age 59½ (1099-R code 1). Subject to 10% additional tax
    #     (Schedule 2 line 8 → Form 5329).
    pension_distributions_taxable: Decimal = Decimal(0)
    ira_distributions_taxable: Decimal = Decimal(0)
    social_security_benefits: Decimal = Decimal(0)
    tax_exempt_interest: Decimal = Decimal(0)
    early_withdrawal_subject_to_penalty: Decimal = Decimal(0)

    # IRA deductibility (§219). The deduction for Traditional IRA contributions
    # phases out by MAGI when the taxpayer (or spouse, for MFJ) is an active
    # participant in an employer retirement plan (W-2 box 13).
    #   - is_covered_by_workplace_plan:     primary filer is active participant
    #   - spouse_covered_by_workplace_plan: spouse is active participant (MFJ only)
    #   - taxpayer_age:                     used to apply the 50+ catch-up limit
    is_covered_by_workplace_plan: bool = False
    spouse_covered_by_workplace_plan: bool = False
    taxpayer_age: int | None = None

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

    # ACA Marketplace inputs for Premium Tax Credit (Form 8962). Leave at
    # defaults if you didn't receive marketplace coverage.
    marketplace_household_size: int = 0        # tax family size for FPL lookup
    marketplace_slcsp_annual: Decimal = Decimal(0)   # second-lowest-cost silver plan
    marketplace_plan_premium_annual: Decimal = Decimal(0)  # what you actually paid
    marketplace_advance_ptc_paid: Decimal = Decimal(0)     # APTC reported on 1095-A
    marketplace_state_is_ak: bool = False
    marketplace_state_is_hi: bool = False

    # Above-the-line adjustments (Schedule 1 Part II), excluding ½ SE tax (engine adds it)
    hsa_deduction: Decimal = Decimal(0)
    other_adjustments: Decimal = Decimal(0)
    # Common above-the-line adjustments (Schedule 1 Part II).
    student_loan_interest_paid: Decimal = Decimal(0)  # Sch 1 line 21; engine caps at $2500 and phases out by MAGI
    educator_expenses: Decimal = Decimal(0)           # Sch 1 line 11; capped per year ($250→$300 in 2022+)

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
    savers_credit: Decimal = Decimal(0)                    # Form 8880 (nonrefundable)
    actc: Decimal = Decimal(0)                             # Additional CTC (refundable)
    ptc_net: Decimal = Decimal(0)                          # net PTC (positive = refundable credit)
    ptc_excess_aptc_repayment: Decimal = Decimal(0)        # additional tax owed (Form 8962)
    personal_exemption_used: Decimal = Decimal(0)          # pre-TCJA only
    pease_reduction: Decimal = Decimal(0)                  # pre-TCJA only
    # Retirement income (Phase 2 federal coverage).
    social_security_taxable: Decimal = Decimal(0)          # taxable portion of SSA-1099 box 5
    pension_taxable: Decimal = Decimal(0)                  # 1040 line 5b (pensions/annuities)
    ira_taxable: Decimal = Decimal(0)                      # 1040 line 4b (IRA dist)
    early_withdrawal_penalty: Decimal = Decimal(0)         # Schedule 2 line 8 / Form 5329
    # IRA deductibility (§219) — claimed amount after limit + active-participant phaseout.
    ira_deduction_allowed: Decimal = Decimal(0)            # Schedule 1 line 20
    ira_deduction_disallowed: Decimal = Decimal(0)         # phased-out portion (becomes basis)
    student_loan_interest_deduction: Decimal = Decimal(0)  # Sch 1 line 21 (after $2500 cap + phaseout)
    educator_expense_deduction: Decimal = Decimal(0)       # Sch 1 line 11 (after annual cap)
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
    # Saver's Credit (Form 8880) — optional.
    savers_credit: dict[str, Any] | None = None
    # Premium Tax Credit (Form 8962) — optional.
    ptc: dict[str, Any] | None = None
    # Personal exemption (TY2017 and earlier). When set:
    #   {amount: 4050, phaseout_start: {...}, phaseout_complete: {...}}
    # Engine subtracts amount × (1 + spouse + dependents) from AGI.
    personal_exemption: dict[str, Any] | None = None
    # Pease limitation on itemized deductions (TY2017 and earlier).
    #   {threshold: {...}, rate: 0.03, max_reduction: 0.80}
    pease: dict[str, Any] | None = None
    # NOL pre-TCJA could offset 100% of taxable income; post-2017 capped at 80%.
    nol_full_offset: bool = False
    # Social Security benefits taxability (§86). Defaults to current-law
    # thresholds if absent. Engine applies tiered formula on provisional income.
    #   {base_threshold: {single, mfj, mfs, hoh, qss},
    #    second_threshold: {...},
    #    first_tier_rate: 0.50, second_tier_rate: 0.85}
    social_security: dict[str, Any] | None = None
    # 10% early-withdrawal penalty (§72(t)). Defaults to 0.10 if absent.
    early_withdrawal_penalty_rate: Decimal = Decimal("0.10")
    # Traditional IRA deduction (§219). When None, contributions are deductible
    # in full (legacy behavior). When set, applies contribution limit + active-
    # participant phaseout against MAGI.
    #   {contribution_limit: {under_50, fifty_plus},
    #    phaseout_covered: {single: {start, end}, mfj: {...}, mfs: {...}, ...},
    #    phaseout_spouse_covered_only: {mfj: {...}, mfs: {...}}}
    ira_deduction: dict[str, Any] | None = None
    # Student loan interest deduction (§221). When None, deduction allowed in full.
    #   {max_deduction: 2500,
    #    phaseout: {single: {start, end}, mfj: {...}, mfs: {disabled: true}, ...}}
    student_loan_interest: dict[str, Any] | None = None
    # Educator expense deduction (§62(a)(2)(D)). When None, no cap enforced.
    #   {per_educator_cap: 300}  # doubled on MFJ when both spouses are educators
    educator_expense: dict[str, Any] | None = None


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
