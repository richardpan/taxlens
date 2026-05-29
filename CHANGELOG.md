# Changelog

All notable changes to TaxLens.

## [0.20.0] — 2026

### Added — Phase-2 federal credits (Forms 2441, 5695, 8936)

Three of the most commonly-claimed credits that were missing now flow
through the engine end-to-end.

**Form 2441 — Child & Dependent Care Credit:**
- New inputs: `dependent_care_expenses`, `num_qualifying_care_persons`,
  `spouse_earned_income`.
- Engine enforces:
  - Expense cap ($3k for one qualifying person, $6k for two or more).
  - Earned-income limit (MFJ: lesser of two spouses' earned income).
  - §21(a)(2) stepped rate schedule (35% → 20% in 1% / $2,000 AGI steps).
- TY2021 (ARPA) special: $8k / $16k caps, 50%-floor schedule, **refundable**.
  Surfaced via `dependent_care_credit_refundable` on `TaxResult`.

**Form 5695 — Residential Clean Energy Credit (solar / geothermal /
battery / wind):**
- New input: `residential_clean_energy_cost`.
- Year-accurate rate: 30% pre-2020, 26% in 2020–2021, 30% from 2022
  (Inflation Reduction Act restoration).

**Form 8936 — Clean Vehicle Credit:**
- New inputs: `clean_vehicle_credit_claimed`, `clean_vehicle_is_used`.
- Pre-2023: pass-through (no MAGI cap).
- 2023+: MAGI cap enforced ($150k single / $300k MFJ for new vehicles;
  $75k / $150k for used). When AGI exceeds the applicable cap, the
  entire credit is disqualified per §30D(f)(10).

**Tests:** +16 dedicated (267 total). Covers DCC caps + rate tiers +
MFJ earned-income limit + ARPA refundable path; RCE year-aware rates;
CVC MAGI cap behavior (new vs used, MFJ vs single, pre-2023 pass-through);
integration test combining all three.

When YAML lacks the relevant config block the engine produces $0 for
that credit (back-compat for legacy YAML).

## [0.19.0] — 2026

### Added — Common income & above-the-line adjustments

Three commonly-encountered items closed in this drop:

- **Unemployment compensation** (1099-G box 1, Schedule 1 line 7) — new
  `Return.unemployment_compensation` field, included in gross income.
- **Student loan interest deduction (§221)** — new
  `Return.student_loan_interest_paid`, capped at $2,500/year, MAGI
  phaseout via linear ramp, fully disabled for MFS per §221(e)(2).
  YAML brackets supplied for every year 2015–2024 (2024: single
  $80k–$95k, MFJ $165k–$195k).
- **Educator expense deduction (§62(a)(2)(D))** — new
  `Return.educator_expenses`, per-educator cap ($250 → $300 in 2022),
  auto-doubled on MFJ when both spouses are educators (caller passes
  the combined paid amount).

New `TaxResult` fields: `student_loan_interest_deduction`,
`educator_expense_deduction`.

When YAML lacks `student_loan_interest:` or `educator_expense:` the
engine falls back to full-deduction behavior (back-compat).

**Tests:** +13 (251 total). Covers unemployment, SLI tiers + MFS
prohibition + MFJ thresholds, educator caps + historical $250 cap +
MFJ doubling.

## [0.18.0] — 2026

### Added — Traditional IRA deduction phaseout (§219(g))

The engine previously treated every dollar of Traditional IRA contributions
as an above-the-line deduction with no limit and no active-participant
phaseout. That's correct only for filers (and spouses) NOT covered by a
workplace retirement plan. This release closes the gap end-to-end.

**Inputs (new optional `Return` fields):**
- `is_covered_by_workplace_plan` — primary filer is an active participant
  (W-2 box 13 "Retirement plan" checked).
- `spouse_covered_by_workplace_plan` — relevant only on MFJ/MFS; uses the
  higher $230k–$240k phaseout window (2024 figures).
- `taxpayer_age` — drives the 50+ catch-up contribution limit.

**Engine:**
- New `_compute_ira_deduction()` helper. Approximates MAGI as
  AGI-before-IRA-deduction (other above-the-line adjustments still subtracted),
  applies the annual §219(b) contribution limit (with 50+ catch-up), then the
  §219(g) active-participant phaseout via linear ramp.
- The disallowed portion is surfaced separately as `ira_deduction_disallowed`
  on `TaxResult` (economically becomes nondeductible basis in the IRA).
- When `rules.ira_deduction` is absent the engine keeps legacy
  "deductible-in-full" behavior — backwards compatible.

**YAML config:** all 10 federal years (2015–2024) now ship year-accurate
contribution limits and phaseout brackets (e.g. 2024: $7k/$8k, single covered
$77k–$87k, MFJ covered $123k–$143k, spouse-covered-only $230k–$240k, MFS
$0–$10k).

**Tests:** +11 dedicated IRA deduction tests (238 total, all passing).
Covers full deduction when uncovered, contribution capped at limit, 50+
catch-up, full/partial/zero deduction across the phaseout range, MFJ
thresholds, spouse-covered-only window, and historical limits (2015 $5500,
2019 $6000).

## [0.17.0] — 2026

### Added — Retirement income (federal coverage gap)

Five new optional `Return` inputs and full engine handling:

- **`social_security_benefits`** — taxability computed under IRC §86 with
  the canonical two-tier provisional-income test (base 25k single /
  32k MFJ; second 34k / 44k). Tax-exempt interest is included in
  provisional income even though it stays out of AGI. MFS thresholds
  are $0 (always 85%).
- **`tax_exempt_interest`** — feeds §86 PI only, never AGI.
- **`pension_distributions_taxable`** (1099-R box 2a — qualified plans).
- **`ira_distributions_taxable`** (1099-R IRA distributions).
- **`early_withdrawal_subject_to_penalty`** — drives the §72(t) 10%
  additional tax (Form 5329 short form). Added to `total_tax` and
  surfaced as a separate audit-trail step.

New `TaxResult` fields: `social_security_taxable`, `pension_taxable`,
`ira_taxable`, `early_withdrawal_penalty`.

YAML: every federal year (2015-2024) now ships a `social_security:`
block. Thresholds have been statutory since 1993, so the same numbers
apply across the entire decade of coverage.

Tests: **+10 retirement-income tests** (227 total, all passing) covering
all three tiers of SS taxability for both single and MFJ, the
tax-exempt-interest interaction, mixed retiree round-trip, and the
early-withdrawal penalty.

## [0.16.1] — 2026

### Fixed — "No federal rules for tax year" in packaged builds

**Critical hotfix.** Every PDF import in the Electron/installer builds was
failing with `No federal rules for tax year ...` because the `tax_rules/`
directory lived at the **repo root** rather than inside the Python package.
`rules.py` resolved the path via `Path(__file__).resolve().parents[2] /
"tax_rules"` — which worked in development (where it points back to the
repo root) but broke in every packaged distribution (wheel, PyInstaller,
Electron) where there is no `tax_rules` directory two levels above the
module.

Fix:
- Moved `tax_rules/` to `src/taxlens/tax_rules/` so it's inside the
  package and gets bundled into the wheel automatically.
- Updated `rules.py` to look at `Path(__file__).parent / "tax_rules"`.
- Updated `desktop/scripts/build_backend.py` and `build-backend.ps1`
  PyInstaller `--add-data` arguments to point at the new location and
  bundle under `taxlens/tax_rules/` so the runtime path resolves
  correctly inside the frozen executable too.
- Updated `README.md` references.

Verified: the v0.16.1 wheel now contains all 10 federal year YAMLs plus
state and locality YAMLs under `taxlens/tax_rules/`. **217 tests passing.**

## [0.16.0] — 2026

### Added — UX quick wins: better errors, delete returns

- **Import errors are now actionable.** The front-end `api()` helper now
  parses the FastAPI `detail` field, so failed imports show the actual
  diagnostic (`Could not parse foo.pdf: ValueError: Could not detect tax
  year ...`) instead of a raw JSON envelope. Each failed file row gets a
  collapsible **"Show technical details"** disclosure with the traceback
  tail and a **"Copy diagnostic"** button — copy it into a GitHub issue
  and we (or you) can extend importer patterns from there.
- **Delete returns from the dashboard.** Each row in the Returns table
  has a trash icon that prompts to confirm, calls `DELETE /api/returns/{id}`,
  invalidates the cache, and re-renders. Useful for cleaning up a bad
  PDF import without nuking the whole DB.
- Drag-and-drop multi-file import was already supported — verified it
  still works alongside the new error-detail rendering.

### Tests

- 3 new API tests in `tests/test_api.py` cover the 422 diagnostic path,
  the `/api/debug/extract` graceful-failure behaviour, and the full
  delete round-trip. **217 tests passing.**

## [0.15.2] — 2026

### Added — TurboTax, H&R Block, FreeTaxUSA PDF compatibility

Third-party tax software exports follow the IRS 1040 layout but each has
its own quirks: explicit `Filing Status: X` markers instead of checkbox
indicators, column-split layouts where pdfplumber emits label and amount
on adjacent lines, and cover/summary pages preceding the actual form.
This release hardens the importer for all three major vendors:

- **Filing-status detection rewritten** with three tiers: (1) explicit
  "Filing Status: X" / "Your filing status is X" markers (TurboTax,
  H&R Block, FreeTaxUSA cover pages); (2) checkbox indicators on the
  actual form; (3) count-based fallback (the selected status appears more
  times in the document than the option labels do). Eliminates a class of
  false positives where the first-listed option (MFJ) was always picked
  because all 5 labels appear once on the IRS form.
- **Next-line money fallback** in `_first_money_after`: if the label line
  has no amount, look at the next non-empty line (column-split layouts
  in TurboTax/H&R Block PDFs). Uses a stricter ≥3-digit or decimal
  money pattern there to avoid grabbing stray line references.
- **New test fixtures** in `tests/third_party_pdfs.py` generate mock
  TurboTax, H&R Block, and FreeTaxUSA-style PDFs.
- **9 new golden tests** in `tests/test_third_party_pdfs.py` cover all 5
  filing statuses × all 3 vendors plus year detection across vendor
  header styles. **214 tests passing.**

## [0.15.1] — 2026

### Fixed — PDF upload no longer returns opaque 500 errors

Real-world third-party tax-software PDFs (FreeTaxUSA, TurboTax, H&R Block)
follow the IRS Form 1040 layout but use the exact statutory line phrasing
(e.g. line 1a reads *"Total amount from Form(s) W-2, box 1"* — there is no
word "Wages"). Earlier `LINE_PATTERNS` required keywords the IRS form
doesn't actually print, and any other unexpected exception (encrypted PDF,
pdfplumber crash, pydantic validation error) bubbled up as an unhelpful
**500 Internal Server Error**.

