from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app.config import ROOT, AppConfig, load_config, write_config
from app.davidlloyd_client import DavidLloydClient, DavidLloydError
from app.padel import PadelBookingService, Slot


app = FastAPI(title="David Lloyd Login Backend", version="0.1.0")
logger = logging.getLogger("davidlloyd-backend")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


def client() -> DavidLloydClient:
    return DavidLloydClient(load_config())


def padel_service() -> PadelBookingService:
    cfg = load_config()
    return PadelBookingService(DavidLloydClient(cfg), cfg.padel)


class BookGeneratedRequest(BaseModel):
    attempts: int = Field(default=1, ge=1, le=20)


class BookSlotRequest(BaseModel):
    date: str
    time: str
    member_id: str | None = None
    court_id: int | None = None


class ConfigUpdateRequest(BaseModel):
    username: str
    password: str | None = None
    device_id: str
    signature_mode: str
    padel: dict[str, Any]


def handle_error(exc: DavidLloydError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "message": str(exc),
            "upstream_status_code": exc.status_code,
            "upstream_body": exc.body,
        },
    )


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error during %s %s", request.method, request.url.path)
    if isinstance(exc, ValidationError):
        return JSONResponse(
            status_code=400,
            content={"detail": "Configuration validation failed", "errors": exc.errors()},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": type(exc).__name__, "message": str(exc)},
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def frontend() -> FileResponse:
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/api/config")
def api_config() -> dict:
    try:
        cfg = load_config()
        data = cfg.model_dump()
        data["password"] = ""
        data["password_is_set"] = bool(cfg.password)
        return data
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/config")
def update_api_config(payload: ConfigUpdateRequest) -> dict:
    try:
        current = load_config()
        merged = current.model_dump()
        merged["username"] = payload.username
        merged["device_id"] = payload.device_id
        merged["signature_mode"] = payload.signature_mode
        merged["padel"] = payload.padel
        if payload.password:
            merged["password"] = payload.password

        updated = AppConfig.model_validate(merged)
        write_config(updated)
        response = updated.model_dump()
        response["password"] = ""
        response["password_is_set"] = bool(updated.password)
        return {"ok": True, "config": response}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/auth/status")
def auth_status() -> dict:
    try:
        return client().status()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/auth/login")
def login() -> dict:
    try:
        result = client().login()
        return {
            "ok": True,
            "access_token_expires_at": result.access_token_expires_at,
            "hmac_expires_at": result.hmac_expires_at,
            "user_id": result.user_id,
            "scopes": result.scopes,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.post("/auth/refresh-token")
def refresh_token() -> dict:
    try:
        return client().refresh_token()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.post("/hmac/refresh")
def refresh_hmac() -> dict:
    try:
        return client().refresh_hmac()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.get("/members/me/membership-status")
def membership_status() -> dict:
    try:
        return client().membership_status()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.get("/padel/config")
def padel_config() -> dict:
    try:
        return load_config().padel.model_dump()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/padel/slots")
def padel_slots() -> dict:
    try:
        return {"slots": padel_service().slots()}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.get("/padel/availability/{date}")
def padel_availability(date: str, member_id: str | None = None) -> dict:
    try:
        return padel_service().availability(date=date, member_id=member_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.post("/padel/book-generated")
def padel_book_generated(payload: BookGeneratedRequest | None = None) -> dict:
    try:
        request = payload or BookGeneratedRequest()
        return padel_service().book_generated_slots(attempts=request.attempts)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.post("/padel/book-slot")
def padel_book_slot(payload: BookSlotRequest) -> dict:
    try:
        return padel_service().try_book(
            slot=Slot(date=payload.date, time=payload.time),
            court_id=payload.court_id,
            member_id=payload.member_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc
