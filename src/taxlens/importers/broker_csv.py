"""Broker 1099-B CSV importer (Fidelity / Schwab / generic).

Parses a 1099-B realized-gains CSV export and produces a *partial* `Return`
populated with long-term + short-term capital gains. Users typically use this
as a side-input to combine with a manual or PDF Return — the service layer's
what-if endpoint can merge fields, or callers can construct a base Return
manually.

Recognized column headers (case-insensitive, fuzzy):
  - Symbol / Security
  - Quantity
  - Proceeds / Sales price
  - Cost basis / Basis
  - Date acquired / Acquired
  - Date sold / Disposed / Sold
  - Term / Holding period            ← preferred; if absent we infer from dates
  - Box / 1099-B box                 ← optional; "A"/"D" = LT covered, etc.
  - Type                             ← optional; "Collectibles" / "1250"
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from taxlens.importers import Imported, sha256_file
from taxlens.models import FilingStatus, Return


ZERO = Decimal(0)

# Header aliases. Lower-cased, stripped of punctuation.
ALIASES = {
    "symbol":   {"symbol", "security", "ticker", "description"},
    "proceeds": {"proceeds", "sales price", "sale price", "gross proceeds", "amount"},
    "basis":    {"cost basis", "basis", "cost", "adjusted cost basis"},
    "acquired": {"date acquired", "acquired", "purchase date", "open date"},
    "sold":     {"date sold", "sold", "disposed", "close date", "settlement date"},
    "term":     {"term", "holding period", "long/short"},
    "type":     {"type", "category", "asset type"},
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, h in enumerate(header):
        h_n = _norm(h)
        for key, vocab in ALIASES.items():
            if h_n in vocab and key not in mapping:
                mapping[key] = i
                break
    return mapping


def _parse_money(s: str) -> Decimal:
    if s is None:
        return ZERO
    s = s.strip().replace(",", "").replace("$", "")
    if not s or s in {"-", "--"}:
        return ZERO
    # Handle parentheses for negatives, e.g. "(1,234.56)"
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        v = Decimal(s)
    except InvalidOperation:
        return ZERO
    return -v if neg else v


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_long_term(row_term: str, acquired: Optional[date], sold: Optional[date]) -> bool:
    t = row_term.lower()
    if "long" in t or t.strip() in {"lt", "l"}:
        return True
    if "short" in t or t.strip() in {"st", "s"}:
        return False
    if acquired and sold:
        return (sold - acquired).days > 365
    return False  # safest default = short-term


def import_csv(path: Path, *, tax_year: int | None = None,
               filing_status: FilingStatus = FilingStatus.SINGLE) -> Imported:
    """Parse a broker 1099-B CSV into a partial Return containing only
    long_term_capital_gains, short_term_capital_gains, collectibles_gains,
    unrecaptured_1250_gains. tax_year defaults to the year of the latest sold date."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    # Try to skip any pre-table junk lines (some brokers prepend headers like
    # "Account Number:  ..."). We do this by scanning for the first line that
    # looks like a real header (contains 'proceeds' or 'cost').
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        ln_n = _norm(ln)
        if "proceeds" in ln_n or "cost basis" in ln_n or "basis" in ln_n:
            start = i
            break
    body = "\n".join(lines[start:])

    reader = csv.reader(io.StringIO(body))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"{path}: no parseable rows")

    header = rows[0]
    mapping = _map_columns(header)
    needed = {"proceeds", "basis"}
    missing = needed - set(mapping.keys())
    if missing:
        raise ValueError(f"{path}: required columns not found: {sorted(missing)}")

    lt = ZERO
    st = ZERO
    coll = ZERO
    unrec_1250 = ZERO
    latest_sold: Optional[date] = None
    warnings: list[str] = []
    skipped = 0

    for r in rows[1:]:
        if len(r) <= max(mapping.values()):
            skipped += 1
            continue
        proceeds = _parse_money(r[mapping["proceeds"]])
        basis = _parse_money(r[mapping["basis"]])
        gain = proceeds - basis
        if gain == 0:
            continue

        term_str = r[mapping["term"]] if "term" in mapping else ""
        acq = _parse_date(r[mapping["acquired"]]) if "acquired" in mapping else None
        sold = _parse_date(r[mapping["sold"]]) if "sold" in mapping else None
        if sold and (latest_sold is None or sold > latest_sold):
            latest_sold = sold
        type_str = (r[mapping["type"]] if "type" in mapping else "").lower()

        is_lt = _is_long_term(term_str, acq, sold)
        if "collectible" in type_str:
            coll += gain                     # collectibles must be LT to get the 28% rate
        elif "1250" in type_str:
            unrec_1250 += gain
        elif is_lt:
            lt += gain
        else:
            st += gain

    if tax_year is None:
        tax_year = latest_sold.year if latest_sold else datetime.now().year - 1

    if skipped:
        warnings.append(f"Skipped {skipped} malformed row(s).")
    if not (lt or st or coll or unrec_1250):
        warnings.append("CSV parsed but no realized gains/losses found.")

    ret = Return(
        tax_year=tax_year,
        filing_status=filing_status,
        long_term_capital_gains=lt,
        short_term_capital_gains=st,
        collectibles_gains=coll,
        unrecaptured_1250_gains=unrec_1250,
    )
    return Imported(
        ret=ret,
        source="broker_csv",
        source_hash=sha256_file(path),
        source_filename=path.name,
        warnings=warnings,
    )