This release:

- **Broadens `LINE_PATTERNS`** to match the canonical IRS phrasing used by
  third-party software — line 1a (W-2 box 1), line 1z (sum of 1a–1h), line
  24 (Add lines 22 and 23), line 10 (Adjustments from Schedule 1), and
  more permissive variants for AGI / taxable income / withholding.
- **Broadens `YEAR_PATTERNS`** to handle "2023 Form 1040", "OMB No.
  1545-0074 2023", "For the year Jan 1 – Dec 31, 2023", and "Tax Year 2023".
- **Wraps `pdfplumber.open`** to detect encrypted/password-protected PDFs
  and emit a clear 400 instead of a 500.
- **Wraps `Return()` construction** so a bad field value yields a
  diagnostic 400 listing the detected year/status/fields, not a 500.
- **Broadens the import endpoint** (`POST /api/returns/import`) to catch
  every exception and return HTTP 422 with the exception type, message,
  and traceback tail — so users immediately see what failed.
- **New diagnostic endpoint** `POST /api/debug/extract` returns the raw
  per-page text pdfplumber sees, so failing imports can be debugged
  without server access (and without sharing the PDF with anyone).

No behavior changes for already-working PDFs. **205 tests passing.**

## [0.15.0] — 2026

### Added — "What changed?" diff with driver attribution

