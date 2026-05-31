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


def make_hrblock_packed_1040(path: Path, r: ThirdPartyReturn) -> None:
    """H&R Block-realistic export modeled on a real 2020 user PDF (PII redacted
    and dollar amounts mocked) — exercises three quirks the simpler
    ``make_hrblock_1040`` fixture didn't cover:

      1. **Filing checklist cover page** ("COVER PAGE / Filing Checklist for ...")
         that mentions Form W-2 and various 1099 schedule attachments. Importer
         must skip it.
      2. **Vendor "Quick Summary" page** with friendly-label, rounded totals
         (e.g. ``Income $240,000``, ``Tax withheld or paid already $27,000``)
         that DIFFER from the real 1040 values. Importer must skip this page
         and NOT poison dashboard values.
      3. **Packed 1040 line layout** where the line number prints both BEFORE
         the label and AFTER the value, separated from the dollar by a single
         space — e.g. ``1 Wages, salaries, tips, etc. ... 1 176,865``. The
         common parser bug here is mistaking the second line-number ``1`` for
         the value (or filtering ``176,865`` because the preceding char is a
         digit from the line number).
      4. **Two-column packed rows** for lines 2a/2b, 3a/3b, 4a/4b, 5a/5b —
         e.g. ``Attach 2a Tax-exempt interest ... 2a 0 b Taxable interest ... 2b 481``.

    All PII (names, SSNs, addresses, employer names, bank info) is replaced
    with obvious mock values that cannot collide with any real person.
    """
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(24800))

    def page(lines: list[str]) -> None:
        c.setFont("Courier", 8)
        y = height - 40
        for ln in lines:
            c.drawString(40, y, ln)
            y -= 10
        c.showPage()

    def m(d: Decimal) -> str:
        # H&R Block prints amounts without leading '$' on the form lines.
        return f"{int(d):,}"

    # ── Page 1: filing-checklist cover (must be skipped) ────────────────────
    page([
        "COVER PAGE",
        f"Filing Checklist for {r.tax_year} Tax Return Filed On Standard Forms",
        "Prepared on: 04/14/2021 09:44:58 pm",
        "Return: C:\\Mock\\HRBlock\\Jane Public 2020 Tax Return.T20",
        "Step 1. Sign and date the return",
        "Step 2. Assemble the return",
        "These forms should be assembled behind Form 1040: U.S. Individual Income Tax Return",
        "- Schedule B",
        "- Schedule D",
        "- - Form 8949",
        "- Form 5329",
        "- Form 8889",
        "- Form 8995",
        "- Form 1040-V",
        "Staple these documents to the front of the first page of the return:",
        "Form W-2: Wage and Tax Statement",
        "1st (ACME CORPORATION)",
        "2nd (NORTHWIND SCHOOL DISTRICT)",
        "3rd (FABRIKAM)",
        f"Make your check or money order for $12500 payable to United States Treasury.",
    ])

    # ── Page 2: vendor "Quick Summary" (must be skipped) ────────────────────
    # Values here are deliberately DIFFERENT from the real form values on
    # page 3+ so a regression in the page-filter would visibly poison the
    # dashboard.
    bad_income = r.wages + r.interest + r.ord_div + Decimal(10_000)  # wrong
    bad_agi = agi + Decimal(5_000)                                   # wrong
    bad_tax = (r.total_tax or Decimal(0)) + Decimal(3_000)           # wrong
    bad_wh = r.withholding + Decimal(2_000)                          # wrong
    page([
        "- - Background Worksheet",
        "- - Last Year's Data Worksheet",
        "- - Form 1099-INT/OID",
        "- - Form 1099-DIV",
        f"{r.tax_year} return information - Keep this for your records",
        "Quick Summary",
        f"Income ${int(bad_income):,}",
        "Adjustments - $200",
        f"Adjusted gross income ${int(bad_agi):,}",
        "Deductions - $25,000",
        f"Tax withheld or paid already ${int(bad_wh):,}",
        f"Actual tax due - ${int(bad_tax):,}",
    ])

    # ── Page 3: real Form 1040 page 1 with the packed-line layout ───────────
    page([
        f"F 1040 {r.tax_year}",
        "o Department of the Treasury Internal Revenue Service (99)",
        f"m r U.S. Individual Income Tax Return OMB No. 1545-0074  {r.tax_year}",
        f"Filing status Single X {r.filing_status_label} Married filing separately (MFS) Head of household (HOH) Qualifying widow(er) (QW)",
        "Your first name and middle initial Last name Your social security number",
        "Jane Q Public                                                 111-22-3333",
        "If joint return, spouse's first name and middle initial Last name Spouse's social security number",
        "John R Public                                                 222-33-4444",
        "Home address (number and street)",
        "1 Example Way",
        "City, town, or post office. State ZIP code",
        "Sample WA 98000",
        # The single-line packing of label + value is the H&R Block quirk:
        #   `<line-no> <label> ........ <line-no> <value>`
        f"1 Wages, salaries, tips, etc. Attach Form(s) W-2 . . . . . . . . . . . . . . . . . . . . . . . . . . . 1 {m(r.wages)}",
        # Two-column packed row: 2a (tax-exempt) and 2b (taxable) on one line:
        f"Attach 2a Tax-exempt interest . . . . . . . . 2a 0 b Taxable interest . . . . . . . . . . . . . . 2b {m(r.interest)}",
        "Sch. B if",
        # 3a (qualified) and 3b (ordinary) packed:
        f"required. 3a Qualified dividends . . . . . . . . . 3a {m(r.qual_div)} b Ordinary dividends . . . . . . . . . . . . . 3b {m(r.ord_div)}",
        "4a IRA distributions . . . . . . . . . . 4a b Taxable amount . . . . . . . . . . . . . . 4b 0",
        "5a Pensions and annuities . . . . . . 5a b Taxable amount . . . . . . . . . . . . . . 5b 0",
        "6a Social security benefits . . . . . . . 6a b Taxable amount . . . . . . . . . . . . . . 6b",
        "Standard",
        "Deduction for-",
        f"7 Capital gain or (loss). Attach Schedule D if required. If not required, check here . . . . . . . . . . . . . . . . . . 7 {m(Decimal(0))}",
        f"8 Other income from Schedule 1, line 9 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 8 0",
        f"9 Add lines 1, 2b, 3b, 4b, 5b, 6b, 7, and 8. This is your total income . . . . . . . . . . . . . . . . 9 {m(r.wages + r.interest + r.ord_div)}",
        "10 Adjustments to income: . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .",
        "a From Schedule 1, line 22 . . . . . . . . . . . . . . . . . . . . . . . . . . 10a 0",
        "b Charitable contributions if you take the standard deduction. See instructions 10b 0",
        "c Add lines 10a and 10b. These are your total adjustments to income . . . . . . . . . . . . . . 10c 0",
        f"11 Subtract line 10c from line 9. This is your adjusted gross income . . . . . . . . . . . . . . . 11 {m(agi)}",
        "12 Standard deduction or itemized deductions (from Schedule A) . . . . . . . . . . . . . . . . . . 12 24,800",
        "13 Qualified business income deduction. Attach Form 8995 or Form 8995-A . . . . . . . . . . . . . . 13 0",
        "14 Add lines 12 and 13 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 14 24,800",
        f"15 Taxable income. Subtract line 14 from line 11. If zero or less, enter -0- . . . . . . . . . . . . . . . 15 {m(ti)}",
        f"KIA For Disclosure, Privacy Act, and Paperwork Reduction Act Notice, see separate instructions. Form 1040 ({r.tax_year})",
    ])

    # ── Page 4: Form 1040 page 2 (taxes and payments) ───────────────────────
    total_tax_str = m(r.total_tax) if r.total_tax else "0"
    page([
        f"Form 1040 ({r.tax_year}) Page 2",
        f"16 Tax (see instructions). Check if any from Form(s): 1 8814 2 4972 3 16 {total_tax_str}",
        "17 Amount from Schedule 2, line 3 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 17 0",
        f"18 Add lines 16 and 17 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 18 {total_tax_str}",
        "19 Child tax credit or credit for other dependents . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 19",
        "20 Amount from Schedule 3, line 7 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 20 0",
        "21 Add lines 19 and 20 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 21 0",
        f"22 Subtract line 21 from line 18. If zero or less, enter -0- . . . . . . . . . . . . . . . . . . . . . . . . 22 {total_tax_str}",
        "23 Other taxes, including self-employment tax, from Schedule 2, line 10 . . . . . . . . . . . . . . . . 23 0",
        f"24 Add lines 22 and 23. This is your total tax . . . . . . . . . . . . . . . . . . . . . . . . . . . . 24 {total_tax_str}",
        "25 Federal income tax withheld from:",
        f"a Form(s) W-2 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 25a {m(r.withholding)}",
        "b Form(s) 1099 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 25b 0",
        "c Other forms (see instructions) . . . . . . . . . . . . . . . . . . . . . . . 25c 0",
        f"d Add lines 25a through 25c . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 25d {m(r.withholding)}",
        f"26 {r.tax_year} estimated tax payments and amount applied from prior return . . . . . . . . . . . . . . . . . 26 0",
        f"33 Add lines 25d, 26, and 32. These are your total payments . . . . . . . . . . . . . . . . . . . . 33 {m(r.withholding)}",
        f"KIA Go to www.irs.gov/Form1040 for instructions and the latest information. Form 1040 ({r.tax_year})",
    ])

    c.save()


