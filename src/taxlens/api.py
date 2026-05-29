"""FastAPI sidecar serving the web UI and the JSON API.

Run with:  uvicorn taxlens.api:app --port 8765
Or:        taxlens serve
"""
from __future__ import annotations

import shutil
import tempfile
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from taxlens.service import TaxLensService

WEB_DIR = Path(__file__).parent / "web"

try:
    APP_VERSION = _pkg_version("taxlens")
except PackageNotFoundError:  # pragma: no cover — editable install fallback
    APP_VERSION = "dev"

app = FastAPI(title="TaxLens", version=APP_VERSION)
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


@app.get("/api/diff")
def diff_returns(left: int, right: int) -> dict[str, Any]:
    out = service.diff_returns(left, right)
    if out is None:
        raise HTTPException(404, "left or right return not found")
    return out


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
    except Exception as e:
        # Anything else (encrypted PDF, parser crash, pydantic validation, etc.)
        # — return diagnostic info instead of a bare 500 so the user sees what went wrong.
        import traceback
        tb = traceback.format_exc().splitlines()[-6:]
        raise HTTPException(
            422,
            f"Could not parse {file.filename or 'upload'}: {type(e).__name__}: {e}. "
            f"Try /api/debug/extract to inspect the PDF text. Tail: {' | '.join(tb)}",
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/debug/extract")
async def debug_extract(file: UploadFile = File(...)) -> dict[str, Any]:
    """Diagnostic: extract raw text from a PDF (or first 4KB of any file) without
    trying to parse it as a return. Helps debug import failures by showing
    exactly what the importer sees."""
    suffix = Path(file.filename or "").suffix.lower() or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        if suffix == ".pdf":
            import pdfplumber
            try:
                with pdfplumber.open(str(tmp_path)) as pdf:
                    pages = [(p.extract_text() or "") for p in pdf.pages]
                return {
                    "filename": file.filename,
                    "kind": "pdf",
                    "page_count": len(pages),
                    "nonempty_pages": sum(1 for p in pages if p.strip()),
                    "pages": [{"index": i, "text": p[:8000]} for i, p in enumerate(pages)],
                }
            except Exception as e:
                return {
                    "filename": file.filename,
                    "kind": "pdf",
                    "error": f"{type(e).__name__}: {e}",
                    "hint": "PDF may be encrypted, corrupted, or scanned without text layer.",
                }
        else:
            raw = tmp_path.read_bytes()[:4096]
            try:
                return {"filename": file.filename, "kind": "text", "preview": raw.decode("utf-8", errors="replace")}
            except Exception:
                return {"filename": file.filename, "kind": "binary", "size": tmp_path.stat().st_size}
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


@app.post("/api/returns/{return_id}/simulate/roth")
def simulate_roth(return_id: int, body: dict[str, Any]) -> dict[str, Any]:
    out = service.simulate_roth(return_id, body.get("amount", 0))
    if out is None:
        raise HTTPException(404)
    return out


@app.post("/api/returns/{return_id}/simulate/tlh")
def simulate_tlh(return_id: int, body: dict[str, Any]) -> dict[str, Any]:
    out = service.simulate_tlh(return_id, body.get("loss_amount", 0))
    if out is None:
        raise HTTPException(404)
    return out


# Static UI ───────────────────────────────────────────────────────────────────

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # Stale-cache busting: rewrite `app.js` references to include a version
    # query string, and force `no-cache` on the HTML shell itself. Without
    # this, a browser that loaded a prior version keeps serving the old
    # bundle from disk-cache and never sees UI fixes (e.g. the returns-
    # menu button) until the user manually hard-refreshes.
    _INDEX_HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace(
        '/static/app.js', f'/static/app.js?v={APP_VERSION}'
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> Response:
        return Response(
            content=_INDEX_HTML,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
else:
    @app.get("/")
    def index() -> JSONResponse:  # pragma: no cover
        return JSONResponse({"message": "TaxLens API is running; web UI not bundled."})
