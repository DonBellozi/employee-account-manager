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
    tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        if "hr_source_records" in tables:
            columns = {
                column["name"]
                for column in inspector.get_columns("hr_source_records")
            }
            if "personal_email" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE hr_source_records "
                        "ADD COLUMN personal_email VARCHAR(320) NOT NULL DEFAULT ''"
                    )
                )

        if "onec_additional_sources" in tables:
            columns = {
                column["name"]
                for column in inspector.get_columns("onec_additional_sources")
            }
            folder_added = "imap_folder" not in columns
            if folder_added:
                connection.execute(
                    text(
                        "ALTER TABLE onec_additional_sources "
                        "ADD COLUMN imap_folder VARCHAR(512) NOT NULL DEFAULT 'INBOX'"
                    )
                )
            if "is_primary" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE onec_additional_sources "
                        "ADD COLUMN is_primary BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if folder_added:
                connection.execute(
                    text(
                        "UPDATE onec_additional_sources "
                        "SET imap_folder = :folder"
                    ),
                    {"folder": settings.onec_imap_folder.strip() or "INBOX"},
                )
