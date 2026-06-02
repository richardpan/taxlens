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
    r"^\s*(?:[\.\s]+|\d+\s*[a-z]?|\([^)]*\)|Attach\s+(?:Schedule|Form|Form\(s\))\s+\S.*|[^\w\s]{1,3}|[A-Za-z]{1,3})\s*$",
    re.IGNORECASE,
)


def _money(s: str) -> Decimal:
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    return Decimal(s)


_FORM_ID_PREFIX_WORDS = frozenset({
    "form", "forms", "schedule", "sch", "no", "no.", "ein", "ssn",
    "tin", "ein:", "no:", "ref", "ref.", "rev", "rev.", "omb",
    "line", "lines", "page", "pages", "part", "section",
    # Form-suffix tokens that follow a form number ("1040-SR", "1040-NR",
    # "1099-R") — handled below via the dash-glue check, but listed for
    # documentation.
})


def _is_form_id_digit(tail: str, start: int) -> bool:
    """True if the money match at `start` is actually part of a form identifier
    like 'W-2', '1099-R', '8949', 'Form 1116', 'Sch B', 'OMB No. 1545-0074',
    or a line-label echo like '6a' / '25b' / '1z'. Without this guard,
    'Federal income tax withheld from Form(s) W-2' would match '-2' as the
    withholding amount, packed rows like '6a Social security benefits . . . 6a'
    would extract a stray '6', and Schedule B's 'OMB No. 1545-0074' header
    would inject '1545' as the ordinary-dividends value.

    Note: the _MONEY regex begins with ``\\s*`` so the match's `start` may
    point at a leading space rather than the first digit. We skip past any
    leading whitespace before inspecting the preceding character — otherwise
    two adjacent money values like ``7 37,020`` would cause the second value
    to be mis-flagged because the previous char (the last digit of the first
    value) is alphanumeric.
    """
    # Skip leading whitespace within the matched span so we examine the char
    # before the actual digit/sign, not before the leading space.
    while start < len(tail) and tail[start].isspace():
        start += 1
    # Preceded by `[Letter]-` → part of a form code like W-2 / 1099-R.
    if start >= 2 and tail[start - 1] == "-" and tail[start - 2].isalpha():
        return True
    # Preceded by `[Digit]-` → trailing segment of a hyphenated code like
    # 'OMB No. 1545-0074' or '2020-12-31'. The leading segment is handled
    # by the word-prefix check below ("No.", "OMB"), but the trailing
    # segment needs its own guard.
    if start >= 2 and tail[start - 1] == "-" and tail[start - 2].isdigit():
        return True
    # Preceded by word char (digit or letter) with no separator → glued
    # identifier, not a money column.
    if start >= 1 and (tail[start - 1].isalnum() or tail[start - 1] == "-"):
        return True
    # Preceding whitespace-separated word is a form / reference word
    # ("Form 1040", "Schedule D", "OMB No. 1545", "Line 7", "Section 199A").
    prefix = tail[:start].rstrip()
    if prefix:
        idx = max(prefix.rfind(" "), prefix.rfind("\t"))
        prev_word = (prefix[idx + 1:] if idx >= 0 else prefix).lower().strip(",;:()[]")
        if prev_word in _FORM_ID_PREFIX_WORDS:
            return True
    # Followed by a single lowercase letter and a word boundary → line-label
    # echo like '6a', '25b', '1z'. Real money values are never glued to a
    # trailing letter on IRS / vendor exports. We also require the matched
    # token itself to look like a bare line number (1-2 digits, no comma,
    # no decimal) — otherwise '37,020' followed by 'and' would false-fire.
    end = start
    while end < len(tail) and (tail[end].isdigit() or tail[end] in ",.-$"):
        end += 1
    token = tail[start:end]
    bare = re.fullmatch(r"-?\d{1,2}", token) is not None
    if bare and end < len(tail) and tail[end].isalpha() and tail[end].islower():
        # Confirm word boundary after the letter (avoid filtering '6abc').
        if end + 1 >= len(tail) or not tail[end + 1].isalpha():
            return True
    # Followed by `-[A-Za-z]` → form-code suffix like '1040-SR', '1099-R',
    # '5329-A'. The leading number is a form identifier, not money.
    if end < len(tail) - 1 and tail[end] == "-" and tail[end + 1].isalpha():
        return True
    return False


def _money_matches_in(tail: str) -> list:
    """All money matches in `tail` that are not inside form identifiers."""
    money_pat = re.compile(_MONEY)
    return [m for m in money_pat.finditer(tail) if not _is_form_id_digit(tail, m.start())]


_LINE_NO_ECHO = re.compile(r"(?:^|\s)(\d{1,2}[a-z]?)\s*$")
# Words that, when they're the LAST whitespace-separated token preceding a
# money match, mean the "money" is really a reference (line number, page
# number, column letter) embedded in form-instruction prose. Without this
# filter, "Combine lines 1a through 6 in column (h)" extracts a bare "6";
# "go to Part III on page 2" extracts "2"; etc.
_REF_PREFIX_WORDS = frozenset({
    "through", "line", "lines", "column", "columns", "page", "pages",
    "part", "parts", "form", "forms", "schedule", "sch", "box", "boxes",
    "section", "item", "items", "code", "codes", "paragraph", "subsection",
})


