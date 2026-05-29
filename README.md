# TaxLens

A local-first tool that ingests US tax return PDFs (Form 1040 + common schedules), extracts income and tax line items, recomputes the tax math transparently, and visualizes trends across years.

- **Transparent math** — every dollar of tax traceable to a formula and its inputs
- **Multi-year** — drop in any number of years; see trends instantly
- **Privacy-first** — PDFs parsed locally; nothing leaves your machine

## Repo layout

```
taxlens/
├── src/taxlens/         # Python tax engine (pure functions, decimal arithmetic)
├── tax_rules/federal/   # Year-versioned bracket/credit YAML — change rules without touching code
├── tests/               # Engine tests + golden return fixtures
└── pyproject.toml
```

## Status

- [x] Phase 1 — Engine + 2023/2024 federal rules + reconciliation tests
- [x] Phase 2 — Import pipeline (PDF via pdfplumber, TXF, JSON/YAML manual)
- [x] Phase 3 — SQLite persistence, FastAPI sidecar, single-page web UI
- [x] Phase 4 — Multi-year dashboard, year detail, math view, what-if editor, compare view
- [x] Phase 5 (v0.2) — AMT (Form 6251), Schedule D 28%/25% worksheet, CA state module, at-rest DB encryption (Fernet), Electron desktop shell
- [ ] v1.x — OCR fallback for scanned PDFs, signed installers, more states, Schedule E (rentals)

See `tax_rules/federal/` for the rule tables and `tests/fixtures/returns/` for golden returns.

## Quick start

```powershell
cd taxlens
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest                              # 39 tests, ~10s
taxlens import path\to\1040.pdf     # also accepts .txf / .json / .yaml
taxlens list
taxlens serve                       # opens http://127.0.0.1:8765 in your browser
taxlens lock                        # encrypt local DB with a passphrase
taxlens unlock                      # decrypt; serve auto-prompts if locked
```

### Desktop (Electron) shell

```powershell
cd desktop
npm install
npm start                           # spawns the sidecar + opens a native window
```
See `desktop/README.md`.

The web UI has six screens: Import (drag-drop), Dashboard (multi-year), Year detail (waterfall + bracket fill), Show the math (audit trail), What-if editor (live recompute), Compare.

## Architecture

```
┌────────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  Browser (vanilla JS + │ ◀──▶│  FastAPI sidecar     │ ◀──▶│  SQLite (encrypted   │
│  Tailwind + Chart.js)  │     │  taxlens.api         │     │  in Phase 5)         │
└────────────────────────┘     │                      │     │  - returns           │
                               │  ┌─────────────────┐ │     │  - computation_cache │
                               │  │ service layer   │ │     │  - overrides         │
                               │  └─────────────────┘ │     └──────────────────────┘
                               │  ┌─────────────────┐ │
                               │  │ importers       │◀──── PDF / TXF / JSON / YAML
                               │  │  pdf, txf, …    │ │
                               │  └─────────────────┘ │
                               │  ┌─────────────────┐ │
                               │  │ engine (pure)   │◀──── tax_rules/federal/*.yaml
                               │  │  Decimal math   │ │
                               │  └─────────────────┘ │
                               └──────────────────────┘
```

## Design principles

1. **Engine is pure.** No I/O, no globals, no floats — `Decimal` everywhere money is involved.
2. **Rules live in YAML**, never in code. A new tax year is a one-file PR.
3. **Every computation step is recorded** as `(label, formula, inputs, output)` so the UI can render an audit trail and cite back to the source PDF.
4. **Never silently "fix" a return.** Surface deltas; let the user decide.
5. **Idempotent imports.** Re-importing the same file (matched by sha256) replaces, never duplicates.

## Adding a new tax year

1. Drop a new file `tax_rules/federal/{YEAR}.yaml` with brackets, std deduction, FICA caps, NIIT/Add'l Medicare thresholds, CTC params. Use 2024 as a template.
2. Add a golden return YAML in `tests/fixtures/returns/`.
3. Run `pytest` — engine snapshot tests must match within $0.01.

## License

MIT (see LICENSE).