The **Compare** tab now includes a "What changed?" panel below the side-by-side
table that explains *why* total tax shifted between two returns. For every
input that differs between the two returns, the engine is re-run with that
single field swapped to measure its independent contribution to the total-tax
delta. Rule-change attribution (when the two returns are different tax years)
gets its own line so you can see "TCJA cut my tax by $4,200" vs "but my AGI
grew by $20k which added $4,800."

Each driver is rendered as a horizontal bar (right for tax increases, left for
decreases), color-coded by kind (income / deduction / credit / payment /
rules), with a magnitude label and the raw values that changed. The
unattributed residual (from non-linear interactions like AMT crossover or
bracket boundaries) is shown at the bottom.

### Added — `GET /api/diff?left=&right=`

New REST endpoint returns:
- `overall_tax_delta`
- ordered list of `drivers` with `attributed_tax`, `kind`, `left`, `right`
- `residual` (delta minus sum of attributions)
- `left` / `right` summary block

### Added — Historical-year PDF round-trip goldens

Three new PDF goldens prove the importer + engine flow works end-to-end for:
- **TY2018** — first post-TCJA year (lower brackets, doubled SD)
- **TY2017** — last pre-TCJA year (verifies personal exemption activates)
- **TY2021** — ARPA expanded CTC (verifies fully-refundable flow)

### Tests

7 new tests (`test_v15_diff_and_pdf.py`): 4 for the diff service + 3 for
historical PDF round-trips. **205 tests passing.**

## [0.14.0] — 2026

### Added — Historical accuracy: 10 years of federal rules (TY2015-2024)

TaxLens can now compute federal tax for any year from 2015 through 2024.
Each year ships its own `tax_rules/federal/{year}.yaml` with the exact IRS
brackets, standard deductions, AMT/QBI thresholds, SS wage bases, CTC
parameters, and contribution limits published in the corresponding Rev. Proc.

**New historical-year files** (8 added):
- TY2015, TY2016, TY2017 — **pre-TCJA**
- TY2018, TY2019, TY2020, TY2021, TY2022 — **post-TCJA**

### Added — Pre-TCJA engine path

For TY2015-2017, the engine now applies three pre-TCJA features that the
prior code didn't model. These are gated entirely by the rules YAML, so
post-TCJA years are unaffected.

