"""Mock third-party-software 1040 PDF generators (TurboTax, H&R Block, FreeTaxUSA).

These replicate the *formatting quirks* of each vendor's exports — header
style, filing-status marker, column-split layouts, and canonical IRS line
phrasing (e.g. line 1a says "Total amount from Form(s) W-2, box 1", not
"Wages"). The goal is to lock in importer compatibility via golden tests
without needing actual user PDFs (which would contain PII).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas


def _money(d: Decimal | int) -> str:
    n = Decimal(d).quantize(Decimal("0.01"))
    sign = "-" if n < 0 else ""
    return f"${sign}{int(abs(n)):,}"


@dataclass
class ThirdPartyReturn:
    tax_year: int
    filing_status_label: str  # "Single", "Married Filing Jointly", etc.
    wages: Decimal = Decimal(0)
    interest: Decimal = Decimal(0)
    qual_div: Decimal = Decimal(0)
    ord_div: Decimal = Decimal(0)
    withholding: Decimal = Decimal(0)
    qualifying_children: int = 0
    agi: Decimal | None = None
    taxable_income: Decimal | None = None
    total_tax: Decimal | None = None


def make_turbotax_1040(path: Path, r: ThirdPartyReturn) -> None:
    """TurboTax-style export: cover page + Form 1040 page with canonical IRS
    phrasing (no 'Wages' word on line 1a) and explicit 'Filing Status:' marker."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    # Cover page (TurboTax always leads with one).
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 60, "TurboTax")
    c.setFont("Helvetica", 11)
    c.drawString(50, height - 90, f"{r.tax_year} Federal Tax Return")
    c.drawString(50, height - 110, f"Filing Status: {r.filing_status_label}")
    c.drawString(50, height - 130, f"Tax Year: {r.tax_year}")
    c.showPage()

    # Form 1040 page — canonical IRS phrasing exactly as the form prints it.
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, height - 40, "Form 1040 U.S. Individual Income Tax Return")
    c.setFont("Helvetica", 8)
    c.drawString(50, height - 54, f"OMB No. 1545-0074  {r.tax_year}")
    c.setFont("Helvetica", 10)

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(14600))
    items = [
        # Note: line 1a uses the actual IRS phrasing — NO word "Wages".
        ("1a", "Total amount from Form(s) W-2, box 1 (see instructions)", _money(r.wages)),
        ("1z", "Add lines 1a through 1h", _money(r.wages)),
        ("2b", "Taxable interest", _money(r.interest)),
        ("3a", "Qualified dividends", _money(r.qual_div)),
        ("3b", "Ordinary dividends", _money(r.ord_div)),
        ("11", "Adjusted gross income. Subtract line 10 from line 9", _money(agi)),
        ("15", "Taxable income. Subtract line 14 from line 11", _money(ti)),
        ("24", "Add lines 22 and 23. This is your total tax",
            _money(r.total_tax) if r.total_tax else "$0"),
        ("25a", "Federal income tax withheld from Form(s) W-2", _money(r.withholding)),
    ]
    y = height - 90
    for lineno, label, value in items:
        c.drawString(50, y, lineno)
        c.drawString(80, y, label)
        c.drawString(500, y, value)
        y -= 14
    c.showPage()
    c.save()


def make_hrblock_1040(path: Path, r: ThirdPartyReturn) -> None:
    """H&R Block-style export: column-split layout where pdfplumber may emit
    label and amount on adjacent lines. Uses 'Your filing status is X' marker."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    # H&R Block header.
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, height - 40, f"H&R Block - {r.tax_year} Form 1040")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 60, f"Your filing status is {r.filing_status_label}")
    c.drawString(50, height - 75, f"Number of qualifying children: {r.qualifying_children}")

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(14600))

    # H&R Block puts amounts in a right-aligned column far from the label,
    # with dot-leaders. Use a tab-stop arrangement that pdfplumber may
    # split into separate lines depending on font/widths.
    items = [
        ("1a Total amount from Form(s) W-2, box 1", _money(r.wages)),
        ("1z Add lines 1a through 1h", _money(r.wages)),
        ("2b Taxable interest", _money(r.interest)),
        ("3a Qualified dividends", _money(r.qual_div)),
        ("3b Ordinary dividends", _money(r.ord_div)),
        ("11 Adjusted gross income", _money(agi)),
        ("15 Taxable income", _money(ti)),
        ("24 Add lines 22 and 23. This is your total tax",
            _money(r.total_tax) if r.total_tax else "$0"),
        ("25a Federal income tax withheld from Form(s) W-2", _money(r.withholding)),
    ]
    y = height - 110
    for label, value in items:
        dots = "." * max(3, 60 - len(label))
        c.drawString(50, y, f"{label} {dots} {value}")
        y -= 14
    c.showPage()
    c.save()


def make_freetaxusa_1040(path: Path, r: ThirdPartyReturn) -> None:
    """FreeTaxUSA-style export: minimalist cover + canonical IRS body."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, height - 40, f"FreeTaxUSA {r.tax_year}")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 60, f"Filing Status: {r.filing_status_label}")
    c.drawString(50, height - 75, "Form 1040")
    c.drawString(50, height - 90, f"For the year Jan. 1 - Dec. 31, {r.tax_year}")

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(14600))

    items = [
        ("1a Total amount from Form(s) W-2, box 1", _money(r.wages)),
        ("1z Add lines 1a through 1h", _money(r.wages)),
        ("2b Taxable interest", _money(r.interest)),
        ("3a Qualified dividends", _money(r.qual_div)),
        ("3b Ordinary dividends", _money(r.ord_div)),
        ("10 Adjustments to income from Schedule 1, line 26", "$0"),
        ("11 Adjusted gross income", _money(agi)),
        ("15 Taxable income", _money(ti)),
        ("24 Add lines 22 and 23. This is your total tax",
            _money(r.total_tax) if r.total_tax else "$0"),
        ("25a Federal income tax withheld from Form(s) W-2", _money(r.withholding)),
    ]
    y = height - 120
    for label, value in items:
        c.drawString(50, y, f"{label} ...... {value}")
        y -= 14
    c.showPage()
    c.save()
