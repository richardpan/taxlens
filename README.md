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

- [x] Phase 0 — Engine scaffold + 2024 federal rules + reconciliation tests
- [ ] Phase 1 — PDF/TXF import pipeline
- [ ] Phase 2 — Full schedule coverage (B, D, 1, 2, 3, SE)
- [ ] Phase 3 — Tauri + React UI with what-if editor

See the full plan and UI mockups in the design folder.

## Quick start

```powershell
cd taxlens
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

## Design principles

1. **Engine is pure.** No I/O, no globals, no floats — `Decimal` everywhere money is involved.
2. **Rules live in YAML**, never in code. A new tax year is a one-file PR.
3. **Every computation step is recorded** as `(label, formula, inputs, output)` so the UI can render an audit trail and cite back to the source PDF.
4. **Never silently "fix" a return.** Surface deltas; let the user decide.

## License

MIT (see LICENSE).