def _pick_money(tail: str, matches: list) -> "re.Match | None":
    """Choose the most-likely VALUE match from `matches` (all non-form-id
    money matches in `tail`).

    IRS / H&R Block packed-row layouts repeat the line number right before
    the value, e.g.::

        1 Wages, salaries, tips, etc. ......... 1 176,865
        3a Qualified dividends ....... 3a 1,374 b Ordinary dividends ....... 3b 2,223
        7 Capital gain or (loss). Attach Sch D ....... 7 37,020

    Naively taking the LAST money picks ``2,223`` (the *next column's* value)
    on packed rows, and naively taking the FIRST picks the trailing
    line-number echo (``1``, ``7``).

    Heuristic: prefer the first money whose IMMEDIATELY PRECEDING
    whitespace-separated token looks like a line-number echo (``\\d{1,2}[a-z]?``).
    If none qualify, fall back to the last match — but skip "reference"
    matches whose preceding word is in ``_REF_PREFIX_WORDS`` (the digit is
    really part of "lines 1 through 6" / "page 2" / "column (h)" prose,
    not a column value).
    """
    def preceding_word(m: "re.Match") -> str:
        s = m.start()
        while s < len(tail) and tail[s].isspace():
            s += 1
        prefix = tail[:s].rstrip()
        # Last whitespace-separated token.
        idx = max(prefix.rfind(" "), prefix.rfind("\t"))
        word = prefix[idx + 1:] if idx >= 0 else prefix
        return word.lower().strip(".,;:()[]")

    for m in matches:
        # The text BEFORE this money match, with any leading-of-match
        # whitespace skipped first (since _MONEY starts with \s*).
        s = m.start()
        while s < len(tail) and tail[s].isspace():
            s += 1
        prefix = tail[:s]
        if _LINE_NO_ECHO.search(prefix):
            return m
    # Fall-back path: pick the last match that ISN'T a reference fragment
    # from form-instruction prose.
    for m in reversed(matches):
        if preceding_word(m) not in _REF_PREFIX_WORDS:
            return m
    return None


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
            picked = _pick_money(tail, money_matches)
            if picked is not None:
                # Guard against trailing line-number echo with NO value
                # column. E.g. Form 8889 line 13 when the user has no
                # personal HSA contribution renders as
                #     "13 HSA deduction (see instructions). . . . . . . 13"
                # — the trailing "13" is the line-number echo column,
                # not a $13 deduction. If the picked match is a bare
                # 1-2 digit integer at end-of-line AND equals the
                # leading line-number on this same line, treat as no
                # value and fall through to the next-line scan.
                picked_str = picked.group(0).strip()
                is_echo = False
                if re.fullmatch(r"\d{1,2}", picked_str):
                    if tail[picked.end():].strip() == "":
                        m_lead = re.match(r"\s*(\d{1,2})[a-z]?\s", line)
                        if m_lead and m_lead.group(1) == picked_str:
                            is_echo = True
                if not is_echo:
                    try:
                        return _money(picked.group(0))
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
            # The loose-layout stream sometimes MERGES the next form row
            # into what should be the continuation of the prior label.
            # Detect that pattern: a 1-2 digit line-number followed by
            # 3+ alphabetic label chars within the first ~80 chars of
            # the line. This catches e.g. "Deduction for- 7 Capital gain"
            # without rejecting legitimate label-wraps like "term capital
            # gains or losses, go to Part II below..." (which has no
            # digit-then-label sequence early in the line).
            if re.search(r"(?:^|\s)\d{1,2}[a-z]?\s+[A-Za-z]{3,}", nxt_raw[:80]):
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
            # Fallback: "<line-number-echo> <integer>" continuation lines
            # like "8 0", "25c 0", "10c 220" — common when the value column
            # is rendered on its own row by vendor exports. Strict_money_pat
            # rejects bare 1-2 digit integers; allow them here because the
            # line-number echo gives us confidence this IS the value column.
            m_echo = re.match(r"^\s*\d{1,2}[a-z]?\s+(-?\d{1,6})\s*$", nxt_raw)
            if m_echo:
                try:
                    return _money(m_echo.group(1))
                except InvalidOperation:
                    pass
            break
    return None


