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
    r"^\s*(?:[\.\s]+|\d{1,2}\s*[a-z]?|\([^)]*\)|Attach\s+(?:Schedule|Form|Form\(s\))\s+\S.*|[^\w\s]{1,3}|[A-Za-z]{1,3})\s*$",
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
    # Followed directly by an uppercase letter (no separator) → glued form
    # identifier like '1040NR', '5329A'. pdfplumber sometimes emits the form
    # suffix without the dash, e.g. 'Form 1040, line 25, or Form\n1040NR'
    # where the line-wrapped '1040NR' has no preceding "Form" word on the
    # same line and would otherwise pass the prefix-word check.
    if end < len(tail) and tail[end].isalpha() and tail[end].isupper():
        return True
    return False


def _money_matches_in(tail: str) -> list:
    """All money matches in `tail` that are not inside form identifiers."""
    money_pat = re.compile(_MONEY)
    return [m for m in money_pat.finditer(tail) if not _is_form_id_digit(tail, m.start())]


def _nearby_doubled_echo(lines: list[str], i: int, digit: str) -> bool:
    """True when a line within ±2 of ``i`` matches ``N N`` where N is a
    1-2 digit line-number echo (e.g. ``21 21``, ``27 27``). Used to
    confirm that a trailing bare 1-2 digit at the END of a label line
    is the line-number echo column — not a real value — when the
    standard echo guards can't fire (no leading line-number, no
    next-line money). The doubled-echo pattern is unique to pre-TCJA
    1040 layouts that print the line-number column on rows that have
    no value, so its presence nearby is strong evidence we're in a
    section whose echo column is rendering without a value column.
    """
    pat = re.compile(r"^\s*(\d{1,2})[a-z]?\s+(\d{1,2})[a-z]?\s*$")
    for j in range(max(0, i - 2), min(i + 3, len(lines))):
        if j == i:
            continue
        m = pat.match(lines[j])
        if m and m.group(1) == m.group(2):
            return True
    return False


def _next_line_has_money(lines: list[str], i: int) -> bool:
    """Quick lookahead: does the next non-empty, non-noise line within a
    short window contain a *real* money value (≥ 3 digits or a decimal)?
    Used by the widened echo-guard to decide whether a bare 1-2 digit
    end-of-line token is an echo column (with the real value on the next
    row) or a legitimate small integer.
    """
    strict = re.compile(
        r"\$?\s*-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,})(?:\.[0-9]{1,2})?"
        r"|\$?\s*-?[0-9]+\.[0-9]{1,2}"
    )
    for j in range(i + 1, min(i + 4, len(lines))):
        nxt = lines[j].strip()
        if not nxt:
            continue
        if _NOISE_LINE.match(nxt):
            continue
        if strict.search(nxt):
            return True
        return False
    return False


_LINE_NO_ECHO = re.compile(r"(?:^|\s)(\d{1,2}[a-z]?)\s*$")
# Fields that legitimately extract small (1-2 digit) integers — exempt from
# the widened echo-guard which would otherwise discard counts like
# qualifying_children=2.
_COUNT_FIELDS = frozenset({"qualifying_children"})
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


_STRICT_MONEY_PAT = re.compile(
    r"\$?\s*-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,})(?:\.[0-9]{1,2})?|\$?\s*-?[0-9]+\.[0-9]{1,2}"
)


def _try_same_line_money(
    line: str,
    label_match: re.Match[str],
    *,
    lines: list[str],
    i: int,
    label_key: str | None,
    echo_guarded: list[str] | None,
) -> Decimal | None:
    """Same-line extraction phase of :func:`_first_money_after`.

    Returns the money value found on the same line as the label, or
    ``None`` to signal the caller should fall through to the next-line
    fallback scan. Mutates ``echo_guarded`` when a bare 1-2 digit
    line-number echo is detected so downstream phantom-override logic
    can avoid discarding a layout-stream value.
    """
    tail = line[label_match.end():]
    pn = _PAREN_NEG.search(tail)
    money_matches = _money_matches_in(tail)
    if pn:
        try:
            return -_money(pn.group(1))
        except InvalidOperation:
            pass
    if not money_matches:
        return None
    picked = _pick_money(tail, money_matches)
    if picked is None:
        return None
    # Guard against trailing line-number echo with NO value
    # column. E.g. Form 8889 line 13 when the user has no
    # personal HSA contribution renders as
    #     "13 HSA deduction (see instructions). . . . . . . 13"
    # — the trailing "13" is the line-number echo column,
    # not a $13 deduction.
    #
    # Two situations trigger the guard:
    # 1. Picked digit equals the leading line-number on the
    #    same line (the canonical "echo column" case).
    # 2. Picked is a bare 1-2 digit integer at end-of-line
    #    AND it is the ONLY money on the line AND no leading
    #    line-number is present. This catches pre-TCJA-style
    #    layouts where pdfplumber emits the label and value
    #    on separate lines but ALSO renders the line-number
    #    echo column on the label line, leaving the label
    #    line ending in a bare line-number (e.g. "Wages,
    #    salaries, tips, etc. ... 7" with the actual wages
    #    on the next line). Without this branch, ``_pick_money``
    #    returns the echo digit and we report wages=$7.
    picked_str = picked.group(0).strip()
    is_echo = False
    if re.fullmatch(r"\d{1,2}", picked_str) and tail[picked.end():].strip() == "":
        m_lead = re.match(r"\s*(\d{1,2})[a-z]?\s", line)
        # Also recognise the line-number when it appears as
        # a standalone 1-2 digit token within the first ~25
        # chars of the line (e.g. "Income 7 Wages, ..." in
        # pre-TCJA layouts where pdfplumber merges the
        # section header onto the first data row). Only
        # treat as a line-number when an alphabetic label
        # follows it. Allow multiple sidebar words before
        # the line number — pre-TCJA pages often merge
        # the standard-deduction sidebar ("Standard
        # Deduction for— Single or") onto a 1040 row,
        # giving lines like "Single or 48 Foreign tax
        # credit. Attach Form 1116 ... 48". The line
        # number is still the canonical anchor; the
        # leading words are sidebar bleed.
        m_inline_no = (
            None if m_lead else
            re.match(r"\s*[A-Za-z]{2,}(?:\s+[A-Za-z]+){0,4}\s+(\d{1,2})[a-z]?\s+[A-Za-z]", line[:60])
        )
        if m_lead and m_lead.group(1) == picked_str:
            is_echo = True
        elif m_inline_no and m_inline_no.group(1) == picked_str:
            is_echo = True
            if echo_guarded is not None and label_key:
                echo_guarded.append(label_key)
        elif (len(money_matches) == 1 and m_lead is None
              and label_key not in _COUNT_FIELDS):
            # Bare line-number at end-of-line, no leading
            # number to confirm — almost certainly an echo
            # column on a wrapped label. Fall through to
            # the next-line scan, which will pick up the
            # real value column. Skip count fields
            # (qualifying_children) which legitimately
            # extract small integers.
            if _next_line_has_money(lines, i):
                is_echo = True
                if echo_guarded is not None and label_key:
                    echo_guarded.append(label_key)
            else:
                # Even without next-line money, recognize
                # the trailing digit as an echo when it
                # appears as a doubled echo (``N N``) on
                # a nearby line — strong evidence the
                # form is rendering line-number echo
                # columns without real value columns,
                # not a sub-$100 whole-dollar value.
                if _nearby_doubled_echo(lines, i, picked_str):
                    is_echo = True
                    if echo_guarded is not None and label_key:
                        echo_guarded.append(label_key)
    if is_echo:
        return None
    try:
        return _money(picked.group(0))
    except InvalidOperation:
        return None


