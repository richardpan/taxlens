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
# Parens-negative: `(1,500)` and `(1,500.00)` → -1500
_PAREN_NEG = re.compile(r"\(\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*\)")
# Pure-noise lines we skip when scanning forward for a value:
#   - dot-leader-only: ". . . . . . . ." or "........"
#   - bare line-letter: "1a" / "25 a" / "1z"
#   - parenthetical hint: "(see instructions)" / "(Form 8949)"
#   - "Attach Schedule ..." or "Attach Form ..." continuations
_NOISE_LINE = re.compile(
    r"^\s*(?:[\.\s]+|\d+\s*[a-z]?|\([^)]*\)|Attach\s+(?:Schedule|Form|Form\(s\))\s+\S.*)\s*$",
    re.IGNORECASE,
)


def _money(s: str) -> Decimal:
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    return Decimal(s)


def _is_form_id_digit(tail: str, start: int) -> bool:
    """True if the money match at `start` is actually part of a form identifier
    like 'W-2', '1099-R', '8949', 'Form 1116', 'Sch B'. Without this guard,
    'Federal income tax withheld from Form(s) W-2' would match '-2' as the
    withholding amount."""
    # Preceded by `[Letter]-` → part of a form code like W-2 / 1099-R.
    if start >= 2 and tail[start - 1] == "-" and tail[start - 2].isalpha():
        return True
    # Preceded by word char (digit or letter) with no separator → glued
    # identifier, not a money column.
    if start >= 1 and (tail[start - 1].isalnum() or tail[start - 1] == "-"):
        return True
    return False


def _money_matches_in(tail: str) -> list:
    """All money matches in `tail` that are not inside form identifiers."""
    money_pat = re.compile(_MONEY)
    return [m for m in money_pat.finditer(tail) if not _is_form_id_digit(tail, m.start())]


def _first_money_after(label_re: str, text: str) -> Decimal | None:
    """Find the first money string that appears on the SAME LINE as a label match,
    falling back to the next several non-empty lines if the label line has no
    number (TurboTax / H&R Block / FreeTaxUSA often render label and amount in
    separate text columns, which pdfplumber emits on adjacent lines, frequently
    with noise lines like dot-leaders or '(see instructions)' in between).

    Money matches that are actually part of a form identifier (`W-2`, `1099-R`,
    `8949`) are filtered out — otherwise the withholding line would extract
    '-2' from 'Form(s) W-2'.
    """
    label_pat = re.compile(label_re, re.IGNORECASE)
    # Stricter pattern for next-line fallback: real money has ≥3 digits or a cent decimal.
    strict_money_pat = re.compile(
        r"\$?\s*-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,})(?:\.[0-9]{1,2})?|\$?\s*-?[0-9]+\.[0-9]{1,2}"
    )
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = label_pat.search(line)
        if not m:
            continue
        tail = line[m.end():]
        pn = _PAREN_NEG.search(tail)
        money_matches = _money_matches_in(tail)
        if pn:
            try:
                return -_money(pn.group(1))
            except InvalidOperation:
                pass
        if money_matches:
            try:
                return _money(money_matches[-1].group(0))
            except InvalidOperation:
                pass
        # Same-line fallback failed — scan up to 5 next non-empty lines,
        # skipping pure noise.
        for j in range(i + 1, min(i + 6, len(lines))):
            nxt_raw = lines[j]
            nxt = nxt_raw.strip()
            if not nxt:
                continue
            if re.match(r"^\s*(?:Line\s*)?\d+\s*[a-z]?\s+[A-Za-z]{3,}", nxt_raw):
                break
            if _NOISE_LINE.match(nxt):
                continue
            pn = _PAREN_NEG.search(nxt)
            if pn:
                try:
                    return -_money(pn.group(1))
                except InvalidOperation:
                    pass
            strict = [
                m for m in strict_money_pat.finditer(nxt)
                if not _is_form_id_digit(nxt, m.start())
            ]
            if strict:
                try:
                    return _money(strict[-1].group(0))
                except InvalidOperation:
                    pass
            break
    return None


