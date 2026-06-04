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

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from taxlens.service import TaxLensService

WEB_DIR = Path(__file__).parent / "web"

def _read_version() -> str:
    """Resolve the package version.

    Prefer installed-package metadata; fall back to parsing
    ``pyproject.toml`` next to the source tree so source/editable
    runs (where the dist-info may be missing or stale) still surface
    the real version in the UI footer instead of the literal "dev".
    """
    try:
        return _pkg_version("taxlens")
    except PackageNotFoundError:
        pass
    # Walk up looking for pyproject.toml.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            try:
                import tomllib  # py311+
            except ModuleNotFoundError:  # pragma: no cover
                import tomli as tomllib  # type: ignore[no-redef]
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
                v = data.get("project", {}).get("version")
                if v:
                    return str(v)
            except Exception:
                pass
            break
    return "dev"


APP_VERSION = _read_version()

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
        # Extract import-log basename (if any) from the warnings list so the
        # UI can render a "View log" link without re-parsing the string.
        import_log = None
        for w in warnings:
            if w.startswith("Import log written to: "):
                p = Path(w[len("Import log written to: "):].strip())
                import_log = p.name
                break
        return {
            "id": row.id,
            "tax_year": row.tax_year,
            "filing_status": row.filing_status,
            "source": row.source,
            "warnings": warnings,
            "import_log": import_log,
            "result": result.model_dump(mode="json"),
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        # Anything else (encrypted PDF, parser crash, pydantic validation, etc.)
        # — return diagnostic info instead of a bare 500 so the user sees what went wrong.
        import traceback
        tb = traceback.format_exc().splitlines()[-6:]
        raise HTTPException(
            422,
            f"Could not parse {file.filename or 'upload'}: {type(e).__name__}: {e}. "
            f"Try /api/debug/extract to inspect the PDF text. Tail: {' | '.join(tb)}",
        ) from e
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

            from taxlens.importers.pdf import (
                _is_form_page,
                _layout_text,
                _extract_fields,
                LINE_PATTERNS,
            )
            from taxlens.importers.acroform import extract_acroform_fields
            acroform_fields, acroform_warnings = extract_acroform_fields(tmp_path)
            try:
                with pdfplumber.open(str(tmp_path)) as pdf:
                    page_texts: list[tuple[str, str, str]] = []
                    for p in pdf.pages:
                        page_texts.append((
                            p.extract_text() or "",
                            _layout_text(p, y_tol=3.0),
                            _layout_text(p, y_tol=8.0),
                        ))
                default_pages = [d for d, _, _ in page_texts]
                tight_pages = [t for _, t, _ in page_texts]
                loose_pages = [l for _, _, l in page_texts]
                default_fields, _, _ = _extract_fields(default_pages)
                tight_fields, _, _ = _extract_fields(tight_pages)
                loose_fields, _, _ = _extract_fields(loose_pages)
                only_layout = (set(tight_fields) | set(loose_fields)) - set(default_fields)
                return {
                    "filename": file.filename,
                    "kind": "pdf",
                    "page_count": len(page_texts),
                    "nonempty_pages": sum(1 for d, _, _ in page_texts if d.strip()),
                    "fields_acroform": {k: str(v) for k, v in acroform_fields.items()},
                    "acroform_warnings": acroform_warnings,
                    "fields_default_text": {k: str(v) for k, v in default_fields.items()},
                    "fields_layout_tight": {k: str(v) for k, v in tight_fields.items()},
                    "fields_layout_loose": {k: str(v) for k, v in loose_fields.items()},
                    "fields_only_in_layout": sorted(only_layout),
                    "known_field_names": sorted(LINE_PATTERNS.keys()),
                    "pages": [
                        {
                            "index": i,
                            "is_form_page": _is_form_page(d) or _is_form_page(t) or _is_form_page(l),
                            "text": d[:8000],
                            "layout_tight": t[:8000],
                            "layout_loose": l[:8000],
                        }
                        for i, (d, t, l) in enumerate(page_texts)
                    ],
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
    paths = list(demo_files())
    for path, (row, result, warnings) in zip(paths, service.import_files(paths)):
        if row is None or result is None:
            loaded.append({
                "id": None, "tax_year": None, "filing_status": None,
                "total_tax": None, "warnings": warnings,
                "source_filename": path.name, "error": True,
            })
            continue
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


@app.post("/api/returns/{return_id}/simulate/roth-ladder")
def simulate_roth_ladder(return_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Project a multi-year Roth conversion ladder. Body schema:
    ``{"schedule": [40000, 40000, 40000]}`` — the i-th entry is the
    conversion amount in year (base.tax_year + i)."""
    schedule = body.get("schedule") or []
    if not isinstance(schedule, list) or not schedule:
        raise HTTPException(400, "schedule must be a non-empty list")
    out = service.simulate_roth_ladder(return_id, schedule)
    if out is None:
        raise HTTPException(404)
    return out


@app.post("/api/returns/{return_id}/simulate/tlh-projection")
def simulate_tlh_projection(return_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Project the depletion of a TLH carryforward across future years
    (worst-case $3k/yr ordinary offset). Body: ``{"loss_amount": ...,
    "years": 10}``."""
    out = service.project_tlh_carryforward(
        return_id,
        body.get("loss_amount", 0),
        body.get("years", 10),
    )
    if out is None:
        raise HTTPException(404)
    return out


# Import-log access ───────────────────────────────────────────────────────────
# Each PDF import writes a diagnostic log (every AcroForm field, every
# pattern hit, the final extracted dict). Surface them via the dashboard
# so users can inspect / share them when reporting missing fields.

@app.get("/api/import-logs")
def list_import_logs() -> list[dict[str, Any]]:
    from taxlens.importers.import_log import logs_dir
    d = logs_dir()
    out: list[dict[str, Any]] = []
    if not d.exists():
        return out
    for p in sorted(d.glob("import-*.log"), reverse=True)[:200]:
        try:
            st = p.stat()
            out.append({
                "name": p.name,
                "size": st.st_size,
                "modified": int(st.st_mtime),
            })
        except OSError:
            continue
    return out


@app.get("/api/import-logs/{name}")
def get_import_log(name: str) -> Response:
    from taxlens.importers.import_log import logs_dir
    # Hard-guard against path traversal — only allow basenames that match
    # our own naming convention.
    if "/" in name or "\\" in name or ".." in name or not name.startswith("import-"):
        raise HTTPException(400, "invalid log name")
    p = logs_dir() / name
    if not p.is_file():
        raise HTTPException(404)
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="text/plain; charset=utf-8",
    )


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
    ).replace(
        "{{APP_VERSION}}", APP_VERSION
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
                # Defense-in-depth: even if someone adds a CDN <script> tag
                # later, this CSP blocks the browser from making the
                # external request. Tailwind's runtime JIT compiler uses
                # eval() so we have to allow 'unsafe-eval'; the critical
                # restriction is ``connect-src 'self'`` which prevents
                # any fetch/XHR/WebSocket leaving the local server.
                "Content-Security-Policy": (
                    "default-src 'self'; "
                    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                    "style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; "
                    "connect-src 'self'; "
                    "font-src 'self' data:; "
                    "frame-ancestors 'none'"
                ),
            },
        )
else:
    @app.get("/")
    def index() -> JSONResponse:  # pragma: no cover
        return JSONResponse({"message": "TaxLens API is running; web UI not bundled."})
