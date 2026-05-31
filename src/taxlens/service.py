"""Service layer: stateful operations that combine importers, engine, and DB.

The FastAPI router and the CLI both call into this module so the two front
doors stay in sync.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from taxlens import compute
from taxlens.db import (
    ComputationCache,
    Override,
    StoredReturn,
    default_db_path,
    dumps,
    make_sessionmaker,
)
from taxlens.importers import Imported, import_path
from taxlens.models import Return, TaxResult


def _to_jsonable(v: Any) -> Any:
    """Coerce Decimal → str (JSON-safe). Pass through everything else."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v.quantize(Decimal("0.01")))
    return v


@dataclass
class TaxLensService:
    sessionmaker_: sessionmaker[Session]

    @classmethod
    def open(cls, db_path: Path | None = None) -> "TaxLensService":
        return cls(make_sessionmaker(db_path or default_db_path()))

    # ── ingest ───────────────────────────────────────────────────────────────

    def import_file(self, path: Path) -> tuple[StoredReturn, TaxResult, list[str]]:
        imported = import_path(path)
        return self._store(imported)

    def import_return(self, ret: Return, *, source: str = "manual",
                      source_hash: str = "", source_filename: str | None = None,
                      warnings: list[str] | None = None) -> tuple[StoredReturn, TaxResult, list[str]]:
        imp = Imported(
            ret=ret,
            source=source,
            source_hash=source_hash,
            source_filename=source_filename,
            warnings=warnings or [],
        )
        return self._store(imp)

    def _store(self, imp: Imported) -> tuple[StoredReturn, TaxResult, list[str]]:
        result = compute(imp.ret)
        with self.sessionmaker_() as s:
            # Replace any existing row with the same hash (idempotent re-import).
            if imp.source_hash:
                existing = s.execute(
                    select(StoredReturn).where(StoredReturn.source_hash == imp.source_hash)
                ).scalar_one_or_none()
                if existing is not None:
                    s.delete(existing)
                    s.flush()

            row = StoredReturn(
                tax_year=imp.ret.tax_year,
                filing_status=imp.ret.filing_status.value,
                source=imp.source,
                source_hash=imp.source_hash,
                source_filename=imp.source_filename,
                return_json=dumps(imp.ret.model_dump(mode="json")),
            )
            row.cache = ComputationCache(result_json=dumps(result.model_dump(mode="json")))
            s.add(row)
            s.commit()
            s.refresh(row)
            # After any import, recompute the carryforward chain so the new
            # return inherits prior-year losses (and propagates its own).
            self._reflow_carryforwards()
            return row, result, imp.warnings

    # ── multi-year carryforward chain ────────────────────────────────────────

    def _reflow_carryforwards(self) -> None:
        """Recompute every stored return in tax-year order, threading all multi-year
        carryforwards from each year into the next. Idempotent.

        Threaded carryforwards:
          - capital loss (§1212(b)) — always carries
          - NOL (§172) — always carries (post-TCJA, indefinite)
          - passive losses (§469, Form 8582) — always carries
          - AMT credit (Form 8801) — always carries
          - Foreign Tax Credit (§904) — 10-year limit (we don't expire here; small return counts)
          - charitable contribution (§170(d)) — 5-year limit (we don't expire here)

        All chains reset on year gaps >1 (defensive: we can't infer what happened
        in the missing years)."""
        carryforward_keys = [
            ("capital_loss_carryforward_in", "capital_loss_carryforward_out"),
            ("nol_carryforward_in",          "nol_carryforward_out"),
            ("suspended_passive_losses_carryforward", "passive_loss_disallowed"),
            ("amt_credit_carryforward_in",   "amt_credit_carryforward_out"),
            ("ftc_carryforward_in",          "ftc_carryforward_out"),
            ("charitable_carryover_in",      "charitable_carryover_out"),
            ("ira_basis_in",                 "ira_basis_out"),
            ("excess_ira_contributions_in",  "excess_ira_contributions_out"),
        ]
        with self.sessionmaker_() as s:
            rows = s.execute(
                select(StoredReturn).order_by(StoredReturn.tax_year, StoredReturn.imported_at)
            ).scalars().all()
            carry_state: dict[str, Decimal] = {in_k: Decimal("0") for in_k, _ in carryforward_keys}
            # Per-property accumulated depreciation, keyed by property id, threaded
            # forward across years like the scalar carryforwards above.
            prop_accum: dict[str, Decimal] = {}
            # Structured FTC carryforward lots (per §904(c) 10-year aging).
            # Threaded forward as a list of {"year": int, "amount": Decimal}
            # entries; entries older than 10 years are dropped inside the
            # engine's _compute_ftc.
            ftc_lots_state: list[dict[str, Any]] = []
            # Structured NOL carryforward lots (per §172 pre-TCJA 20-year aging).
            nol_lots_state: list[dict[str, Any]] = []
            # Per-activity suspended PAL balances threaded across years.
            per_activity_pal_state: dict[str, Decimal] = {}
            prev_year: int | None = None
            for row in rows:
                if prev_year is not None and row.tax_year - prev_year > 1:
                    carry_state = {in_k: Decimal("0") for in_k, _ in carryforward_keys}
                    prop_accum = {}
                    ftc_lots_state = []
                    nol_lots_state = []
                    per_activity_pal_state = {}
                data = json.loads(row.return_json)
                for in_k, _ in carryforward_keys:
                    data[in_k] = str(carry_state[in_k])
                data["ftc_carryforward_lots_in"] = ftc_lots_state
                data["nol_carryforward_lots_in"] = nol_lots_state
                # Update prior_accumulated_depreciation on each rental property
                # from the running per-property accumulator. Also thread
                # per-activity suspended-loss buckets forward.
                for p in data.get("rental_properties", []) or []:
                    pid = p.get("id")
                    if pid and pid in prop_accum:
                        p["prior_accumulated_depreciation"] = str(prop_accum[pid])
                    if pid and pid in per_activity_pal_state:
                        p["suspended_loss_in"] = str(per_activity_pal_state[pid])
                ret = Return(**self._decimalize(data))
                result = compute(ret)
                row.return_json = dumps(ret.model_dump(mode="json"))
                if row.cache is None:
                    row.cache = ComputationCache(result_json=dumps(result.model_dump(mode="json")))
                else:
                    row.cache.result_json = dumps(result.model_dump(mode="json"))
                # Propagate each chain forward.
                for in_k, out_k in carryforward_keys:
                    carry_state[in_k] = getattr(result, out_k, None) or Decimal("0")
                # Thread FTC lots forward as well.
                ftc_lots_state = list(getattr(result, "ftc_carryforward_lots_out", []) or [])
                # And NOL lots (pre-TCJA vintages age out at 20 years inside the engine).
                nol_lots_state = list(getattr(result, "nol_carryforward_lots_out", []) or [])
                # And per-activity PAL buckets.
                per_activity_pal_state = {
                    pid: Decimal(str(amt))
                    for pid, amt in (result.per_activity_suspended_pal_out or {}).items()
                }
                for pid, accum in (result.depreciation_accumulated_out or {}).items():
                    prop_accum[pid] = Decimal(str(accum))
                prev_year = row.tax_year
            s.commit()

    # ── query ────────────────────────────────────────────────────────────────

    def list_returns(self) -> list[dict[str, Any]]:
        with self.sessionmaker_() as s:
            rows = s.execute(
                select(StoredReturn).order_by(StoredReturn.tax_year)
            ).scalars().all()
            return [self._summary(r) for r in rows]

    def get_return(self, return_id: int) -> dict[str, Any] | None:
        with self.sessionmaker_() as s:
            row = s.get(StoredReturn, return_id)
            if row is None:
                return None
            return self._full(row)

    def get_by_year(self, year: int) -> dict[str, Any] | None:
        with self.sessionmaker_() as s:
            row = s.execute(
                select(StoredReturn).where(StoredReturn.tax_year == year)
                .order_by(StoredReturn.imported_at.desc())
            ).scalars().first()
            if row is None:
                return None
            return self._full(row)

    def diff_returns(self, left_id: int, right_id: int) -> dict[str, Any] | None:
        """Attribute the total-tax delta between two returns to individual drivers.

        For each tracked input dimension we re-run the engine with just that
        field swapped from L to R and measure how much L's total_tax shifts.
        We also run an "L inputs, R tax year" probe to isolate rule-change
        contribution (different brackets / SD / credits).

        Returns a dict with overall deltas and an ordered list of drivers, each
        of the form: {label, left, right, delta, attributed_tax, kind}.
        """
        with self.sessionmaker_() as s:
            l_row = s.get(StoredReturn, left_id)
            r_row = s.get(StoredReturn, right_id)
        if l_row is None or r_row is None:
            return None
        l_data = self._decimalize(json.loads(l_row.return_json))
        r_data = self._decimalize(json.loads(r_row.return_json))
        l_ret = Return(**l_data)
        r_ret = Return(**r_data)
        l_res = compute(l_ret)
        r_res = compute(r_ret)
        overall_delta = r_res.total_tax - l_res.total_tax

        # Field dimensions to probe. Each entry: (Return field, label, kind).
        probes = [
            ("wages",                       "Wages",                "income"),
            ("interest_income",             "Interest income",      "income"),
            ("ordinary_dividends",          "Ordinary dividends",   "income"),
            ("qualified_dividends",         "Qualified dividends",  "income"),
            ("long_term_capital_gains",     "Long-term cap gains",  "income"),
            ("short_term_capital_gains",    "Short-term cap gains", "income"),
            ("se_income",                   "Self-employment",      "income"),
            ("rental_net_income",           "Rental income",        "income"),
            ("other_ordinary_income",       "Other ordinary",       "income"),
            ("qualifying_children",         "Qualifying children",  "credits"),
            ("other_dependents",            "Other dependents",     "credits"),
            ("traditional_401k_contributions", "401(k) deferral",   "deductions"),
            ("traditional_ira_contributions", "Traditional IRA",    "deductions"),
            ("hsa_deduction",               "HSA deduction",        "deductions"),
            ("salt_paid",                   "SALT paid",            "deductions"),
            ("mortgage_interest",           "Mortgage interest",    "deductions"),
            ("charitable_contributions",    "Charitable",           "deductions"),
            ("itemized_deductions",         "Itemized total",       "deductions"),
            ("federal_withholding",         "Federal withholding",  "payments"),
            ("estimated_payments",          "Estimated payments",   "payments"),
            ("foreign_taxes_paid",          "Foreign taxes paid",   "credits"),
        ]

        drivers: list[dict[str, Any]] = []
        for field_name, label, kind in probes:
            l_val = getattr(l_ret, field_name, None)
            r_val = getattr(r_ret, field_name, None)
            if l_val == r_val:
                continue
            # Swap one field at a time, holding L's tax year + other inputs constant.
            probed = l_ret.model_copy(update={field_name: r_val})
            try:
                probed_res = compute(probed)
            except Exception:
                continue
            attributed = probed_res.total_tax - l_res.total_tax
            # For payments (withholding, estimated), total_tax doesn't move but
            # refund does. Report refund delta as the "attributed" effect instead.
            if kind == "payments":
                attributed = -(probed_res.refund_or_owed - l_res.refund_or_owed)
            drivers.append({
                "field": field_name,
                "label": label,
                "kind": kind,
                "left": _to_jsonable(l_val),
                "right": _to_jsonable(r_val),
                "delta": _to_jsonable(
                    (Decimal(r_val) - Decimal(l_val)) if isinstance(l_val, (int, Decimal)) else None
                ),
                "attributed_tax": _to_jsonable(attributed),
            })

        # Rule-change attribution: swap L's tax_year to R's, keep all inputs.
        rule_attributed: Decimal | None = None
        if l_ret.tax_year != r_ret.tax_year:
            try:
                rule_probe = l_ret.model_copy(update={"tax_year": r_ret.tax_year})
                rule_res = compute(rule_probe)
                rule_attributed = rule_res.total_tax - l_res.total_tax
                drivers.append({
                    "field": "_rules",
                    "label": f"Rule changes ({l_ret.tax_year} → {r_ret.tax_year})",
                    "kind": "rules",
                    "left": l_ret.tax_year,
                    "right": r_ret.tax_year,
                    "delta": None,
                    "attributed_tax": _to_jsonable(rule_attributed),
                })
            except Exception:
                pass

        # Sort by absolute attributed_tax desc, but stash "rules" near the top
        # so users see "the law changed" as a first-class driver.
        def _sort_key(d: dict[str, Any]) -> tuple[int, float]:
            attr = abs(float(d["attributed_tax"] or 0))
            return (0 if d["kind"] == "rules" else 1, -attr)

        drivers.sort(key=_sort_key)

        # Residual: overall_delta - sum(attributed). Reveals non-linear interactions
        # (e.g. when AMT crosses over, when bracket boundaries are hit, etc.).
        attributed_sum = sum((Decimal(str(d["attributed_tax"])) for d in drivers if d["attributed_tax"] is not None), Decimal(0))
        residual = overall_delta - attributed_sum

        return {
            "left":  {"id": left_id,  "tax_year": l_ret.tax_year, "total_tax": _to_jsonable(l_res.total_tax),
                       "refund_or_owed": _to_jsonable(l_res.refund_or_owed), "agi": _to_jsonable(l_res.agi)},
            "right": {"id": right_id, "tax_year": r_ret.tax_year, "total_tax": _to_jsonable(r_res.total_tax),
                       "refund_or_owed": _to_jsonable(r_res.refund_or_owed), "agi": _to_jsonable(r_res.agi)},
            "overall_tax_delta": _to_jsonable(overall_delta),
            "drivers": drivers,
            "residual": _to_jsonable(residual),
        }

    def advise_return(self, return_id: int) -> dict[str, Any] | None:
        """Run the single-year advisor on one stored return."""
        from taxlens.advisor import advise
        with self.sessionmaker_() as s:
            row = s.get(StoredReturn, return_id)
            if row is None:
                return None
            ret = Return(**self._decimalize(json.loads(row.return_json)))
            result = compute(ret)
            recs = advise(ret, result)
            return {
                "return_id": return_id,
                "tax_year": ret.tax_year,
                "recommendations": [r.to_dict() for r in recs],
            }

    def advise_all(self) -> dict[str, Any]:
        """Run single-year + multi-year advisors across every stored return."""
        from taxlens.advisor import advise
        from taxlens.advisor_multi import advise_multi
        per_year: list[dict[str, Any]] = []
        history: list[tuple[Return, TaxResult]] = []
        with self.sessionmaker_() as s:
            rows = s.execute(
                select(StoredReturn).order_by(StoredReturn.tax_year.asc())
            ).scalars().all()
            for row in rows:
                ret = Return(**self._decimalize(json.loads(row.return_json)))
                result = compute(ret)
                history.append((ret, result))
                per_year.append({
                    "return_id": row.id,
                    "tax_year": ret.tax_year,
                    "recommendations": [r.to_dict() for r in advise(ret, result)],
                })
        cross = [r.to_dict() for r in advise_multi(history)]
        return {"per_year": per_year, "cross_year": cross}

    def delete_return(self, return_id: int) -> bool:
        with self.sessionmaker_() as s:
            row = s.get(StoredReturn, return_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    # ── what-if ──────────────────────────────────────────────────────────────

    def whatif(self, return_id: int, edits: dict[str, Any]) -> dict[str, Any] | None:
        """Recompute with the given field overrides applied to the stored Return.

        Does not persist the result; returns the new TaxResult alongside the
        original for side-by-side rendering.
        """
        with self.sessionmaker_() as s:
            row = s.get(StoredReturn, return_id)
            if row is None:
                return None
            base = Return(**self._decimalize(json.loads(row.return_json)))
            # Use full validation so string edits (from JSON/form inputs) coerce to Decimal.
            merged = {**base.model_dump(), **self._decimalize(edits)}
            updated = Return.model_validate(merged)
            original_result = compute(base)
            whatif_result = compute(updated)
            return {
                "original": original_result.model_dump(mode="json"),
                "whatif": whatif_result.model_dump(mode="json"),
                "edits": {k: str(v) for k, v in edits.items()},
            }

    # ── planning simulators ──────────────────────────────────────────────────

    def _load_return(self, return_id: int) -> Return | None:
        with self.sessionmaker_() as s:
            row = s.get(StoredReturn, return_id)
            if row is None:
                return None
            return Return(**self._decimalize(json.loads(row.return_json)))

    def simulate_roth(self, return_id: int, amount: Decimal) -> dict[str, Any] | None:
        from taxlens.simulators import simulate_roth_conversion
        base = self._load_return(return_id)
        if base is None:
            return None
        return simulate_roth_conversion(base, Decimal(amount)).to_json()

    def simulate_tlh(self, return_id: int, loss_amount: Decimal) -> dict[str, Any] | None:
        from taxlens.simulators import simulate_tax_loss_harvest
        base = self._load_return(return_id)
        if base is None:
            return None
        return simulate_tax_loss_harvest(base, Decimal(loss_amount)).to_json()

    def commit_override(self, return_id: int, field: str, new_value: str, reason: str | None) -> bool:
        with self.sessionmaker_() as s:
            row = s.get(StoredReturn, return_id)
            if row is None:
                return False
            base_dict = json.loads(row.return_json)
            previous = base_dict.get(field)
            s.add(Override(
                return_id=return_id, field=field,
                previous_value=None if previous is None else str(previous),
                new_value=new_value, reason=reason,
            ))
            base_dict[field] = new_value
            new_ret = Return(**self._decimalize(base_dict))
            new_result = compute(new_ret)
            row.return_json = dumps(new_ret.model_dump(mode="json"))
            row.cache = ComputationCache(result_json=dumps(new_result.model_dump(mode="json")))
            s.commit()
            return True

    # ── helpers ──────────────────────────────────────────────────────────────

    def _summary(self, row: StoredReturn) -> dict[str, Any]:
        cached = json.loads(row.cache.result_json) if row.cache else {}
        return {
            "id": row.id,
            "tax_year": row.tax_year,
            "filing_status": row.filing_status,
            "source": row.source,
            "source_filename": row.source_filename,
            "imported_at": row.imported_at.isoformat(),
            "agi": cached.get("agi"),
            "total_tax": cached.get("total_tax"),
            "refund_or_owed": cached.get("refund_or_owed"),
            "reconciled": (
                cached.get("reconciliation_delta") is not None
                and abs(Decimal(cached["reconciliation_delta"])) <= Decimal("1.00")
            ) if cached.get("reconciliation_delta") is not None else None,
            "reconciliation_delta": cached.get("reconciliation_delta"),
        }

    def _full(self, row: StoredReturn) -> dict[str, Any]:
        return {
            "id": row.id,
            "tax_year": row.tax_year,
            "filing_status": row.filing_status,
            "source": row.source,
            "source_filename": row.source_filename,
            "imported_at": row.imported_at.isoformat(),
            "return": json.loads(row.return_json),
            "result": json.loads(row.cache.result_json) if row.cache else None,
            "overrides": [
                {
                    "field": o.field,
                    "previous_value": o.previous_value,
                    "new_value": o.new_value,
                    "reason": o.reason,
                    "created_at": o.created_at.isoformat(),
                } for o in row.overrides
            ],
        }

    @staticmethod
    def _decimalize(obj: Any) -> Any:
        # Only money-like fields need Decimal conversion; pydantic coerces strings → Decimal.
        if isinstance(obj, dict):
            return {k: TaxLensService._decimalize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [TaxLensService._decimalize(v) for v in obj]
        return obj
