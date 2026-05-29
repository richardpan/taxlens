"""Realistic multi-page IRS Form 1040 PDF generator for golden-fixture tests.

Closer to actual IRS layout than the minimal synthetic_pdf — exercises:
  - Page 1 (lines 1-11): identification, income, AGI
  - Page 2 (lines 12-38): deduction, taxable income, tax, credits, payments
  - Schedule 1 additional income / adjustments
  - Schedule 2 additional taxes (AMT, SE)
  - Schedule 3 nonrefundable credits (FTC, child + dep care, etc.)
  - Schedule B interest + dividend detail
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas


def _money(d: Decimal | int | None) -> str:
    if d is None:
        return ""
    n = Decimal(d).quantize(Decimal("0.01"))
    sign = "-" if n < 0 else ""
    n = abs(n)
    whole, _ = divmod(n, 1)
    return f"${sign}{int(whole):,}"


@dataclass
class Realistic1040:
    tax_year: int
    filing_status_label: str
    wages: Decimal = Decimal(0)
    interest: Decimal = Decimal(0)
    qual_div: Decimal = Decimal(0)
    ord_div: Decimal = Decimal(0)
    long_term_capital_gains: Decimal = Decimal(0)
    se_income: Decimal = Decimal(0)
    other_income: Decimal = Decimal(0)             # Schedule 1 line 9
    adjustments: Decimal = Decimal(0)              # Schedule 1 line 26
    withholding: Decimal = Decimal(0)
    estimated: Decimal = Decimal(0)
    total_tax_reported: Decimal | None = None
    agi_reported: Decimal | None = None
    taxable_income_reported: Decimal | None = None
    qualifying_children: int = 0
    amt: Decimal = Decimal(0)                      # Schedule 2 line 1
    se_tax: Decimal = Decimal(0)                   # Schedule 2 line 4
    foreign_tax_credit: Decimal = Decimal(0)       # Schedule 3 line 1
    interest_payers: list[tuple[str, Decimal]] = field(default_factory=list)
    div_payers: list[tuple[str, Decimal]] = field(default_factory=list)


def make_realistic_1040(path: Path, r: Realistic1040) -> None:
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    def page_header(title: str) -> None:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(50, height - 40, title)
        c.setFont("Helvetica", 8)
        c.drawString(50, height - 52, f"OMB No. 1545-0074 Tax Year {r.tax_year}")

    def lines(items: list[tuple[str, str]], start_y: int = None) -> int:
        y = start_y if start_y is not None else height - 80
        c.setFont("Helvetica", 10)
        for label, value in items:
            text = f"{label} {'.' * 6} {value}" if value else label
            c.drawString(50, y, text)
            y -= 14
        return y

    # ── page 1: Form 1040 page 1 (identification + income through AGI) ──────
    page_header(f"Form 1040  U.S. Individual Income Tax Return  ({r.tax_year})")
    y = height - 80
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Filing Status: [X] {r.filing_status_label}"); y -= 16
    c.drawString(50, y, f"Number of qualifying children: {r.qualifying_children}"); y -= 20

    # Compute totals
    additional = r.other_income
    total_income = r.wages + r.interest + r.ord_div + r.long_term_capital_gains + r.se_income + additional
    agi = r.agi_reported if r.agi_reported is not None else (total_income - r.adjustments)
    items = [
        ("Line 1a  Wages, salaries, tips (Form W-2 box 1)", _money(r.wages)),
        ("Line 2b  Taxable interest (Schedule B if required)", _money(r.interest)),
        ("Line 3a  Qualified dividends", _money(r.qual_div)),
        ("Line 3b  Ordinary dividends", _money(r.ord_div)),
        ("Line 7   Capital gain or (loss). Attach Schedule D",
            _money(r.long_term_capital_gains)),
        ("Line 8   Additional income from Schedule 1, line 10", _money(additional)),
        ("Line 9   Total income (sum of lines 1z, 2b-8)", _money(total_income)),
        ("Line 10  Adjustments to income from Schedule 1, line 26", _money(r.adjustments)),
        ("Line 11  Adjusted gross income (line 9 minus line 10)", _money(agi)),
    ]
    lines(items, y)
    c.showPage()

    # ── page 2: Form 1040 page 2 (deduction → tax → payments → refund) ─────
    page_header(f"Form 1040 page 2  ({r.tax_year})")
    ti = r.taxable_income_reported if r.taxable_income_reported is not None else (agi - Decimal(29200))
    items = [
        ("Line 12  Standard deduction or itemized (Schedule A)", "$—"),
        ("Line 15  Taxable income (line 11 minus line 14)", _money(ti)),
        ("Line 16  Tax (see Tax Tables / Schedule D worksheet)", "$—"),
        ("Line 17  Amount from Schedule 2, line 3 (AMT etc.)", _money(r.amt)),
        ("Line 22  Subtract line 21 from line 18", "$—"),
        ("Line 23  Other taxes from Schedule 2, line 21", _money(r.se_tax)),
        ("Line 24  Total tax (add lines 22 and 23)",
            _money(r.total_tax_reported) if r.total_tax_reported else "$—"),
        ("Line 25a Federal income tax withheld from Form(s) W-2", _money(r.withholding)),
        ("Line 26  2024 estimated tax payments and amount applied from prior year",
            _money(r.estimated)),
        ("Line 33  Total payments", _money(r.withholding + r.estimated)),
        ("Line 37  Amount you owe (or 34 for refund)", "$—"),
    ]
    lines(items)
    c.showPage()

    # ── Schedule 1: additional income & adjustments ────────────────────────
    if r.other_income or r.adjustments or r.se_income:
        page_header(f"Schedule 1 (Form 1040)  Additional Income and Adjustments to Income  ({r.tax_year})")
        items = [
            ("Part I  Additional Income", ""),
            ("Line 3   Business income or (loss). Attach Schedule C", _money(r.se_income)),
            ("Line 8   Other income", _money(r.other_income)),
            ("Line 10  Total additional income (Part I)", _money(r.se_income + r.other_income)),
            ("", ""),
            ("Part II  Adjustments to Income", ""),
            ("Line 15  Deductible part of self-employment tax",
                _money((r.se_tax / 2) if r.se_tax else Decimal(0))),
            ("Line 26  Total adjustments to income (Part II)", _money(r.adjustments)),
        ]
        lines(items)
        c.showPage()

    # ── Schedule 2: additional taxes ────────────────────────────────────────
    if r.amt or r.se_tax:
        page_header(f"Schedule 2 (Form 1040)  Additional Taxes  ({r.tax_year})")
        items = [
            ("Line 1   Alternative minimum tax. Attach Form 6251", _money(r.amt)),
            ("Line 3   Add lines 1 and 2", _money(r.amt)),
            ("Line 4   Self-employment tax. Attach Schedule SE", _money(r.se_tax)),
            ("Line 21  Total other taxes (sum of lines 4-18)", _money(r.se_tax)),
        ]
        lines(items)
        c.showPage()

    # ── Schedule 3: credits and payments ────────────────────────────────────
    if r.foreign_tax_credit:
        page_header(f"Schedule 3 (Form 1040)  Additional Credits and Payments  ({r.tax_year})")
        items = [
            ("Line 1   Foreign tax credit. Attach Form 1116 if required",
                _money(r.foreign_tax_credit)),
            ("Line 8   Total nonrefundable credits", _money(r.foreign_tax_credit)),
        ]
        lines(items)
        c.showPage()

    # ── Schedule B: interest + dividend detail ──────────────────────────────
    if r.interest_payers or r.div_payers:
        page_header(f"Schedule B (Form 1040)  Interest and Ordinary Dividends  ({r.tax_year})")
        y = height - 80
        c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Part I  Interest"); y -= 14
        c.setFont("Helvetica", 10)
        for name, amt in r.interest_payers:
            c.drawString(70, y, f"{name} ...... {_money(amt)}"); y -= 14
        if r.interest_payers:
            c.drawString(50, y, f"Line 2 Total interest ...... {_money(r.interest)}"); y -= 20
        c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Part II  Ordinary Dividends"); y -= 14
        c.setFont("Helvetica", 10)
        for name, amt in r.div_payers:
            c.drawString(70, y, f"{name} ...... {_money(amt)}"); y -= 14
        if r.div_payers:
            c.drawString(50, y, f"Line 6 Total ordinary dividends ...... {_money(r.ord_div)}"); y -= 20
        c.showPage()

    c.save()
