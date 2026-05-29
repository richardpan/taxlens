"""Load year-versioned federal tax rules from YAML."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from taxlens.models import Rules

# Locate the tax_rules directory INSIDE the package so it ships with the wheel
# (and works when installed via pip, in an Electron app, or any other packaged
# distribution). Previously this was at `parents[2] / "tax_rules"`, which only
# worked when running from the dev repo and silently broke every PDF import in
# packaged builds with "No federal rules for tax year ..." errors.
_PKG_DIR = Path(__file__).resolve().parent
RULES_DIR = _PKG_DIR / "tax_rules" / "federal"


def _to_decimal(obj: Any) -> Any:
    """Recursively convert numeric leaves to Decimal so YAML floats can't sneak in."""
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return Decimal(str(obj))
    return obj


@lru_cache(maxsize=None)
def load_rules(year: int, rules_dir: Path | None = None) -> Rules:
    """Load and validate the federal rules for a given tax year."""
    base = rules_dir or RULES_DIR
    path = base / f"{year}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No federal rules for tax year {year} (looked at {path}). "
            f"Add tax_rules/federal/{year}.yaml."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _to_decimal(raw)
    for key in ("ordinary_brackets", "qualified_brackets"):
        raw[key] = {
            status: [(Decimal(low), Decimal(rate)) for low, rate in brackets]
            for status, brackets in raw[key].items()
        }
    return Rules(**raw)


STATE_RULES_DIR = _PKG_DIR / "tax_rules" / "state"


@lru_cache(maxsize=None)
def load_state_rules(state: str, year: int, rules_dir: Path | None = None) -> "StateRules":
    """Load `tax_rules/state/{state}/{year}.yaml` (case-insensitive state)."""
    from taxlens.models import StateRules  # local import to avoid cycles
    base = rules_dir or STATE_RULES_DIR
    path = base / state.lower() / f"{year}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No {state} rules for tax year {year} (looked at {path}). "
            f"Add tax_rules/state/{state.lower()}/{year}.yaml."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _to_decimal(raw)
    for key in ("ordinary_brackets", "qualified_brackets"):
        if key in raw and raw[key]:
            raw[key] = {
                status: [(Decimal(low), Decimal(rate)) for low, rate in brackets]
                for status, brackets in raw[key].items()
            }
    return StateRules(**raw)


LOCALITY_RULES_DIR = _PKG_DIR / "tax_rules" / "locality"


@lru_cache(maxsize=None)
def load_locality_rules(locality: str, year: int, rules_dir: Path | None = None) -> dict:
    """Load `tax_rules/locality/{locality}/{year}.yaml`. Returns a raw dict
    (locality rules vary widely; no shared Pydantic model yet)."""
    base = rules_dir or LOCALITY_RULES_DIR
    path = base / locality.lower() / f"{year}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No {locality} locality rules for {year} (looked at {path})."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _to_decimal(raw)
    if "ordinary_brackets" in raw and raw["ordinary_brackets"]:
        raw["ordinary_brackets"] = {
            status: [(Decimal(low), Decimal(rate)) for low, rate in brackets]
            for status, brackets in raw["ordinary_brackets"].items()
        }
    return raw
