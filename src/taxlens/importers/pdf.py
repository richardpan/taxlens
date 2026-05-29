"""PDF importer for IRS Form 1040 returns.

Strategy:
  1. Extract text per page (pdfplumber).
  2. Detect tax year via "Form 1040 (YYYY)" or header lines.
  3. Detect filing status via literal text match (preferring "checked" markers).
  4. For each line of interest, run ordered regexes; first match wins.
  5. Money strings tolerate $, commas, and whitespace.

The interface is `import_pdf(path) -> Imported`. This is intentionally
permissive — real-world tax-software outputs need more templates, and v2
will add positional / bbox fallbacks and OCR (Tesseract).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

from taxlens.importers import Imported, sha256_file
from taxlens.models import FilingStatus, Return

_MONEY = r"\$?\s*-?[0-9][0-9,]*(?:\.[0-9]{1,2})?"


def _money(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", "").replace(" ", ""))


def _first_money_after(label_re: str, text: str) -> Decimal | None:
    """Find the first money string that appears on the SAME LINE as a label match.

    Earlier versions used a permissive cross-line bridge, which let stray
    digits like '-2' from 'Form W-2 box 1' or '22' from '(add lines 22 and 23)'
    bleed into the value. We now constrain matching to one physical line."""
    label_pat = re.compile(label_re, re.IGNORECASE)
    money_pat = re.compile(_MONEY)
    for line in text.splitlines():
        m = label_pat.search(line)
        if not m:
            continue
        # Look at the tail after the label match end.
        tail = line[m.end():]
        # Take the LAST money on the line (handles "(add lines 22 and 23) ...... $1,234").
        money_matches = list(money_pat.finditer(tail))
        if not money_matches:
            continue
        try:
            return _money(money_matches[-1].group(0))
        except InvalidOperation:
            continue
    return None


LINE_PATTERNS: dict[str, list[str]] = {
    "wages":                   [r"Line\s*1[az]?\s+Wages",
                                r"\b1\s*[az]?\b[^\n]{0,40}?Wages"],
    "interest_income":         [r"Line\s*2b\b[^\n]{0,40}?Taxable interest",
                                r"\b2\s*b\b[^\n]{0,40}?Taxable interest"],
    "qualified_dividends":     [r"Line\s*3a\b[^\n]{0,40}?Qualified dividends",
                                r"\b3\s*a\b[^\n]{0,40}?Qualified dividends"],
    "ordinary_dividends":      [r"Line\s*3b\b[^\n]{0,40}?Ordinary dividends",
                                r"\b3\s*b\b[^\n]{0,40}?Ordinary dividends"],
    "long_term_capital_gains": [r"Line\s*7\b[^\n]{0,80}?Capital gain"],
    "se_income":               [r"Line\s*3\b[^\n]{0,40}?Business income",
                                r"Schedule\s*C[^\n]{0,40}?Net profit"],
    "other_ordinary_income":   [r"Line\s*8\b[^\n]{0,40}?Other income"],
    "other_adjustments":       [r"Line\s*26\b[^\n]{0,80}?Total adjustments to income"],
    "foreign_taxes_paid":      [r"Line\s*1\b[^\n]{0,80}?Foreign tax credit"],
    "agi_reported":            [r"Line\s*11\b[^\n]{0,40}?Adjusted gross income"],
    "taxable_income_reported": [r"Line\s*15\b[^\n]{0,40}?Taxable income"],
    "total_tax_reported":      [r"Line\s*24\b[^\n]{0,40}?Total tax"],
    "federal_withholding":     [r"Line\s*25a?\b[^\n]{0,80}?Federal income tax withheld"],
    "estimated_payments":      [r"Line\s*26\b[^\n]{0,80}?estimated tax payments"],
    "qualifying_children":     [r"Number of qualifying children"],
}

YEAR_PATTERNS = [
    re.compile(r"Form\s*1040[^\n]{0,30}?(20\d{2})", re.IGNORECASE),
    re.compile(r"\b(20\d{2})\b\s+U\.?S\.?\s*Individual", re.IGNORECASE),
    re.compile(r"Tax\s*Year\s*[:\-]?\s*(20\d{2})", re.IGNORECASE),
]

STATUS_PATTERNS = [
    (FilingStatus.MFJ,    re.compile(r"Married filing jointly", re.IGNORECASE)),
    (FilingStatus.MFS,    re.compile(r"Married filing separately", re.IGNORECASE)),
    (FilingStatus.HOH,    re.compile(r"Head of household", re.IGNORECASE)),
    (FilingStatus.QSS,    re.compile(r"Qualifying surviving spouse", re.IGNORECASE)),
    (FilingStatus.SINGLE, re.compile(r"\bSingle\b")),
]

CHECKED_HINT = re.compile(r"\[\s*[xX✓]\s*\]|\(X\)|☒|\u2611|\[X\]")


def _extract_text_per_page(path: Path) -> tuple[list[str], bool]:
    """Returns (pages, ocr_used). When pdfplumber finds no text on most pages,
    we fall back to Tesseract OCR via `pytesseract` + `pdf2image` (if installed)."""
    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for p in pdf.pages:
            pages.append(p.extract_text() or "")

    nonempty = sum(1 for p in pages if p.strip())
    if nonempty >= max(1, len(pages) // 2):
        return pages, False

    # Fallback to OCR. Soft-deps: pytesseract + pdf2image + Poppler binaries +
    # Tesseract binary. If any are missing we keep the empty result and let the
    # caller surface a warning rather than crashing the import.
    try:
        import pdf2image                          # type: ignore[import-not-found]
        import pytesseract                        # type: ignore[import-not-found]
    except Exception:
        return pages, False

    try:
        images = pdf2image.convert_from_path(str(path), dpi=300)
    except Exception:
        return pages, False

    ocr_pages: list[str] = []
    for img in images:
        try:
            ocr_pages.append(pytesseract.image_to_string(img))
        except Exception:
            ocr_pages.append("")
    return ocr_pages, True


def _detect_year(pages: list[str]) -> int | None:
    for txt in pages:
        for pat in YEAR_PATTERNS:
            m = pat.search(txt)
            if m:
                return int(m.group(1))
    return None


def _detect_status(pages: list[str]) -> FilingStatus | None:
    joined = "\n".join(pages)
    for line in joined.splitlines():
        if CHECKED_HINT.search(line):
            for status, pat in STATUS_PATTERNS:
                if pat.search(line):
                    return status
    for status, pat in STATUS_PATTERNS:
        if pat.search(joined):
            return status
    return None


def _extract_fields(pages: list[str]) -> tuple[dict[str, Decimal], int, list[str]]:
    out: dict[str, Decimal] = {}
    warnings: list[str] = []
    qualifying_children = 0
    joined = "\n".join(pages)

    for field, patterns in LINE_PATTERNS.items():
        for pat in patterns:
            value = _first_money_after(pat, joined)
            if value is not None:
                if field == "qualifying_children":
                    try:
                        qualifying_children = int(value)
                    except Exception:
                        warnings.append("qualifying_children unparseable")
                else:
                    out[field] = value
                break
    return out, qualifying_children, warnings


def import_pdf(path: Path) -> Imported:
    pages, ocr_used = _extract_text_per_page(path)
    tax_year = _detect_year(pages)
    filing_status = _detect_status(pages)
    fields, children, warnings = _extract_fields(pages)

    if tax_year is None:
        raise ValueError(
            f"Could not detect tax year in {path.name}. "
            "Add a template for this form layout or use manual import."
        )
    if filing_status is None:
        warnings.append("filing status not detected; defaulting to single")
        filing_status = FilingStatus.SINGLE

    reported_total_tax = fields.pop("total_tax_reported", None)
    fields.pop("agi_reported", None)
    fields.pop("taxable_income_reported", None)

    ret = Return(
        tax_year=tax_year,
        filing_status=filing_status,
        qualifying_children=children,
        reported_total_tax=reported_total_tax,
        **fields,
    )
    if ocr_used:
        warnings.insert(0, "PDF appeared to be scanned; used OCR fallback. Results may be approximate — please double-check.")
    return Imported(
        ret=ret,
        source="pdf-ocr" if ocr_used else "pdf",
        source_hash=sha256_file(path),
        source_filename=path.name,
        warnings=warnings,
    )
