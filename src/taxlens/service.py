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
            return row, result, imp.warnings

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
