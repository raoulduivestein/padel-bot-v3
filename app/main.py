from __future__ import annotations

import concurrent.futures
import logging
import re
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app.config import ROOT, AppConfig, load_config, write_config
from app.davidlloyd_client import DavidLloydClient, DavidLloydError
from app.invites import cancel_invite, create_invite, get_invite, read_invites, update_invite
from app.padel import PadelBookingService, Slot
from app.phonebook import read_phonebook, sync_booking_players, sync_config_players, update_entry, upsert_player
from app.run_history import append_run_history, read_run_history
from app.whatsapp import WhatsAppError, whatsapp_manager


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
    fresh_login: bool = True


class BookSlotRequest(BaseModel):
    date: str
    time: str
    member_id: str | None = None
    court_id: int | None = None


class UpdateBookingPlayersRequest(BaseModel):
    encodedBookingReference: str
    playersEncodedContactIds: list[str] = Field(max_length=4)


class CancelBookingRequest(BaseModel):
    encodedBookingReference: str


class WhatsAppSendRequest(BaseModel):
    phone: str
    message: str


class InvitePlayer(BaseModel):
    encodedContactId: str
    fullName: str | None = None
    phone: str | None = None
    memberReferenceNumber: str | None = None
    homeClubSiteId: int | None = None


class SendInviteRequest(BaseModel):
    encodedBookingReference: str
    booking: dict[str, Any]
    player: InvitePlayer


class SendTakeoverRequest(BaseModel):
    encodedBookingReference: str
    booking: dict[str, Any]
    recipient: InvitePlayer
    participants: list[InvitePlayer] = Field(min_length=1, max_length=4)


ACTIVE_INVITE_STATUSES = {"pending", "sent", "send_failed"}


class ConfigUpdateRequest(BaseModel):
    username: str
    password: str | None = None
    device_id: str
    public_base_url: str | None = None
    signature_mode: str
    padel: dict[str, Any]


class InviteMessagesUpdateRequest(BaseModel):
    invite_message_templates: list[str]
    takeover_message_template: str | None = None


class PhonebookUpsertRequest(BaseModel):
    encodedContactId: str
    fullName: str | None = None
    memberReferenceNumber: str | None = None
    homeClubSiteId: int | None = None
    source: str = "tool"


class PhonebookUpdateRequest(BaseModel):
    encodedContactId: str
    fullName: str | None = None
    phone: str | None = None
    notes: str | None = None


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
        merged["public_base_url"] = payload.public_base_url.strip() if payload.public_base_url else None
        merged["signature_mode"] = payload.signature_mode
        merged["padel"] = payload.padel
        if payload.password:
            merged["password"] = payload.password

        updated = AppConfig.model_validate(merged)
        write_config(updated)
        sync_config_players(updated)
        response = updated.model_dump()
        response["password"] = ""
        response["password_is_set"] = bool(updated.password)
        return {"ok": True, "config": response}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/invite-messages")
def update_invite_messages(payload: InviteMessagesUpdateRequest) -> dict:
    try:
        current = load_config()
        templates = [template.strip() for template in payload.invite_message_templates if template.strip()]
        if not templates:
            raise HTTPException(status_code=400, detail="At least one invite message is required")
        merged = current.model_dump()
        merged["padel"]["invite_message_templates"] = templates
        merged["padel"]["invite_message_template"] = templates[0]
        if payload.takeover_message_template and payload.takeover_message_template.strip():
            merged["padel"]["takeover_message_template"] = payload.takeover_message_template.strip()
        updated = AppConfig.model_validate(merged)
        write_config(updated)
        return {
            "ok": True,
            "invite_message_template": updated.padel.invite_message_template,
            "invite_message_templates": updated.padel.invite_message_templates,
            "takeover_message_template": updated.padel.takeover_message_template,
        }
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


def normalize_booking(booking: dict[str, Any]) -> dict[str, Any]:
    details = booking.get("details") or {}
    players = details.get("players") or []
    return {
        "date": booking.get("date"),
        "startTime": booking.get("startTime"),
        "duration": booking.get("duration"),
        "status": booking.get("status"),
        "clubName": booking.get("clubName") or details.get("clubName"),
        "activityName": booking.get("activityName") or details.get("activityName"),
        "courtId": details.get("courtId"),
        "encodedBookingReference": booking.get("encodedBookingReference"),
        "canMemberCancel": booking.get("canMemberCancel"),
        "players": [
            {
                "name": player.get("fullName") or player.get("name"),
                "encodedContactId": player.get("encodedContactId"),
                "memberReferenceNumber": player.get("memberReferenceNumber"),
                "homeClubSiteId": player.get("homeClubSiteId"),
                "paymentRequiredForCourtBookings": player.get("paymentRequiredForCourtBookings"),
            }
            for player in players
        ],
        "raw": booking,
    }