LINE_PATTERNS: dict[str, list[str]] = {
    "wages":                   [r"Line\s*1[az]?\s+Wages",
                                r"\b1\s*[az]?\b[^\n]{0,40}?Wages",
                                # Actual IRS 1040 line 1a phrasing — no "Wages" word
                                r"\b1\s*a\b[^\n]{0,80}?Form\(s\)\s*W-?2[^\n]{0,30}?box\s*1",
                                # Looser: "Form(s) W-2" anywhere on the line.
                                r"\b1\s*a\b[^\n]{0,80}?Form\(s\)\s*W-?2",
                                # Line 1z is the W-2 totals line on post-2021 1040
                                r"\b1\s*z\b[^\n]{0,80}?Add\s+lines?\s*1a\s+through\s+1h",
                                # FreeTaxUSA summary-page phrasings
                                r"Wages,\s*salaries,?\s*tips",
                                r"Wages\s+and\s+salaries"],
    "interest_income":         [r"Line\s*2b\b[^\n]{0,40}?Taxable interest",
                                r"\b2\s*b\b[^\n]{0,40}?Taxable interest",
                                r"\bTaxable\s+interest\b"],
    "qualified_dividends":     [r"Line\s*3a\b[^\n]{0,40}?Qualified dividends",
                                r"\b3\s*a\b[^\n]{0,40}?Qualified dividends",
                                r"\bQualified\s+dividends\b"],
    "ordinary_dividends":      [r"Line\s*3b\b[^\n]{0,40}?Ordinary dividends",
                                r"\b3\s*b\b[^\n]{0,40}?Ordinary dividends",
                                r"\bOrdinary\s+dividends\b"],
    "long_term_capital_gains": [r"Line\s*7\b[^\n]{0,80}?Capital gain",
                                r"\b7\b[^\n]{0,80}?Capital gain\s+or\s+\(loss\)",
                                # FreeTaxUSA summary — distinguishes LT vs ST
                                r"\bLong[-\s]term\s+capital\s+gain",
                                r"\bNet\s+long[-\s]term\s+capital\s+gain"],
    "short_term_capital_gains":[r"\bShort[-\s]term\s+capital\s+gain",
                                r"\bNet\s+short[-\s]term\s+capital\s+gain"],
    "se_income":               [r"Line\s*3\b[^\n]{0,40}?Business income",
                                r"Schedule\s*C[^\n]{0,40}?Net profit",
                                r"\b3\b[^\n]{0,40}?Business income\s+or\s+\(loss\)",
                                r"\bSelf[-\s]employment\s+income"],
    "other_ordinary_income":   [r"Line\s*8\b[^\n]{0,40}?Other income",
                                r"\b8\b[^\n]{0,40}?(?:Additional|Other) income"],
    "pension_distributions_taxable": [
                                r"\b5\s*b\b[^\n]{0,40}?(?:Pensions|Taxable amount)",
                                r"\bPensions\s+and\s+annuities"],
    "ira_distributions_taxable": [
                                r"\b4\s*b\b[^\n]{0,40}?(?:IRA|Taxable amount)",
                                r"\bIRA\s+distributions\b[^\n]{0,40}?taxable"],
    "social_security_benefits":[r"\b6\s*a\b[^\n]{0,40}?Social security benefits",
                                r"\bSocial\s+security\s+benefits"],
    "unemployment_compensation":[r"\bUnemployment\s+compensation"],
    "other_adjustments":       [r"Line\s*26\b[^\n]{0,80}?Total adjustments to income",
                                # Schedule 1 line 26 in FreeTaxUSA
                                r"\b10\b[^\n]{0,80}?Adjustments to income\s+from\s+Schedule\s*1"],
    "foreign_taxes_paid":      [r"Line\s*1\b[^\n]{0,80}?Foreign tax credit",
                                r"Foreign tax credit\.?\s+Attach\s+Form\s*1116"],
    "agi_reported":            [r"Line\s*11\b[^\n]{0,40}?Adjusted gross income",
                                r"\b11\b[^\n]{0,80}?Adjusted gross income",
                                r"\bAdjusted\s+gross\s+income\b"],
    "taxable_income_reported": [r"Line\s*15\b[^\n]{0,40}?Taxable income",
                                r"\b15\b[^\n]{0,80}?Taxable income",
                                r"\bTaxable\s+income\b"],
    "total_tax_reported":      [r"Line\s*24\b[^\n]{0,40}?Total tax",
                                r"\b24\b[^\n]{0,80}?(?:total tax|Add lines\s*22\s+and\s+23)",
                                r"\bTotal\s+tax\b"],
    "federal_withholding":     [r"Line\s*25a?\b[^\n]{0,80}?Federal income tax withheld",
                                r"\b25\s*a?\b[^\n]{0,80}?Federal income tax withheld",
                                r"\bFederal\s+(?:income\s+)?tax\s+withheld"],
    "estimated_payments":      [r"Line\s*26\b[^\n]{0,80}?estimated tax payments",
                                r"\b26\b[^\n]{0,80}?estimated tax payments",
                                r"\bEstimated\s+tax\s+payments\b"],
    "qualifying_children":     [r"Number of qualifying children",
                                r"Qualifying children[^\n]{0,40}?for\s+child\s+tax\s+credit"],
}

YEAR_PATTERNS = [
    # "Form 1040 (2023)" — older IRS style
    re.compile(r"Form\s*1040[^\n]{0,30}?(20\d{2})", re.IGNORECASE),
    # "2023 Form 1040" — TurboTax, FreeTaxUSA, H&R Block headers
    re.compile(r"\b(20\d{2})\s+Form\s*1040", re.IGNORECASE),
    # IRS official line: "U.S. Individual Income Tax Return 2023"
    re.compile(r"\b(20\d{2})\b\s+U\.?S\.?\s*Individual", re.IGNORECASE),
    re.compile(r"U\.?S\.?\s*Individual[^\n]{0,80}?(20\d{2})", re.IGNORECASE),
    # "Tax Year: 2023" — many third-party formats
    re.compile(r"Tax\s*Year\s*[:\-]?\s*(20\d{2})", re.IGNORECASE),
    # "For the year Jan. 1 - Dec. 31, 2023" — IRS line above the title
    re.compile(r"For\s+the\s+year[^\n]{0,80}?(20\d{2})", re.IGNORECASE),
    # FreeTaxUSA cover-page footer "Tax Year 2023"
    re.compile(r"Tax\s*Year\s+(20\d{2})", re.IGNORECASE),
    # Last-resort: OMB number line "OMB No. 1545-0074  2023"
    re.compile(r"OMB\s*No\.\s*1545-0074[^\n]{0,30}?(20\d{2})", re.IGNORECASE),
]

