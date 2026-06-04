"""Typer-based CLI. Entry point: `taxlens` after `pip install -e .`."""
from __future__ import annotations

import contextlib
import webbrowser
from decimal import Decimal
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from taxlens.service import TaxLensService

app = typer.Typer(help="TaxLens — local-first multi-year tax return analyzer.")
console = Console()


@app.command("import")
def import_cmd(
    paths: list[Path] = typer.Argument(..., exists=True, readable=True),
) -> None:
    """Import one or more tax-return PDFs / TXF / JSON / YAML files.

    Multiple files are parsed in parallel (process pool) — ~6× faster
    than serial for batch imports.
    """
    service = TaxLensService.open()
    if len(paths) == 1:
        row, result, warnings = service.import_file(paths[0])
        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/]")
        badge = "[green]✓ reconciled[/]" if result.reconciled() else (
            f"[yellow]Δ ${result.reconciliation_delta}[/]" if result.reconciliation_delta is not None
            else "[dim]no reported value[/]"
        )
        console.print(
            f"[bold]Imported[/]: {paths[0].name} → TY {row.tax_year} "
            f"({row.filing_status.upper()})  total tax ${result.total_tax}  {badge}"
        )
        return
    results = service.import_files(paths)
    for path, (row, result, warnings) in zip(paths, results):
        if row is None or result is None:
            console.print(f"[red]✗ {path.name}: {warnings[0] if warnings else 'failed'}[/]")
            continue
        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/]")
        badge = "[green]✓ reconciled[/]" if result.reconciled() else (
            f"[yellow]Δ ${result.reconciliation_delta}[/]" if result.reconciliation_delta is not None
            else "[dim]no reported value[/]"
        )
        console.print(
            f"[bold]Imported[/]: {path.name} → TY {row.tax_year} "
            f"({row.filing_status.upper()})  total tax ${result.total_tax}  {badge}"
        )


@app.command("list")
def list_cmd() -> None:
    """List all stored returns."""
    service = TaxLensService.open()
    rows = service.list_returns()
    if not rows:
        console.print("[dim]No returns imported yet.[/]")
        return
    table = Table(title="TaxLens returns")
    for col in ("id", "year", "status", "source", "AGI", "total tax", "refund/owed", "reconciled"):
        table.add_column(col)
    for r in rows:
        recon = "—" if r["reconciled"] is None else ("✓" if r["reconciled"] else f"Δ {r['reconciliation_delta']}")
        table.add_row(
            str(r["id"]), str(r["tax_year"]), r["filing_status"], r["source"],
            f"${r.get('agi') or '—'}", f"${r.get('total_tax') or '—'}",
            f"${r.get('refund_or_owed') or '—'}", recon,
        )
    console.print(table)


@app.command("show")
def show_cmd(
    year_or_id: int = typer.Argument(..., help="Tax year (e.g. 2024) or numeric return id ≥ 1000"),
) -> None:
    """Show the full audit trail for a year (or a return id ≥ 1000)."""
    service = TaxLensService.open()
    out = service.get_return(year_or_id) if year_or_id >= 1000 else service.get_by_year(year_or_id)
    if out is None:
        raise typer.Exit(code=1)
    result = out["result"]
    console.print(f"[bold]TY {out['tax_year']}  {out['filing_status'].upper()}[/]")
    console.print(f"AGI           ${result['agi']}")
    console.print(f"Taxable       ${result['taxable_income']}")
    console.print(f"Total tax     ${result['total_tax']}")
    console.print(f"Refund/owed   ${result['refund_or_owed']}")
    console.print()
    console.print("[bold]Computation trail[/]")
    for step in result["steps"]:
        console.print(f"  [{step['index']:>2}] {step['label']:<48} = ${step['output']}")
    console.print(f"  formula: [dim]{step['formula']}[/]")