def format_date_nl(value: Any) -> str:
    text = str(value or "")
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return text or "-"


def format_time_nl(value: Any) -> str:
    text = str(value or "")
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        return text or "-"


def update_booking_players_with_ids(encoded_booking_reference: str, player_ids: list[str]) -> dict:
    if len(player_ids) > 4:
        raise HTTPException(status_code=400, detail="A booking can have at most 4 players")
    if len(set(player_ids)) != len(player_ids):
        raise HTTPException(status_code=400, detail="Duplicate players are not allowed")

    cfg = load_config()
    booking_reference = quote(encoded_booking_reference, safe="")
    data = DavidLloydClient(cfg).mobile_put(
        f"/clubs/{cfg.padel.club_id}/members/me/bookings/"
        f"{booking_reference}/players?return-booking=true",
        payload={"playersEncodedContactIds": player_ids},
    )
    returned_booking = data.get("booking") if isinstance(data, dict) else None
    if isinstance(returned_booking, dict):
        sync_booking_players([returned_booking])
        return {"ok": True, "booking": normalize_booking(returned_booking), "raw": data}
    return {"ok": True, "raw": data}


def cancel_booking_by_ref(encoded_booking_reference: str) -> dict:
    if not encoded_booking_reference:
        raise HTTPException(status_code=400, detail="encodedBookingReference is required")
    cfg = load_config()
    booking_reference = quote(encoded_booking_reference, safe="")
    result = DavidLloydClient(cfg).mobile_post(
        f"/clubs/{cfg.padel.club_id}/classes/bookings/by-ref/{booking_reference}/cancel",
        payload={},
    )
    return {"ok": True, "cancelled": True, "raw": result}


def find_booking(encoded_booking_reference: str) -> dict[str, Any] | None:
    data = client().bookings()
    bookings = data.get("bookings", []) if isinstance(data, dict) else []
    for booking in bookings:
        if booking.get("encodedBookingReference") == encoded_booking_reference:
            return normalize_booking(booking)
    return None


def active_invite(invite: dict[str, Any]) -> bool:
    return invite.get("status") in ACTIVE_INVITE_STATUSES


def render_invite_page(invite: dict[str, Any], *, message: str | None = None, status_code: int = 200) -> HTMLResponse:
    booking = invite.get("booking") or {}
    player = invite.get("player") or {}
    status = str(invite.get("status") or "")
    can_respond = active_invite(invite)
    action_html = (
        f"""
        <form method="post" action="/invite/{invite["token"]}/accept"><button class="primary" type="submit">Accepteren</button></form>
        <form method="post" action="/invite/{invite["token"]}/reject"><button type="submit">Weigeren</button></form>
        """
        if can_respond
        else ""
    )
    notice = f"<p class='notice'>{escape(message)}</p>" if message else ""
    body = f"""
    <!doctype html>
    <html lang="nl">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Padel uitnodiging</title>
        <style>
          :root {{ --bg:#f6f7f8; --surface:#fff; --line:#d8dde3; --text:#171a1f; --muted:#66707f; --accent:#0f766e; }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
          main {{ width: min(560px, calc(100% - 32px)); margin: 48px auto; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 12px 30px rgba(15,23,42,.08); padding: 22px; }}
          h1 {{ margin: 0 0 16px; font-size: 26px; line-height: 1.15; }}
          .meta {{ display: grid; gap: 10px; border: 1px solid var(--line); border-radius: 7px; background: #fbfcfd; padding: 14px; margin-bottom: 16px; }}
          .meta span {{ display: block; color: var(--muted); font-size: 13px; }}
          .meta strong {{ display: block; margin-top: 3px; color: var(--text); }}
          p {{ color: var(--muted); line-height: 1.5; }}
          .notice {{ color: var(--text); }}
          form {{ display: inline-block; margin-right: 8px; }}
          button {{ border: 1px solid var(--line); border-radius: 7px; padding: 10px 14px; background: white; color: var(--text); cursor: pointer; font: inherit; }}
          .primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
          .status {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; background: #fff; color: var(--text); }}
        </style>
      </head>
      <body>
        <main>
          <h1>🎾 Padel uitnodiging</h1>
          <div class="meta">
            <div><span>👤 Speler</span><strong>{escape(str(player.get("fullName") or "-"))}</strong></div>
            <div><span>📅 Datum en tijd</span><strong>{escape(format_date_nl(booking.get("date")))} om {escape(format_time_nl(booking.get("startTime")))}</strong></div>
            <div><span>📍 Locatie</span><strong>{escape(str(booking.get("clubName") or "David Lloyd"))}</strong></div>
            <div><span>✅ Status</span><strong class="status">{escape(status)}</strong></div>
          </div>
          {notice}
          {action_html}
        </main>
      </body>
    </html>
    """
    return HTMLResponse(body, status_code=status_code)