- **Personal exemption** (§151) — $4,000 (2015) / $4,050 (2016, 2017)
  multiplied by (1 + spouse + dependents), with PEP step-phaseout (2% per
  $2,500 over threshold, fully phased out at threshold + $122,500).
- **Pease limitation** (§68) on itemized deductions — 3% of AGI over the
  Pease threshold, capped at 80% of itemized.
- **CTC at $1,000/kid** with a $3,000 earned-income threshold for ACTC
  (no $1,400 per-kid refundable cap; refundable portion = 15% × (earned − $3k)
  capped at total CTC).

### Added — ARPA-expanded 2021 CTC (simplified)

TY2021 models the post-ARPA fully-refundable CTC at $3,000/child with no
earnings test and no per-kid cap. **Caveat:** ARPA also paid $3,600 for
children under 6, which requires an age field we don't track per-kid;
users with under-6 kids should cross-check against their actual Schedule 8812.

### Added — TaxResult fields

- `personal_exemption_used` — Decimal, surfaces the pre-TCJA exemption amount
- `pease_reduction` — Decimal, surfaces the pre-TCJA Pease cut

### Added — `Rules` schema fields (all optional)

- `personal_exemption: {amount, phaseout_start, phaseout_complete}`
- `pease: {threshold, rate, max_reduction}`
- `nol_full_offset: bool` — pre-TCJA NOL could fully offset taxable income;
  post-2017 capped at 80% (existing default)
- `ctc.actc_no_kid_cap` — pre-TCJA ACTC has no per-kid cap
- `ctc.actc_full_refund` — ARPA 2021 path (no earnings test, no per-kid cap)
- `ctc.actc_earned_threshold` / `ctc.actc_rate` — pre-TCJA was $3,000 / 15%

### Tests

18 new tests in `test_v14_historical_years.py` lock the bracket walk,
standard deduction, and personal exemption for every year 2015-2022
against hand-calculated values. TCJA boundary, pre-TCJA Pease, pre-TCJA
$3k ACTC threshold, ARPA fully-refundable CTC, and SS wage base drift
are all individually verified. **198 tests passing.**

### Caveats

- State YAMLs only exist for TY2024. Computing historical-year state tax
  requires multi-year state rule files (deferred — large surface area).
- Pre-TCJA EITC, Saver's, and education credits weren't backfilled into
  the historical YAMLs (the engine gates these as optional; they simply
  return 0 if absent). Federal regular tax math is fully accurate.

## [0.13.0] — 2026

### Added — multi-year trend visualizations

The **Trends** tab now includes two new sections in addition to the
existing AGI/tax line chart, effective-vs-marginal rate chart, and
income-composition stacked bars:

- **Tax composition by year** — stacked bars breaking out what drives
  total tax each year: ordinary + qualified tax, AMT add-on, SE tax,
  NIIT (3.8%), Additional Medicare (0.9%), excess APTC repayment.
- **Year-over-year change table** — side-by-side metrics across all
  imported years with per-step deltas (AGI, taxable income, total
  tax, effective rate in percentage points, refund/owed, wages, LTCG,
  total credits). Increases in tax are red, decreases green.

### Housekeeping

- **httpx2 added to dev dependencies** to clear the Starlette
  TestClient deprecation warning. No production dependency change.

## [0.12.0] — 2026

### Added — 8 more state YAMLs (21 states total)

**No-tax states** (stubs): NV, SD, WY, AK, TN, NH. (Engine returns $0
state tax cleanly when these are selected. NH note: wage tax never
existed; the I&D tax was phased to 3% in 2024 and repealed in 2025.)

**New income-tax states:**
- **Wisconsin** — 4-bracket graduated (3.5% / 4.4% / 5.3% / 7.65%),
  with the maximum standard deduction approximated (WI's actual SD
  phases out with AGI).
- **Indiana** — flat 3.05% (down from 3.15% in 2023). Standard-deduction
  slot used to approximate the personal-exemption baseline.

### Tests
- 180 passing (was 172). 8 new state tests including bracket-walk
  verification for WI and the flat-rate check for IN.

## [0.11.0] — 2026

### Added — Saver's Credit, ACTC, Premium Tax Credit

- **Saver's Credit (Form 8880)** — nonrefundable retirement-savings credit.
  AGI-tiered 50% / 20% / 10% rates by filing status. Up to $2,000 of
  contributions per person counted ($4,000 MFJ), so max credit = $1,000 /
  $2,000. Pulls from existing `traditional_401k`, `roth_401k`,
  `traditional_ira`, and `roth_ira` contribution fields.
- **Additional Child Tax Credit (ACTC, Form 8812)** — refundable. CTC now
  splits cleanly: nonrefundable portion is used against tax first, and the
  unused portion flows refundable as ACTC, capped at `$1,700 × kids`
  (TY 2024; $1,600 for TY 2023) and at `15% × (earned − $2,500)`. This
  unlocks meaningful refunds for low-income families who couldn't fully
  use the nonrefundable CTC.
