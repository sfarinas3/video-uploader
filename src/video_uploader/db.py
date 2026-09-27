from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

_engine = None


def get_engine(db_path: Path | str = "data/video_uploader.sqlite3"):
    global _engine
    if _engine is None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return _engine


def create_db_and_tables(engine=None) -> None:
    SQLModel.metadata.create_all(engine or get_engine())


def get_session(engine=None) -> Session:
    return Session(engine or get_engine())