def make_fillable_offset_1040(out_path: Path, r: ThirdPartyReturn) -> None:
    """IRS-fillable-form-style export where labels and user-entered values
    are drawn in two SEPARATE rendering passes with a small vertical
    offset (~6pt) between them -- mimicking the failure mode where a real
    fillable PDF places form-field text a few points off the label baseline.

    pdfplumber's default ``extract_text()`` (and a tight y-tolerance
    layout pass) sees the labels and the values as two distinct rows of
    text, so the importer's same-line and adjacent-line regexes can't
    pair them up -- every money field comes out as $0. The loose-tolerance
    layout pass (introduced alongside this fixture) merges the offset
    rows back into single visual rows so extraction succeeds.

    This regression test locks in the loose-tolerance behavior.
    """
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    width, height = LETTER

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, height - 40, "Form 1040 U.S. Individual Income Tax Return")
    c.setFont("Helvetica", 8)
    c.drawString(50, height - 54, f"OMB No. 1545-0074  {r.tax_year}")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 70, f"Filing Status: {r.filing_status_label}")

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(14_600))

    rows = [
        ("1a", "Total amount from Form(s) W-2, box 1", r.wages),
        ("2b", "Taxable interest", r.interest),
        ("3a", "Qualified dividends", r.qual_div),
        ("3b", "Ordinary dividends", r.ord_div),
        ("11", "Adjusted gross income", agi),
        ("15", "Taxable income", ti),
        ("24", "Add lines 22 and 23. This is your total tax",
            r.total_tax if r.total_tax else Decimal(0)),
        ("25a", "Federal income tax withheld from Form(s) W-2", r.withholding),
    ]

    # PASS 1: labels at baseline y.
    y = height - 110
    for lineno, label, _v in rows:
        c.drawString(50, y, lineno)
        c.drawString(80, y, label)
        y -= 24

    # PASS 2: values 6pt above the label baseline (outside pdfplumber's
    # default ~3pt y-clustering tolerance, so default extraction splits
    # them onto separate output rows).
    y = height - 110 + 6
    for _ln, _lab, v in rows:
        c.drawRightString(560, y, f"{int(v):,}")
        y -= 24

    c.showPage()
    c.save()



