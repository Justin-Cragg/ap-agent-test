"""Programmatic Alembic: `ap-agent migrate` applies migrations shipped in the
package. On PostgreSQL an advisory lock stops concurrent replicas/jobs from
migrating at the same time."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    engine = create_engine(url)
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(727275)"))
            try:
                cfg = alembic_config(url)
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, revision)
                conn.commit()
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(727275)"))
    else:
        command.upgrade(alembic_config(url), revision)
