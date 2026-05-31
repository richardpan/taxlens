"""AcroForm-aware PDF extractor — reads form widget values directly.

Many real tax-return PDFs (IRS fillable forms, TurboTax/H&R Block exports,
FreeTaxUSA "save as PDF") embed the user-entered values as **AcroForm
widgets** rather than rasterising them into the page text stream. When
that happens, ``pdfplumber.extract_text()`` only sees the static
*background* (line labels, instructions, dotted leaders) and reports the
field values as either missing or — worse — picks up an adjacent row's
amount because labels and widgets render with small vertical offsets.

This extractor pulls field values straight from the form dictionary with
``pypdf.PdfReader.get_fields()``. Each field carries:

- ``/T``  the internal field name (e.g. ``topmostSubform[0].Page1[0].f1_32[0]``).
- ``/V``  the user-entered value (string, number, or ``/Yes`` / ``/Off`` for checkboxes).
- ``/TU`` an optional "tooltip" string that, on official IRS PDFs, is the
  human-readable line description (e.g. ``"Wages, salaries, tips, etc.
  Attach Form(s) W-2."``). This is the killer feature: it lets us map
  fields to TaxLens lines without hard-coding vendor-specific names.

The extractor returns a ``dict[str, Decimal]`` of money fields keyed by
the same names ``Return`` uses, plus a list of warnings. It returns an
empty dict (no warnings) when the PDF has no AcroForm — the caller
should treat that as "skip, fall through to pdfplumber".
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Re-use the SAME label regexes the text extractor uses so a field whose
# tooltip is ``"Taxable interest"`` maps to ``interest_income`` exactly
# the way the text-mode importer would map it. Importing here would
# create a cycle; we import lazily in _classify_tooltip().


_MONEY_CHARS = re.compile(r"[^\d\-.,()]")


def _parse_money(raw: Any) -> Decimal | None:
    """Coerce a raw AcroForm value into a Decimal, or return None if it
    isn't a money string. Handles ``"$1,234.56"``, ``"(500)"`` for
    negative, ``"   "`` for blank, and the occasional integer/float that
    sneaks through some PDF writers."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None
    if not isinstance(raw, str):
        # pypdf sometimes wraps values in IndirectObject / TextStringObject;
        # both stringify cleanly.
        raw = str(raw)
    s = raw.strip()
    if not s:
        return None
    # Paren-negative: (1,234.56) → -1234.56
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = _MONEY_CHARS.sub("", s)
    if not s or s in {"-", ".", ","}:
        return None
    s = s.replace(",", "")
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    return -v if neg else v


def _classify_tooltip(tooltip: str) -> str | None:
    """Match a field's ``/TU`` tooltip against the same LINE_PATTERNS the
    text-mode extractor uses, and return the ``Return`` field name on
    first match. Returns None if no pattern matches.

    The tooltip on official IRS fillable PDFs is a near-verbatim copy of
    the form's printed line label, so the existing regexes hit it
    directly without any tax-vendor-specific tweaks.
    """
    if not tooltip:
        return None
    # Lazy import to avoid the pdfplumber import cycle.
    from taxlens.importers.pdf import LINE_PATTERNS

    for field, patterns in LINE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, tooltip, re.IGNORECASE):
                return field
    return None


# Heuristic name-based fallback for PDFs whose widgets have NO tooltip
# (some vendor exports strip /TU). These regex fragments are checked
# against the *internal* field name (/T). Keep them deliberately
# conservative — we only want very high-confidence matches.
_NAME_HEURISTICS: list[tuple[str, str]] = [
    (r"wages?|w[\-_]?2", "wages"),
    (r"interest", "interest_income"),
    (r"qual(ified)?[\-_]?div", "qualified_dividends"),
    (r"ord(inary)?[\-_]?div", "ordinary_dividends"),
    (r"adjusted[\-_]?gross|^agi$|_agi[_\b]", "agi_reported"),
    (r"taxable[\-_]?income", "taxable_income_reported"),
    (r"total[\-_]?tax|tax_24", "total_tax_reported"),
    (r"withhold|wh_25|fed[\-_]?wh", "federal_withholding"),
    (r"capital[\-_]?gain", "long_term_capital_gains"),
    (r"social[\-_]?security|ss_benefit", "social_security_benefits"),
]


