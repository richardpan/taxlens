"""Unit tests for the bracket walker. These run without loading any rules."""
from decimal import Decimal

import pytest

from taxlens.brackets import walk_brackets

D = Decimal


def test_zero_amount_returns_zero():
    tax, fills = walk_brackets(D(0), [(D(0), D("0.10"))])
    assert tax == 0
    assert fills == []


def test_single_bracket():
    tax, fills = walk_brackets(D(1000), [(D(0), D("0.10"))])
    assert tax == D(100)
    assert len(fills) == 1
    assert fills[0].amount_in_bracket == D(1000)
    assert fills[0].tax_in_bracket == D(100)


def test_two_brackets_partial_fill():
    # 10% to 100, 20% above
    brackets = [(D(0), D("0.10")), (D(100), D("0.20"))]
    tax, fills = walk_brackets(D(250), brackets)
    # 100 × 0.10 + 150 × 0.20 = 10 + 30 = 40
    assert tax == D(40)
    assert [f.amount_in_bracket for f in fills] == [D(100), D(150)]


def test_stack_above_skips_lower_brackets():
    # Qualified income stacked above $100 of ordinary, into a 0/15/20 schedule.
    brackets = [(D(0), D("0.00")), (D(50), D("0.15")), (D(500), D("0.20"))]
    tax, fills = walk_brackets(D(200), brackets, stack_above=D(100))
    # 200 sits entirely in the 15% bracket → 30
    assert tax == D(30)
    assert len(fills) == 1
    assert fills[0].rate == D("0.15")


def test_negative_amount_raises():
    with pytest.raises(ValueError):
        walk_brackets(D(-1), [(D(0), D("0.10"))])