LINE_PATTERNS: dict[str, list[str]] = {
    "wages":                   [# Actual IRS 1040 line 1a phrasing (strongest, try first
                                # so Form 8959 "1 Medicare wages and tips from Form W-2,
                                # box 5" can't preempt it).
                                r"\b1\s*a\b[^\n]{0,80}?Form\(s\)\s*W-?2[^\n]{0,30}?box\s*1",
                                # Looser: "Form(s) W-2" anywhere on the line.
                                r"\b1\s*a\b[^\n]{0,80}?Form\(s\)\s*W-?2",
                                # IRS fillable-PDF tooltip for TY2022+ line 1a
                                # often omits the "1a" prefix and reads just
                                # "Total amount from Form(s) W-2, box 1
                                # (see instructions)". Match the tooltip
                                # text directly so AcroForm extraction picks
                                # it up regardless of line label.
                                r"Total\s+amount\s+from\s+Form\(s\)\s*W-?2",
                                r"from\s+Form\(s\)\s*W-?2,?\s*box\s*1\b",
                                # Line 1z is the W-2 totals line on post-2021 1040
                                r"\b1\s*z\b[^\n]{0,80}?Add\s+lines?\s*1a\s+through\s+1h",
                                r"Add\s+lines?\s*1a\s+through\s+1h",
                                r"Line\s*1[az]?\s+Wages",
                                # FreeTaxUSA summary-page phrasings
                                r"Wages,\s*salaries,?\s*tips",
                                r"Wages\s+and\s+salaries",
                                # Last-resort loose pattern. Negative lookahead
                                # excludes Form 8959 ("Medicare wages...box 5")
                                # and Form 8919 contexts so we don't grab
                                # Box-5 Medicare wages instead of Box-1 wages.
                                r"\b1\s*[az]?\b(?![^\n]*Medicare\s+wages)(?![^\n]*box\s*5)[^\n]{0,40}?Wages"],
    "interest_income":         [r"Line\s*2b\b[^\n]{0,40}?Taxable interest",
                                r"\b2\s*b\b[^\n]{0,40}?Taxable interest",
                                # Loose fallback (FreeTaxUSA-summary phrasing).
                                # Skipped on lines that look like a Schedule
                                # B / form-instructions header (those embed
                                # "Taxable interest" in prose rather than
                                # the 1040 line 2b value column).
                                r"\bTaxable\s+interest\b"],
    "qualified_dividends":     [r"Line\s*3a\b[^\n]{0,40}?Qualified dividends",
                                r"\b3\s*a\b[^\n]{0,40}?Qualified dividends",
                                # Loose fallback for vendor summary pages.
                                # Anchor to 'qualified dividends' followed
                                # within 40 chars by the 1040 line marker
                                # (column echo '3a' or word 'line 3a') so
                                # we don't pick up Schedule D's "Qualified
                                # Dividends and Capital Gain Tax Worksheet"
                                # heading or 1040-NR instructions text.
                                r"\bQualified\s+dividends\b[^\n]{0,40}?3\s*a\b",
                                r"\b3\s*a\s+Qualified\s+dividends",
                                # Bare tooltip variant — IRS AcroForm /TU
                                # fields read "Qualified dividends" alone.
                                # Negative lookahead rejects Schedule D's
                                # "Qualified Dividends and Capital Gain Tax
                                # Worksheet" heading.
                                r"\bQualified\s+dividends\b(?!\s+and\b)(?!\s+and\s+Capital)",
                                ],
    "ordinary_dividends":      [r"Line\s*3b\b[^\n]{0,40}?Ordinary dividends",
                                r"\b3\s*b\b[^\n]{0,40}?Ordinary dividends",
                                # Loose fallback: require 3b context within
                                # 40 chars or "b Ordinary dividends" packed
                                # format. Plain "Ordinary dividends" alone
                                # would false-match Schedule B's "Interest
                                # and Ordinary Dividends" title.
                                r"\bOrdinary\s+dividends\b[^\n]{0,40}?3\s*b\b",
                                r"\bb\s+Ordinary\s+dividends",
                                # Bare tooltip variant — negative lookbehind
                                # rejects Sch B's "Interest and Ordinary
                                # Dividends" heading.
                                r"(?<!and\s)\bOrdinary\s+dividends\b",
                                ],
    "long_term_capital_gains": [
                                # Prefer Schedule D line 15 ("Net long-term capital
                                # gain or (loss). Combine lines 8a through 14")
                                # when the PDF actually contains Schedule D —
                                # 1040 line 7 is the COMBINED ST+LT total, not
                                # the long-term portion, so using line 7 for
                                # LTCG overstates long-term and understates tax
                                # on the short-term piece.
                                r"\bNet\s+long[-\s]term\s+capital\s+gain\s+or\s+\(loss\)\.?\s+Combine\s+lines?\s*8a\s+through\s+14",
                                # FreeTaxUSA summary — distinguishes LT vs ST
                                r"\bNet\s+long[-\s]term\s+capital\s+gain",
                                r"\bLong[-\s]term\s+capital\s+gain",
                                # Fallback: 1040 line 7 ("Capital gain or
                                # (loss). Attach Schedule D"). This is the
                                # combined total; treat as LTCG only when
                                # Schedule D wasn't found. The engine taxes
                                # LTCG at preferential rates so this fallback
                                # UNDERSTATES tax when the gain is actually
                                # short-term — flagged via warning downstream.
                                r"Line\s*7\b[^\n]{0,80}?Capital gain",
                                r"\b7\b[^\n]{0,80}?Capital gain\s+or\s+\(loss\)",
                                r"Capital\s+gain\s+or\s+\(loss\)\.?\s+Attach\s+Schedule\s*D",
                                ],
    "short_term_capital_gains":[
                                # Prefer Schedule D line 7 ("Net short-term
                                # capital gain or (loss). Combine lines 1a
                                # through 6"). Anchor on the full phrasing
                                # so we don't false-match the Part I header
                                # ("Short-Term Capital Gains and Losses") or
                                # line 6 ("Short-term capital LOSS carryover").
                                r"\bNet\s+short[-\s]term\s+capital\s+gain\s+or\s+\(loss\)\.?\s+Combine\s+lines?\s*1a\s+through\s+6",
                                # FreeTaxUSA summary phrasings
                                r"\bNet\s+short[-\s]term\s+capital\s+gain",
                                r"\bShort[-\s]term\s+capital\s+gain\b(?![^\n]{0,40}?loss\s+carryover)",
                                ],
    "se_income":               [r"Line\s*3\b[^\n]{0,40}?Business income",
                                r"Schedule\s*C[^\n]{0,40}?Net profit",
                                r"\b3\b[^\n]{0,40}?Business income\s+or\s+\(loss\)",
                                # Schedule SE line 6 is the authoritative net
                                # earnings figure; pin to its line-prefix +
                                # full label so we don't accidentally match
                                # the Form 8959 line-8 cross-reference (which
                                # only appears when SE tax > 0 and whose
                                # next-line text contains "Form 1040" — that
                                # form-id was being mis-extracted as a
                                # $1,040 SE-income value).
                                r"^\s*6\s+Net\s+earnings\s+from\s+self[-\s]employment",
                                ],
    "other_ordinary_income":   [
                                # Prefer Schedule 1 line 8 ("Other income.
                                # List type and amount") — this is the TRUE
                                # "other income" bucket. 1040 line 8 is the
                                # PASSTHROUGH of Sch 1 line 9 (which also
                                # contains unemployment from Sch 1 line 7);
                                # extracting 1040 line 8 directly would
                                # double-count unemployment_compensation.
                                r"\b8\s+Other\s+income\.?\s+List\s+type",
                                # FreeTaxUSA / vendor summary phrasings that
                                # explicitly label "other ordinary income"
                                # (distinct from unemployment).
                                r"\bOther\s+ordinary\s+income\b",
                                ],
    "pension_distributions_taxable": [
                                r"\b5\s*b\b[^\n]{0,40}?(?:Pensions|Taxable amount)",
                                r"\bPensions\s+and\s+annuities",
                                # IRS line 5b tooltip
                                r"Pensions\s+and\s+annuities[^\n]{0,40}?Taxable\s+amount"],
    "ira_distributions_taxable": [
                                r"\b4\s*b\b[^\n]{0,40}?(?:IRA|Taxable amount)",
                                r"\bIRA\s+distributions\b[^\n]{0,40}?taxable",
                                # IRS line 4b tooltip
                                r"IRA\s+distributions[^\n]{0,40}?Taxable\s+amount"],
    "social_security_benefits":[
                                # Prefer line 6b (TAXABLE amount), not line 6a
                                # (gross benefits). Packed-row format puts both
                                # on one line: "6a Social security benefits ...
                                # 6a {gross} b Taxable amount ... 6b {taxable}".
                                # Anchoring on "Taxable amount" gets _pick_money
                                # to the right column. _is_form_id_digit now
                                # filters the trailing "6b" line-label echo.
                                r"\bSocial\s+security\s+benefits[^\n]{0,200}?Taxable\s+amount",
                                r"\b6\s*b\b[^\n]{0,40}?Taxable\s+amount",
                                # Multi-row layout fallback
                                r"\bb\s+Taxable\s+amount[^\n]{0,40}?6\s*b\b",
                                # Last resort — gross benefits 6a (may overstate
                                # if entire amount isn't taxable; engine applies
                                # the §86 worksheet on top).
                                r"\b6\s*a\b[^\n]{0,40}?Social\s+security\s+benefits",
                                ],
    "unemployment_compensation":[r"\bUnemployment\s+compensation"],
    "hsa_deduction":           [
                                # Schedule 1 line 13 (TY2019+) / line 25 (TY2018) /
                                # 1040 line 25 (pre-2018). All share the phrase
                                # "Health savings account deduction" verbatim and
                                # all reference Form 8889.
                                r"\bHealth\s+savings\s+account\s+deduction\b",
                                r"Form\s*8889\b[^\n]{0,80}?deduction",
                                r"\bHSA\s+deduction\b",
                                # Form 8889 line 13 ("HSA deduction. Smaller of
                                # line 2 or line 12") — when the user includes
                                # the 8889 itself, this is the canonical value.
                                r"\b13\b[^\n]{0,80}?HSA\s+deduction",
                                ],
    "other_adjustments":       [r"Line\s*26\b[^\n]{0,80}?Total adjustments to income",
                                # Schedule 1 line 26 in FreeTaxUSA
                                r"\b10\b[^\n]{0,80}?Adjustments to income\s+from\s+Schedule\s*1"],
    "charitable_contributions_non_itemizer": [
                                # 1040 line 10b (TY2020 — above-the-line) and
                                # 1040 line 12b (TY2021 — below-the-line). Both
                                # share the verbatim "Charitable contributions
                                # if you take the standard deduction" phrase.
                                r"Charitable\s+contributions\s+if\s+you\s+take\s+the\s+standard\s+deduction",
                                ],
    "schedule_2_other_taxes_reported": [
                                # 1040 line 23 — Schedule 2 Part II "Other Taxes"
                                # total. Verbatim phrasing varies slightly by
                                # year (line numbers in the cross-reference
                                # change: Sch 2 line 10 in TY2020, line 21
                                # TY2022+) but always begins with "Other taxes".
                                r"Other\s+taxes,?\s+including\s+self-employment\s+tax,?\s+from\s+Schedule\s*2",
                                r"Line\s*23\b[^\n]{0,80}?Other\s+taxes",
                                ],
    "child_tax_credit_reported": [
                                # 1040 line 19 — nonrefundable CTC + Credit
                                # for Other Dependents from Schedule 8812.
                                # Anchored on the leading "19" line-number
                                # prefix; without it, case-insensitive
                                # matching on the word "Child" silently
                                # catches line 28 ("additional child tax
                                # credit") and pulls in its dollar value.
                                r"^\s*19\s+(?:Nonrefundable\s+)?Child\s+tax\s+credit",
                                r"^\s*19\s+Child\s+tax\s+credit\s+or\s+credit\s+for\s+other\s+dependents",
                                ],
    "additional_ctc_reported": [
                                # 1040 line 28 — Refundable ACTC / ARPA
                                # refundable CTC from Schedule 8812.
                                r"^\s*28\s+Refundable\s+(?:child\s+tax\s+credit|additional\s+child\s+tax\s+credit)",
                                r"^\s*28\s+Additional\s+child\s+tax\s+credit\s+from\s+Schedule\s*8812",
                                ],
    "deduction_reported": [
                                # 1040 line 12 — Standard or itemized
                                # deduction as actually printed. TY2024
                                # and earlier render this as a single
                                # row prefixed "12 ..."; TY2025+ (OBBB
                                # restructure) splits the row into
                                # 12a/b/c/d components with the TOTAL
                                # on 12e — the leading-line-number
                                # column shows just "e" with the full
                                # "12e" appearing as the trailing
                                # echo. Both layouts are caught here.
                                r"^\s*12\s+Standard\s+deduction\s+or\s+itemized\s+deductions",
                                r"^\s*12\b[^\n]{0,80}?Itemized\s+deductions\s+\(from\s+Schedule\s*A\)",
                                r"^\s*e\s+Standard\s+deduction\s+or\s+itemized\s+deductions",
                                ],
    "foreign_taxes_paid":      [r"Line\s*1\b[^\n]{0,80}?Foreign tax credit",
                                r"Foreign tax credit\.?\s+Attach\s+Form\s*1116"],
    "qualified_reit_ptp_dividends": [
                                # Form 8995 line 6 — Qualified REIT dividends
                                # and publicly traded partnership (PTP) income.
                                # These are taxed as ordinary dividends but
                                # eligible for the 20% Section 199A deduction.
                                r"Qualified\s+REIT\s+dividends\s+and\s+(?:publicly\s+traded\s+partnership|PTP)",
                                r"\b6\b[^\n]{0,80}?REIT\s+dividends",
                                ],
    "agi_reported":            [r"Line\s*11\b[^\n]{0,40}?Adjusted gross income",
                                r"\b11\b[^\n]{0,80}?Adjusted gross income",
                                r"\bAdjusted\s+gross\s+income\b"],
    "taxable_income_reported": [r"Line\s*15\b[^\n]{0,40}?Taxable income",
                                r"\b15\b[^\n]{0,80}?Taxable income",
                                r"\bTaxable\s+income\b"],
    "total_tax_reported":      [r"Line\s*24\b[^\n]{0,40}?Total tax",
                                r"\b24\b[^\n]{0,80}?(?:total tax|Add lines\s*22\s+and\s+23)",
                                r"\bTotal\s+tax\b"],
    "federal_withholding":     [
                                # Prefer line 25d ("Add lines 25a through 25c")
                                # — the W-2 + 1099 + other-forms TOTAL. Older
                                # 1040s (pre-2020) had one withholding line
                                # (25 or 25a) and no breakout, so the 25a
                                # fallbacks below still cover them.
                                r"\b25\s*d\b[^\n]{0,80}?Add\s+lines?\s*25a",
                                r"\bd\s+Add\s+lines?\s*25a\s+through\s+25c",
                                r"\bAdd\s+lines?\s*25a\s+through\s+25c",
                                # Fallback: line 25a or single-line withholding
                                # (only W-2 — UNDERSTATES total when 1099
                                # withholding is also present).
                                r"Line\s*25a?\b[^\n]{0,80}?Federal income tax withheld",
                                r"\b25\s*a?\b[^\n]{0,80}?Federal income tax withheld",
                                r"\bFederal\s+(?:income\s+)?tax\s+withheld",
                                ],
    "estimated_payments":      [r"Line\s*26\b[^\n]{0,80}?estimated tax payments",
                                r"\b26\b[^\n]{0,80}?estimated tax payments",
                                r"\bEstimated\s+tax\s+payments\b"],
    "qualifying_children":     [r"Number of qualifying children",
                                r"Qualifying children[^\n]{0,40}?for\s+child\s+tax\s+credit"],
    # Internal-only: 1040 line 8 (the Sch-1-line-9 PASSTHROUGH total). Tracked
    # separately so post-processing can subtract unemployment_compensation
    # before assigning what's left to other_ordinary_income — the dollars on
    # 1040 line 8 ALREADY include unemployment, so blindly using them as
    # "other" double-counts. Stripped before constructing Return.
    "_form1040_line8_total":   [r"\b8\b[^\n]{0,60}?Other\s+income\s+from\s+Schedule\s*1",
                                r"\bLine\s*8\b[^\n]{0,60}?Other\s+income\s+from\s+Schedule\s*1",
                                r"\bOther\s+income\s+from\s+Schedule\s*1"],
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

# ─── Page classification: only extract from real IRS form pages ──────────────
# Vendors (FreeTaxUSA, TurboTax, H&R Block) often print a friendly summary
# page first with rounded / partial figures that don't match the underlying
# 1040 (e.g. summary "Wages and Salaries" may roll Sch C net profit into a
# single total). If we extract from those pages, the dashboard reports the
# wrong numbers. We classify each page and only consider those that look
# like a genuine IRS form.
#
# A page qualifies as an IRS form page if it contains ANY of these strong
# signals:
#   - "Form 1040" header next to the IRS phrase or OMB number
#   - "Schedule X (Form 1040)" header  (X ∈ 1,2,3,A,B,C,D,E,SE,EIC,H,J,R)
#   - "Form NNNN" with an OMB number nearby
#   - "Department of the Treasury — Internal Revenue Service" footer
#   - The IRS Cat. No. footer (e.g. "Cat. No. 11320B")
_FORM_PAGE_PATTERNS = [
    re.compile(r"Form\s*1040(?:-SR|-NR|-X)?\b", re.IGNORECASE),
    re.compile(r"Schedule\s+(?:[1-3]|A|B|C|D|E|SE|EIC|H|J|R|8812)\b\s*\(?\s*Form\s*1040\)?",
               re.IGNORECASE),
    re.compile(r"\bForm\s*\d{3,5}[A-Z]?\b", re.IGNORECASE),  # Form 8606, 8949, 2441, etc.
    re.compile(r"Department\s+of\s+the\s+Treasury", re.IGNORECASE),
    re.compile(r"Internal\s+Revenue\s+Service", re.IGNORECASE),
    re.compile(r"OMB\s*No\.\s*1545-\d{4}", re.IGNORECASE),
    re.compile(r"\bCat\.?\s*No\.\s*\d{4,6}[A-Z]?\b", re.IGNORECASE),
    re.compile(r"U\.?S\.?\s+Individual\s+Income\s+Tax\s+Return", re.IGNORECASE),
]

# Pages that match these are explicit summary / cover pages we want to
# *exclude* even if a stray "Form 1040" mention appears on them.
_SUMMARY_PAGE_PATTERNS = [
    re.compile(r"Tax\s+Return\s+Summary", re.IGNORECASE),
    re.compile(r"Return\s+Summary", re.IGNORECASE),
    re.compile(r"\bSummary\s+of\s+(?:Your\s+)?(?:Return|Tax)", re.IGNORECASE),
    re.compile(r"\bElectronic\s+Filing\s+Instructions", re.IGNORECASE),
    re.compile(r"^\s*Cover\s*Page\s*$", re.IGNORECASE | re.MULTILINE),
]


def _is_form_page(text: str) -> bool:
    """True iff `text` looks like a real IRS form page (not a vendor summary
    cover, instruction page, or filing-confirmation page).

    Decision rule: require BOTH (a) a positive IRS-form signal AND (b) no
    explicit summary-page marker. A single OMB number alone is sufficient
    because vendor summary pages never include OMB numbers."""
    if not text or not text.strip():
        return False
    if any(s_pat.search(text) for s_pat in _SUMMARY_PAGE_PATTERNS):
        return False
    return any(pat.search(text) for pat in _FORM_PAGE_PATTERNS)


def _form_pages(pages: list[str]) -> list[str]:
    """Filter `pages` down to only those that look like real IRS form pages.
    If NO page qualifies (e.g. unusual export with only a summary), falls
    back to the original page list so we don't refuse to import anything."""
    keep = [p for p in pages if _is_form_page(p)]
    return keep if keep else pages


def _layout_text(page, y_tol: float = 3.0) -> str:
    """Reconstruct a page's text from positioned words, clustering by
    y-coordinate so each visual row becomes ONE text line.

    Why this is needed: pdfplumber's default ``page.extract_text()`` walks
    the page's text stream in PDF source order. When an IRS-form PDF
    renders labels in a left column and amounts in a far-right "box" column
    with a wide gap between them — or worse, renders all labels first and
    all amounts in a second pass at slightly different y coordinates (which
    is what many fillable/printed IRS PDFs actually do) — the default
    extractor emits the label and the amount on *separate* output lines
    (sometimes with several unrelated rows in between), which our
    same-line regexes can't navigate. The result is an import where
    everything parses to $0.

    By contrast, ``extract_words()`` returns each word with its (x0, top,
    x1, bottom) bounding box. We cluster on ``top`` (within ``y_tol``) to
    recover the visual row, then sort each cluster by ``x0`` so the label
    and its amount end up on the same reconstructed line — which the
    existing regex/value picker can then handle correctly.

    Pass a larger ``y_tol`` to merge label rows with value rows that were
    drawn at a small vertical offset (common in fillable forms where the
    user-entered value is placed a few points above or below the label
    baseline).

    Returns an empty string if pdfplumber can't yield words for the page.
    """
    try:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    except Exception:
        return ""
    if not words:
        return ""
    words.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[str] = []
    cur: list[dict] = []
    cur_y: float | None = None
    for w in words:
        if cur_y is None or abs(w["top"] - cur_y) <= y_tol:
            cur.append(w)
            if cur_y is None:
                cur_y = w["top"]
        else:
            cur.sort(key=lambda x: x["x0"])
            lines.append(" ".join(x["text"] for x in cur))
            cur = [w]
            cur_y = w["top"]
    if cur:
        cur.sort(key=lambda x: x["x0"])
        lines.append(" ".join(x["text"] for x in cur))
    return "\n".join(lines)


def _extract_text_per_page(path: Path) -> tuple[list[str], list[list[str]], bool]:
    """Returns (default_text_pages, layout_text_streams, ocr_used).

    Multiple parallel text streams are produced for each page:

    - ``default_text_pages[i]`` is whatever ``page.extract_text()`` returns
      (PDF source-order text reconstruction, what we've always used).
    - ``layout_text_streams[k][i]`` is rebuilt by clustering
      ``extract_words()`` output by y-coordinate so each visual row becomes
      one text line. We produce streams at two y-tolerances:
        - tight (3pt) preserves distinct form rows cleanly
        - loose (8pt) merges label and value rows that were drawn at small
          vertical offsets — common in fillable IRS forms where the
          user-entered amount sits a few points above/below the label
          baseline, which otherwise causes the default extractor (and the
          tight layout pass) to split them onto separate output lines.

    All streams are passed to field extraction so a label/value pair
    broken in one stream can still match in another. When pdfplumber finds
    no text on most pages (scanned PDFs without a text layer), we fall
    back to Tesseract OCR — in which case the OCR text is reused as the
    sole stream.
    """
    default_pages: list[str] = []
    layout_tight: list[str] = []
    layout_loose: list[str] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for p in pdf.pages:
                default_pages.append(p.extract_text() or "")
                layout_tight.append(_layout_text(p, y_tol=3.0))
                layout_loose.append(_layout_text(p, y_tol=8.0))
    except Exception as e:
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

    nonempty = sum(1 for p in default_pages if p.strip())
    if nonempty >= max(1, len(default_pages) // 2):
        return default_pages, [layout_tight, layout_loose], False

    # OCR fallback (soft-deps).
    try:
        import pdf2image                          # type: ignore[import-not-found]
        import pytesseract                        # type: ignore[import-not-found]
    except Exception:
        return default_pages, [layout_tight, layout_loose], False
    try:
        images = pdf2image.convert_from_path(str(path), dpi=300)
    except Exception:
        return default_pages, [layout_tight, layout_loose], False
    ocr_pages: list[str] = []
    for img in images:
        try:
            ocr_pages.append(pytesseract.image_to_string(img))
        except Exception:
            ocr_pages.append("")
    return ocr_pages, [list(ocr_pages)], True


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


# W-2 Box 12 elective-deferral parser. The 1040 itself doesn't show 401(k)
# contributions (they're already excluded from Box 1 wages), so the only
# way to recover them is to look at the W-2 form text — which is included
# in many vendor-bundled PDF returns. Codes we map:
#   D   → traditional 401(k)              traditional_401k_contributions
#   AA  → Roth 401(k)                     roth_401k_contributions
#   BB  → Roth 403(b)                     roth_401k_contributions (bucketed)
#   EE  → Roth governmental 457(b)        roth_401k_contributions (bucketed)
#   E   → 403(b) salary reduction         traditional_401k_contributions (bucketed)
#   G   → 457(b) salary reduction         traditional_401k_contributions (bucketed)
#   S   → SIMPLE 401(k) / 408(p)          traditional_401k_contributions (bucketed)
# We sum across multiple W-2s (joint returns / multiple employers).
_W2_FINGERPRINT = re.compile(
    # Must be the actual W-2 form, NOT a 1040 line that REFERENCES the
    # W-2. Plain "Form W-2" appears all over the 1040 (e.g., line 25a
    # "Federal income tax withheld from Form(s) W-2") so it's too noisy.
    # We only accept phrases that exclusively appear on the W-2 itself:
    #   * "Wage and Tax Statement" — the W-2's letterhead title.
    #   * "Box 12[a-d]" with a required sub-letter — only W-2 rows
    #     use these markers; the 1040 never references them this way.
    r"Wage\s+and\s+Tax\s+Statement"
    r"|\bBox\s*12[a-d]\b",
    re.IGNORECASE,
)
# Match "12a D 19,500.00" / "12b  AA 5000" / "D 19500.00" inside a Box 12 region.
_BOX12_ROW = re.compile(
    r"(?:^|\s)(?:12[a-d]\s+)?([A-Z]{1,2})\s+\$?\s*"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\b"
)
_TRAD_CODES = {"D", "E", "F", "G", "H", "S"}
_ROTH_CODES = {"AA", "BB", "EE"}
# Code W on box 12 is "Employer contributions to a Health Savings Account
# (including employee pre-tax payroll contributions)". This is INFO-ONLY —
# already excluded from Box 1 wages, no Schedule 1 adjustment — but
# important for total HSA balance tracking and the Advisor's HSA-cap rule.
_HSA_PAYROLL_CODES = {"W"}

def _extract_w2_box12_deferrals(joined_text: str) -> dict[str, Decimal]:
    """Return totals across all W-2 forms in the joined PDF text for
    pre-tax (traditional) and Roth elective deferrals reported in Box 12,
    plus HSA pre-tax payroll contributions (code W). Returns {} if no W-2
    fingerprint is found.
    """
    if not _W2_FINGERPRINT.search(joined_text):
        return {}
    trad = Decimal(0)
    roth = Decimal(0)
    hsa_payroll = Decimal(0)
    # Walk line by line, only consider lines that appear to be Box-12 data
    # (line starts with "12a"/"12b"/etc OR appears within ~10 lines after
    # a "Box 12" marker). This avoids picking up "Form 1099-R" code letters
    # or unrelated capital-letter prose like "AA" used in addresses.
    lines = joined_text.splitlines()
    in_box12 = 0

    def consume_row(raw_line: str) -> bool:
        """Try to pull a code+amount pair from this line. Returns True if
        we matched a known code (used to extend the in-region window)."""
        nonlocal trad, roth, hsa_payroll
        matched = False
        # Strict full-line form: "[Box ]?12a D 19,500.00" — possibly the
        # whole line. The regex permits leading "Box " and optional
        # 12<letter> prefix.
        full = re.match(
            r"^\s*(?:Box\s*)?(?:12[a-d]\s+)?([A-Z]{1,2})\s+\$?\s*"
            r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*$",
            raw_line,
            re.IGNORECASE,
        )
        rows: list[tuple[str, str]] = []
        if full:
            rows.append((full.group(1), full.group(2)))
        else:
            # Mid-line form: "Box 12a D 19500.00 12b AA 5000.00".
            for m in re.finditer(
                r"(?:^|\s)(?:Box\s*)?12[a-d]\s+([A-Z]{1,2})\s+\$?\s*"
                r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\b",
                raw_line,
                re.IGNORECASE,
            ):
                rows.append((m.group(1), m.group(2)))
        for code, amt in rows:
            code = code.upper()
            try:
                v = _money(amt)
            except InvalidOperation:
                continue
            if v <= 0:
                continue
            if code in _TRAD_CODES:
                trad += v
                matched = True
            elif code in _ROTH_CODES:
                roth += v
                matched = True
            elif code in _HSA_PAYROLL_CODES:
                hsa_payroll += v
                matched = True
        return matched

    for raw in lines:
        is_marker = re.search(r"\bBox\s*12[a-d]?\b", raw, re.IGNORECASE)
        starts_with_12letter = bool(re.match(r"^\s*12[a-d]\b", raw))
        if is_marker or starts_with_12letter:
            consume_row(raw)
            in_box12 = 6
            continue
        if in_box12 > 0:
            in_box12 -= 1
            if consume_row(raw):
                in_box12 = 4
    out: dict[str, Decimal] = {}
    if trad > 0:
        out["traditional_401k_contributions"] = trad
    if roth > 0:
        out["roth_401k_contributions"] = roth
    if hsa_payroll > 0:
        # Maps to Return.hsa_contributions (employee + employer pre-tax HSA
        # routed through payroll). NOT the same as hsa_deduction, which is
        # the post-tax direct contribution claimed on Sch 1.
        out["hsa_contributions"] = hsa_payroll
    return out


# ──────────────────── Form 8889 (HSA) ────────────────────
#
# Form 8889 is filed alongside the 1040 whenever the taxpayer has any
# HSA activity. It carries authoritative HSA contribution data even when
# the W-2 isn't bundled in the PDF:
#
#   Line 1  Coverage under an HDHP — Self-only / Family
#   Line 2  HSA contributions YOU made (or on your behalf), excluding
#           employer/cafeteria-plan contributions and rollovers.
#   Line 9  Employer contributions made to your HSAs (THIS INCLUDES
#           amounts you elected to contribute through a cafeteria plan,
#           i.e. exactly what W-2 Box 12 code W reports).
#   Line 13 HSA deduction = min(line 2, line 12) → flows to Sch 1 line 13
#
# We map line 9 → Return.hsa_contributions (same semantic as Box 12 W).
# Line 13 / Sch 1 line 13 already feeds Return.hsa_deduction via
# LINE_PATTERNS, so direct contributions are also covered.

_FORM_8889_FINGERPRINT = re.compile(
    r"Form\s*8889\b|Health\s+Savings\s+Accounts?\s*\(HSAs?\)",
    re.IGNORECASE,
)

def _extract_form_8889(joined_text: str) -> dict[str, object]:
    """Pull HSA contribution data from any Form 8889 in the PDF text.

    Returns a dict that may contain:
        hsa_contributions: Decimal — from line 9 (employer + cafeteria-plan)
        _form_8889_present: bool   — sentinel; tells caller we saw the form
        _hsa_coverage: str         — "self" or "family" if line 1 detected
    Returns {} if no Form 8889 fingerprint is present.
    """
    if not _FORM_8889_FINGERPRINT.search(joined_text):
        return {}
    out: dict[str, object] = {"_form_8889_present": True}

    # Line 9: "Employer contributions made to your HSAs for <YYYY>".
    # Consume the year in the anchor so _first_money_after's tail starts
    # AFTER the year — otherwise the 4-digit year gets picked as money.
    line9 = _first_money_after(
        r"Employer\s+contributions\s+made\s+to\s+your\s+HSAs?(?:\s+for\s+\d{4})?",
        joined_text,
    )
    if line9 is None:
        line9 = _first_money_after(
            r"\b9\b[^\n]{0,40}?Employer\s+contributions(?:[^\n]{0,40}?\d{4})?",
            joined_text,
        )
    if line9 is not None and line9 > 0:
        out["hsa_contributions"] = line9

    # Line 1: HDHP coverage type. The form has two adjacent checkboxes —
    # Self-only and Family. Vendors render the marked one as ☒, [X],
    # ✓, or other glyphs. Detect by finding a "marked-bracket" pattern
    # (e.g. "[X]", "[✓]", "☒") followed within ~20 chars by either word.
    marked = (
        r"(?:\[\s*[XV✓\u2713]\s*\]|[☒\u2611])"  # [X] / [✓] / ☒ / ☑
    )
    for raw in joined_text.splitlines():
        if not re.search(r"\bHDHP\b", raw, re.IGNORECASE):
            continue
        m_self = re.search(marked + r"\s*Self[-\s]?only", raw, re.IGNORECASE)
        m_fam = re.search(marked + r"\s*Family", raw, re.IGNORECASE)
        if m_fam and not m_self:
            out["_hsa_coverage"] = "family"
        elif m_self and not m_fam:
            out["_hsa_coverage"] = "self"
        break
    return out


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


def _merge_field_results(*results: tuple[dict[str, Decimal], int, list[str]]) -> tuple[dict[str, Decimal], int, list[str]]:
    """Pick the most-complete (fields, children, warnings) tuple from
    ``results``. The "winner" is the result with the largest dict; ties
    are broken in argument order so existing fixtures (where default-text
    extraction has always worked) keep their prior behavior.

    Why "most complete" instead of "first non-None per field":
    default-text extraction on PDFs whose labels and values are rendered
    on separate lines (e.g. fillable IRS forms) sometimes still produces
    a value — but it's the WRONG value (an adjacent row's amount or a
    bare line-number). Per-field merging treats those wrong values as
    truthy and lets them poison the result. Picking the single stream
    that recovered the most fields is safer: it lets the loose-layout
    pass replace a broken default-text pass wholesale, instead of being
    overwritten field-by-field.

    Fields from runner-up streams that aren't in the winner are still
    folded in (set-default semantics) so a partial recovery from another
    stream isn't discarded entirely.
    """
    if not results:
        return {}, 0, []
    winner_idx = max(range(len(results)), key=lambda i: len(results[i][0]))
    base_fields, base_children, base_warnings = results[winner_idx]
    merged = dict(base_fields)
    children = base_children
    warnings = list(base_warnings)
    for i, (fields, ch, ws) in enumerate(results):
        if i == winner_idx:
            continue
        for k, v in fields.items():
            merged.setdefault(k, v)
        if ch and not children:
            children = ch
        for w in ws:
            if w not in warnings:
                warnings.append(w)
    return merged, children, warnings


def import_pdf(path: Path) -> Imported:
    from taxlens.importers.import_log import ImportLogger, logging_enabled
    logger: ImportLogger | None = ImportLogger(source_path=path) if logging_enabled() else None
    if logger is not None:
        logger.section("Source")
        logger.kv("path", str(path))

    # PASS 0: AcroForm widgets. If the PDF embeds field values directly
    # (most IRS fillable forms and many vendor exports do), reading them
    # from the form dictionary is dramatically more reliable than scraping
    # the rendered text — values come straight from the source instead of
    # being inferred from layout. We still run the text-based extractor
    # to fill in any gaps and to detect year/status.
    from taxlens.importers.acroform import extract_acroform_fields, extract_acroform_meta
    acroform_fields, acroform_warnings = extract_acroform_fields(path, logger=logger)
    acroform_meta = extract_acroform_meta(path) if acroform_fields else {}

    default_pages, layout_streams, ocr_used = _extract_text_per_page(path)
    # A page qualifies as a real IRS form page if EITHER its default text or
    # any of its layout-reconstructed texts shows IRS-form markers, and no
    # stream shows an explicit summary marker.
    def keep_page(idx: int) -> bool:
        d = default_pages[idx]
        layouts = [ls[idx] for ls in layout_streams]
        for text in [d, *layouts]:
            if any(p.search(text) for p in _SUMMARY_PAGE_PATTERNS):
                return False
        return _is_form_page(d) or any(_is_form_page(l) for l in layouts)

    form_indices = [i for i in range(len(default_pages)) if keep_page(i)]
    if not form_indices:
        form_indices = list(range(len(default_pages)))
    default_form_pages = [default_pages[i] for i in form_indices]
    layout_form_streams = [[ls[i] for i in form_indices] for ls in layout_streams]
    summary_excluded = len(default_pages) - len(form_indices)

    # Detect year/status from any available stream.
    tax_year: int | None = acroform_meta.get("tax_year")
    filing_status: FilingStatus | None = None
    for stream in [default_form_pages, *layout_form_streams, default_pages, *layout_streams]:
        if tax_year is None:
            tax_year = _detect_year(stream)
        if filing_status is None:
            filing_status = _detect_status(stream)
        if tax_year is not None and filing_status is not None:
            break

    # Run field extraction against EVERY text stream; the first stream that
    # produces a value for a given field wins, in priority order:
    #   1. default text  (most conservative; preserves prior behavior)
    #   2. tight layout  (handles wide column gaps)
    #   3. loose layout  (handles small vertical offsets in fillable forms)
    # This is the key robustness fix: a label/value pair that one stream
    # splits across non-adjacent lines will still be recovered from another.
    default_result = _extract_fields(default_form_pages)
    layout_results = [_extract_fields(stream) for stream in layout_form_streams]
    fields, children, fwarnings = _merge_field_results(default_result, *layout_results)
    warnings = list(fwarnings)

    # Reconciliation passthrough fields can legitimately be $0 on the
    # source 1040 even though the label IS present (e.g. line 19
    # nonrefundable CTC = blank/0 when the filer's credit was instead
    # claimed as the refundable line 28 ACTC, or line 28 = blank when
    # the credit was fully absorbed nonrefundable). Distinguishing
    # "$0 claimed" from "label not present" matters: the engine uses
    # these as caps on its modeled credit, so leaving them as None
    # would let the engine over-claim. Backfill 0 whenever the label
    # text appears on the page but no money value followed it.
    _ZERO_BACKFILL_LABELS = {
        "child_tax_credit_reported": [
            re.compile(r"^\s*19\s+(?:Nonrefundable\s+)?Child\s+tax\s+credit", re.IGNORECASE | re.MULTILINE),
        ],
        "additional_ctc_reported": [
            re.compile(r"^\s*28\s+Refundable\s+(?:child\s+tax\s+credit|additional\s+child\s+tax\s+credit)", re.IGNORECASE | re.MULTILINE),
            re.compile(r"^\s*28\s+Additional\s+child\s+tax\s+credit\s+from\s+Schedule\s*8812", re.IGNORECASE | re.MULTILINE),
        ],
    }
    all_text = "\n".join(default_form_pages + [s for stream in layout_form_streams for s in stream])
    for fname, patterns in _ZERO_BACKFILL_LABELS.items():
        if fname in fields:
            continue
        if any(p.search(all_text) for p in patterns):
            fields[fname] = Decimal(0)
    # If a layout stream recovered fields the default missed (or replaced
    # buggy default-extraction values wholesale), surface that — it's the
    # single most useful signal when diagnosing user reports of zero-value
    # imports.
    layout_only = set()
    for lr in layout_results:
        layout_only |= set(lr[0].keys())
    layout_only -= set(default_result[0].keys())
    if layout_only:
        warnings.append(
            f"Layout-aware extraction recovered {len(layout_only)} field(s) that "
            f"default text extraction missed: {sorted(layout_only)}."
        )

    # AcroForm values OVERRIDE text-derived values. The form dictionary is
    # the authoritative source — text extraction is, at best, OCR-ing the
    # rendered version of the same data — so when both agree we waste no
    # cycles, and when they disagree the AcroForm value is the one we
    # trust. Surface a warning whenever an override actually happens so
    # the user can spot any unexpected discrepancies on the dashboard.
    if acroform_fields:
        overrides: list[str] = []
        new_keys: list[str] = []
        for k, v in acroform_fields.items():
            existing = fields.get(k)
            if existing is None:
                new_keys.append(k)
            elif existing != v:
                overrides.append(f"{k}: text={existing} → acroform={v}")
            fields[k] = v
        warnings.append(
            f"AcroForm extraction supplied {len(acroform_fields)} field(s) "
            f"directly from PDF form widgets (the authoritative source)."
        )
        if new_keys:
            warnings.append(f"AcroForm added: {sorted(new_keys)}.")
        if overrides:
            warnings.append("AcroForm overrode text-extracted values: " + "; ".join(overrides))
        warnings.extend(acroform_warnings)

    if tax_year is None:
        raise ValueError(
            f"Could not detect tax year in {path.name}. "
            "Add a template for this form layout or use manual import."
        )
    if filing_status is None:
        warnings.append("filing status not detected; defaulting to single")
        filing_status = FilingStatus.SINGLE

    reported_total_tax = fields.pop("total_tax_reported", None)

    # Reconcile 1040 line 8 (the Sch-1-line-9 passthrough total) with the
    # individual income buckets to avoid double-counting unemployment.
    # 1040 line 8 = sum of Sch 1 lines 1-8 (refunds + alimony + business +
    # rentals + farm + UNEMPLOYMENT + other). If we already captured
    # unemployment_compensation from Sch 1 line 7 AND we're about to use
    # 1040 line 8 as "other ordinary income", we'd count the unemployment
    # dollars twice. Strategy:
    #   - If we extracted Sch 1 line 8 directly (other_ordinary_income is
    #     already set), trust it and discard the 1040 line 8 total.
    #   - Otherwise, derive other = max(0, line8 - unemployment).
    line8_total = fields.pop("_form1040_line8_total", None)
    if line8_total is not None:
        unemp = fields.get("unemployment_compensation", Decimal(0)) or Decimal(0)
        if "other_ordinary_income" not in fields:
            derived = line8_total - unemp
            if derived < 0:
                derived = Decimal(0)
            if derived > 0:
                fields["other_ordinary_income"] = derived
            if unemp > 0 and line8_total >= unemp:
                warnings.append(
                    "Reconciled 1040 line 8 against Sch 1 line 7 (unemployment) "
                    "to avoid double-counting; assigned the residual to other "
                    "ordinary income."
                )

    # Recover W-2 box 12 elective deferrals (codes D/AA/etc) — the 1040
    # itself doesn't show 401(k) contributions, so this is the only way
    # to capture them when the W-2 is bundled in the same PDF.
    w2_text = "\n".join(default_pages)
    for stream in layout_form_streams:
        w2_text += "\n" + "\n".join(stream)
    box12 = _extract_w2_box12_deferrals(w2_text)
    # Provenance: if a W-2 is anywhere in this PDF we have authoritative
    # data on the box-12 buckets (401(k) deferrals, HSA payroll). If no
    # W-2 is present, default-zero contributions are NOT a confirmed zero
    # — they're "unknown". Advisor rules use this flag.
    w2_present = bool(_W2_FINGERPRINT.search(w2_text))
    if w2_present:
        fields["w2_data_present"] = True
        # W-2 Box 12 code W is the canonical payroll-HSA source, so
        # seeing a W-2 also gives authoritative HSA data.
        fields["hsa_data_known"] = True
    box12_added: list[str] = []
    for k, v in box12.items():
        # Only add — never override a value that text or AcroForm already
        # supplied (the user may have manually entered totals elsewhere).
        if fields.get(k) in (None, Decimal(0)):
            fields[k] = v
            box12_added.append(f"{k}=${int(v):,}")
    if box12_added:
        warnings.append(
            "Recovered pre-tax payroll contributions from W-2 box 12: "
            + ", ".join(box12_added)
        )

    # Form 8889 (HSA) — independent of W-2. Even on 1040-only PDFs the
    # 8889 itself is usually included whenever the filer touched an HSA,
    # so this is a strong second source for hsa_contributions.
    f8889 = _extract_form_8889(w2_text)
    form_8889_present = bool(f8889.pop("_form_8889_present", False))
    f8889.pop("_hsa_coverage", None)  # not yet wired into the Return model
    f8889_added: list[str] = []
    for k, v in f8889.items():
        if fields.get(k) in (None, Decimal(0)):
            fields[k] = v
            f8889_added.append(f"{k}=${int(v):,}")  # type: ignore[arg-type]
    if f8889_added:
        warnings.append(
            "Recovered HSA contributions from Form 8889: "
            + ", ".join(f8889_added)
        )
    if form_8889_present:
        # Authoritative HSA data is in the PDF — advisor's verify-hsa
        # rule should NOT fire even if no W-2 was bundled.
        fields["hsa_data_known"] = True  # type: ignore[assignment]

    if (
        not box12_added
        and not form_8889_present
        and not w2_present
        and fields.get("wages", Decimal(0)) >= Decimal(10_000)
    ):
        warnings.append(
            "No W-2 or Form 8889 detected in this PDF — 401(k) and HSA "
            "payroll contributions can't be verified. Edit them on the "
            "year's What-if tab if you want advisor recommendations to "
            "reflect your actual contributions."
        )

    if not fields and reported_total_tax is None and summary_excluded < len(default_pages):
        warnings.append(
            "WARNING: detected an IRS-form page but extracted no money values. "
            "This usually means the PDF uses a non-standard layout. Upload to "
            "POST /api/debug/extract to inspect what the importer is seeing."
        )

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
    if summary_excluded > 0:
        warnings.append(
            f"Skipped {summary_excluded} summary / non-IRS-form page(s) so vendor "
            f"cover totals don't override the actual 1040 values."
        )

    # Capture text-extraction outcomes and final state in the per-import
    # log, then flush to disk. The log path is appended to warnings so
    # the dashboard can link to it and so issue-report copy-paste
    # naturally includes the location.
    if logger is not None:
        logger.section("Text-extraction pass")
        logger.kv("default_fields_extracted", sorted(default_result[0].keys()))
        for i, lr in enumerate(layout_results):
            logger.kv(f"layout_stream_{i}_fields", sorted(lr[0].keys()))
        if layout_only:
            logger.kv("recovered_by_layout_only", sorted(layout_only))
        logger.section("Detection")
        logger.kv("tax_year", tax_year)
        logger.kv("filing_status", filing_status.value if filing_status else None)
        logger.kv("qualifying_children", children)
        logger.kv("ocr_used", ocr_used)
        logger.kv("summary_pages_excluded", summary_excluded)
        logger.final_fields(
            {**fields,
             "reported_total_tax": reported_total_tax} if reported_total_tax is not None
            else fields
        )
        logger.warnings(warnings)
        try:
            log_path = logger.write()
            warnings.append(f"Import log written to: {log_path}")
        except OSError as e:
            warnings.append(f"Could not write import log: {e}")

    return Imported(
        ret=ret,
        source="pdf-ocr" if ocr_used else "pdf",
        source_hash=sha256_file(path),
        source_filename=path.name,
        warnings=warnings,
    )
