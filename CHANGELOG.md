# Changelog

All notable changes to TaxLens.

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
