from sqlalchemy import inspect, text
from sqlmodel import create_engine

from video_uploader.db import create_db_and_tables


def test_adds_columns_missing_from_an_older_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    with engine.begin() as conn:
        # platformjob as it was before cover_time_ms existed (trimmed).
        conn.execute(text("CREATE TABLE platformjob (id INTEGER PRIMARY KEY, platform VARCHAR)"))
        conn.execute(text("INSERT INTO platformjob (id, platform) VALUES (1, 'youtube')"))

    create_db_and_tables(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("platformjob")}
    assert "cover_time_ms" in columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT platform, cover_time_ms FROM platformjob")).all() == [
            ("youtube", None)
        ]


def test_running_twice_is_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'new.sqlite3'}")
    create_db_and_tables(engine)
    create_db_and_tables(engine)
    assert "cover_time_ms" in {c["name"] for c in inspect(engine).get_columns("platformjob")}
