"""Generate synthetic Form-1040-shaped PDFs for extractor tests.

The text layout deliberately mirrors phrases the extractor's regexes look for
so the round-trip test exercises real pdfplumber + real regexes.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas


def _money(d: Decimal | int) -> str:
    n = Decimal(d).quantize(Decimal("0.01"))
    sign = "-" if n < 0 else ""
    n = abs(n)
    whole, frac = divmod(n, 1)
    return f"${sign}{int(whole):,}"


def make_1040_pdf(
    path: Path,
    *,
    tax_year: int,
    filing_status_label: str,
    wages: Decimal,
    interest: Decimal = Decimal(0),
    qual_div: Decimal = Decimal(0),
    ord_div: Decimal = Decimal(0),
    long_term_capital_gains: Decimal = Decimal(0),
    se_income: Decimal = Decimal(0),
    withholding: Decimal = Decimal(0),
    estimated: Decimal = Decimal(0),
    total_tax_reported: Decimal | None = None,
    qualifying_children: int = 0,
) -> None:
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER
    y = height - 50

    def line(text: str, size: int = 10) -> None:
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(50, y, text)
        y -= 14

    line(f"Form 1040 ({tax_year}) — U.S. Individual Income Tax Return", size=12)
    line("")
    line(f"Filing Status: [X] {filing_status_label}")
    line(f"Number of qualifying children: {qualifying_children}")
    line("")
    line(f"Line 1a   Wages, salaries, tips ...........................  {_money(wages)}")
    if interest:
        line(f"Line 2b   Taxable interest ................................  {_money(interest)}")
    if qual_div:
        line(f"Line 3a   Qualified dividends .............................  {_money(qual_div)}")
    if ord_div:
        line(f"Line 3b   Ordinary dividends ..............................  {_money(ord_div)}")
    if long_term_capital_gains:
        line(f"Line 7    Capital gain or (loss). Attach Schedule D ........  {_money(long_term_capital_gains)}")
    if se_income:
        line(f"          Schedule C — Net profit ..........................  {_money(se_income)}")
    line("")
    line("Line 11   Adjusted gross income ...........................  $—")
    line("Line 15   Taxable income ..................................  $—")
    if total_tax_reported is not None:
        line(f"Line 24   Total tax .......................................  {_money(total_tax_reported)}")
    if withholding:
        line(f"Line 25a  Federal income tax withheld from W-2 ............  {_money(withholding)}")
    if estimated:
        line(f"Line 26   2024 estimated tax payments .....................  {_money(estimated)}")
    c.showPage()
    c.save()
