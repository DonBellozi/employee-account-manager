from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_compatibility_schema() -> None:
    """Добавляет совместимые поля в существующую локальную БД без ее сброса."""
    inspector = inspect(engine)
    if "hr_source_records" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("hr_source_records")}
    if "personal_email" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE hr_source_records "
                "ADD COLUMN personal_email VARCHAR(320) NOT NULL DEFAULT ''"
            )
        )