- **Premium Tax Credit (Form 8962)** — full APTC reconciliation. Inputs:
  household size, SLCSP annual premium, actual plan premium paid, advance
  PTC paid. Engine computes %-of-FPL (2023 FPL for TY 2024 returns),
  looks up the piecewise-linear applicable figure (post-ARPA/IRA: no
  400% cliff, 8.5% cap), and reconciles. If PTC > APTC: net positive
  flows as a refundable payment. If APTC > PTC: excess flows as
  additional tax, **capped per FPL bucket** below 400% ($375/$750,
  $975/$1,950, $1,625/$3,250 for TY 2024).

### Added — Return inputs
- `marketplace_household_size`, `marketplace_slcsp_annual`,
  `marketplace_plan_premium_annual`, `marketplace_advance_ptc_paid`

### Added — TaxResult outputs
- `savers_credit`, `actc`, `ptc_net`, `ptc_excess_aptc_repayment`

### Engine
- CTC computation refactored to return `(total, kid_portion)` so ACTC
  can compute its refundable ceiling correctly with phaseout applied.
- Excess APTC repayment is added on the tax side (Form 1040 line 17/2);
  refundable PTC is added on the payments side (line 31).

### UI
- New cards on Year detail for ACTC, Saver's Credit, PTC refund, and
  excess APTC repayment — visible only when non-zero.

### Tests
- 172 passing (was 156). 16 new tests covering Saver's 50/20/10 tiers,
  contribution caps, ACTC earnings-test, ACTC zero when CTC absorbed,
  PTC simple refund, exact APTC match, capped excess repayment, no-cliff
  above 400% FPL, and total-tax impact.

## [0.10.0] — 2026

### Added — refundable credits (EITC + AOTC)
TaxLens now does the two biggest refundable credits:

- **Earned Income Tax Credit (Schedule EIC)** — refundable. Trapezoid by
  number of qualifying children (0/1/2/3+), with phase-in / plateau /
  phase-out. Phase-out is against the **greater of** earned income or AGI
  (per IRS Pub. 596 to prevent gaming with investment income).
  Disqualifiers: filing MFS, or investment income above the annual limit
  ($11,600 for TY 2024; $11,000 for TY 2023).
- **American Opportunity Tax Credit (AOTC, Form 8863)** — per qualifying
  student (max 4). 100% of first $2,000 + 25% of next $2,000 = $2,500
  max per student. **40% refundable** ($1,000), 60% nonrefundable. MFS
  disallowed. MAGI phaseout: single $80–90k / MFJ $160–180k.
- **Lifetime Learning Credit (LLC, Form 8863)** — nonrefundable. 20% of
  qualified expenses (capped at $10,000 pool per return) = $2,000 max.
  Same MAGI window as AOTC.

### Added — Return inputs
- `aotc_qualified_expenses: list[Decimal]` — one entry per qualifying student
- `llc_qualified_expenses: Decimal` — single per-return pool

### Added — TaxResult outputs
- `eitc`, `aotc_nonrefundable`, `aotc_refundable`, `llc_credit`

### Engine
- Refundable credits now flow into the payments side of the refund
  computation (just like Form 1040 line 27 EITC and line 29 AOTC), so
  they can push `refund_or_owed` positive even when total tax is $0.

### UI
- New cards on the Year detail page for EITC, AOTC (refundable + nonrefundable
  shown separately), and LLC — visible only when non-zero.

### Tests
- 156 passing (was 132). 24 new tests: 11 EITC (phase-in, plateau,
  phase-out, full phaseout, MFS disqualifier, investment income limit,
  zero earned income, childless plateau, refundability, 2023 sanity,
  3-kid MFJ plateau), 13 education credit (AOTC tiered math, multi-student,
  per-student cap, LLC math, expense cap, phaseout midpoint, MFJ window,
  MFS disqualifier, refundability, EITC stacking).

## [0.9.0] — 2026

### Added — Schedule E MACRS depreciation (Form 4562)
TaxLens now does per-property depreciation for rental real-estate. Each
`RentalProperty` carries cost basis (land excluded), property type,
in-service year/month, and prior accumulated depreciation. The engine:

- Computes current-year **straight-line mid-month** depreciation for
  **residential** (27.5y) and **nonresidential** (39y) real property.
- Supports **5-year** (appliances, 200%DB→SL) and **15-year** (land
  improvements, 150%DB→SL) personal-property classes via the exact
  Rev. Proc. 87-57 half-year-convention tables.
- Subtracts depreciation from `rental_net_income` **before** the Form 8582
  passive-loss logic — so depreciation losses can be absorbed by the
  $25k active-participation allowance.
- On disposal (`disposed_year` == tax_year), prorates the exit-year
  deduction mid-month, computes total realized gain, and routes the
  accumulated-depreciation portion into **unrecaptured §1250 gain**
  (taxed at the 25% cap rate via the existing Sch D worksheet stack);
  any excess flows into long-term capital gains.