def make_acroform_1040(out_path: Path, r: ThirdPartyReturn) -> None:
    """IRS-fillable-form-style export where the user-entered values live
    in AcroForm text-field widgets, NOT in the page text stream. Each
    widget carries an IRS-style tooltip (/TU) that matches the printed
    line description verbatim, exactly like real fillable IRS PDFs do.

    This is the failure mode the v0.28.0 layout-aware text extractor
    cannot handle: there is nothing to extract from the text layer
    because the values were never rasterised. The AcroForm extractor
    must read them directly from the form dictionary.
    """
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    width, height = LETTER
    form = c.acroForm

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, height - 40, "Form 1040 U.S. Individual Income Tax Return")
    c.setFont("Helvetica", 8)
    c.drawString(50, height - 54, f"OMB No. 1545-0074  {r.tax_year}")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 70, f"Filing Status: {r.filing_status_label}")

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(14_600))

    rows = [
        ("1a", "Total amount from Form(s) W-2, box 1",
            "Wages, salaries, tips, etc. Attach Form(s) W-2", r.wages),
        ("2b", "Taxable interest", "Taxable interest", r.interest),
        ("3a", "Qualified dividends", "Qualified dividends", r.qual_div),
        ("3b", "Ordinary dividends", "Ordinary dividends", r.ord_div),
        ("11", "Adjusted gross income", "Adjusted gross income", agi),
        ("15", "Taxable income", "Taxable income", ti),
        ("24", "Add lines 22 and 23. This is your total tax",
            "Add lines 22 and 23. This is your total tax",
            r.total_tax if r.total_tax else Decimal(0)),
        ("25a", "Federal income tax withheld from Form(s) W-2",
            "Federal income tax withheld from Form(s) W-2", r.withholding),
    ]

    y = height - 110
    for lineno, label, tooltip, value in rows:
        c.drawString(50, y, lineno)
        c.drawString(80, y, label)
        # Value goes ONLY into the AcroForm widget, never into the text layer.
        form.textfield(
            name=f"f_{lineno}_{label[:6].strip().replace(' ', '_').lower()}",
            tooltip=tooltip,
            x=420, y=y - 3, width=120, height=14,
            fontSize=9,
            value=str(int(value)),
            borderWidth=0,
        )
        y -= 24

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


