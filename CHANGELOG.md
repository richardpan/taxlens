# Changelog

All notable changes to TaxLens.

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
