# Changelog

All notable changes to TaxLens.

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
