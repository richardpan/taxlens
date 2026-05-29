"""Typer-based CLI. Entry point: `taxlens` after `pip install -e .`."""
from __future__ import annotations

import json
import webbrowser
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
    path: Path = typer.Argument(..., exists=True, readable=True),
) -> None:
    """Import a tax return PDF, TXF, JSON, or YAML."""
    service = TaxLensService.open()
    row, result, warnings = service.import_file(path)
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
            raise typer.Exit(code=1)

    url = f"http://{host}:{port}/"
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
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
        raise typer.Exit(code=1)
    console.print(f"[green]✓ Unlocked[/] → {plain}")


if __name__ == "__main__":
    app()