def _try_next_line_money(lines: list[str], i: int) -> Decimal | None:
    """Next-line fallback phase of :func:`_first_money_after`.

    Scans up to 5 non-empty lines after ``i`` looking for a value
    column rendered separately from the label. Returns the money
    value found, or ``None`` if the scan reaches a hard break
    boundary (page header, next labeled row) without finding one.
    """
    for j in range(i + 1, min(i + 6, len(lines))):
        nxt_raw = lines[j]
        nxt = nxt_raw.strip()
        if not nxt:
            continue
        if re.match(r"^\s*(?:Line\s*)?\d+\s*[a-z]?\s+[A-Za-z]{3,}", nxt_raw):
            return None
        # The loose-layout stream sometimes MERGES the next form row
        # into what should be the continuation of the prior label.
        # Detect that pattern: a 1-2 digit line-number followed by
        # 3+ alphabetic label chars within the first ~80 chars of
        # the line. This catches e.g. "Deduction for- 7 Capital gain"
        # without rejecting legitimate label-wraps like "term capital
        # gains or losses, go to Part II below..." (which has no
        # digit-then-label sequence early in the line).
        if re.search(r"(?:^|\s)\d{1,2}[a-z]?\s+[A-Za-z]{3,}", nxt_raw[:80]):
            return None
        # Page-break boundary: Form 1040 prints "Department of the
        # Treasury – Internal Revenue Service (99)" / "U.S. Individual
        # Income Tax Return" / "OMB No. ..." at the top of every page.
        # When a label appears at the bottom of one page with no value,
        # the fallback would otherwise scan into the next page's header
        # and capture stray tokens like "(99)" or the form year. Break
        # so the outer loop tries the next occurrence of the label
        # (post-page-break, the value usually appears on the same line
        # as a re-statement of the label, e.g. pre-2018 line 38
        # "Amount from line 37 (adjusted gross income) ... 100,922").
        if re.search(
            r"Department\s+of\s+the\s+Treasury|"
            r"Internal\s+Revenue\s+Service|"
            r"U\.S\.\s+Individual\s+Income|"
            r"OMB\s+No\.",
            nxt_raw,
            re.IGNORECASE,
        ):
            return None
        if _NOISE_LINE.match(nxt):
            continue
        pn = _PAREN_NEG.search(nxt)
        if pn:
            # Apply the same strictness to paren-negatives in the
            # next-line scan as we do to plain money: require either a
            # thousands-grouping comma, a cent decimal, or 3+ digits.
            # IRS Form 1040 prints "(99)" as a fixed OMB indicator on
            # every pre-2020 first page (e.g. "Department of the
            # Treasury–Internal Revenue Service (99)"); without this
            # guard, fallbacks for fields whose label appears late on
            # the prior page (e.g. AGI line 37) capture "(99)" as -99.
            inner = pn.group(1)
            if ("," in inner) or ("." in inner) or len(inner) >= 3:
                try:
                    return -_money(inner)
                except InvalidOperation:
                    pass
        strict = [
            m for m in _STRICT_MONEY_PAT.finditer(nxt)
            if not _is_form_id_digit(nxt, m.start())
            # Reject `$`-prefixed values in next-line fallback. IRS
            # 1040 columnar values are bare digits with comma group
            # separators (no `$` glyph); the only places `$N,NNN`
            # actually appears in vendor-rendered PDFs are the
            # standard-deduction sidebar ("$12,200", "$24,400",
            # "$18,350" etc.) and cover-page payment instructions
            # ("$1,004 your payment goes through"). When a label
            # extraction falls through to the next-line scan and
            # the only candidate begins with `$`, that's almost
            # certainly sidebar bleed (e.g. TY2019 line 5b taxable
            # SS shows the std-deduction sidebar's $12,200 directly
            # underneath a blank value column).
            and not nxt[m.start():m.start()+1] == "$"
        ]
        if strict:
            # Peek-ahead: if the current line is JUST a bare money
            # value (no surrounding label text) AND the next non-empty
            # line is a TOTALING successor row (e.g. "22 Combine the
            # amounts ... is your total income"), this value belongs
            # to that totalizer, not to the label we're currently
            # extracting. pdfplumber sometimes floats the value
            # column for the totalizer ABOVE its label when an
            # immediately-prior labeled row has an empty value
            # column (e.g. pre-TCJA line 21 "Other income" with
            # value 0 followed by "90,015." on its own line followed
            # by "22 Combine the amounts ... is your total income").
            # Without this guard the line-21 fallback captures the
            # total-income value. We restrict the skip to totalizer
            # successors so we don't over-suppress the standard
            # case where a bare value column legitimately precedes
            # a non-totaling labeled row.
            if re.match(r"^\s*\$?\s*-?\d[\d,]*(?:\.\d{0,2})?\s*$", nxt):
                for k in range(j + 1, min(j + 4, len(lines))):
                    peek_raw = lines[k]
                    peek = peek_raw.strip()
                    if not peek:
                        continue
                    if _NOISE_LINE.match(peek):
                        continue
                    if re.match(
                        r"^\s*(?:Line\s*)?\d+\s*[a-z]?\s+(?:"
                        r"Combine|Add\s+lines?|Total|Subtract\s+line"
                        r")\b",
                        peek_raw,
                        re.IGNORECASE,
                    ):
                        strict = []
                    break
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
        m_echo = re.match(r"^\s*(\d{1,2})[a-z]?\s+(-?\d{1,6})\s*$", nxt_raw)
        if m_echo:
            # Reject pure "doubled echo" rows like "21 21" or "27 27"
            # — these are line-number-echo columns with NO value
            # column rendered, common in pre-TCJA vendor exports
            # for empty-value rows. The two integers being equal
            # AND both ≤ 99 (so the value, if real, would have to
            # be a sub-$100 whole-dollar amount — never the case
            # on a real 1040) is a strong signature.
            lead_int, val_int = m_echo.group(1), m_echo.group(2)
            if lead_int == val_int and len(val_int) <= 2:
                return None
            try:
                return _money(m_echo.group(2))
            except InvalidOperation:
                pass
        # End-of-line variant: the value column is at the END of a
        # wrapped continuation line, anchored by a line-number echo
        # immediately before the value (e.g. Schedule D line 7 wraps:
        # "...go to Part III on page 2 . . . . . 7 74."). The line
        # starts with continuation prose, not the line number, so
        # the start-anchored m_echo above doesn't fire. We require
        # the dot-leader/whitespace gap before the line-number to
        # avoid false-firing on prose like "page 2".
        m_tail_echo = re.search(
            r"(?:\.\s*){2,}\s*\d{1,2}[a-z]?\s+(-?\d{1,6})\s*\.?\s*$",
            nxt_raw,
        )
        if m_tail_echo:
            try:
                return _money(m_tail_echo.group(1))
            except InvalidOperation:
                pass
        return None
    return None


