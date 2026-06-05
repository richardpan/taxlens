"""Named, lazily-evaluated PDF text-extraction backends.

Different IRS forms and vendor layouts respond differently to the available
PDF text extractors. Historically each importer (1040, W-2, AcroForm)
re-implemented its own "open the file, try extractor A, then B, then C until
one yields non-empty output" loop. That pattern grew the following sins:

  * The same PDF was opened multiple times (pdfplumber + pypdf both walk the
    file from disk independently).
  * The implicit fallback ordering was buried inside every parser, so adding
    a new extractor (e.g. pypdf to fix ADP-style W-2 layouts) required
    touching every call site.
  * Detection helpers received already-extracted text via positional args,
    losing the ability to consult additional sources without growing
    parameter lists.

`TextSources` consolidates those concerns. One instance per import wraps a
single PDF path and provides every available text view as a memoized
property. Importers declare the source ordering they care about via
``named_streams(...)``; the facade computes each source at most once and
returns a uniform ``[(name, pages), ...]`` list.

Available sources:

  * ``"pypdf"``           — pypdf's content-stream-order text. Cleanest on
                            vendor-rendered W-2s where pdfplumber's column
                            clustering corrupts the Box 12 column.
  * ``"default"``         — pdfplumber's ``page.extract_text()``. Default
                            text-stream walk, what we've used since v0.1.
  * ``"layout-tight"``    — pdfplumber ``extract_words()`` clustered by
                            y-coordinate at 3pt tolerance. Recovers
                            label/value pairs split across logical columns.
  * ``"layout-loose"``    — same as above at 8pt tolerance. Merges fillable-
                            form rows where the user-entered value sits a
                            few points above/below the label baseline.

Streams that don't exist for a given PDF (e.g. pypdf raises on a corrupt
content stream) yield empty page lists and are skipped by callers that test
for non-empty results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Iterable


# Order matters here only as a documentation cue. Callers pick the order
# they want via ``named_streams``.
KNOWN_SOURCE_NAMES = ("pypdf", "default", "layout-tight", "layout-loose")


@dataclass
class TextSources:
    """Lazy facade over the available PDF text-extraction backends.

    Properties are computed on first access and cached. Construct one
    instance per import; pass it to detection helpers and importers
    instead of pre-extracted page lists.
    """

    path: Path
    # When True, ``default_pages`` was produced via OCR fallback (no
    # text layer). Importers that gate on this can skip layout streams
    # entirely (OCR pages are already best-effort flat text).
    _ocr_used: bool = field(default=False, init=False)
    _pdfplumber_loaded: bool = field(default=False, init=False)
    _default_pages: list[str] = field(default_factory=list, init=False)
    _layout_tight_pages: list[str] = field(default_factory=list, init=False)
    _layout_loose_pages: list[str] = field(default_factory=list, init=False)

    # ── pdfplumber-derived sources ─────────────────────────────────────
    #
    # These three streams share a single pdfplumber file open and a single
    # ``extract_words()`` call per page (extract_words is the dominant
    # cost; clustering at two y-tolerances reuses its output). We compute
    # them eagerly on first access of any of the three, since separating
    # them would re-open the file.

    def _ensure_pdfplumber(self) -> None:
        if self._pdfplumber_loaded:
            return
        # Imported lazily to keep this module importable in environments
        # without pdfplumber (test fixtures that build TextSources from
        # synthetic data via ``from_pages``).
        from taxlens.importers.pdf._core import _extract_text_per_page

        default, layout_streams, ocr_used = _extract_text_per_page(self.path)
        self._default_pages = default
        # ``_extract_text_per_page`` returns layout streams in order
        # [tight, loose]; preserve that ordering here.
        if len(layout_streams) >= 1:
            self._layout_tight_pages = layout_streams[0]
        if len(layout_streams) >= 2:
            self._layout_loose_pages = layout_streams[1]
        self._ocr_used = ocr_used
        self._pdfplumber_loaded = True

    @property
    def default_pages(self) -> list[str]:
        self._ensure_pdfplumber()
        return self._default_pages

    @property
    def layout_tight_pages(self) -> list[str]:
        self._ensure_pdfplumber()
        return self._layout_tight_pages

    @property
    def layout_loose_pages(self) -> list[str]:
        self._ensure_pdfplumber()
        return self._layout_loose_pages

    @property
    def layout_streams(self) -> list[list[str]]:
        """Backwards-compat tuple returned by the old extractor:
        ``[layout_tight, layout_loose]``."""
        self._ensure_pdfplumber()
        return [self._layout_tight_pages, self._layout_loose_pages]

    @property
    def ocr_used(self) -> bool:
        self._ensure_pdfplumber()
        return self._ocr_used

    # ── pypdf-derived source ───────────────────────────────────────────
    #
    # pypdf walks the PDF's content stream in document order, which on
    # vendor-rendered W-2s produces cleaner per-copy text than pdfplumber's
    # column clustering. We use it as a fallback for any importer where
    # column-aware reconstruction matters less than per-line preservation.

    @cached_property
    def pypdf_pages(self) -> list[str]:
        try:
            from pypdf import PdfReader
        except Exception:
            return []
        try:
            reader = PdfReader(str(self.path))
            return [(p.extract_text() or "") for p in reader.pages]
        except Exception:
            return []

    # ── Stream lookup by name ─────────────────────────────────────────

    def _stream_by_name(self, name: str) -> list[str]:
        if name == "pypdf":
            return self.pypdf_pages
        if name == "default":
            return self.default_pages
        if name == "layout-tight":
            return self.layout_tight_pages
        if name == "layout-loose":
            return self.layout_loose_pages
        raise ValueError(
            f"Unknown text source {name!r}. Known sources: "
            + ", ".join(KNOWN_SOURCE_NAMES)
        )

    def named_streams(
        self, order: Iterable[str]
    ) -> list[tuple[str, list[str]]]:
        """Return ``[(name, pages), ...]`` for each requested source, in
        the requested order. Unknown names raise; empty streams are kept
        in the output (callers that want to skip them can filter on
        ``pages`` themselves)."""
        return [(name, self._stream_by_name(name)) for name in order]

    def all_pages_joined(self, order: Iterable[str]) -> str:
        """Convenience: concatenate every page from every requested
        source into a single newline-separated string. Useful for
        document-wide marker checks (e.g. "is this a W-2?")."""
        chunks: list[str] = []
        for _, pages in self.named_streams(order):
            chunks.extend(pages)
        return "\n".join(chunks)

    # ── Test/synthetic-fixture constructor ────────────────────────────

    @classmethod
    def from_pages(
        cls,
        default_pages: list[str],
        *,
        layout_tight_pages: list[str] | None = None,
        layout_loose_pages: list[str] | None = None,
        pypdf_pages: list[str] | None = None,
        path: Path | None = None,
    ) -> "TextSources":
        """Build a fully-populated ``TextSources`` from already-extracted
        page lists. Used by tests and by callers that want to seed the
        facade with synthetic data without opening a real PDF."""
        inst = cls(path=path or Path("/synthetic"))
        inst._default_pages = list(default_pages)
        inst._layout_tight_pages = list(
            layout_tight_pages if layout_tight_pages is not None else default_pages
        )
        inst._layout_loose_pages = list(
            layout_loose_pages if layout_loose_pages is not None else default_pages
        )
        inst._pdfplumber_loaded = True
        if pypdf_pages is not None:
            # Seed the cached_property by setting it on the instance dict.
            inst.__dict__["pypdf_pages"] = list(pypdf_pages)
        return inst
