from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

_engine = None


def get_engine(db_path: Path | str = "data/video_uploader.sqlite3"):
    global _engine
    if _engine is None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return _engine


# create_all() creates missing tables but never alters existing ones, so
# columns added to a model after a user's database was created are added
# here. Only nullable columns belong in this list -- SQLite can't add a
# NOT NULL column without a default.
_ADDED_COLUMNS = [
    ("platformjob", "cover_time_ms", "INTEGER"),
]


def create_db_and_tables(engine=None) -> None:
    engine = engine or get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine) -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def get_session(engine=None) -> Session:
    return Session(engine or get_engine())
