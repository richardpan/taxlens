"""Per-import diagnostic log writer.

When a user reports "field X didn't get imported", we need to see what
the extractor *saw* on the PDF — which AcroForm fields exist, what
their tooltips say, which text-extraction passes ran, and which
patterns matched (or didn't). Re-running with a stack trace tells us
nothing because the importer's job is to silently coerce arbitrary
PDFs into a known schema; gaps look like missing data, not errors.

This module writes a plain-text log next to TaxLens's database on every
import, capturing the full decision trail. Default location:
``~/.taxlens/logs/import-<YYYYMMDD-HHMMSS>-<filename>.log``. Override
with ``TAXLENS_LOGS_DIR``; disable entirely with
``TAXLENS_IMPORT_LOG=0``.

The log path is surfaced as a warning on the resulting ``Imported``
object so the dashboard can link to it and so issue-report copy-paste
includes the location.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def logging_enabled() -> bool:
    """Off when ``TAXLENS_IMPORT_LOG=0``; on otherwise."""
    return os.environ.get("TAXLENS_IMPORT_LOG", "1") != "0"


def logs_dir() -> Path:
    """Resolve log directory; create on demand."""
    override = os.environ.get("TAXLENS_LOGS_DIR")
    if override:
        p = Path(override)
    else:
        # Sibling of the SQLite DB so logs travel with the data store.
        from taxlens.db import default_db_path
        p = default_db_path().parent / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class ImportLogger:
    """Buffers human-readable log lines for a single PDF import.

    Threaded through the extractor entry points as an optional kwarg so
    test code (and callers that don't want a log file) can pass ``None``
    and the importer skips all log work.
    """

    source_path: Path
    started_at: datetime = field(default_factory=datetime.now)
    _lines: list[str] = field(default_factory=list)

    # ── structured writers ──────────────────────────────────────────────

    def section(self, title: str) -> None:
        self._lines.append("")
        self._lines.append(f"=== {title} ===")

    def info(self, msg: str) -> None:
        self._lines.append(msg)

    def kv(self, key: str, value: Any) -> None:
        self._lines.append(f"  {key}: {value}")

    def acroform_field(
        self,
        *,
        name: str,
        tooltip: str,
        raw_value: Any,
        parsed_value: Any,
        target: str | None,
        status: str,
    ) -> None:
        """Log one AcroForm widget.

        status: MAPPED / UNMAPPED / ZERO_SKIPPED / NO_VALUE
        """
        n = (name[:48] + "…") if len(name) > 48 else name
        t = (tooltip[:64] + "…") if len(tooltip) > 64 else tooltip
        self._lines.append(
            f"  [{status:13s}] name={n!r}\n"
            f"                   tooltip={t!r}\n"
            f"                   raw={raw_value!r}  parsed={parsed_value}  target={target}"
        )

    def conflict_resolution(
        self, target: str, picked_value: Any, candidates: list[tuple[str, Any]]
    ) -> None:
        self._lines.append(f"  conflict on {target} → picked {picked_value}")
        for n, v in candidates:
            marker = "★" if v == picked_value else " "
            self._lines.append(f"      {marker} {n} = {v}")

    def text_match(
        self, *, target: str, value: Any, pattern: str, source: str,
    ) -> None:
        """One successful text-pattern match in a text-extraction pass."""
        self._lines.append(
            f"  MATCHED {target:32s} = {value}  (source={source}, pattern={pattern!r})"
        )

    def final_fields(self, fields: dict[str, Any]) -> None:
        self.section("Final extracted fields")
        if not fields:
            self._lines.append("  (none)")
            return
        for k in sorted(fields.keys()):
            self._lines.append(f"  {k:32s} = {fields[k]}")

    def warnings(self, warnings: list[str]) -> None:
        self.section("Warnings")
        if not warnings:
            self._lines.append("  (none)")
            return
        for w in warnings:
            self._lines.append(f"  - {w}")

    # ── output ──────────────────────────────────────────────────────────

    def render(self) -> str:
        header = [
            "TaxLens import log",
            f"  started_at: {self.started_at.isoformat(timespec='seconds')}",
            f"  source:     {self.source_path}",
        ]
        try:
            size = self.source_path.stat().st_size
            header.append(f"  size:       {size:,} bytes")
        except OSError:
            pass
        return "\n".join(header + self._lines) + "\n"

    def write(self) -> Path:
        ts = self.started_at.strftime("%Y%m%d-%H%M%S")
        # Sanitize stem: keep alnum/_/-, replace others with _, cap at 40 chars.
        raw = self.source_path.stem or "import"
        stem = "".join(c if (c.isalnum() or c in "_-") else "_" for c in raw)[:40]
        path = logs_dir() / f"import-{ts}-{stem}.log"
        path.write_text(self.render(), encoding="utf-8")
        return path
