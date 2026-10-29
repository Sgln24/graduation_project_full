from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy import engine_from_config

from database import Base, database_url, get_engine
import lms_models  # noqa: F401


config = context.config
target_metadata = Base.metadata
MANAGED_TABLES = {"users", "sessions", "lectureseek_jobs"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in MANAGED_TABLES
    table = getattr(obj, "table", None)
    if table is not None and table.name not in MANAGED_TABLES:
        return False
    return True


def run_migrations_offline():
    context.configure(
        url=database_url(config.attributes.get("db_path")),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = get_engine(config.attributes.get("db_path"))
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