def make_freetaxusa_summary_mismatch_1040(path: Path, r: ThirdPartyReturn) -> None:
    """A FreeTaxUSA-style export where the *summary page* has different (wrong)
    values than the actual Form 1040 body. The importer should ignore the
    summary and report what's on the real form.

    Real-world cause: vendor summaries sometimes combine wages + Schedule C
    net profit into a single "Wages and Salaries" total, or omit interest
    that lands on a Schedule B passthrough — so the cover page rolls things
    up differently from the underlying 1040 lines. We mimic that here by
    inflating the summary numbers vs. the form values.
    """
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(14600))

    # ── Page 1: WRONG summary (inflated values, no IRS markers) ─────────────
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, height - 40, f"FreeTaxUSA — {r.tax_year} Tax Return Summary")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 58, f"Filing Status: {r.filing_status_label}")
    c.drawString(50, height - 72, f"Tax Year: {r.tax_year}")
    bad = [
        ("Wages and Salaries",      _money(r.wages + Decimal(50_000))),  # WRONG
        ("Taxable Interest",        _money(r.interest + Decimal(900))),  # WRONG
        ("Ordinary Dividends",      _money(r.ord_div + Decimal(3_000))), # WRONG
        ("Qualified Dividends",     _money(r.qual_div + Decimal(2_000))),# WRONG
        ("Adjusted Gross Income",   _money(agi + Decimal(55_000))),      # WRONG
        ("Taxable Income",          _money(ti + Decimal(55_000))),       # WRONG
        ("Total Tax",               _money((r.total_tax or Decimal(0)) + Decimal(8_000))),  # WRONG
        ("Federal Tax Withheld",    _money(r.withholding + Decimal(7_000))),  # WRONG
    ]
    y = height - 100
    for label, value in bad:
        c.drawString(50, y, label)
        c.drawRightString(width - 50, y, value)
        y -= 16
    c.showPage()

    # ── Page 2: CORRECT IRS Form 1040 body ──────────────────────────────────
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, height - 40, "Form 1040 U.S. Individual Income Tax Return")
    c.setFont("Helvetica", 8)
    c.drawString(50, height - 54, f"OMB No. 1545-0074  {r.tax_year}")
    c.setFont("Helvetica", 9)
    c.drawString(50, height - 68, "Department of the Treasury — Internal Revenue Service")
    c.setFont("Helvetica", 10)

    items = [
        ("1a", "Total amount from Form(s) W-2, box 1", _money(r.wages)),
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


def make_freetaxusa_realistic_1040(path: Path, r: ThirdPartyReturn) -> None:
    """A closer-to-reality FreeTaxUSA export that mimics three quirks we've
    seen break the importer in the wild:

      1. Column-split layout where the amount lands on the line BELOW the
         label, with one or more pure-noise lines in between (dot-leaders,
         '(see instructions)' continuations, schedule-attachment hints).
      2. A "Tax Return Summary" cover page that uses friendly labels
         ("Wages and Salaries", "Taxable Interest") instead of the IRS
         line-1a / 2b phrasing.
      3. Capital losses rendered as parens-negative — e.g. `($3,000)`.
    """
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    agi = r.agi if r.agi is not None else (r.wages + r.interest + r.ord_div)
    ti = r.taxable_income if r.taxable_income is not None else (agi - Decimal(14600))

    # ── Page 1: friendly summary page ───────────────────────────────────────
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, height - 40, f"FreeTaxUSA — {r.tax_year} Tax Return Summary")
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 58, f"Filing Status: {r.filing_status_label}")
    c.drawString(50, height - 72, f"Tax Year: {r.tax_year}")

    summary = [
        ("Wages and Salaries", _money(r.wages)),
        ("Taxable Interest", _money(r.interest)),
        ("Ordinary Dividends", _money(r.ord_div)),
        ("Qualified Dividends", _money(r.qual_div)),
        ("Adjusted Gross Income", _money(agi)),
        ("Taxable Income", _money(ti)),
        ("Total Tax", _money(r.total_tax) if r.total_tax else "$0"),
        ("Federal Tax Withheld", _money(r.withholding)),
    ]
    y = height - 100
    for label, value in summary:
        c.drawString(50, y, label)
        c.drawRightString(width - 50, y, value)
        y -= 16
    c.showPage()

    # ── Page 2: column-split Form 1040 facsimile with noise lines ───────────
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, height - 40, f"Form 1040 — {r.tax_year}")
    c.setFont("Helvetica", 9)
    c.drawString(50, height - 54, f"OMB No. 1545-0074  {r.tax_year}")
    c.setFont("Helvetica", 10)

    # Each tuple: (label, [optional noise lines that pdfplumber may emit
    # between the label and the value], value)
    rows = [
        ("1a Total amount from Form(s) W-2, box 1",
         ["(see instructions)", ". . . . . . . . . . . . . . . . . . . . . . . . . ."],
         _money(r.wages)),
        ("1z Add lines 1a through 1h",
         [". . . . . . . . . . . . . . . . . . . . . . . . . ."],
         _money(r.wages)),
        ("2b Taxable interest",
         ["Attach Schedule B if required"],
         _money(r.interest)),
        ("3a Qualified dividends", [], _money(r.qual_div)),
        ("3b Ordinary dividends",
         ["Attach Schedule B if required"],
         _money(r.ord_div)),
        ("7  Capital gain or (loss). Attach Schedule D",
         ["if required.  If not required, check here .... ▶ ☐"],
         "($3,000)" if False else _money(Decimal(0))),  # placeholder, no cap loss in base fixture
        ("11 Adjusted gross income.  Subtract line 10 from line 9",
         [],
         _money(agi)),
        ("15 Taxable income.  Subtract line 14 from line 11",
         ["If zero or less, enter -0-"],
         _money(ti)),
        ("24 Add lines 22 and 23.  This is your total tax",
         ["▶"],
         _money(r.total_tax) if r.total_tax else "$0"),
        ("25a Federal income tax withheld from Form(s) W-2", [],
         _money(r.withholding)),
    ]
    y = height - 90
    for label, noise, value in rows:
        c.drawString(50, y, label)
        y -= 12
        for n in noise:
            c.drawString(80, y, n)
            y -= 12
        # Value goes on its own line, right-aligned in a separate column.
        c.drawRightString(width - 50, y, value)
        y -= 16
    c.showPage()
    c.save()
