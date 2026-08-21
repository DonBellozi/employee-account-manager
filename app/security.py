from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models import DomainAccessUser, LocalUser, UserRole

password_hash = PasswordHash.recommended()


class CSRFMismatchError(Exception):
    """Сессионный CSRF-токен отсутствует или не совпадает с токеном формы."""


@dataclass(frozen=True)
class CurrentUser:
    username: str
    role: str
    source: str


def normalize_username(username: str) -> str:
    """Привести DOMAIN\\user, user@domain и user к одному sAMAccountName."""

    normalized = username.strip()
    if "\\" in normalized:
        normalized = normalized.rsplit("\\", 1)[-1]
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized.strip().lower()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return password_hash.verify(password, hashed)
    except Exception:
        return False


def ensure_bootstrap_admin(db: Session, settings: Settings) -> None:
    bootstrap_username = normalize_username(settings.bootstrap_admin_username)
    existing = db.scalar(select(LocalUser).where(LocalUser.username == bootstrap_username))
    if existing:
        changed = False
        if existing.role != UserRole.ADMIN:
            existing.role = UserRole.ADMIN
            changed = True
        if not existing.is_active:
            existing.is_active = True
            changed = True
        if changed:
            db.commit()

        if (
            not settings.dry_run
            and settings.auth_mode in {"local", "hybrid"}
            and verify_password("ChangeMeNow!123", existing.password_hash)
        ):
            raise RuntimeError("Измените стандартный пароль локального администратора перед рабочим запуском")
        return

    if not settings.dry_run and settings.bootstrap_admin_password == "ChangeMeNow!123":
        raise RuntimeError("Замените стандартный BOOTSTRAP_ADMIN_PASSWORD перед рабочим запуском")
    if len(settings.bootstrap_admin_password) < 12:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD должен быть не короче 12 символов")

    db.add(
        LocalUser(
            username=bootstrap_username,
            password_hash=hash_password(settings.bootstrap_admin_password),
            role=UserRole.ADMIN,
            is_active=True,
        )
    )
    db.commit()


def authenticate_local(db: Session, username: str, password: str) -> CurrentUser | None:
    normalized = normalize_username(username)
    user = db.scalar(
        select(LocalUser).where(
            LocalUser.username == normalized,
            LocalUser.is_active.is_(True),
        )
    )
    if not user or not verify_password(password, user.password_hash):
        return None
    return CurrentUser(username=user.username, role=user.role.value, source="local")


def get_domain_access(db: Session, username: str) -> DomainAccessUser | None:
    normalized = normalize_username(username)
    return db.scalar(
        select(DomainAccessUser).where(
            DomainAccessUser.username == normalized,
            DomainAccessUser.is_active.is_(True),
        )
    )


def get_current_user(request: Request) -> CurrentUser:
    data: dict[str, Any] | None = request.session.get("user")
    if not data:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    username = normalize_username(str(data["username"]))
    source = str(data.get("source", "local"))

    # Права и состояние проверяются при каждом запросе. Поэтому отключение
    # доступа или смена роли в админке не требуют повторного входа.
    with SessionLocal() as db:
        if source == "ad":
            account = db.scalar(
                select(DomainAccessUser).where(
                    DomainAccessUser.username == username,
                    DomainAccessUser.is_active.is_(True),
                )
            )
        else:
            account = db.scalar(
                select(LocalUser).where(
                    LocalUser.username == username,
                    LocalUser.is_active.is_(True),
                )
            )

        if account is None:
            request.session.clear()
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            )

        role = account.role.value

    if data.get("role") != role or data.get("username") != username:
        request.session["user"] = {
            "username": username,
            "role": role,
            "source": source,
        }

    return CurrentUser(username=username, role=role, source=source)


def require_admin(request: Request) -> CurrentUser:
    user = get_current_user(request)
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Раздел доступен только администраторам",
        )
    return user


def require_operator(request: Request) -> CurrentUser:
    """Разрешить действие администратору или штатному оператору."""

    user = get_current_user(request)
    if user.role not in {UserRole.ADMIN.value, UserRole.OPERATOR.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Раздел доступен администраторам и операторам",
        )
    return user


def get_or_create_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


def validate_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("csrf")
    if not expected or not secrets.compare_digest(str(expected), submitted):
        raise CSRFMismatchError
