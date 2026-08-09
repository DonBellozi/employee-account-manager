from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_current_user, get_or_create_csrf
from app.services.hr_registry_multisource import (
    MultiSourceHRRegistryViewService,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(
    Jinja2Templates(directory="app/templates")
)


@router.get("/employees/registry")
def employee_registry_multisource(
    request: Request,
    q: str = "",
    status: str = "all",
    source: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    user = get_current_user(request)
    if status not in {
        "all",
        "issues",
        "ok",
        "checked",
        "not_checked",
    }:
        status = "all"

    service = MultiSourceHRRegistryViewService(settings, db)
    current_query = request.url.query
    return_to = request.url.path
    if current_query:
        return_to += f"?{current_query}"

    source_options = service.source_options()
    valid_sources = {item["id"] for item in source_options}
    selected_source = (
        source.strip().lower()
        if source.strip().lower() in valid_sources
        else ""
    )

    return templates.TemplateResponse(
        request,
        "hr_registry.html",
        {
            "user": user,
            "csrf": get_or_create_csrf(request),
            "rows": service.list_rows(
                query=q,
                status=status,
                source_id=selected_source,
                limit=1000,
            ),
            "summary": service.summary(
                source_id=selected_source,
            ),
            "query": q,
            "selected_status": status,
            "source_options": source_options,
            "selected_source": selected_source,
            "return_to": return_to,
        },
    )
