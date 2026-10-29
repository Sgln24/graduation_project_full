import os
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


_ENGINES = {}


def database_url(db_path=None):
    configured = os.getenv("LECTURESEEK_DATABASE_URL", "").strip()
    if configured:
        return configured
    path = Path(db_path or os.getenv(
        "SLIDEDEC_LMS_DB",
        str(Path(__file__).resolve().parent / "slidedec_lms.sqlite3"),
    )).resolve()
    return URL.create("sqlite", database=str(path)).render_as_string(hide_password=False)


def get_engine(db_path=None):
    url = database_url(db_path)
    if url not in _ENGINES:
        engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        if url.startswith("sqlite"):
            @event.listens_for(engine, "connect")
            def _enable_sqlite_foreign_keys(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        _ENGINES[url] = engine
    return _ENGINES[url]


def dispose_database(db_path=None):
    engine = _ENGINES.pop(database_url(db_path), None)
    if engine is not None:
        engine.dispose()


@contextmanager
def session_scope(db_path=None):
    session = Session(get_engine(db_path), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upgrade_database(db_path=None):
    root = Path(__file__).resolve().parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes["db_path"] = str(db_path) if db_path else None
    command.upgrade(config, "head")
