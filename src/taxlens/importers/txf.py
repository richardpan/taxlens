"""TXF (Tax eXchange Format) importer — minimal subset for 1040 line items.

Records are delimited by `^`. Each record contains a record-type line, a
reference-number line (e.g. `N260`), and an amount line (e.g. `$240000.00`).

Reference codes used:
  N260 wages              N287 ordinary dividends   N488 SE income (Sch C net)
  N286 interest           N623 qualified dividends  N501 HSA deduction
  N321 LTCG net           N322 STCG net             N521 federal withholding
                                                    N532 estimated payments
  N999 filing status (1..5)
  N998 qualifying children count
  N997 tax year
  N996 reported total tax
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from taxlens.importers import Imported, sha256_file
from taxlens.models import FilingStatus, Return

CODE_TO_FIELD = {
    "260": "wages",
    "286": "interest_income",
    "287": "ordinary_dividends",
    "623": "qualified_dividends",
    "321": "long_term_capital_gains",
    "322": "short_term_capital_gains",
    "488": "se_income",
    "501": "hsa_deduction",
    "521": "federal_withholding",
    "532": "estimated_payments",
}

STATUS_MAP = {
    "1": FilingStatus.SINGLE,
    "2": FilingStatus.MFJ,
    "3": FilingStatus.MFS,
    "4": FilingStatus.HOH,
    "5": FilingStatus.QSS,
}


def _split_records(text: str) -> list[list[str]]:
    records: list[list[str]] = []
    for raw in text.replace("\r\n", "\n").split("^"):
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines:
            records.append(lines)
    return records


def import_txf(path: Path) -> Imported:
    text = path.read_text(encoding="utf-8", errors="replace")
    records = _split_records(text)

    fields: dict[str, Decimal] = {}
    tax_year: int | None = None
    filing_status: FilingStatus | None = None
    children = 0
    reported_total_tax: Decimal | None = None
    warnings: list[str] = []

    for rec in records:
        n_code: str | None = None
        amount: Decimal | None = None
        for line in rec:
            if line.startswith("N"):
                n_code = line[1:].strip()
            elif line.startswith("$"):
                try:
                    amount = Decimal(line[1:].replace(",", "").strip())
                except Exception:
                    warnings.append(f"unparseable amount: {line!r}")
        if n_code is None or amount is None:
            continue
        if n_code == "997":
            tax_year = int(amount)
        elif n_code == "999":
            filing_status = STATUS_MAP.get(str(int(amount)))
        elif n_code == "998":
            children = int(amount)
        elif n_code == "996":
            reported_total_tax = amount
        elif n_code in CODE_TO_FIELD:
            field = CODE_TO_FIELD[n_code]
            fields[field] = fields.get(field, Decimal(0)) + amount

    if tax_year is None:
        raise ValueError(f"TXF missing tax year (N997) in {path}")
    if filing_status is None:
        raise ValueError(f"TXF missing filing status (N999) in {path}")

    ret = Return(
        tax_year=tax_year,
        filing_status=filing_status,
        qualifying_children=children,
        reported_total_tax=reported_total_tax,
        **fields,
    )
    return Imported(
        ret=ret,
        source="txf",
        source_hash=sha256_file(path),
        source_filename=path.name,
        warnings=warnings,
    )