@app.command("reconcile")
def reconcile_cmd(
    paths: list[Path] = typer.Argument(
        ..., exists=True, readable=True,
        help="One or more PDF files, or directories containing PDFs.",
    ),
    max_delta: float = typer.Option(
        2.00, "--max-delta",
        help="Fail (exit code 1) if any |delta| exceeds this dollar threshold.",
    ),
    recursive: bool = typer.Option(
        False, "--recursive/--no-recursive",
        help="Recurse into subdirectories when a directory is given.",
    ),
) -> None:
    """Reconcile one or more tax-return PDFs against their reported total tax.

    Imports each PDF in-memory (NO writes to the local DB), computes the
    engine's total tax, compares against the reported total tax extracted
    from the PDF, and prints a delta table. Exits non-zero if any |delta|
    exceeds ``--max-delta`` (default $2.00). Use this as a pre-release
    regression gate against your own private fixture directory: nothing
    you point it at gets committed or persisted.
    """
    from taxlens.engine import compute
    from taxlens.importers import import_path
    from taxlens.rules import load_rules

    targets: list[Path] = []
    for p in paths:
        if p.is_dir():
            pattern = "**/*.pdf" if recursive else "*.pdf"
            targets.extend(sorted(p.glob(pattern)))
        else:
            targets.append(p)
    if not targets:
        console.print("[yellow]No PDFs found to reconcile.[/]")
        raise typer.Exit(code=0)

    table = Table(title=f"Reconciliation ({len(targets)} file(s))")
    for col in ("file", "year", "computed", "reported", "delta", "status"):
        table.add_column(col)

    threshold = Decimal(str(max_delta))
    worst = Decimal(0)
    failures = 0
    errors = 0
    for fp in targets:
        try:
            imp = import_path(fp)
            rules = load_rules(imp.ret.tax_year)
            res = compute(imp.ret, rules)
        except Exception as exc:
            table.add_row(fp.name, "?", "—", "—", "—", f"[red]error: {exc}[/]")
            errors += 1
            continue
        delta = res.reconciliation_delta
        if delta is None:
            table.add_row(
                fp.name, str(imp.ret.tax_year),
                f"${res.total_tax}", "—", "—", "[dim]no reported value[/]",
            )
            continue
        if abs(delta) > abs(worst):
            worst = delta
        if abs(delta) > threshold:
            failures += 1
            status = f"[red]FAIL Δ ${delta} > ${threshold}[/]"
        else:
            status = "[green]OK[/]"
        table.add_row(
            fp.name, str(imp.ret.tax_year),
            f"${res.total_tax}",
            f"${res.reported_total_tax}",
            f"${delta}",
            status,
        )

    console.print(table)
    console.print(
        f"[bold]Summary:[/] {len(targets)} file(s), "
        f"{failures} over-threshold, {errors} error(s), worst |Δ| = ${abs(worst)}"
    )
    if failures or errors:
        raise typer.Exit(code=1)


@app.command("delete")
def delete_cmd(return_id: int) -> None:
    """Delete a return by id."""
    service = TaxLensService.open()
    if service.delete_return(return_id):
        console.print(f"[green]Deleted return {return_id}[/]")
    else:
        console.print(f"[red]No return with id {return_id}[/]")
        raise typer.Exit(code=1)


@app.command("serve")
def serve_cmd(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    passphrase: Optional[str] = typer.Option(
        None, "--passphrase", "-p",
        help="If the DB is locked, decrypt it with this passphrase before serving.",
    ),
) -> None:
    """Run the local FastAPI server + web UI."""
    from taxlens import secure_db
    if secure_db.is_locked():
        if not passphrase:
            passphrase = typer.prompt("Passphrase", hide_input=True)
        try:
            secure_db.unlock(passphrase)
            console.print("[green]✓ Database unlocked.[/]")
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(code=1) from e

    url = f"http://{host}:{port}/"
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    console.print(f"[green]TaxLens UI:[/] {url}")
    uvicorn.run("taxlens.api:app", host=host, port=port, log_level="info")


@app.command("lock")
def lock_cmd(
    passphrase: Optional[str] = typer.Option(None, "--passphrase", "-p"),
) -> None:
    """Encrypt the local SQLite DB at rest."""
    from taxlens import secure_db
    if not passphrase:
        passphrase = typer.prompt("New passphrase", hide_input=True, confirmation_prompt=True)
    blob = secure_db.lock(passphrase)
    console.print(f"[green]✓ Locked[/] → {blob}")


@app.command("unlock")
def unlock_cmd(
    passphrase: Optional[str] = typer.Option(None, "--passphrase", "-p"),
) -> None:
    """Decrypt the local SQLite DB."""
    from taxlens import secure_db
    if not passphrase:
        passphrase = typer.prompt("Passphrase", hide_input=True)
    try:
        plain = secure_db.unlock(passphrase)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1) from e
    console.print(f"[green]✓ Unlocked[/] → {plain}")


if __name__ == "__main__":
    app()
