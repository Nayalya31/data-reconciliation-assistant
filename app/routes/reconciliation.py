from __future__ import annotations

import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.services.reconciliation_repository import ReconciliationRepository
from app.services.reconciliation_service import Mapping, ReconciliationError, ReconciliationService


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.templates_dir))
repository = ReconciliationRepository(settings)
service = ReconciliationService()
router = APIRouter()


def _user(request: Request) -> str | None:
    return request.session.get("user_id")


def _require_user(request: Request) -> str:
    user = _user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход.")
    return user


def _result_for(request: Request, reconciliation_id: str) -> dict:
    record = repository.get_result(reconciliation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Сверка не найдена.")
    if not record["is_demo"] and record["owner_id"] != _user(request):
        raise HTTPException(status_code=403, detail="Нет доступа к этой сверке.")
    return record


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error_message": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    valid = secrets.compare_digest(username, settings.app_username) and secrets.compare_digest(password, settings.app_password)
    if not valid:
        return templates.TemplateResponse("login.html", {"request": request, "error_message": "Неверный логин или пароль."}, status_code=401)
    request.session.clear()
    request.session["user_id"] = username
    return RedirectResponse("/reconciliation", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/reconciliation")
async def reconciliation_page(request: Request):
    user = _user(request)
    return templates.TemplateResponse(
        "reconciliation.html",
        {"request": request, "user": user, "history": repository.list_results(user) if user else [], "error_message": None},
    )


async def _stage(upload: UploadFile, side: str, owner: str) -> dict:
    original_name = Path(upload.filename or "upload").name
    extension = Path(original_name).suffix.lower()
    if extension not in service.allowed_extensions:
        raise ReconciliationError("Для сверки загрузите CSV или XLSX.")
    directory = settings.storage_dir / "reconciliation_inputs"
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    path = directory / f"{token}{extension}"
    size = 0
    try:
        with path.open("wb") as buffer:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_file_size_bytes:
                    raise ReconciliationError(f"Файл превышает лимит {settings.max_file_size}.")
                buffer.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    frame = service.read(path)
    repository.save_upload(token=token, owner_id=owner, side=side, original_name=original_name, stored_path=str(path), columns=list(frame.columns))
    return {"token": token, "name": original_name, "columns": list(frame.columns), "suggested": service.suggest_mapping(list(frame.columns))}


@router.post("/reconciliation/upload")
async def upload_pair(request: Request, ours_file: UploadFile = File(...), counterparty_file: UploadFile = File(...)):
    owner = _require_user(request)
    try:
        ours = await _stage(ours_file, "ours", owner)
        counterparty = await _stage(counterparty_file, "counterparty", owner)
    except ReconciliationError as exc:
        return templates.TemplateResponse("reconciliation.html", {"request": request, "user": owner, "history": repository.list_results(owner), "error_message": str(exc)}, status_code=400)
    return templates.TemplateResponse("reconciliation_mapping.html", {"request": request, "ours": ours, "counterparty": counterparty})


@router.post("/reconciliation/run")
async def run_reconciliation(
    request: Request,
    ours_token: str = Form(...), counterparty_token: str = Form(...),
    ours_date: str = Form(...), ours_document: str = Form(...), ours_operation: str = Form(...), ours_amount: str = Form(...),
    counter_date: str = Form(...), counter_document: str = Form(...), counter_operation: str = Form(...), counter_amount: str = Form(...),
):
    owner = _require_user(request)
    ours_meta = repository.get_upload(ours_token, owner)
    counter_meta = repository.get_upload(counterparty_token, owner)
    if not ours_meta or not counter_meta:
        raise HTTPException(status_code=404, detail="Загруженные файлы не найдены.")
    try:
        ours = service.read(Path(ours_meta["stored_path"]))
        counter = service.read(Path(counter_meta["stored_path"]))
        result = service.reconcile(ours, counter, Mapping(ours_date, ours_document, ours_operation, ours_amount), Mapping(counter_date, counter_document, counter_operation, counter_amount))
    except ReconciliationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record = _persist_result(result, owner, False, ours_meta["original_name"], counter_meta["original_name"])
    return RedirectResponse(f"/reconciliation/{record['id']}", status_code=303)


def _persist_result(result: dict, owner: str | None, is_demo: bool, ours_name: str, counter_name: str) -> dict:
    reconciliation_id = uuid4().hex
    output_dir = settings.output_dir / "reconciliations"
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / f"{reconciliation_id}.xlsx"
    chart = output_dir / f"{reconciliation_id}.png"
    service.export_excel(result, report)
    service.export_chart(result, chart)
    record = {"id": reconciliation_id, "owner_id": owner, "is_demo": is_demo, "ours_name": ours_name,
              "counterparty_name": counter_name, "summary": result["summary"], "result": result,
              "report_path": str(report), "chart_path": str(chart)}
    repository.save_result(record)
    return record


@router.get("/reconciliation/demo")
async def demo_reconciliation():
    examples = Path(__file__).resolve().parents[2] / "examples"
    ours_path, counter_path = examples / "reconciliation_ours.csv", examples / "reconciliation_counterparty.csv"
    result = service.reconcile(service.read(ours_path), service.read(counter_path),
                               Mapping("Дата", "Документ", "Операция", "Сумма"),
                               Mapping("Transaction date", "Document No", "Type", "Amount"))
    record = _persist_result(result, None, True, ours_path.name, counter_path.name)
    return RedirectResponse(f"/reconciliation/{record['id']}", status_code=303)


@router.get("/reconciliation/{reconciliation_id}")
async def reconciliation_result(request: Request, reconciliation_id: str):
    record = _result_for(request, reconciliation_id)
    return templates.TemplateResponse("reconciliation_result.html", {"request": request, "record": record})


@router.get("/reconciliation/{reconciliation_id}/chart")
async def reconciliation_chart(request: Request, reconciliation_id: str):
    record = _result_for(request, reconciliation_id)
    return FileResponse(record["chart_path"], media_type="image/png")


@router.get("/reconciliation/{reconciliation_id}/report")
async def reconciliation_report(request: Request, reconciliation_id: str):
    record = _result_for(request, reconciliation_id)
    return FileResponse(record["report_path"], filename=f"reconciliation-{reconciliation_id[:8]}.xlsx")
