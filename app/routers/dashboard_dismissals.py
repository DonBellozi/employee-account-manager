from __future__ import annotations

import json
from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import (
    ADProvisioningOperation,
    AuditLog,
    BlockingOperation,
    BlockingQueueItem,
    DismissalSchedule,
    ProvisioningOperation,
)
from app.models_notifications import DismissalEquipmentNotice
from app.models_dismissal_lifecycle import (
    ADReactivationAlert,
    FinalDismissalBlockRun,
    FinalDismissalBlockTarget,
)
from app.routers.employees import (
    _ad_provisioning_journal_item,
    _blocking_journal_item,
    _dismissal_journal_item,
    _provisioning_journal_item,
)
from app.security import get_current_user, get_or_create_csrf, validate_csrf
from app.services.upcoming_dismissals import (
    DEFERRAL_ACTION,
    UpcomingDismissalService,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(
    Jinja2Templates(directory="app/templates")
)


def _context(request: Request, **kwargs):
    user = get_current_user(request)
    return {
        "user": user,
        "csrf": get_or_create_csrf(request),
        **kwargs,
    }


def _deferral_journal_item(event: AuditLog) -> dict[str, object]:
    try:
        payload = json.loads(event.details or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    dismissal_date = str(payload.get("dismissal_date") or "")
    deferred_until = str(payload.get("deferred_until") or "")
    previous_until = str(payload.get("previous_deferred_until") or "")

    def display_date(value: str) -> str:
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            return value

    organization_names = [
        str(item.get("source_name") or item.get("source_id") or "").strip()
        for item in payload.get("organizations") or []
        if isinstance(item, dict)
    ]

    details = [
        ("ФИО", str(payload.get("fio") or "")),
        ("Логин AD", str(payload.get("login") or "")),
        ("Корпоративная почта", str(payload.get("corporate_email") or "")),
        ("Дата окончательного увольнения", display_date(dismissal_date)),
        ("Блокировка отложена до", display_date(deferred_until)),
        ("Организации", ", ".join(name for name in organization_names if name)),
    ]
    if previous_until:
        details.insert(
            5,
            ("Предыдущая отсрочка", display_date(previous_until)),
        )

    return {
        "kind": "dismissal",
        "record_id": event.id,
        "created_at": event.created_at,
        "action": "Отсрочка блокировки",
        "subject": str(payload.get("fio") or "Работник"),
        "login": str(payload.get("login") or ""),
        "corporate_email": str(payload.get("corporate_email") or ""),
        "personal_email": "",
        "mail_domain": "",
        "operator": event.actor,
        "status_key": "success",
        "status_label": "Отложено на 7 дней",
        "details": details,
        "error_message": "",
        "completed_at": None,
    }


def _dismissal_notice_journal_item(
    notice: DismissalEquipmentNotice,
) -> dict[str, object]:
    try:
        recipients = json.loads(notice.recipients_json or "[]")
    except (TypeError, json.JSONDecodeError):
        recipients = []
    if not isinstance(recipients, list):
        recipients = []

    sent = [
        str(item.get("email") or "").strip()
        for item in recipients
        if isinstance(item, dict) and item.get("sent")
    ]
    pending = [
        str(item.get("email") or "").strip()
        for item in recipients
        if isinstance(item, dict) and not item.get("sent")
    ]
    corporate_sent = next(
        (
            str(item.get("email") or "").strip()
            for item in recipients
            if isinstance(item, dict)
            and item.get("sent")
            and item.get("kind") == "corporate"
        ),
        "",
    )

    labels = {
        "pending": ("running", "Ожидает отправки"),
        "partial": ("partial", "Отправлено частично"),
        "failed": ("failed", "Ошибка отправки"),
        "sent": ("success", "Отправлено"),
        "cancelled": ("partial", "Отменено"),
    }
    status_key, status_label = labels.get(
        notice.status,
        ("running", notice.status),
    )

    details = [
        ("ФИО", notice.fio),
        (
            "Дата окончательного увольнения",
            notice.dismissal_date.strftime("%d.%m.%Y"),
        ),
        ("Домен отправителя", notice.sender_domain),
        ("Отправлено на", ", ".join(value for value in sent if value)),
    ]
    if pending:
        details.append(
            ("Еще не отправлено", ", ".join(value for value in pending if value))
        )
    details.append(("Попыток", str(int(notice.attempts or 0))))

    return {
        "kind": "dismissal",
        "record_id": notice.id,
        "created_at": notice.created_at,
        "action": "Уведомление о возврате оборудования",
        "subject": notice.fio or "Работник",
        "login": "",
        "corporate_email": corporate_sent,
        "personal_email": "",
        "mail_domain": notice.sender_domain,
        "operator": "Система",
        "status_key": status_key,
        "status_label": status_label,
        "details": details,
        "error_message": notice.last_error,
        "completed_at": notice.sent_at or notice.cancelled_at,
    }



def _final_dismissal_block_journal_item(
    run: FinalDismissalBlockRun,
    targets: list[FinalDismissalBlockTarget],
) -> dict[str, object]:
    labels = {
        "pending": ("running", "Ожидает"),
        "running": ("running", "Выполняется"),
        "partial": ("partial", "Выполнено частично"),
        "success": ("success", "Выполнено"),
        "intervention": ("failed", "Требует вмешательства"),
        "cancelled": ("partial", "Отменено"),
    }
    status_key, status_label = labels.get(
        run.status,
        ("running", run.status),
    )

    target_labels = {
        "pending": "Ожидает блокировки",
        "completed": "Заблокирована системой",
        "already_completed": "Уже была заблокирована",
        "intervention": "Требует вмешательства",
        "cancelled": "Отменено",
    }

    blocking_systems = []
    for target in targets:
        label = (
            "Active Directory"
            if target.system == "ad"
            else "Zimbra"
        )
        blocking_systems.append(
            {
                "label": label,
                "result": target_labels.get(
                    target.status,
                    target.status,
                ),
                "timestamp": (
                    target.completed_at
                    or target.last_attempt_at
                ),
            }
        )

    ad_identifier = next(
        (
            item.target_identifier
            for item in targets
            if item.system == "ad"
            and item.target_identifier
        ),
        "",
    )
    details = [
        ("ФИО", run.fio),
        (
            "Дата окончательного увольнения",
            run.dismissal_date.strftime("%d.%m.%Y"),
        ),
        (
            "Дата автоматической блокировки",
            run.effective_block_date.strftime("%d.%m.%Y")
            + " 19:10",
        ),
        (
            "Целей AD",
            str(len(targets)),
        ),
    ]

    return {
        "kind": "blocking",
        "record_id": run.id,
        "created_at": run.created_at,
        "action": "Автоблокировка при увольнении",
        "subject": run.fio or "Работник",
        "login": ad_identifier,
        "corporate_email": "",
        "personal_email": "",
        "mail_domain": "",
        "operator": "Система",
        "status_key": status_key,
        "status_label": status_label,
        "details": details,
        "blocking_systems": blocking_systems,
        "equipment_snapshot": [],
        "error_message": run.last_error,
        "completed_at": run.completed_at or run.cancelled_at,
    }

def _journal_items(db: Session) -> list[dict[str, object]]:
    provisioning_operations = db.scalars(
        select(ProvisioningOperation)
        .order_by(desc(ProvisioningOperation.created_at))
        .limit(50)
    ).all()
    dismissal_operations = db.scalars(
        select(DismissalSchedule)
        .order_by(desc(DismissalSchedule.created_at))
        .limit(50)
    ).all()
    ad_provisioning_operations = db.scalars(
        select(ADProvisioningOperation)
        .order_by(desc(ADProvisioningOperation.created_at))
        .limit(50)
    ).all()
    blocking_operations = db.scalars(
        select(BlockingOperation)
        .order_by(desc(BlockingOperation.created_at))
        .limit(50)
    ).all()
    blocking_operation_ids = [item.id for item in blocking_operations]
    blocking_queue_items = (
        db.scalars(
            select(BlockingQueueItem).where(
                BlockingQueueItem.operation_id.in_(blocking_operation_ids)
            )
        ).all()
        if blocking_operation_ids
        else []
    )
    queue_by_operation: dict[int, list[BlockingQueueItem]] = {}
    for queue_item in blocking_queue_items:
        queue_by_operation.setdefault(
            queue_item.operation_id,
            [],
        ).append(queue_item)

    deferral_events = db.scalars(
        select(AuditLog)
        .where(AuditLog.action == DEFERRAL_ACTION)
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(50)
    ).all()
    dismissal_notices = db.scalars(
        select(DismissalEquipmentNotice)
        .order_by(
            desc(DismissalEquipmentNotice.created_at),
            desc(DismissalEquipmentNotice.id),
        )
        .limit(50)
    ).all()

    final_block_runs = db.scalars(
        select(FinalDismissalBlockRun)
        .order_by(
            desc(FinalDismissalBlockRun.created_at),
            desc(FinalDismissalBlockRun.id),
        )
        .limit(50)
    ).all()
    final_block_run_ids = [item.id for item in final_block_runs]
    final_block_targets = (
        db.scalars(
            select(FinalDismissalBlockTarget).where(
                FinalDismissalBlockTarget.run_id.in_(
                    final_block_run_ids
                )
            )
        ).all()
        if final_block_run_ids
        else []
    )
    final_targets_by_run: dict[
        int,
        list[FinalDismissalBlockTarget],
    ] = {}
    for target in final_block_targets:
        final_targets_by_run.setdefault(
            target.run_id,
            [],
        ).append(target)

    items = [
        *(
            _provisioning_journal_item(item)
            for item in provisioning_operations
        ),
        *(
            _ad_provisioning_journal_item(item)
            for item in ad_provisioning_operations
        ),
        *(
            _blocking_journal_item(
                item,
                queue_by_operation.get(item.id, []),
            )
            for item in blocking_operations
        ),
        *(
            _dismissal_journal_item(item)
            for item in dismissal_operations
        ),
        *(
            _deferral_journal_item(item)
            for item in deferral_events
        ),
        *(
            _dismissal_notice_journal_item(item)
            for item in dismissal_notices
        ),
        *(
            _final_dismissal_block_journal_item(
                item,
                final_targets_by_run.get(item.id, []),
            )
            for item in final_block_runs
        ),
    ]
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[:50]


@router.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    get_current_user(request)
    dismissal_error = request.query_params.get("dismissal_error", "")
    try:
        upcoming = UpcomingDismissalService(
            settings,
            db,
        ).list_upcoming(limit=20)
    except Exception as exc:
        db.rollback()
        upcoming = []
        dismissal_error = str(exc)

    ad_reactivation_alerts = list(
        db.scalars(
            select(ADReactivationAlert)
            .where(ADReactivationAlert.status == "open")
            .order_by(
                desc(ADReactivationAlert.updated_at),
                desc(ADReactivationAlert.id),
            )
        ).all()
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            journal_items=_journal_items(db),
            ad_reactivation_alerts=ad_reactivation_alerts,
            upcoming_dismissals=upcoming,
            dismissal_message=request.query_params.get(
                "dismissal_message",
                "",
            ),
            dismissal_error=dismissal_error,
            dry_run=settings.dry_run,
        ),
    )


@router.get("/dismissals/upcoming/fragment")
def upcoming_dismissals_fragment(
    request: Request,
    message: str = "",
    error: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Обновить только виджет ближайших увольнений без перезагрузки журнала."""
    get_current_user(request)
    try:
        upcoming = UpcomingDismissalService(
            settings,
            db,
        ).list_upcoming(limit=20)
        dismissal_error = error
    except Exception as exc:
        db.rollback()
        upcoming = []
        dismissal_error = str(exc)

    return templates.TemplateResponse(
        request,
        "upcoming_dismissals_fragment.html",
        _context(
            request,
            upcoming_dismissals=upcoming,
            dismissal_message=message,
            dismissal_error=dismissal_error,
        ),
    )


@router.post("/dismissals/upcoming/defer")
def defer_upcoming_dismissal(
    request: Request,
    worker_key: str = Form(...),
    dismissal_date: date = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    try:
        result = UpcomingDismissalService(
            settings,
            db,
        ).defer(
            worker_key=worker_key,
            expected_dismissal_date=dismissal_date,
            operator_username=user.username,
        )
        message = (
            f"Блокировка для {result['fio']} отложена до "
            f"{result['deferred_until'].strftime('%d.%m.%Y')}"
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"ok": True, "message": message},
                status_code=200,
            )
        return RedirectResponse(
            f"/?dismissal_message={quote_plus(message)}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse(
            f"/?dismissal_error={quote_plus(str(exc))}",
            status_code=303,
        )