def render_takeover_page(invite: dict[str, Any], *, message: str | None = None, status_code: int = 200) -> HTMLResponse:
    booking = invite.get("booking") or {}
    recipient = invite.get("player") or {}
    participants = invite.get("participants") or []
    status = str(invite.get("status") or "")
    can_cancel = active_invite(invite)
    participant_rows = "\n".join(
        f"""
        <li>
          <span>{escape(str(player.get("fullName") or player.get("encodedContactId") or "-"))}</span>
          <button type="button" data-copy="{escape(str(player.get("fullName") or ""))}">Kopieer naam</button>
        </li>
        """
        for player in participants
    )
    action_html = (
        f"""
        <form method="post" action="/takeover/{invite["token"]}/cancel">
          <button class="danger" type="submit">Baan annuleren</button>
        </form>
        """
        if can_cancel
        else ""
    )
    notice = f"<p class='notice'>{escape(message)}</p>" if message else ""
    body = f"""
    <!doctype html>
    <html lang="nl">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Baan overnemen</title>
        <style>
          :root {{ --bg:#f6f7f8; --surface:#fff; --line:#d8dde3; --text:#171a1f; --muted:#66707f; --accent:#0f766e; --danger:#b42318; }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
          main {{ width: min(620px, calc(100% - 32px)); margin: 32px auto; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 12px 30px rgba(15,23,42,.08); padding: 22px; }}
          h1 {{ margin: 0 0 12px; font-size: 26px; line-height: 1.15; }}
          h2 {{ margin: 18px 0 10px; font-size: 16px; }}
          p {{ color: var(--muted); line-height: 1.5; }}
          .warning {{ border: 1px solid #f1c7c2; border-radius: 7px; background: #fff7f6; color: var(--text); padding: 12px; margin: 14px 0; }}
          .notice {{ color: var(--text); }}
          .meta {{ display: grid; gap: 10px; border: 1px solid var(--line); border-radius: 7px; background: #fbfcfd; padding: 14px; margin-bottom: 14px; }}
          .meta span {{ display: block; color: var(--muted); font-size: 13px; }}
          .meta strong {{ display: block; margin-top: 3px; color: var(--text); }}
          ul {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }}
          li {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; border: 1px solid var(--line); border-radius: 7px; background: #fbfcfd; padding: 10px; }}
          button {{ border: 1px solid var(--line); border-radius: 7px; padding: 10px 14px; background: white; color: var(--text); cursor: pointer; font: inherit; }}
          .danger {{ background: var(--danger); border-color: var(--danger); color: white; margin-top: 16px; width: 100%; }}
          .status {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; background: #fff; color: var(--text); }}
          @media (max-width: 520px) {{ main {{ width: 100%; min-height: 100vh; margin: 0; border: 0; border-radius: 0; }} li {{ grid-template-columns: 1fr; }} }}
        </style>
      </head>
      <body>
        <main>
          <h1>🎾 Baan overnemen</h1>
          <p>Deze baan kan door jou worden overgenomen. Annuleer de baan alleen als je direct daarna zelf in de David Lloyd app opnieuw gaat boeken.</p>
          <div class="warning"><strong>Let op:</strong> zodra je annuleert komt de baan vrij. Boek direct opnieuw met de spelers hieronder, anders kan iemand anders de baan reserveren.</div>
          <div class="meta">
            <div><span>👤 Ontvanger</span><strong>{escape(str(recipient.get("fullName") or "-"))}</strong></div>
            <div><span>📅 Datum en tijd</span><strong>{escape(format_date_nl(booking.get("date")))} om {escape(format_time_nl(booking.get("startTime")))}</strong></div>
            <div><span>📍 Locatie</span><strong>{escape(str(booking.get("clubName") or "David Lloyd"))} - Court {escape(str(booking.get("courtId") or "-"))}</strong></div>
            <div><span>✅ Status</span><strong class="status">{escape(status)}</strong></div>
          </div>
          {notice}
          <h2>👥 Spelers om opnieuw toe te voegen</h2>
          <ul>{participant_rows or "<li><span>Geen spelers opgegeven.</span></li>"}</ul>
          {action_html}
        </main>
        <script>
          document.querySelectorAll("[data-copy]").forEach((button) => {{
            button.addEventListener("click", async () => {{
              await navigator.clipboard.writeText(button.dataset.copy || "");
              button.textContent = "Gekopieerd";
              setTimeout(() => button.textContent = "Kopieer naam", 1200);
            }});
          }});
        </script>
      </body>
    </html>
    """
    return HTMLResponse(body, status_code=status_code)