- Threads per-property `prior_accumulated_depreciation` across years in
  the service-level carryforward reflow (just like NOL/PAL/etc).

### Added — new `TaxResult` fields
- `depreciation_current_year` — total MACRS deduction this year
- `depreciation_accumulated_out` — per-property accumulated total, used
  by the next year's reflow to update each property's prior accumulated
  depreciation automatically.

### UI
- New "MACRS depreciation (Form 4562)" card on the Year detail page,
  shown only when non-zero.

### Tests
- 132 passing (was 122). 10 new tests cover residential mid-month
  (Jan / Jul edges, full middle year), nonresidential 39y, the 5-year
  HY table (years 0 and 1), disposal recapture math, and engine
  integration for single property / multiple properties / no-op.

## [0.8.0] — 2026

### Added — realistic multi-page PDF golden fixtures
- New `tests/realistic_1040.py` generator emits a full Form 1040 page 1+2
  plus Schedule 1, 2, 3, and B in the same layout style as real IRS forms,
  with proper section headers and `Line N <label> ...... $value` rows.
- 5 new round-trip tests in `tests/test_pdf_golden.py` covering MFJ with
  dividends + Sch B, Schedule C self-employment, high-income with AMT + FTC,
  retiree, and a parametrized sweep over all 5 filing statuses.

### Fixed — PDF importer line-bridging bug
- The previous regex bridge `[^\n\r$0-9-]*` would happily skip across
  intervening text and pick up stray digits like `-2` from "Form W-2 box 1"
  or `22` from "(add lines 22 and 23)" instead of the actual money value.
- `_first_money_after` now matches the label and value on the **same
  physical line** and takes the **last** money string on that line — robust
  against IRS-style labels that mention other line numbers in parentheses.

### Added — broader PDF line coverage
- New importer patterns for **Form 1040 Line 8** (additional income from
  Schedule 1), **Line 26** (other adjustments), **Schedule 3 Line 1**
  (foreign tax credit), and a broader **Schedule SE** income capture.

### Tests
- 122 passing (was 113).

## [0.7.0] — 2026

### Added — multi-year carryforward suite
TaxLens now threads **six** different carryforwards automatically across
imported years (chain resets only on year gaps >1):

- **NOL §172** — net operating losses, post-TCJA 80%-of-taxable-income cap
  on use, excess carries forward indefinitely.
- **Passive loss §469** — already computed each year; now reflowed
  multi-year (Form 8582 chain).
- **AMT credit (Form 8801)** — prior-year AMT (e.g. from ISO exercises)
  becomes a credit usable in years where AMT = 0. Current-year AMT adds
  to next year's credit.
- **Foreign Tax Credit §901/§904** — `foreign_taxes_paid` field; simplified
  §904 limit (capped at regular tax); excess carries forward.
- **Charitable contribution §170(d)** — excess over 60% AGI cash cap when
  itemizing carries forward; surviving carryover preserved on
  standard-deduction years.
- **Capital loss §1212(b)** — already shipped in v0.5; documented here as
  part of the unified carryforward story.

