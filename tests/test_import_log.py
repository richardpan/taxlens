"""Tests for the per-import diagnostic log writer.

When a user reports "field X wasn't imported", we need to see what the
extractor saw. The log captures every AcroForm field (mapped or not),
every text-pattern hit, and the final extracted dict. These tests verify
the logger contract without needing reportlab/real PDFs — the logger is
driven directly with synthetic data, mirroring how the importer calls it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from taxlens.importers.import_log import ImportLogger, logging_enabled, logs_dir


@pytest.fixture
def tmp_logs(tmp_path, monkeypatch):
    """Redirect log writes to a tmp dir + ensure logging is on."""
    monkeypatch.setenv("TAXLENS_LOGS_DIR", str(tmp_path))
    monkeypatch.delenv("TAXLENS_IMPORT_LOG", raising=False)
    return tmp_path


def test_logging_enabled_default_on():
    """If TAXLENS_IMPORT_LOG is unset, logging is on."""
    os.environ.pop("TAXLENS_IMPORT_LOG", None)
    assert logging_enabled() is True


def test_logging_disabled_when_env_zero(monkeypatch):
    monkeypatch.setenv("TAXLENS_IMPORT_LOG", "0")
    assert logging_enabled() is False


def test_logs_dir_respects_override_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXLENS_LOGS_DIR", str(tmp_path / "logs"))
    d = logs_dir()
    assert d == tmp_path / "logs"
    assert d.is_dir()


def test_logger_records_acroform_mapped_field(tmp_logs):
    logger = ImportLogger(source_path=Path("/fake/return.pdf"))
    logger.section("AcroForm pass")
    logger.acroform_field(
        name="topmostSubform[0].Page1[0].f1_32[0]",
        tooltip="Total amount from Form(s) W-2, box 1",
        raw_value="120000",
        parsed_value=120000,
        target="wages",
        status="MAPPED",
    )
    out = logger.render()
    assert "MAPPED" in out
    assert "wages" in out
    assert "120000" in out
    assert "Total amount from Form(s) W-2" in out


def test_logger_records_unmapped_field(tmp_logs):
    logger = ImportLogger(source_path=Path("/fake/return.pdf"))
    logger.acroform_field(
        name="f1_99", tooltip="Something not yet recognized",
        raw_value="5000", parsed_value=5000, target=None, status="UNMAPPED",
    )
    out = logger.render()
    assert "UNMAPPED" in out
    assert "Something not yet recognized" in out
    assert "5000" in out


def test_logger_conflict_resolution_shows_winner(tmp_logs):
    logger = ImportLogger(source_path=Path("/fake/return.pdf"))
    logger.conflict_resolution(
        "wages", 120000,
        [("f1_per_w2", 60000), ("f1_total", 120000)],
    )
    out = logger.render()
    assert "conflict on wages" in out
    assert "120000" in out
    # Winner marked with star
    assert "★" in out


def test_logger_final_fields_sorted(tmp_logs):
    logger = ImportLogger(source_path=Path("/fake/return.pdf"))
    logger.final_fields({"wages": 120000, "interest_income": 450, "agi": 119550})
    out = logger.render()
    # All three present
    assert "wages" in out
    assert "interest_income" in out
    assert "agi" in out
    # And sorted (agi before interest before wages)
    assert out.index("agi") < out.index("interest_income") < out.index("wages")


def test_logger_write_creates_file_with_expected_name(tmp_logs):
    logger = ImportLogger(source_path=Path("/some/dir/My_2023_Return.pdf"))
    logger.info("test entry")
    path = logger.write()
    assert path.exists()
    assert path.parent == tmp_logs
    assert path.name.startswith("import-")
    assert path.name.endswith(".log")
    assert "My_2023_Return" in path.name
    content = path.read_text(encoding="utf-8")
    assert "test entry" in content
    assert "My_2023_Return.pdf" in content


def test_logger_sanitizes_unsafe_filename_chars(tmp_logs):
    """Slashes, spaces, and Unicode in the source filename must not
    escape the logs directory or create weird filenames."""
    logger = ImportLogger(source_path=Path("/tmp/Return 2023 (final)/../weird name.pdf"))
    path = logger.write()
    # Resulting log filename should contain no path separators, parens,
    # or spaces from the source stem.
    assert "/" not in path.name
    assert "\\" not in path.name
    assert "(" not in path.name
    assert ")" not in path.name
    assert " " not in path.name
    assert path.parent == tmp_logs


def test_acroform_extract_emits_log_entries_when_logger_provided(tmp_logs, monkeypatch):
    """End-to-end: drive extract_acroform_fields with a fake PdfReader and
    assert the logger captured every field. No reportlab needed — we
    monkey-patch pypdf.PdfReader to return canned field dicts."""
    from taxlens.importers import acroform

    class FakeField(dict):
        pass

    class FakeReader:
        def __init__(self, _path):
            pass

        def get_fields(self):
            return {
                "f_wages": FakeField({
                    "/T": "topmostSubform.f1_32",
                    "/TU": "Total amount from Form(s) W-2, box 1",
                    "/V": "120000",
                }),
                "f_mystery": FakeField({
                    "/T": "f1_99",
                    "/TU": "Some line we don't know yet",
                    "/V": "5000",
                }),
                "f_zero": FakeField({
                    "/T": "f1_40",
                    "/TU": "Taxable interest",
                    "/V": "0",
                }),
            }

    class FakePypdf:
        PdfReader = FakeReader

    monkeypatch.setitem(
        __import__("sys").modules, "pypdf", FakePypdf,
    )

    logger = ImportLogger(source_path=Path("/fake/return.pdf"))
    fields, warnings = acroform.extract_acroform_fields(
        Path("/fake/return.pdf"), logger=logger,
    )
    assert fields == {"wages": 120000}  # mystery unmapped, zero skipped

    rendered = logger.render()
    assert "MAPPED" in rendered
    assert "UNMAPPED" in rendered
    assert "ZERO_SKIPPED" in rendered
    # And the unmapped field's tooltip is logged so we can pattern-match later
    assert "Some line we don't know yet" in rendered
