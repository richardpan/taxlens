"""Smoke tests for the recently-added state YAMLs and the 1099-B CSV importer."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from taxlens.engine import compute
from taxlens.importers.broker_csv import import_csv
from taxlens.models import FilingStatus, Return


def _base_return(state: str, wages: Decimal, *, status: FilingStatus = FilingStatus.SINGLE,
                 tax_year: int = 2024) -> Return:
    return Return(
        tax_year=tax_year,
        filing_status=status,
        state=state,
        wages=wages,
    )


# ---------- state YAMLs --------------------------------------------------

def _state_tax(res) -> Decimal:
    sr = res.state_result
    assert sr is not None, "expected a StateResult"
    return sr.state_tax


def test_ny_single_2024_has_state_tax():
    r = _base_return("NY", Decimal("100000"))
    res = compute(r)
    assert _state_tax(res) > Decimal("3000")


def test_il_flat_rate_4_95_percent():
    r = _base_return("IL", Decimal("100000"))
    res = compute(r)
    assert Decimal("4000") < _state_tax(res) < Decimal("5500")


@pytest.mark.parametrize("state", ["TX", "FL", "WA"])
def test_no_income_tax_states(state):
    r = _base_return(state, Decimal("200000"))
    res = compute(r)
    assert _state_tax(res) == Decimal("0")


# ---------- broker CSV importer -----------------------------------------

CSV_BASIC = """Symbol,Quantity,Date Acquired,Date Sold,Proceeds,Cost Basis
AAPL,100,2020-03-15,2024-06-10,18000.00,9000.00
TSLA,50,2024-01-05,2024-09-20,6000.00,8500.00
MSFT,25,2018-11-01,2024-02-14,11000.00,4000.00
"""


def test_broker_csv_basic_lt_st_split(tmp_path: Path):
    p = tmp_path / "1099b.csv"
    p.write_text(CSV_BASIC, encoding="utf-8")
    imported = import_csv(p)
    r = imported.ret
    # AAPL + MSFT both long-term:  (18000-9000) + (11000-4000) = 16000
    assert r.long_term_capital_gains == Decimal("16000.00")
    # TSLA short-term loss: 6000 - 8500 = -2500
    assert r.short_term_capital_gains == Decimal("-2500.00")
    assert r.tax_year == 2024
    assert imported.source == "broker_csv"


def test_broker_csv_handles_money_formatting(tmp_path: Path):
    body = (
        "Symbol,Date Acquired,Date Sold,Proceeds,Cost Basis\n"
        "X,01/02/2020,06/30/2024,\"$1,500.00\",\"$1,000.00\"\n"
        "Y,02/15/2024,11/01/2024,\"$2,000.00\",\"$2,500.00\"\n"
    )
    p = tmp_path / "fmt.csv"
    p.write_text(body, encoding="utf-8")
    imported = import_csv(p)
    r = imported.ret
    assert r.long_term_capital_gains == Decimal("500.00")
    # Y is a short-term loss: 2000 - 2500 = -500
    assert r.short_term_capital_gains == Decimal("-500.00")


def test_broker_csv_rejects_missing_required_columns(tmp_path: Path):
    p = tmp_path / "bad.csv"
    p.write_text("Symbol,Quantity\nAAPL,100\n", encoding="utf-8")
    with pytest.raises(ValueError):
        import_csv(p)
