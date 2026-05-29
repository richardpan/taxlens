"""Import pipeline. Each importer turns an input file into a `Return` plus
provenance metadata, and never persists anything itself."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from taxlens.models import Return


@dataclass(frozen=True)
class Imported:
    """The output of any importer."""
    ret: Return
    source: str           # pdf | txf | manual
    source_hash: str      # sha256 of the source bytes
    source_filename: str | None
    warnings: list[str]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def import_path(path: Path) -> Imported:
    """Dispatch by file extension."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from taxlens.importers.pdf import import_pdf
        return import_pdf(path)
    if suffix == ".txf":
        from taxlens.importers.txf import import_txf
        return import_txf(path)
    if suffix in (".yaml", ".yml", ".json"):
        from taxlens.importers.manual import import_manual
        return import_manual(path)
    raise ValueError(f"unsupported file type: {suffix} ({path})")