def _first_money_after(label_re: str, text: str, *,
                       echo_guarded: list[str] | None = None,
                       label_key: str | None = None) -> Decimal | None:
    """Find the first money string that appears on the SAME LINE as a label match,
    falling back to the next several non-empty lines if the label line has no
    number (TurboTax / H&R Block / FreeTaxUSA often render label and amount in
    separate text columns, which pdfplumber emits on adjacent lines, frequently
    with noise lines like dot-leaders or '(see instructions)' in between).

    Money matches that are actually part of a form identifier (`W-2`, `1099-R`,
    `8949`) are filtered out — otherwise the withholding line would extract
    '-2' from 'Form(s) W-2'.

    When ``echo_guarded`` is provided and the same-line scan refuses a bare
    1-2 digit line-number echo, the caller's ``label_key`` is appended so
    downstream phantom-override logic can avoid discarding a layout-stream
    value just because the default-stream label-line had no value column.
    """
    label_pat = re.compile(label_re, re.IGNORECASE)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = label_pat.search(line)
        if not m:
            continue
        val = _try_same_line_money(
            line, m,
            lines=lines, i=i,
            label_key=label_key, echo_guarded=echo_guarded,
        )
        if val is not None:
            return val
        val = _try_next_line_money(lines, i)
        if val is not None:
            return val
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
    "schedule_2_part_i_reported": [
                                # 1040 line 17 (TY2020+) — Schedule 2 Part I
                                # passthrough (AMT + excess APTC repayment).
                                r"^\s*17\s+Amount\s+from\s+Schedule\s*2,?\s*line\s*3\b",
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
    "schedule_3_line_8_reported": [
                                # 1040 line 20 — Schedule 3 line 8 total
                                # of nonrefundable credits. Anchored on
                                # the line-number prefix to avoid
                                # accidentally matching Schedule 3
                                # itself (its line 8 is the same total
                                # but appears later in the PDF).
                                r"^\s*20\s+Amount\s+from\s+Schedule\s*3\s*,\s*line\s*8",
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
    # Anchored to line-start (MULTILINE) so we don't match instructional
    # prose like Form 8962's "...if your filing status is married filing
    # separately unless you qualify..." which would otherwise drag the
    # captured tail to MFS on any return that includes a PTC reconciliation.
    re.compile(
        r"^\s*Your\s+filing\s+status\s+is\s*:?\s*([A-Za-z][^\n.]{0,40})",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Pre-TCJA fillable forms (TY2016/TY2017) print "Filing Status" with
    # no colon, followed by the option list and an inline X marker:
    #   "Filing Status 1 X Single 4 Head of household (with qualifying ...)"
    # Capture the tail so the X-detection branch in _detect_status can
    # identify the selected option.
    re.compile(r"Filing\s+Status\s+(\d?\s*X\s+[A-Za-z][^\n]{0,80})", re.IGNORECASE),
    # TY2019/2018 1040 layout: "Filing status" (no colon) followed by the
    # option list with the selected option marked by a bare uppercase X
    # somewhere in the middle of the option line, e.g.:
    #   "Filing status Single X Married filing jointly Married filing
    #    separately (MFS) Head of household (HOH) Qualifying widow(er) (QW)"
    # Capture the entire option-list tail so the X-detection branch in
    # _detect_status can locate the marker by proximity to the selected
    # status keyword.
    re.compile(r"Filing\s+status\s+([A-Za-z][^\n]{0,180}\sX\s[^\n]{0,80})"),
]

CHECKED_HINT = re.compile(r"\[\s*[xX✓]\s*\]|\(X\)|☒|\u2611|\[X\]")


# ─── Pre-2020 supplemental label-anchored patterns ─────────────────────────
# These fire ONLY when the detected tax year is < 2020. The headline 1040
# fields below all moved across line numbers between TY2018, TY2019, and
# TY2020+; the line-prefixed patterns in LINE_PATTERNS target the TY2020+
# layout. For older returns, anchoring on the verbatim *label* recovers the
# value regardless of the (year-specific) line number, but those same
# label-only fallbacks would over-match on TY2020+ forms (where the same
# label phrase appears elsewhere — e.g. cross-reference text in
# instructions, Schedule 8812, or the "Standard Deduction" sidebar). Gating
# them on year keeps modern-form extraction unchanged.
LINE_PATTERNS_PRE_2020: dict[str, list[str]] = {
    "deduction_reported": [
        # TY2019 line 9 / TY2018 line 8 phrasing.
        r"\bStandard\s+deduction\s+or\s+itemized\s+deductions\b",
        # Pre-TCJA (TY2017 and earlier) 1040 line 40 phrasing. The label
        # straddles two lines on some vendor exports — anchor on the
        # leading "Itemized deductions" with "Schedule A" reference and
        # tolerate the "or your standard deduction" continuation.
        r"\bItemized\s+deductions\s*\(\s*from\s+Schedule\s*A\)",
        r"\bItemized\s+deductions\s+\(from\s+Schedule\s*A\)\s+or\s+your\s+standard\s+deduction",
    ],
    "child_tax_credit_reported": [
        # Anchor on the full label phrase that appears on the actual 1040
        # line (TY2017 and earlier: "Child tax credit ... Attach Schedule
        # 8812"; TY2018: "Child tax credit/credit for other dependents";
        # TY2019: "Child tax credit or credit for other dependents").
        # The bare "\bChild tax credit\b" fallback was removed because it
        # over-matched on instructional sidebar prose ("child tax credit
        # did not live with...") on page 1 of pre-TCJA returns and pulled
        # an unrelated downstream value (typically the standard deduction)
        # via the next-line fallback scan.
        r"Child\s+tax\s+credit(?:\s*/\s*credit|\s+(?:and|or)\s+credit|\.?\s+Attach\s+Schedule\s*8812)",
    ],
    "additional_ctc_reported": [
        r"\bAdditional\s+child\s+tax\s+credit\.?\s+Attach\s+Schedule\s*8812",
        r"\bRefundable\s+(?:additional\s+)?child\s+tax\s+credit\b",
    ],
    "schedule_2_other_taxes_reported": [
        # TY2019 line 15 / TY2018 line 14 (Schedule 4 in 2018, Schedule 2 from 2019).
        r"Other\s+taxes\.?\s+Attach\s+Schedule\s*[24]",
    ],
    "schedule_2_part_i_reported": [
        # TY2019: Schedule 2 line 3 ("Add lines 1 and 2. Enter here and
        # include on Form 1040 or 1040-SR, line 12b"). The 1040 itself
        # only prints the SUM (line 12a + Sch 2 Part I) on line 12b,
        # so we have to anchor on the Schedule 2 form's own line 3
        # phrasing. TY2018 used Schedule 4 with similar phrasing on the
        # corresponding totalizer line.
        r"Add\s+lines\s+1\s+and\s+2\.\s+Enter\s+here\s+and\s+include\s+on\s+Form\s+1040",
    ],
    "schedule_3_line_8_reported": [
        # TY2018-2019 Schedule 3 references — the cross-reference text on
        # the 1040 itself, not the Schedule 3 form's own header.
        r"\bAmount\s+from\s+Schedule\s*3\b",
        r"\bAdd\s+Schedule\s*3\b",
    ],
    "other_ordinary_income": [
        # Pre-TCJA (TY2017 and earlier) 1040 line 21 — "Other income.
        # List type and amount". Common contributors are HSA testing-
        # period income (code "HSA"), gambling winnings, jury duty,
        # cancellation of debt income. Schedule 1 didn't exist yet, so
        # this is the only home for these items pre-2018.
        r"^\s*21\s+Other\s+income\.?\s+List\s+type",
    ],
}


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


def _cluster_words_by_y(words: list[dict], y_tol: float) -> str:
    """Cluster pre-extracted words by y-coordinate into visual rows.
    Each cluster sorted by x0, joined with spaces; rows joined by newlines.
    """
    if not words:
        return ""
    # words must already be sorted by (top, x0); we sort defensively.
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
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
    return _cluster_words_by_y(words, y_tol)


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
                # Extract words ONCE, then cluster at both tolerances.
                # extract_words is the dominant cost (re-runs char→word
                # grouping each call); reusing the result halves layout
                # work per page.
                try:
                    words = p.extract_words(
                        use_text_flow=False, keep_blank_chars=False
                    )
                except Exception:
                    words = []
                layout_tight.append(_cluster_words_by_y(words, 3.0))
                layout_loose.append(_cluster_words_by_y(words, 8.0))
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
            phrase = m.group(1).strip()
            phrase_lower = phrase.lower()
            # 1a. Inline-X marker: a bare uppercase X token (bounded by
            #     whitespace) immediately precedes the selected option's
            #     label. Examples:
            #       "Filing status: X Single Married filing jointly ..."
            #       "Filing Status 1 X Single 4 Head of household ..."
            #     Without this branch, the substring scan below would
            #     match the FIRST keyword present anywhere in the
            #     captured phrase (typically "married filing jointly",
            #     which follows "Single" in the form's printed order)
            #     and return MFJ even on Single-filed returns.
            x_match = re.search(r"(?:^|\s)X\s+(\S.*)", phrase)
            if x_match:
                after = x_match.group(1).lower()
                best_status: FilingStatus | None = None
                best_pos = 31
                for status, needle in explicit_map:
                    pos = after.find(needle)
                    if pos != -1 and pos < best_pos:
                        best_pos = pos
                        best_status = status
                if best_status is not None:
                    return best_status
            for status, needle in explicit_map:
                if needle in phrase_lower:
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


def _extract_fields(
    pages: list[str],
    tax_year: int | None = None,
) -> tuple[dict[str, Decimal], int, list[str], set[str]]:
    out: dict[str, Decimal] = {}
    warnings: list[str] = []
    qualifying_children = 0
    joined = "\n".join(pages)

    use_pre2020_supplement = tax_year is not None and tax_year < 2020
    echo_guarded: list[str] = []

    for field, patterns in LINE_PATTERNS.items():
        all_patterns = list(patterns)
        if use_pre2020_supplement and field in LINE_PATTERNS_PRE_2020:
            all_patterns += LINE_PATTERNS_PRE_2020[field]
        for pat in all_patterns:
            value = _first_money_after(
                pat, joined,
                echo_guarded=echo_guarded, label_key=field,
            )
            if value is not None:
                if field == "qualifying_children":
                    try:
                        qualifying_children = int(value)
                    except Exception:
                        warnings.append("qualifying_children unparseable")
                else:
                    out[field] = value
                break

    # Sanity check: STCG and LTCG with identical non-zero values are
    # essentially impossible in a real return; this fingerprint occurs
    # when the loose LTCG fallback patterns (Schedule D Part II header,
    # 1040 line 7/13 "Capital gain or (loss)") trip a next-line scan
    # that picks up the same aggregate figure as the STCG line. The
    # 1040's line 13 is a COMBINED ST+LT total — when LT is actually
    # zero, attributing it to LTCG falsely applies preferential rates
    # and undercomputes tax. Prefer ST treatment in this case.
    stcg = out.get("short_term_capital_gains")
    ltcg = out.get("long_term_capital_gains")
    if stcg is not None and ltcg is not None and stcg == ltcg and stcg != 0:
        del out["long_term_capital_gains"]
        warnings.append(
            "Dropped long_term_capital_gains because it equals "
            "short_term_capital_gains (likely Schedule D aggregate "
            "bleed; treating gain as short-term)."
        )

    return out, qualifying_children, warnings, set(echo_guarded)


def _labels_present(pages: list[str]) -> set[str]:
    """Return the set of field names whose label regex matches anywhere
    in ``pages`` — regardless of whether ``_first_money_after`` was
    able to recover a money value. Used by the importer's phantom-value
    detector: when the default text stream has the label but no value,
    any layout-stream value is almost certainly noise from an adjacent
    column and should be discarded.
    """
    joined = "\n".join(pages)
    found: set[str] = set()
    for field, patterns in LINE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, joined, re.IGNORECASE):
                found.add(field)
                break
    return found


def _labels_with_missing_value_column(
    pages: list[str],
    line_patterns: dict[str, list[str]],
) -> set[str]:
    """Return field names whose label appears in ``pages`` only on lines
    that end in a bare line-number echo (e.g. ``8a Taxable interest.
    Attach Schedule B if required 8a``) with no money column between
    the label and the trailing echo. These lines indicate the value
    column was rendered separately by the PDF (as a layout-stream
    extraction would recover) and the default-text stream legitimately
    has no value to offer — so a layout-stream value is genuine, not
    phantom, and should NOT be discarded by the phantom-value detector.
    """
    joined = "\n".join(pages)
    lines = joined.splitlines()
    found: set[str] = set()
    money_pat = re.compile(_MONEY)
    for field, patterns in line_patterns.items():
        for pat in patterns:
            label_re = re.compile(pat, re.IGNORECASE)
            label_line_idxs = [i for i, ln in enumerate(lines) if label_re.search(ln)]
            if not label_line_idxs:
                continue
            all_echo_only = True
            for i in label_line_idxs:
                ln = lines[i]
                m = label_re.search(ln)
                tail = ln[m.end():]
                trailing_echo = re.search(r"\b(\d{1,2}[a-z]?)\s*$", tail)
                if not trailing_echo:
                    all_echo_only = False
                    break
                # Money tokens between label and trailing echo, after
                # filtering form-id digits like "1099-R" / "8949".
                pre_echo = tail[: trailing_echo.start()]
                real_money = [
                    mm for mm in money_pat.finditer(pre_echo)
                    if not _is_form_id_digit(pre_echo, mm.start())
                ]
                if real_money:
                    all_echo_only = False
                    break
            if all_echo_only:
                found.add(field)
                break
    return found


def _merge_field_results(
    *results: tuple[dict[str, Decimal], int, list[str], set[str]],
) -> tuple[dict[str, Decimal], int, list[str], set[str]]:
    """Pick the most-complete (fields, children, warnings, echo_guarded)
    tuple from ``results``. The "winner" is the result with the largest
    dict; ties are broken in argument order so existing fixtures (where
    default-text extraction has always worked) keep their prior behavior.

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
    stream isn't discarded entirely. ``echo_guarded`` sets are unioned
    across all results so the phantom-override pass downstream can avoid
    discarding a layout-stream value just because another stream's
    label-line had no money column.
    """
    if not results:
        return {}, 0, [], set()
    winner_idx = max(range(len(results)), key=lambda i: len(results[i][0]))
    base_fields, base_children, base_warnings, base_echo = results[winner_idx]
    merged = dict(base_fields)
    children = base_children
    warnings = list(base_warnings)
    echo_guarded: set[str] = set(base_echo)
    for i, (fields, ch, ws, eg) in enumerate(results):
        if i == winner_idx:
            continue
        for k, v in fields.items():
            merged.setdefault(k, v)
        if ch and not children:
            children = ch
        for w in ws:
            if w not in warnings:
                warnings.append(w)
        echo_guarded |= eg
    return merged, children, warnings, echo_guarded


def _apply_acroform_override(
    fields: dict,
    field_sources: dict[str, str],
    acroform_fields: dict,
    acroform_warnings: list[str],
    warnings: list[str],
) -> None:
    """AcroForm widget values override text-derived ones.

    The form dictionary is the authoritative source — text extraction
    is, at best, OCR-ing the rendered version of the same data — so
    when both agree we waste no cycles, and when they disagree the
    AcroForm value is the one we trust. Surface a warning whenever
    an override actually happens so the user can spot any unexpected
    discrepancies on the dashboard. Mutates ``fields``, ``field_sources``,
    and ``warnings`` in place.
    """
    if not acroform_fields:
        return
    overrides: list[str] = []
    new_keys: list[str] = []
    for k, v in acroform_fields.items():
        existing = fields.get(k)
        if existing is None:
            new_keys.append(k)
        elif existing != v:
            overrides.append(f"{k}: text={existing} → acroform={v}")
        fields[k] = v
        field_sources[k] = "acroform"
    warnings.append(
        f"AcroForm extraction supplied {len(acroform_fields)} field(s) "
        f"directly from PDF form widgets (the authoritative source)."
    )
    if new_keys:
        warnings.append(f"AcroForm added: {sorted(new_keys)}.")
    if overrides:
        warnings.append("AcroForm overrode text-extracted values: " + "; ".join(overrides))
    warnings.extend(acroform_warnings)


def _recover_w2_box12(
    fields: dict, w2_text: str, warnings: list[str],
) -> tuple[bool, int]:
    """Recover W-2 box 12 elective deferrals (codes D/AA/etc) from the
    bundled W-2 text. The 1040 itself doesn't show 401(k) contributions,
    so this is the only way to capture them when the W-2 is in the PDF.

    Returns ``(w2_present, box12_added_count)`` so callers can wire the
    verify-payroll advisor rule. Mutates ``fields`` and ``warnings``
    in place.
    """
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
    return w2_present, len(box12_added)


def _recover_form_8889(
    fields: dict, w2_text: str, warnings: list[str],
) -> bool:
    """Recover HSA contributions from a bundled Form 8889. Independent
    of W-2 — even on 1040-only PDFs the 8889 itself is usually included
    whenever the filer touched an HSA, so this is a strong second
    source for ``hsa_contributions``.

    Returns ``form_8889_present`` so callers can wire advisor flags.
    Mutates ``fields`` and ``warnings`` in place.
    """
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
    return form_8889_present


# ─── Standalone W-2 PDF support ─────────────────────────────────────────────
#
# Some users have W-2s that aren't bundled into their 1040 PDF (employer
# digital W-2 issued separately). When the user drops a W-2-only PDF, we
# detect it BEFORE running the 1040 form-page filter (which would discard
# every page as non-1040) and route it through `_import_w2_standalone`.
# The resulting `Imported` has `source="pdf-w2"`; the service layer
# special-cases it and merges box-12 contributions into the existing
# Return for the same tax year (additive, so multiple W-2s sum correctly).
_FORM_1040_MARKER = re.compile(r"\bForm\s+1040\b", re.IGNORECASE)
_W2_ONLY_MARKER = re.compile(r"Wage\s+and\s+Tax\s+Statement", re.IGNORECASE)


def _is_w2_only_pdf(pages: list[str]) -> bool:
    """True iff the PDF text shows W-2 letterhead but no Form 1040 marker
    on any page. We require the W-2 letterhead specifically (not just
    "Box 12") because a 1040-with-bundled-W-2 also has Box 12 markers."""
    has_w2 = any(_W2_ONLY_MARKER.search(p) for p in pages)
    if not has_w2:
        return False
    has_1040 = any(_FORM_1040_MARKER.search(p) for p in pages)
    return not has_1040


def _import_w2_standalone(
    path: Path, default_pages: list[str], layout_streams: list[list[str]]
) -> Imported:
    """Build an `Imported` for a standalone W-2 PDF. The resulting Return
    is a stub with only year + W-2 box-12 buckets populated; the service
    layer merges it into an existing Return for the same tax year."""
    # Year: try every available text stream so layout/default differences
    # don't drop the detection.
    tax_year: int | None = _detect_year(default_pages)
    if tax_year is None:
        for stream in layout_streams:
            tax_year = _detect_year(stream)
            if tax_year is not None:
                break
    if tax_year is None:
        raise ValueError(
            "Could not detect a tax year on this W-2 PDF. The W-2 must "
            "show a 4-digit year (e.g. '2024 Wage and Tax Statement')."
        )

    # Box 12: feed the joined text from every stream so multi-column W-2
    # layouts that one stream splits awkwardly are still recovered.
    joined = "\n".join(default_pages)
    for stream in layout_streams:
        joined += "\n" + "\n".join(stream)
    box12 = _extract_w2_box12_deferrals(joined)

    if not box12:
        raise ValueError(
            "Detected a W-2 PDF but couldn't read any Box 12 codes "
            "(D, AA, W, etc.). Edit 401(k) / HSA payroll values on "
            "the year's What-if tab instead."
        )

    stub = Return(
        tax_year=tax_year,
        filing_status=FilingStatus.SINGLE,  # placeholder; service merges into existing
        traditional_401k_contributions=box12.get(
            "traditional_401k_contributions", Decimal(0)
        ),
        roth_401k_contributions=box12.get("roth_401k_contributions", Decimal(0)),
        hsa_contributions=box12.get("hsa_contributions", Decimal(0)),
        w2_data_present=True,
    )
    summary_parts = [f"{k}=${int(v):,}" for k, v in box12.items()]
    warnings = [
        f"Standalone W-2 detected for tax year {tax_year}: "
        + ", ".join(summary_parts)
    ]
    return Imported(
        ret=stub,
        source="pdf-w2",
        source_hash=sha256_file(path),
        source_filename=path.name,
        warnings=warnings,
    )


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

    # ── Standalone W-2 fast path ────────────────────────────────────────
    # If the PDF shows W-2 letterhead but no Form 1040 marker on any page,
    # treat it as a standalone W-2 attachment. Run the check against the
    # default text AND every layout stream so spacing differences in one
    # stream don't cause a misclassification.
    all_streams_text = list(default_pages)
    for stream in layout_streams:
        all_streams_text.extend(stream)
    if _is_w2_only_pdf(all_streams_text):
        return _import_w2_standalone(path, default_pages, layout_streams)

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
    default_result = _extract_fields(default_form_pages, tax_year=tax_year)
    layout_results = [_extract_fields(stream, tax_year=tax_year) for stream in layout_form_streams]
    fields, children, fwarnings, echo_guarded_fields = _merge_field_results(default_result, *layout_results)
    warnings = list(fwarnings)

    # ── Per-field provenance tracking ──────────────────────────────
    # Tag each text-extracted field with the stream it came from so
    # the UI can flag layout-only extractions for manual review. We
    # populate this from the unmerged per-stream results before any
    # downstream override / backfill / acroform-supersede pass. Later
    # stages update entries in place when they replace a value.
    field_sources: dict[str, str] = {}
    for fname in fields.keys():
        in_default = fname in default_result[0]
        in_layout = any(fname in r[0] for r in layout_results)
        if in_default and in_layout:
            field_sources[fname] = "merged"
        elif in_default:
            field_sources[fname] = "default"
        else:
            field_sources[fname] = "layout"

    # ── Generic phantom-value override (provenance-aware) ──────────
    # When the default text stream FOUND the field's label but
    # recovered no money value, and a layout stream nonetheless
    # produced a value, that value is almost often noise from an
    # adjacent column (e.g. an IRS form's printed margin reference
    # being column-associated by pdfplumber with a blank row).
    #
    # Only apply this override when the default stream was the
    # most-complete contributor — i.e., default produced more values
    # than every layout stream. In that regime, default text is
    # reliable and layout is being used as a supplement, so layout
    # extras for default-known labels are suspect. When a layout
    # stream is the winner (e.g. fillable forms where label/value
    # are vertically offset and pdfplumber's default extractor splits
    # them across rows), the whole document needs layout extraction
    # and overriding would discard legitimate values.
    default_values = default_result[0]
    layout_value_counts = [len(r[0]) for r in layout_results]
    default_is_dominant = (
        len(default_values) > 0
        and (not layout_value_counts or len(default_values) >= max(layout_value_counts))
    )
    if default_is_dominant:
        default_labels = _labels_present(default_form_pages)
        # Pre-2020 forms commonly render the value column on a separate
        # line from the label (with both a leading and trailing line-
        # number echo on the label line). Default-stream same-line scan
        # legitimately returns None for these — so layout-stream values
        # are genuine, not phantom.
        echo_only_labels = _labels_with_missing_value_column(
            default_form_pages, LINE_PATTERNS
        ) | _labels_with_missing_value_column(
            default_form_pages, LINE_PATTERNS_PRE_2020
        )
        layout_phantom: dict[str, Decimal] = {}
        for fname in list(fields.keys()):
            if fname in default_values:
                continue
            if fname not in default_labels:
                continue
            # Exempt fields whose default-stream same-line scan refused
            # a bare 1-2 digit line-number echo as the value. In that
            # case the label IS present in default text but the value
            # column is on the next line — exactly the layout stream's
            # strength — so layout's value is genuine, not phantom.
            if fname in echo_guarded_fields:
                continue
            if fname in echo_only_labels:
                continue
            layout_phantom[fname] = fields.pop(fname)
            field_sources.pop(fname, None)
        if layout_phantom:
            warnings.append(
                "Discarded layout-stream values for fields whose labels "
                "appeared in the default text without a money value: "
                + ", ".join(sorted(layout_phantom.keys()))
            )

    # Reconciliation cap fields can legitimately be $0 on the source
    # 1040 even though the label IS present (e.g. line 19 nonrefundable
    # CTC = blank/0 when the filer's credit was instead claimed as the
    # refundable line 28 ACTC, or line 28 = blank when the credit was
    # fully absorbed nonrefundable). Distinguishing "$0 claimed" from
    # "label not present" matters: the engine uses these as caps on its
    # modeled credit, so leaving them as None would let the engine
    # over-claim. Backfill 0 whenever the label text appears on the
    # page but no money value followed it. This list is intentionally
    # narrow — only fields where None ≠ 0 in the engine. Generic
    # phantom-value protection (layout vs default stream) is handled
    # earlier and applies to ALL fields automatically.
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
            field_sources[fname] = "zero-backfill"
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

    # AcroForm values OVERRIDE text-derived values.
    _apply_acroform_override(
        fields, field_sources, acroform_fields, acroform_warnings, warnings,
    )

    if tax_year is None:
        raise ValueError(
            f"Could not detect tax year in {path.name}. "
            "Add a template for this form layout or use manual import."
        )
    if filing_status is None:
        warnings.append("filing status not detected; defaulting to single")
        filing_status = FilingStatus.SINGLE

    reported_total_tax = fields.pop("total_tax_reported", None)
    field_sources.pop("total_tax_reported", None)

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

    # Recover W-2 box 12 elective deferrals and Form 8889 HSA detail
    # (independent recovery passes — both can fire on the same PDF).
    w2_text = "\n".join(default_pages)
    for stream in layout_form_streams:
        w2_text += "\n" + "\n".join(stream)
    w2_present, box12_added_count = _recover_w2_box12(fields, w2_text, warnings)
    form_8889_present = _recover_form_8889(fields, w2_text, warnings)

    if (
        not box12_added_count
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

    # ── Filing-status sanity check via reported standard deduction ──────────
    # When the form's "Filing Status" checkbox column doesn't cleanly identify
    # the selected option (e.g. pre-2020 fillable layouts where pdfplumber
    # mis-orders text and the X marker ends up next to a different option than
    # the actual one), the extracted deduction_reported value is a reliable
    # cross-check: if the filer took the standard deduction, the line-40 /
    # line-9 / line-12 amount equals exactly the year's std-deduction figure
    # for one (or more) filing statuses. When the detected status's std
    # deduction doesn't match the reported deduction but exactly one other
    # status's does — or several do (Single and MFS share the same value in
    # most years) — prefer the matching status. We use a priority order
    # (Single > MFS > HOH > QSS > MFJ) since Single is dominant and MFS
    # requires explicit spousal info we'd otherwise see in the form.
    if (filing_status is not None and tax_year is not None
            and "deduction_reported" in fields):
        try:
            from taxlens.rules import load_rules as _load_rules
            _rules = _load_rules(tax_year)
            _std_table = _rules.standard_deduction
            _reported_ded = fields["deduction_reported"]
            _detected_std = _std_table.get(filing_status.value)
            if _detected_std is not None and _reported_ded != _detected_std:
                _matches = [s for s, v in _std_table.items() if v == _reported_ded]
                if _matches and filing_status.value not in _matches:
                    _priority = ["single", "mfs", "hoh", "qss", "mfj"]
                    _best = sorted(
                        _matches,
                        key=lambda s: _priority.index(s) if s in _priority else 99,
                    )[0]
                    warnings.append(
                        f"Filing status corrected from {filing_status.value} to "
                        f"{_best} based on reported standard deduction "
                        f"(${_reported_ded} matches {_best} std deduction for "
                        f"TY{tax_year})."
                    )
                    filing_status = FilingStatus(_best)
        except Exception:
            # Rules unavailable or other failure — leave detected status alone.
            pass

    # Pre-TCJA (TY2017 and earlier) "Other Taxes" passthrough. The
    # post-2018 Schedule 2 Part II fields (Self-employment tax, ACA
    # individual responsibility, Form 8959/8960, Form 5329 excise, etc.)
    # were rendered directly on Form 1040 lines 57-62 in the pre-TCJA
    # layout — there is no Schedule 2 yet. We capture line 56 (tax
    # after credits) and compute ``schedule_2_other_taxes_reported``
    # synthetically as ``line_63 − line_56`` so the existing engine
    # passthrough (residual = reported − engine-modeled) closes the
    # gap for items the engine doesn't model from extracted inputs
    # (most commonly the ACA Shared Responsibility Payment and the
    # Form 8889 HDHP excise).
    if (tax_year is not None and tax_year < 2018
            and reported_total_tax is not None
            and "schedule_2_other_taxes_reported" not in fields):
        _passthrough_text = "\n".join(
            default_form_pages
            + [s for stream in layout_form_streams for s in stream]
        )
        _line56 = _first_money_after(
            r"Subtract\s+line\s+55\s+from\s+line\s+47",
            _passthrough_text,
            label_key="pre_tcja_tax_after_credits",
        )
        if _line56 is not None and _line56 >= 0:
            _residual = reported_total_tax - _line56
            if _residual > 0:
                fields["schedule_2_other_taxes_reported"] = _residual
                field_sources["schedule_2_other_taxes_reported"] = "pre-tcja-synth"

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
        field_sources=field_sources,
    )