def invite_message_templates(config: AppConfig) -> list[str]:
    templates = [template.strip() for template in config.padel.invite_message_templates if template.strip()]
    if templates:
        return templates
    split_templates = [
        template.strip()
        for template in re.split(r"\r?\n---\r?\n", config.padel.invite_message_template)
        if template.strip()
    ]
    return split_templates or [config.padel.invite_message_template]


def format_invite_message(template: str, *, booking: dict[str, Any], player: dict[str, Any], invite_url: str) -> str:
    values = {
        "player_name": player.get("fullName") or "",
        "date": format_date_nl(booking.get("date")),
        "time": format_time_nl(booking.get("startTime")),
        "club_name": booking.get("clubName") or "David Lloyd",
        "court_id": booking.get("courtId") or "-",
        "activity_name": booking.get("activityName") or "Padel",
        "invite_url": invite_url,
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown invite template placeholder: {exc}") from exc


def format_takeover_message(
    template: str,
    *,
    booking: dict[str, Any],
    recipient: dict[str, Any],
    participants: list[dict[str, Any]],
    takeover_url: str,
) -> str:
    participant_names = [
        str(player.get("fullName") or player.get("encodedContactId") or "")
        for player in participants
        if player.get("fullName") or player.get("encodedContactId")
    ]
    values = {
        "recipient_name": recipient.get("fullName") or "",
        "date": format_date_nl(booking.get("date")),
        "time": format_time_nl(booking.get("startTime")),
        "club_name": booking.get("clubName") or "David Lloyd",
        "court_id": booking.get("courtId") or "-",
        "activity_name": booking.get("activityName") or "Padel",
        "players": ", ".join(participant_names),
        "takeover_url": takeover_url,
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown takeover template placeholder: {exc}") from exc


def normalize_phone_digits(phone: str | None) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = f"31{digits[1:]}"
    return digits


def public_url(config: AppConfig, request: Request, route_name: str, **path_params: str) -> str:
    path = request.url_for(route_name, **path_params).path
    if config.public_base_url:
        return f"{config.public_base_url.rstrip('/')}{path}"
    return str(request.url_for(route_name, **path_params))


@app.get("/padel/bookings")
def padel_bookings() -> dict:
    try:
        data = client().bookings()
        bookings = data.get("bookings", []) if isinstance(data, dict) else []
        sync_booking_players(bookings)
        return {
            "bookings": [normalize_booking(booking) for booking in bookings],
            "raw": data,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.put("/padel/bookings/players")
def update_booking_players(payload: UpdateBookingPlayersRequest) -> dict:
    try:
        return update_booking_players_with_ids(
            payload.encodedBookingReference,
            payload.playersEncodedContactIds,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.post("/padel/bookings/cancel")
def cancel_booking(payload: CancelBookingRequest) -> dict:
    try:
        return cancel_booking_by_ref(payload.encodedBookingReference)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.get("/whatsapp/status")
def whatsapp_status() -> dict:
    try:
        return whatsapp_manager.status()
    except (WhatsAppError, concurrent.futures.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/whatsapp/debug")
def whatsapp_debug() -> dict:
    try:
        return whatsapp_manager.debug()
    except (WhatsAppError, concurrent.futures.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/whatsapp/reload")
def whatsapp_reload() -> dict:
    try:
        return whatsapp_manager.reload()
    except (WhatsAppError, concurrent.futures.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/whatsapp/qr")
def whatsapp_qr() -> Response:
    try:
        return Response(content=whatsapp_manager.qr_screenshot(), media_type="image/png")
    except (WhatsAppError, concurrent.futures.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/whatsapp/send")
def whatsapp_send(payload: WhatsAppSendRequest) -> dict:
    try:
        return whatsapp_manager.send_message(phone=payload.phone, message=payload.message)
    except (WhatsAppError, concurrent.futures.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/bookings/invites/send")
def send_booking_invite(payload: SendInviteRequest, request: Request) -> dict:
    player = payload.player.model_dump()
    if not player.get("phone"):
        raise HTTPException(status_code=400, detail="Player has no phone number")
    if len(payload.booking.get("players") or []) >= 4:
        raise HTTPException(status_code=400, detail="A booking with 4 players cannot receive more invites")
    cfg = load_config()
    invite = create_invite(
        encoded_booking_reference=payload.encodedBookingReference,
        player=player,
        booking=payload.booking,
    )
    invite_url = public_url(cfg, request, "invite_page", token=invite["token"])
    booking = invite["booking"]
    messages = [
        format_invite_message(template, booking=booking, player=player, invite_url=invite_url)
        for template in invite_message_templates(cfg)
    ]
    try:
        send_results = []
        for message in messages:
            send_results.append(whatsapp_manager.send_message(phone=player["phone"], message=message))
    except (WhatsAppError, concurrent.futures.TimeoutError) as exc:
        update_invite(invite["token"], status="send_failed", sendError=str(exc), messages=messages)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    update_invite(invite["token"], status="sent", messageCount=len(messages), messages=messages)
    return {"ok": True, "invite": get_invite(invite["token"]), "whatsapp": send_results, "inviteUrl": invite_url}


@app.post("/bookings/takeovers/send")
def send_booking_takeover(payload: SendTakeoverRequest, request: Request) -> dict:
    cfg = load_config()
    recipient = payload.recipient.model_dump()
    participants = [participant.model_dump() for participant in payload.participants]
    if not recipient.get("phone"):
        raise HTTPException(status_code=400, detail="Recipient has no phone number")
    if not participants:
        raise HTTPException(status_code=400, detail="Takeover needs at least one participant")
    invite = create_invite(
        encoded_booking_reference=payload.encodedBookingReference,
        player=recipient,
        booking=payload.booking,
    )
    invite = update_invite(
        invite["token"],
        kind="takeover",
        participants=participants,
    )
    takeover_url = public_url(cfg, request, "takeover_page", token=invite["token"])
    message = format_takeover_message(
        cfg.padel.takeover_message_template,
        booking=invite["booking"],
        recipient=recipient,
        participants=participants,
        takeover_url=takeover_url,
    )
    try:
        send_result = whatsapp_manager.send_message(phone=recipient["phone"], message=message)
    except (WhatsAppError, concurrent.futures.TimeoutError) as exc:
        update_invite(invite["token"], status="send_failed", sendError=str(exc), messages=[message])
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    update_invite(invite["token"], status="sent", messageCount=1, messages=[message])
    return {"ok": True, "invite": get_invite(invite["token"]), "whatsapp": send_result, "takeoverUrl": takeover_url}


@app.get("/bookings/invites")
def booking_invites() -> dict:
    return {"invites": read_invites()}


@app.post("/bookings/invites/{token}/cancel")
def cancel_booking_invite(token: str) -> dict:
    invite = get_invite(token)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.get("status") in {"accepted", "rejected", "cancelled"}:
        return {"ok": True, "invite": invite}
    return {"ok": True, "invite": cancel_invite(token)}


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    changes: dict[str, Any] = {"openCount": int(invite.get("openCount") or 0) + 1}
    if not invite.get("openedAt"):
        changes["openedAt"] = datetime.now().isoformat(timespec="seconds")
    invite = update_invite(token, **changes)
    message = None if active_invite(invite) else "Deze uitnodiging is niet meer actief."
    return render_invite_page(invite, message=message)


@app.get("/takeover/{token}", response_class=HTMLResponse)
def takeover_page(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "takeover":
        raise HTTPException(status_code=404, detail="Takeover not found")
    changes: dict[str, Any] = {"openCount": int(invite.get("openCount") or 0) + 1}
    if not invite.get("openedAt"):
        changes["openedAt"] = datetime.now().isoformat(timespec="seconds")
    invite = update_invite(token, **changes)
    message = None if active_invite(invite) else "Deze overname-link is niet meer actief."
    return render_takeover_page(invite, message=message)


@app.post("/takeover/{token}/cancel", response_class=HTMLResponse)
def takeover_cancel_booking(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "takeover":
        raise HTTPException(status_code=404, detail="Takeover not found")
    if not active_invite(invite):
        return render_takeover_page(invite, message="Deze overname-link is al verwerkt of ingetrokken.", status_code=409)
    try:
        cancel_booking_by_ref(invite["encodedBookingReference"])
        update_invite(token, status="cancelled_for_takeover", cancelledAt=datetime.now().isoformat(timespec="seconds"))
        return render_takeover_page(
            get_invite(token) or invite,
            message="De baan is geannuleerd. Boek nu direct zelf opnieuw in de David Lloyd app met de spelers hieronder.",
        )
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.post("/invite/{token}/accept", response_class=HTMLResponse)
def accept_invite(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if not active_invite(invite):
        return render_invite_page(invite, message="Deze uitnodiging is al verwerkt of ingetrokken.", status_code=409)
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Invite player has no encodedContactId")
    try:
        booking = find_booking(invite["encodedBookingReference"])
        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        player_ids = [player.get("encodedContactId") for player in booking.get("players", []) if player.get("encodedContactId")]
        if len(player_ids) >= 4:
            update_invite(token, status="full")
            return render_invite_page(get_invite(token) or invite, message="Deze boeking zit al vol.", status_code=409)
        if player_id not in player_ids:
            player_ids.append(player_id)
            update_booking_players_with_ids(invite["encodedBookingReference"], player_ids)
        update_invite(token, status="accepted")
        return render_invite_page(get_invite(token) or invite, message="Je bent toegevoegd aan de boeking.")
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.post("/invite/{token}/reject", response_class=HTMLResponse)
def reject_invite(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if not active_invite(invite):
        return render_invite_page(invite, message="Deze uitnodiging is al verwerkt of ingetrokken.", status_code=409)
    update_invite(token, status="rejected")
    return render_invite_page(get_invite(token) or invite, message="Je hebt de uitnodiging geweigerd.")


@app.get("/padel/players/search")
def search_players(q: str) -> dict:
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Search query must be at least 2 characters")
    try:
        data = client().search_players(q.strip())
        return {
            "players": data.get("possiblePlayers", []) if isinstance(data, dict) else [],
            "raw": data,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        raise handle_error(exc) from exc


@app.get("/players/search")
def search_players_alias(q: str) -> dict:
    return search_players(q)


@app.get("/phonebook")
def phonebook() -> dict:
    return {"players": read_phonebook()}


@app.post("/phonebook/upsert")
def phonebook_upsert(payload: PhonebookUpsertRequest) -> dict:
    try:
        entry = upsert_player(
            encoded_contact_id=payload.encodedContactId,
            full_name=payload.fullName,
            member_reference_number=payload.memberReferenceNumber,
            home_club_site_id=payload.homeClubSiteId,
            source=payload.source,
        )
        return {"ok": True, "player": entry}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/phonebook")
def phonebook_update(payload: PhonebookUpdateRequest) -> dict:
    try:
        entry = update_entry(
            encoded_contact_id=payload.encodedContactId,
            full_name=payload.fullName,
            phone=payload.phone,
            notes=payload.notes,
        )
        return {"ok": True, "player": entry}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    request = payload or BookGeneratedRequest()
    try:
        service = padel_service()
        if request.fresh_login:
            service.client.login()
        result = service.book_generated_slots(attempts=request.attempts)
        append_run_history(source="web", attempts=request.attempts, result=result)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DavidLloydError as exc:
        append_run_history(source="web", attempts=request.attempts, error=exc)
        raise handle_error(exc) from exc


@app.get("/padel/runs")
def padel_runs(limit: int = 50) -> dict:
    return {"runs": read_run_history(limit=max(1, min(limit, 200)))}


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
