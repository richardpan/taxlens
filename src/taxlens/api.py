"""FastAPI sidecar serving the web UI and the JSON API.

Run with:  uvicorn taxlens.api:app --port 8765
Or:        taxlens serve
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from taxlens.service import TaxLensService

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="TaxLens", version="0.0.1")
service = TaxLensService.open()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/returns")
def list_returns() -> list[dict[str, Any]]:
    return service.list_returns()


@app.get("/api/returns/{return_id}")
def get_return(return_id: int) -> dict[str, Any]:
    out = service.get_return(return_id)
    if out is None:
        raise HTTPException(404, f"return {return_id} not found")
    return out


@app.delete("/api/returns/{return_id}")
def delete_return(return_id: int) -> dict[str, bool]:
    ok = service.delete_return(return_id)
    if not ok:
        raise HTTPException(404)
    return {"deleted": True}


@app.post("/api/returns/import")
async def import_return(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower() or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        row, result, warnings = service.import_file(tmp_path)
        return {
            "id": row.id,
            "tax_year": row.tax_year,
            "filing_status": row.filing_status,
            "source": row.source,
            "warnings": warnings,
            "result": result.model_dump(mode="json"),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/returns/{return_id}/whatif")
def whatif(return_id: int, edits: dict[str, Any]) -> dict[str, Any]:
    out = service.whatif(return_id, edits)
    if out is None:
        raise HTTPException(404)
    return out


@app.post("/api/returns/{return_id}/override")
def override(return_id: int, payload: dict[str, Any]) -> dict[str, bool]:
    field = payload.get("field")
    value = payload.get("value")
    reason = payload.get("reason")
    if not field or value is None:
        raise HTTPException(400, "field and value are required")
    ok = service.commit_override(return_id, str(field), str(value), reason)
    if not ok:
        raise HTTPException(404)
    return {"ok": True}


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    """Pre-aggregated multi-year summary for the dashboard screen."""
    returns = service.list_returns()
    return {"returns": returns}


@app.get("/api/advisor")
def advisor() -> dict[str, Any]:
    """All recommendations across every stored return."""
    return service.advise_all()


@app.get("/api/returns/{return_id}/advisor")
def advisor_one(return_id: int) -> dict[str, Any]:
    out = service.advise_return(return_id)
    if out is None:
        raise HTTPException(404)
    return out


@app.post("/api/demo/load")
def load_demo() -> dict[str, Any]:
    """Bulk-import the bundled demo returns (idempotent: re-importing replaces)."""
    from taxlens.demo import demo_files
    loaded: list[dict[str, Any]] = []
    for path in demo_files():
        row, result, warnings = service.import_file(path)
        loaded.append({
            "id": row.id, "tax_year": row.tax_year,
            "filing_status": row.filing_status,
            "total_tax": str(result.total_tax),
            "warnings": warnings,
        })
    return {"loaded": loaded, "count": len(loaded)}


# Static UI ───────────────────────────────────────────────────────────────────

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))
else:
    @app.get("/")
    def index() -> JSONResponse:  # pragma: no cover
        return JSONResponse({"message": "TaxLens API is running; web UI not bundled."})
