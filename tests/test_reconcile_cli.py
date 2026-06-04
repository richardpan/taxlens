"""Unit tests for the `taxlens reconcile` CLI subcommand.

The CLI takes one or more PDFs (or a directory) and reports per-file
deltas between engine-computed total_tax and the reported total_tax
extracted from the PDF. Exits non-zero if any |delta| exceeds
``--max-delta`` (default $2). No DB writes; no PDFs are committed —
fixtures are generated in tmp_path.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from taxlens.cli import app
from tests.synthetic_pdf import make_1040_pdf

runner = CliRunner()


def _make_clean_return(path: Path, *, tax_year: int = 2024) -> None:
    """Synthesize a 1040 whose engine output should match the reported
    total_tax embedded in the PDF — the resulting delta is rounding-only."""
    # Single filer, $100k wages, std ded $14,600, taxable $85,400,
    # 2024 single tax (IRS Tax Tables) = $13,847. Reported tax matches
    # engine output → delta = $0.
    make_1040_pdf(
        path,
        tax_year=tax_year,
        filing_status_label="Single",
        wages=Decimal("100000"),
        total_tax_reported=Decimal("13847"),
    )


def test_reconcile_passes_when_under_threshold(tmp_path: Path) -> None:
    pdf = tmp_path / "clean_2024.pdf"
    _make_clean_return(pdf)
    result = runner.invoke(app, ["reconcile", str(pdf), "--max-delta", "5.00"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "1 file(s)" in result.output


def test_reconcile_fails_when_delta_exceeds_threshold(tmp_path: Path) -> None:
    # Reported tax wildly off from what engine will compute → delta
    # well over the default $2 threshold → exit 1.
    pdf = tmp_path / "wrong_2024.pdf"
    make_1040_pdf(
        pdf,
        tax_year=2024,
        filing_status_label="Single",
        wages=Decimal("100000"),
        total_tax_reported=Decimal("0"),
    )
    result = runner.invoke(app, ["reconcile", str(pdf)])
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output


def test_reconcile_directory_input(tmp_path: Path) -> None:
    _make_clean_return(tmp_path / "a.pdf", tax_year=2024)
    _make_clean_return(tmp_path / "b.pdf", tax_year=2024)
    # A non-PDF file should be ignored.
    (tmp_path / "notes.txt").write_text("not a pdf")
    result = runner.invoke(app, ["reconcile", str(tmp_path), "--max-delta", "5.00"])
    assert result.exit_code == 0, result.output
    assert "2 file(s)" in result.output


def test_reconcile_no_pdfs_found(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a pdf")
    result = runner.invoke(app, ["reconcile", str(tmp_path)])
    assert result.exit_code == 0
    assert "No PDFs found" in result.output


def test_reconcile_recursive(tmp_path: Path) -> None:
    sub = tmp_path / "year2024"
    sub.mkdir()
    _make_clean_return(sub / "deep.pdf", tax_year=2024)
    # Without --recursive, no PDFs at top level → none found.
    res_flat = runner.invoke(app, ["reconcile", str(tmp_path), "--max-delta", "5.00"])
    assert "No PDFs found" in res_flat.output
    # With --recursive, the nested PDF is reconciled.
    res_rec = runner.invoke(
        app, ["reconcile", str(tmp_path), "--recursive", "--max-delta", "5.00"],
    )
    assert res_rec.exit_code == 0, res_rec.output
    assert "1 file(s)" in res_rec.output
