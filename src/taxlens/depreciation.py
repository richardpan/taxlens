"""MACRS depreciation for Schedule E real and personal property.

Real property (residential 27.5y, nonresidential 39y) uses straight-line
with the **mid-month convention**: deduction in the placed-in-service
year equals SL × (12.5 − month) / 12, treating the asset as placed mid-month.
On disposal in a later year, the same mid-month convention prorates the
exit year: SL × (month − 0.5) / 12.

Personal property (5y appliances, 15y land improvements) uses the IRS
optional half-year-convention tables (200% DB switching to SL for 5y,
150% DB switching to SL for 15y). Tables are exact from Rev. Proc. 87-57.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .models import RentalProperty

ZERO = Decimal(0)


# Rev. Proc. 87-57 half-year convention tables (percent of basis per year).
# 5-year, 200% DB switching to SL:
_TABLE_5Y_HY = [
    Decimal("0.2000"),
    Decimal("0.3200"),
    Decimal("0.1920"),
    Decimal("0.1152"),
    Decimal("0.1152"),
    Decimal("0.0576"),
]
# 15-year, 150% DB switching to SL:
_TABLE_15Y_HY = [
    Decimal("0.0500"), Decimal("0.0950"), Decimal("0.0855"), Decimal("0.0770"),
    Decimal("0.0693"), Decimal("0.0623"), Decimal("0.0590"), Decimal("0.0590"),
    Decimal("0.0591"), Decimal("0.0590"), Decimal("0.0591"), Decimal("0.0590"),
    Decimal("0.0591"), Decimal("0.0590"), Decimal("0.0591"), Decimal("0.0295"),
]


@dataclass(frozen=True)
class _Class:
    life_years: Decimal           # for real-property SL math
    table: list[Decimal] | None   # for personal-property HY tables


_CLASSES: dict[str, _Class] = {
    "residential": _Class(Decimal("27.5"), None),
    "nonresidential": _Class(Decimal(39), None),
    "personal_5y": _Class(Decimal(5), _TABLE_5Y_HY),
    "personal_15y": _Class(Decimal(15), _TABLE_15Y_HY),
}


@dataclass(frozen=True)
class PropertyResult:
    property_id: str
    current_year_deduction: Decimal
    accumulated_after: Decimal      # prior + current
    sale_recapture_1250: Decimal    # unrecaptured §1250 gain triggered this year
    sale_total_gain: Decimal        # total realized gain (incl. recapture component)


def _round(x: Decimal) -> Decimal:
    # IRS allows whole-dollar rounding; we keep cents for precision.
    return x.quantize(Decimal("0.01"))


def compute_property_year(
    prop: RentalProperty,
    tax_year: int,
) -> PropertyResult:
    """Compute one property's current-year depreciation and (if disposed) recapture."""
    cls = _CLASSES.get(prop.property_type)
    if cls is None or prop.cost_basis <= ZERO or prop.in_service_year <= 0:
        return PropertyResult(prop.id, ZERO, prop.prior_accumulated_depreciation, ZERO, ZERO)

    # If disposed in a PRIOR year, this property is done — no deduction.
    if prop.disposed_year is not None and prop.disposed_year < tax_year:
        return PropertyResult(prop.id, ZERO, prop.prior_accumulated_depreciation, ZERO, ZERO)

    # If placed in service AFTER the tax year, no deduction yet.
    if prop.in_service_year > tax_year:
        return PropertyResult(prop.id, ZERO, prop.prior_accumulated_depreciation, ZERO, ZERO)

    years_in_service = tax_year - prop.in_service_year  # 0 = placed-in-service year
    remaining = prop.cost_basis - prop.prior_accumulated_depreciation
    if remaining <= ZERO:
        remaining = ZERO

    deduction = ZERO

    if cls.table is None:
        # Real property: straight-line mid-month.
        annual = prop.cost_basis / cls.life_years
        if years_in_service == 0:
            # First year mid-month: (12.5 - month) / 12
            frac = (Decimal("12.5") - Decimal(prop.in_service_month)) / Decimal(12)
            if frac < ZERO:
                frac = ZERO
            deduction = annual * frac
        else:
            deduction = annual
        # If disposed THIS year, prorate the exit year mid-month.
        if prop.disposed_year == tax_year and prop.disposed_month:
            exit_frac = (Decimal(prop.disposed_month) - Decimal("0.5")) / Decimal(12)
            # If also placed in service this year, use intersection:
            if years_in_service == 0:
                # (disposed_month - in_service_month) months in service, mid-month both ends
                months = max(ZERO, Decimal(prop.disposed_month) - Decimal(prop.in_service_month))
                deduction = annual * months / Decimal(12)
            else:
                deduction = annual * exit_frac
    else:
        # Personal property: lookup the HY table.
        if 0 <= years_in_service < len(cls.table):
            deduction = prop.cost_basis * cls.table[years_in_service]
        # Half-year on disposal: take half of what the table would say.
        if prop.disposed_year == tax_year and prop.disposed_year != prop.in_service_year:
            deduction = deduction / Decimal(2)

    # Cap deduction to remaining basis.
    if deduction > remaining:
        deduction = remaining
    if deduction < ZERO:
        deduction = ZERO

    accumulated_after = prop.prior_accumulated_depreciation + deduction

    # Disposition: compute gain and §1250 recapture component.
    sale_recapture = ZERO
    sale_gain = ZERO
    if prop.disposed_year == tax_year and prop.sale_price > ZERO:
        adjusted_basis = prop.cost_basis - accumulated_after
        sale_gain = prop.sale_price - adjusted_basis
        if sale_gain > ZERO:
            # Unrecaptured §1250 = min(gain, accumulated depreciation).
            sale_recapture = min(sale_gain, accumulated_after)

    return PropertyResult(
        property_id=prop.id,
        current_year_deduction=_round(deduction),
        accumulated_after=_round(accumulated_after),
        sale_recapture_1250=_round(sale_recapture),
        sale_total_gain=_round(sale_gain),
    )


def compute_all(properties: Iterable[RentalProperty], tax_year: int) -> list[PropertyResult]:
    return [compute_property_year(p, tax_year) for p in properties]
