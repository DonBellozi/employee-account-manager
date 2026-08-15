from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.security import get_current_user, get_or_create_csrf
from app.time_utils import register_datetime_filters

router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


@router.get("/onec")
def onec_overview(request: Request):
    current = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "onec.html",
        {
            "user": current,
            "csrf": get_or_create_csrf(request),
        },
    )