STATUS_PATTERNS = [
    (FilingStatus.MFJ,    re.compile(r"Married filing jointly", re.IGNORECASE)),
    (FilingStatus.MFS,    re.compile(r"Married filing separately", re.IGNORECASE)),
    (FilingStatus.HOH,    re.compile(r"Head of household", re.IGNORECASE)),
    (FilingStatus.QSS,    re.compile(r"Qualifying surviving spouse", re.IGNORECASE)),
    (FilingStatus.SINGLE, re.compile(r"\bSingle\b")),
]

# Explicit selection markers used by TurboTax / H&R Block / FreeTaxUSA on cover
# pages and worksheets. These take priority over the form's option-list scan
# because the option list contains ALL 5 status labels (one of which would
# otherwise be picked spuriously by the joined-text fallback).
STATUS_EXPLICIT = [
    re.compile(r"Filing\s*Status\s*[:\-]\s*([A-Za-z][^\n]{0,40})", re.IGNORECASE),
    re.compile(r"Status\s*[:\-]\s*([A-Za-z][^\n]{0,40})", re.IGNORECASE),
    re.compile(r"Your\s+filing\s+status\s+is\s+([A-Za-z][^\n.]{0,40})", re.IGNORECASE),
]

CHECKED_HINT = re.compile(r"\[\s*[xX✓]\s*\]|\(X\)|☒|\u2611|\[X\]")


def _extract_text_per_page(path: Path) -> tuple[list[str], bool]:
    """Returns (pages, ocr_used). When pdfplumber finds no text on most pages,
    we fall back to Tesseract OCR via `pytesseract` + `pdf2image` (if installed)."""
    pages: list[str] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for p in pdf.pages:
                pages.append(p.extract_text() or "")
    except Exception as e:
        # Common cases: encrypted PDF, malformed xref, pdfplumber/pdfminer crash.
        msg = str(e).lower()
        if "encrypt" in msg or "password" in msg:
            raise ValueError(
                f"PDF is password-protected. Remove the password and re-upload. "
                f"(Underlying error: {e})"
            ) from e
        raise ValueError(
            f"Could not read PDF text layer ({type(e).__name__}: {e}). "
            f"Try POST /api/debug/extract to diagnose."
        ) from e

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
    # Explicit phrase → status map (case-insensitive substring of captured group).
    explicit_map: list[tuple[FilingStatus, str]] = [
        (FilingStatus.MFJ,    "married filing jointly"),
        (FilingStatus.MFS,    "married filing separately"),
        (FilingStatus.HOH,    "head of household"),
        (FilingStatus.QSS,    "qualifying surviving spouse"),
        (FilingStatus.QSS,    "qualifying widow"),  # pre-2022 label
        (FilingStatus.SINGLE, "single"),
    ]

    # 1. Highest priority: explicit "Filing Status: X" markers (TurboTax, H&R Block).
    for pat in STATUS_EXPLICIT:
        for m in pat.finditer(joined):
            phrase = m.group(1).strip().lower()
            for status, needle in explicit_map:
                if needle in phrase:
                    return status

    # 2. Check-mark indicators on the actual form.
    for line in joined.splitlines():
        if CHECKED_HINT.search(line):
            for status, pat in STATUS_PATTERNS:
                if pat.search(line):
                    return status

    # 3. Last-resort fallback — pick whichever status appears the MOST times
    #    (the actual selection is usually echoed on multiple pages/worksheets,
    #    whereas option labels appear once on the form).
    counts = [(status, len(pat.findall(joined))) for status, pat in STATUS_PATTERNS]
    if any(c > 0 for _, c in counts):
        counts.sort(key=lambda x: -x[1])
        if counts[0][1] > counts[1][1]:
            return counts[0][0]
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

    try:
        ret = Return(
            tax_year=tax_year,
            filing_status=filing_status,
            qualifying_children=children,
            reported_total_tax=reported_total_tax,
            **fields,
        )
    except Exception as e:
        raise ValueError(
            f"Extracted fields from {path.name} failed validation: {type(e).__name__}: {e}. "
            f"Detected year={tax_year}, status={filing_status}, fields={sorted(fields.keys())}. "
            f"Try POST /api/debug/extract to inspect raw PDF text."
        ) from e
    if ocr_used:
        warnings.insert(0, "PDF appeared to be scanned; used OCR fallback. Results may be approximate — please double-check.")
    return Imported(
        ret=ret,
        source="pdf-ocr" if ocr_used else "pdf",
        source_hash=sha256_file(path),
        source_filename=path.name,
        warnings=warnings,
    )
