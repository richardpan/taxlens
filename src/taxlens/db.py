"""SQLite persistence via SQLAlchemy 2.0.

Schema is deliberately small. The canonical representation of a return is the
pydantic `Return` model serialized as JSON in `return_json`; queryable columns
exist for listings and joins only. Computed `TaxResult`s are cached in
`ComputationCache` so the UI can hydrate without re-running the engine.

Storage location:
  * `TAXLENS_DB` env var, OR
  * `~/.taxlens/taxlens.sqlite` by default
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def default_db_path() -> Path:
    override = os.environ.get("TAXLENS_DB")
    if override:
        return Path(override)
    base = Path.home() / ".taxlens"
    base.mkdir(parents=True, exist_ok=True)
    return base / "taxlens.sqlite"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"unsupported json type: {type(obj).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, separators=(",", ":"))


class Base(DeclarativeBase):
    pass


class StoredReturn(Base):
    __tablename__ = "returns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tax_year: Mapped[int] = mapped_column(Integer, index=True)
    filing_status: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16))   # pdf | txf | manual
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    return_json: Mapped[str] = mapped_column(Text)

    cache: Mapped["ComputationCache | None"] = relationship(
        back_populates="ret", uselist=False, cascade="all, delete-orphan"
    )
    overrides: Mapped[list["Override"]] = relationship(
        back_populates="ret", cascade="all, delete-orphan"
    )


class ComputationCache(Base):
    __tablename__ = "computation_cache"

    return_id: Mapped[int] = mapped_column(ForeignKey("returns.id", ondelete="CASCADE"), primary_key=True)
    result_json: Mapped[str] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    ret: Mapped[StoredReturn] = relationship(back_populates="cache")


class Override(Base):
    __tablename__ = "overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("returns.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(64))
    previous_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    ret: Mapped[StoredReturn] = relationship(back_populates="overrides")


# Engine + session factory ────────────────────────────────────────────────────

def make_engine(db_path: Path | None = None):
    path = db_path or default_db_path()
    url = f"sqlite:///{path}"
    eng = create_engine(url, future=True)
    Base.metadata.create_all(eng)
    return eng


def make_sessionmaker(db_path: Path | None = None) -> sessionmaker[Session]:
    return sessionmaker(make_engine(db_path), expire_on_commit=False, future=True)


def session_scope(sm: sessionmaker[Session]) -> Iterator[Session]:
    """Context-manager helper: `with session_scope(SM) as s: ...`"""
    s = sm()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