### Added — locality coverage
- **6 Maryland county piggyback income taxes** (Montgomery, Baltimore City,
  Baltimore County, Prince George's, Howard, Anne Arundel) at their 2024
  rates (2.70%–3.20% on MD taxable income).

### Added — UI
- Year-detail tab now surfaces every active carryforward as its own card.

### Tests
- 9 new tests; **113 total, all passing.**

## [0.6.0] — 2026

### Added
- **8 more 2024 state YAMLs**: PA, OH, NC, AZ, MN, CO, MI, MD.
  Total state coverage is now **19** (CA, NY, IL, TX, FL, WA, MA, OR, NJ,
  VA, GA, PA, OH, NC, AZ, MN, CO, MI, MD).
- **2023 backfills** for the v0.5 states (MA, OR, NJ, VA, GA) — including
  GA's pre-flat-tax graduated schedule.
- **$500 Credit for Other Dependents (ODC)** — `other_dependents: int`
  on the `Return` model. Shares the CTC phase-out ($50/$1k AGI over
  $200k single / $400k MFJ).
- **UI surfacing** for two previously-invisible v0.5 features:
  - Capital-loss carryforward to next year shows as a card on Year detail
    when non-zero.
  - NYC/Yonkers locality tax shows as its own card alongside state tax.
- 16 new tests covering all 13 state YAMLs and ODC math. **104 tests
  passing total.**

## [0.5.1] — 2026

### Added
- **Free trust artifacts on every installer** — no paid certs required:
  - **SHA-256 checksum** (`*.sha256`) published next to each installer.
  - **GitHub build-provenance attestation** (Sigstore-signed) — verify
    with `gh attestation verify <file> --repo richardpan/taxlens`.
  - **Ad-hoc `codesign` on macOS** — silences the "TaxLens is damaged"
    error on Apple Silicon. (Right-click → Open still needed once.)
- Expanded README "Download" section with verification commands and
  first-launch warning walkthrough for each OS.

### Decided
- TaxLens will remain **unsigned** for the foreseeable future. EV/Apple
  Developer certs cost $100–300/yr and we don't want users or maintainers
  to fund that. Provenance + checksums are our verification story.

## [0.5.0] — 2026

### Added
- **Multi-year capital-loss carryforward** — when consecutive years are
  imported, §1212(b) losses now flow forward automatically. Each return
  surfaces `capital_loss_carryforward_out`, and the service re-flows the
  chain on every import (resets on year gaps >1).
- **NYC and Yonkers locality income tax** — set `locality: "NYC"` or
  `"YONKERS"` on a NY return. NYC uses its own bracket table; Yonkers
  applies the 16.75% resident surcharge to NY state tax. Locality tax is
  added to `state_result.state_tax` and broken out in
  `state_result.locality_tax`.
- **5 more state YAMLs (2024)**: Massachusetts (5% + 4% millionaire surtax),
  Oregon (graduated to 9.9%), New Jersey (8-bracket graduated to 10.75%),
  Virginia (graduated to 5.75%), Georgia (flat 5.39%).
- **Trends tab** in the web UI — three new visualizations: AGI vs total
  tax over time, effective vs marginal rate, and stacked income
  composition by year. Pure SVG, no chart library dependency.
- 10 new tests covering carryforward chaining, NYC/Yonkers, and the new
  state YAMLs. **88 tests total, all passing.**

## [0.4.0] — 2026

### Added
- **Cross-OS installers via GitHub Actions** (`.github/workflows/release.yml`) —
  on every `v*.*.*` tag, builds Windows NSIS `.exe`, macOS `.dmg`, and Linux
  `.AppImage` installers on native runners and uploads them as release assets.
  End users can now click-to-install with no Python required.
- **Tests CI workflow** (`.github/workflows/tests.yml`) — matrix runs of
  pytest on Win/Mac/Linux × Python 3.11/3.12 on every push and PR.
- **Roth conversion simulator** — `POST /api/returns/{id}/simulate/roth`
  returns marginal tax cost of converting traditional → Roth this year.
- **Tax-loss harvest simulator** — `POST /api/returns/{id}/simulate/tlh`
  returns same-year tax savings of realizing a given LT loss.
- **Planner tab** in the web UI with side-by-side Roth + TLH cards.
- **WA capital-gains excise tax (RCW 82.87)** — 7% over $262k (2024) /
  $250k (2023). State YAMLs gained a `capital_gains_excise_tax:` block.
- **2023 state YAML backfills** for NY, IL, TX, FL, WA.
- **Python OCR extra** — `pip install taxlens[ocr]` now installs
  `pytesseract` + `pdf2image` (Tesseract & Poppler binaries still external).
- **§1211(b) capital-loss limitation in engine** — net cap losses now
  correctly cap at -$3,000 against ordinary income, with a recorded step.
  Excess carryforward is surfaced in the trace (not yet auto-applied to
  future years; that's the multi-year carryforward feature for v0.5).
- **Cross-OS PyInstaller build script** (`desktop/scripts/build_backend.py`)
  — replaces the Windows-only PowerShell script with a portable Python one.

### Fixed
- Engine no longer crashes when a Return has a net capital loss (pre-fix it
  raised `ValueError: amount must be non-negative` from the qualified-rate
  bracket walk).
- AMT and qualified-rate computations now floor net LT+qual at $0 to avoid
  taxing negative income at preferential rates.

### Tests
78 passing (up from 72 in v0.3.0). New tests cover both simulators and the
WA capital-gains tax.

### Notes
- Installers from the v0.4.0 release are **unsigned** (no SmartScreen /
  Gatekeeper bypass yet). Users see a one-time warning on first launch.
  See `docs/signing.md` for the production signing flow.
- macOS installers built in CI are universal-ish but currently x86_64-only
  (the GitHub-hosted macOS runner image determines the slice). For Apple
  Silicon builds, run `npm run dist` locally on an M-series Mac.

## [0.3.0] — 2025

### Added
- **Schedule E (rentals & royalties)** with Form 8582 simplified PAL allowance
  ($25k, phased out $100k–$150k MAGI) and a suspended-passive-loss carryforward
  field. Net rental income flows into AGI and (where applicable) NIIT.
- **K-1 passthrough income** — ordinary business income, interest, ordinary &
  qualified dividends, LT/ST capital gains, §199A QBI, and an SSTB flag.
- **§199A QBI deduction** with SSTB phaseout (single 191,950–241,950 / MFJ
  383,900–483,900 for 2024), 20%-of-QBI cap, and overall-taxable-income limit.
- **ISO bargain element → AMT preference** (Form 6251 Line 2i) — drives AMT
  for early-exercise scenarios.
- **California Mental Health Services Tax** — 1% surcharge over $1M (single)
  / $1.376M (MFJ) for 2024.
- **Tax Savings Advisor ✨** — new tab with a single-year + cross-year rule
  engine. Single-year rules: max 401(k), max HSA, backdoor Roth (warning
  vs. opportunity), bunching donations, tax-loss harvesting, ISO/AMT
  staggering, S-corp election candidate, estimated-tax safe harbor, QBI
  income smoothing. Cross-year rules: Roth conversion window, persistent
  over-withholding, rising-gains TLH discipline.
- **More state rule sets** — NY (§601 brackets), IL (4.95% flat), TX / FL /
  WA (no income tax).
- **Broker CSV importer (1099-B)** — Fidelity / Schwab / generic. Header
  aliasing, money-format tolerance, automatic LT vs. ST inference from
  acquired/sold dates, optional collectibles / §1250 type detection.
- **OCR fallback for scanned PDFs** — uses `pytesseract` + `pdf2image` when
  pdfplumber finds no text. Soft-dep: if Tesseract / Poppler aren't on PATH
  the import falls back gracefully and surfaces a warning.
- **Demo mode** — bundled 2-year + 1-spouse-pair anonymized sample returns
  (`POST /api/demo/load`, "Try demo" button on Import) so new users see a
  populated dashboard in one click.
- **PyInstaller-bundled backend** for the desktop app — Electron now prefers
  `desktop/bin/taxlens-backend.exe`, so end-users no longer need a Python
  install. Build with `desktop/scripts/build-backend.ps1`.
- **Signing & notarization docs** (`docs/signing.md`) covering Windows EV
  signtool flow and macOS notarytool.
- **Hypothesis property tests** for bracket walks and idempotence.

### Changed
- `compute()` now threads Schedule E net + QBI through AGI / taxable income.
- `_compute_amt` adds ISO bargain element to AMTI.
- `_compute_niit` includes K-1 investment income + rental/royalty.
- Federal 2023 + 2024 YAMLs gained `qbi:` and `contribution_limits:` sections.
- CA 2023 + 2024 YAMLs gained `mental_health_services_tax:`.
- `single_2023_se` fixture expected values updated to reflect new QBI deduction.

### Notes
- WA 7% capital-gains tax (>$262k) is **not** yet modeled.
- State YAMLs for NY / IL / TX / FL / WA only ship 2024; backfill in v0.4.
- The Electron app is still unsigned in the GitHub release artifacts; see
  `docs/signing.md` for the production flow.

### Tests
72 passing (up from 39 in v0.2.0), including:
- 8 Schedule E / K-1 / QBI tests
- 5 Hypothesis property tests
- 11 Advisor rule tests (incl. buggy-rule isolation)
- 8 state YAML + broker CSV tests
- 1 demo-loader sanity test

## [0.2.0] — 2025

### Added
- **AMT (Form 6251, simplified)** — AMTI = taxable income + preferences + adjustments;
  exemption with 25%/$1 phaseout above the rev-proc threshold; 26%/28% tentative
  rates around the rate-break. AMT owed = max(0, tentative − regular).
  New `Return.amt_preferences` and `Return.amt_adjustments` inputs.
- **Schedule D capital-gain worksheet** — 4-bucket stacking model:
  ordinary income → unrecaptured §1250 gains (25% cap) → collectibles (28% cap) →
  qualified dividends + LTCG (0/15/20%). New `Return.collectibles_gains` and
  `Return.unrecaptured_1250_gains` inputs.
- **State tax module** with California as the pilot state. New
  `Return.state` field; `tax_rules/state/ca/{2023,2024}.yaml`. CA-style
  treatment (gains as ordinary income) is the default when a state has no
  `qualified_brackets` block.
- **At-rest encryption** for the local SQLite DB (`taxlens lock` / `taxlens
  unlock`; `taxlens serve` prompts for the passphrase if the DB is locked).
  PBKDF2-HMAC-SHA256 (480k iterations) + Fernet (AES-128-CBC + HMAC-SHA256).
- **Electron desktop shell** under `desktop/` — wraps the local sidecar in a
  native window. `npm start` to dev, `npm run dist` to build installers.
- Web UI now surfaces collectibles tax, unrecaptured §1250 tax, AMT, and the
  state-tax tile when a return has `state` set. The donut + compare tables
  include the new buckets.

### Tests
- 12 new tests across AMT triggers, Sch D rate caps, CA basic + LTCG
  ordinary-treatment, and encryption round-trips. Total: **39 passing**.

## [0.1.0] — 2025

Initial MVP — engine, importers (PDF/TXF/JSON/YAML), persistence, FastAPI
sidecar, Typer CLI, vanilla-JS web UI (6 screens), 27 tests.