def _classify_name(name: str) -> str | None:
    if not name:
        return None
    low = name.lower()
    for pat, field in _NAME_HEURISTICS:
        if re.search(pat, low):
            return field
    return None


def extract_acroform_fields(path: Path) -> tuple[dict[str, Decimal], list[str]]:
    """Read AcroForm widget values from ``path`` and map them to TaxLens
    ``Return`` field names.

    Returns ``({}, [])`` (empty, no warnings) when the PDF has no
    AcroForm — the caller should fall back to the text-extraction path.
    Returns ``({...}, [warn, ...])`` when at least one form field was
    found, even if no fields could be classified (warnings explain why).

    Mapping strategy, in order:

    1. **Tooltip (/TU)** matched against ``LINE_PATTERNS`` — the highest
       confidence signal because official IRS fillable PDFs use the
       printed line text as the tooltip verbatim.
    2. **Field name (/T)** matched against ``_NAME_HEURISTICS`` — handles
       vendor exports that strip tooltips but use descriptive names.

    On conflicts (two fields mapping to the same TaxLens field), the
    LARGER value wins. This is a deliberate heuristic: IRS forms often
    have a per-W-2 sub-field plus a total field for the same line, and
    the total is what we want. A warning is emitted naming both.
    """
    try:
        import pypdf  # type: ignore[import-not-found]
    except ImportError:
        return {}, []

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception:
        return {}, []

    try:
        fields = reader.get_fields() or {}
    except Exception:
        return {}, []
    if not fields:
        return {}, []

    warnings: list[str] = []
    out: dict[str, Decimal] = {}
    sources: dict[str, list[tuple[str, Decimal]]] = {}

    for raw_name, fd in fields.items():
        try:
            value = fd.get("/V")
            tooltip = fd.get("/TU") or ""
            name = fd.get("/T") or raw_name
        except Exception:
            continue
        # Coerce pypdf wrapper objects to plain strings.
        tooltip_s = str(tooltip) if tooltip else ""
        name_s = str(name) if name else ""

        target = _classify_tooltip(tooltip_s) or _classify_name(name_s)
        if not target:
            continue

        money = _parse_money(value)
        if money is None or money == 0:
            # Zero values are usually placeholder/empty fields; don't let
            # them overwrite a real value found on another field.
            continue

        sources.setdefault(target, []).append((name_s or raw_name, money))

    for target, candidates in sources.items():
        if len(candidates) == 1:
            out[target] = candidates[0][1]
            continue
        # Conflict: pick the largest. Surface a warning so the user can
        # double-check on the dashboard if the heuristic guessed wrong.
        candidates.sort(key=lambda x: x[1], reverse=True)
        out[target] = candidates[0][1]
        warnings.append(
            f"AcroForm: {target} had {len(candidates)} candidate field(s) "
            f"({', '.join(f'{n}={v}' for n, v in candidates[:4])}); "
            f"picked the largest ({candidates[0][1]})."
        )

    if fields and not out:
        warnings.append(
            f"AcroForm contained {len(fields)} field(s) but none could be "
            f"mapped to known IRS lines (no recognizable tooltips or field "
            f"names). Falling back to text extraction."
        )

    return out, warnings


def extract_acroform_meta(path: Path) -> dict[str, Any]:
    """Tax year + filing status from AcroForm fields, if any can be inferred.
    Conservative — returns ``{}`` rather than guessing. The text-mode
    importer's detectors are still the primary source for these.
    """
    out: dict[str, Any] = {}
    try:
        import pypdf  # type: ignore[import-not-found]
        reader = pypdf.PdfReader(str(path))
        fields = reader.get_fields() or {}
    except Exception:
        return out

    for fd in fields.values():
        try:
            tooltip = str(fd.get("/TU") or "")
            name = str(fd.get("/T") or "")
            value = fd.get("/V")
        except Exception:
            continue
        text = f"{tooltip} {name}".lower()
        if "year" in text and value:
            m = re.search(r"(19|20)\d{2}", str(value))
            if m:
                out.setdefault("tax_year", int(m.group(0)))
        # Filing-status detection on AcroForm is messy (checkbox groups
        # vary wildly). Skip for now; text extractor handles it well.

    return out
