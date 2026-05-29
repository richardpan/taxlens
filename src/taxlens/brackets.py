"""Bracket-walking primitives, shared by ordinary and qualified-income math.

The walker is deliberately split out so it can be tested in isolation and so
the same code path produces the audit trail rows that the UI renders.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from taxlens.models import BracketFill

ZERO = Decimal(0)


def walk_brackets(
    amount: Decimal,
    brackets: Sequence[tuple[Decimal, Decimal]],
    *,
    stack_above: Decimal = ZERO,
) -> tuple[Decimal, list[BracketFill]]:
    """Compute tax on `amount`, optionally treating it as stacked on top of `stack_above`.

    Stacking is what makes qualified-dividend / LTCG math work: the first dollar of
    qualified income falls into whichever qualified bracket sits *above* ordinary
    taxable income, not at $0.

    Args:
        amount: Dollars to tax (must be ≥ 0).
        brackets: Ascending list of (lower_bound, rate) tuples. The implicit upper
                  bound of bracket N is bracket N+1's lower bound; the last bracket
                  has no upper bound.
        stack_above: Dollars already "filled" before this amount.

    Returns:
        (total_tax, [BracketFill, ...])  — only brackets that received >$0 are returned.
    """
    if amount < 0:
        raise ValueError(f"amount must be non-negative, got {amount}")
    if amount == 0 or not brackets:
        return ZERO, []

    start = stack_above
    end = stack_above + amount

    total_tax = ZERO
    fills: list[BracketFill] = []

    for i, (lower, rate) in enumerate(brackets):
        upper = brackets[i + 1][0] if i + 1 < len(brackets) else None

        # Intersect [start, end) with [lower, upper)
        slice_lower = max(start, lower)
        slice_upper = end if upper is None else min(end, upper)
        if slice_upper <= slice_lower:
            continue

        in_bracket = slice_upper - slice_lower
        tax_in_bracket = in_bracket * rate
        total_tax += tax_in_bracket
        fills.append(
            BracketFill(
                lower=lower,
                upper=upper,
                rate=rate,
                amount_in_bracket=in_bracket,
                tax_in_bracket=tax_in_bracket,
            )
        )

    return total_tax, fills
