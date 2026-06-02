from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from datetime import datetime, timedelta
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
BOOKING_TOOL_PLAYERS_PATH = ROOT / "state" / "booking_tool_players.json"


def client() -> DavidLloydClient:
    return DavidLloydClient(load_config())


def padel_service() -> PadelBookingService:
    cfg = load_config()
    return PadelBookingService(DavidLloydClient(cfg), cfg.padel)


def read_booking_tool_players() -> dict[str, list[str]]:
    if not BOOKING_TOOL_PLAYERS_PATH.exists():
        return {}
    with BOOKING_TOOL_PLAYERS_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {}
    return {
        str(ref): [str(player_id) for player_id in player_ids if player_id]
        for ref, player_ids in data.items()
        if isinstance(player_ids, list)
    }


def write_booking_tool_players(entries: dict[str, list[str]]) -> None:
    BOOKING_TOOL_PLAYERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {ref: player_ids for ref, player_ids in entries.items() if player_ids}
    with BOOKING_TOOL_PLAYERS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(cleaned, handle, indent=2, sort_keys=True)


def update_booking_tool_players(encoded_booking_reference: str, player_ids: list[str]) -> None:
    entries = read_booking_tool_players()
    unique: list[str] = []
    for player_id in player_ids:
        if player_id and player_id not in unique:
            unique.append(player_id)
    if unique:
        entries[encoded_booking_reference] = unique
    else:
        entries.pop(encoded_booking_reference, None)
    write_booking_tool_players(entries)


def tool_player_name(player_id: str) -> str:
    cfg = load_config()
    known_name = cfg.padel.known_players.get(player_id)
    if known_name:
        return known_name
    phonebook_entry = next((player for player in read_phonebook() if player.get("encodedContactId") == player_id), None)
    if phonebook_entry and phonebook_entry.get("fullName"):
        return str(phonebook_entry["fullName"])
    return player_id


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
    toolOnlyPlayerIds: list[str] = []


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


class CreateSignupRequest(BaseModel):
    encodedBookingReference: str
    booking: dict[str, Any]
    player: InvitePlayer


ACTIVE_INVITE_STATUSES = {"pending", "sent", "send_failed"}
ACTIVE_PLAYER_SIGNUP_STATUS = "active"


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
    encoded_booking_reference = booking.get("encodedBookingReference")
    normalized_players = [
        {
            "name": player.get("fullName") or player.get("name"),
            "encodedContactId": player.get("encodedContactId"),
            "memberReferenceNumber": player.get("memberReferenceNumber"),
            "homeClubSiteId": player.get("homeClubSiteId"),
            "paymentRequiredForCourtBookings": player.get("paymentRequiredForCourtBookings"),
        }
        for player in players
    ]
    existing_player_ids = {player.get("encodedContactId") for player in normalized_players}
    for player_id in read_booking_tool_players().get(str(encoded_booking_reference or ""), []):
        if player_id in existing_player_ids:
            continue
        normalized_players.append({
            "name": tool_player_name(player_id),
            "encodedContactId": player_id,
            "memberReferenceNumber": None,
            "homeClubSiteId": None,
            "paymentRequiredForCourtBookings": None,
            "toolOnly": True,
        })
    return {
        "date": booking.get("date"),
        "startTime": booking.get("startTime"),
        "duration": booking.get("duration"),
        "status": booking.get("status"),
        "clubName": booking.get("clubName") or details.get("clubName"),
        "activityName": booking.get("activityName") or details.get("activityName"),
        "courtId": details.get("courtId"),
        "bookedMemberEncodedContactId": booking.get("bookedMemberEncodedContactId"),
        "bookedByEncodedContactId": booking.get("bookedByEncodedContactId"),
        "encodedBookingReference": encoded_booking_reference,
        "canMemberCancel": booking.get("canMemberCancel"),
        "players": normalized_players,
        "raw": booking,
    }


def format_date_nl(value: Any) -> str:
    text = str(value or "")
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return text or "-"


def format_weekday_nl(value: Any) -> str:
    text = str(value or "")
    weekdays = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
    try:
        return weekdays[datetime.strptime(text, "%Y-%m-%d").weekday()]
    except ValueError:
        return "-"


def format_time_nl(value: Any) -> str:
    text = str(value or "")
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        return text or "-"


def booking_start_datetime(booking: dict[str, Any]) -> datetime | None:
    try:
        return datetime.strptime(f"{booking.get('date')} {booking.get('startTime')}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def booking_duration_minutes(booking: dict[str, Any]) -> int:
    duration = booking.get("duration") or 60
    try:
        duration_number = int(duration)
    except (TypeError, ValueError):
        return 60
    return duration_number * 60 if duration_number <= 4 else duration_number


def consecutive_bookings_for(base_booking: dict[str, Any]) -> list[dict[str, Any]]:
    data = client().bookings()
    raw_bookings = data.get("bookings", []) if isinstance(data, dict) else []
    bookings = [normalize_booking(booking) for booking in raw_bookings]
    base_ref = base_booking.get("encodedBookingReference")
    base = next((booking for booking in bookings if booking.get("encodedBookingReference") == base_ref), None)
    if base is None:
        base = normalize_booking(base_booking)
    base_start = booking_start_datetime(base)
    if base_start is None:
        return [base]

    same_group = [
        booking
        for booking in bookings
        if booking.get("date") == base.get("date")
        and booking.get("courtId") == base.get("courtId")
        and (booking.get("clubName") or "") == (base.get("clubName") or "")
        and booking_start_datetime(booking) is not None
    ]
    by_start = {booking_start_datetime(booking): booking for booking in same_group}
    selected = [base]

    cursor = base_start
    while True:
        previous = by_start.get(cursor - timedelta(minutes=60))
        if previous is None:
            break
        selected.insert(0, previous)
        cursor -= timedelta(minutes=booking_duration_minutes(previous))

    cursor = base_start
    current = base
    while True:
        next_start = cursor + timedelta(minutes=booking_duration_minutes(current))
        next_booking = by_start.get(next_start)
        if next_booking is None:
            break
        selected.append(next_booking)
        cursor = next_start
        current = next_booking

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for booking in selected:
        ref = booking.get("encodedBookingReference")
        if ref and ref not in seen:
            deduped.append(booking)
            seen.add(ref)
    return deduped or [base]


def booking_player_ids(booking: dict[str, Any]) -> list[str]:
    return [
        player.get("encodedContactId")
        for player in booking.get("players", [])
        if player.get("encodedContactId")
    ]


def configured_non_playing_member_ids() -> set[str]:
    return {member.member_id for member in load_config().padel.members if not member.plays}


def effective_booking_player_ids(booking: dict[str, Any]) -> list[str]:
    non_playing_ids = configured_non_playing_member_ids()
    return [
        player_id
        for player_id in booking_player_ids(booking)
        if player_id not in non_playing_ids
    ]


def unique_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def matching_booking_rule_indexes(config: AppConfig, booking: dict[str, Any]) -> list[int]:
    start = booking_start_datetime(booking)
    if start is None:
        return []
    weekday = start.strftime("%A").lower()
    matches: list[int] = []
    for index, rule in enumerate(config.padel.booking_rules):
        if rule.day.lower() != weekday:
            continue
        for base_time in rule.times:
            try:
                hour, minute = map(int, base_time.split(":"))
            except ValueError:
                continue
            base = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
            end = base + timedelta(hours=rule.duration)
            if base <= start < end:
                matches.append(index)
                break
    return matches


def configured_playing_ids_for_rule(config: AppConfig, rule_index: int, extra_player_id: str | None = None) -> list[str]:
    rule = config.padel.booking_rules[rule_index]
    ids = [
        *[member.member_id for member in config.padel.members if member.plays],
        *config.padel.always_add_player_ids,
        *rule.player_ids,
    ]
    if extra_player_id:
        ids.append(extra_player_id)
    return unique_ids(ids)


def add_player_to_booking_rules(player_id: str, booking: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    reserved_ids = {member.member_id for member in config.padel.members if member.plays} | set(config.padel.always_add_player_ids)
    rule_indexes = matching_booking_rule_indexes(config, booking)
    if not rule_indexes:
        return {"added": False, "reason": "No matching booking rule found", "ruleIndexes": []}
    if player_id in reserved_ids:
        return {"added": False, "reason": "Player is already configured as member or always-add player", "ruleIndexes": rule_indexes}

    added_indexes: list[int] = []
    full_indexes: list[int] = []
    for rule_index in rule_indexes:
        rule = config.padel.booking_rules[rule_index]
        if player_id in rule.player_ids:
            continue
        if len(configured_playing_ids_for_rule(config, rule_index, player_id)) > 4:
            full_indexes.append(rule_index)
            continue
        rule.player_ids.append(player_id)
        added_indexes.append(rule_index)

    if added_indexes:
        write_config(config)
    return {
        "added": bool(added_indexes),
        "ruleIndexes": rule_indexes,
        "addedRuleIndexes": added_indexes,
        "fullRuleIndexes": full_indexes,
    }


def booking_rule_add_blockers(player_id: str, bookings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = load_config()
    reserved_ids = {member.member_id for member in config.padel.members if member.plays} | set(config.padel.always_add_player_ids)
    if player_id in reserved_ids:
        return []

    blockers: list[dict[str, Any]] = []
    checked: set[int] = set()
    for booking in bookings:
        for rule_index in matching_booking_rule_indexes(config, booking):
            if rule_index in checked:
                continue
            checked.add(rule_index)
            rule = config.padel.booking_rules[rule_index]
            if player_id in rule.player_ids:
                continue
            player_count = len(configured_playing_ids_for_rule(config, rule_index, player_id))
            if player_count > 4:
                blockers.append({
                    "ruleIndex": rule_index,
                    "day": rule.day,
                    "times": rule.times,
                    "playerCount": player_count,
                })
    return blockers


def player_signup_rules_payload(invite: dict[str, Any]) -> dict[str, Any]:
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Signup player has no encodedContactId")
    config = load_config()
    reserved_ids = {member.member_id for member in config.padel.members if member.plays} | set(config.padel.always_add_player_ids)
    can_manage = player_id not in reserved_ids
    rules = []
    for index, rule in enumerate(config.padel.booking_rules):
        playing_ids = configured_playing_ids_for_rule(config, index)
        player_count_with_player = len(configured_playing_ids_for_rule(config, index, player_id))
        is_added = player_id in rule.player_ids or player_id in reserved_ids
        rules.append({
            "index": index,
            "day": rule.day,
            "times": rule.times,
            "duration": rule.duration,
            "isAdded": is_added,
            "canAdd": can_manage and not is_added and player_count_with_player <= 4,
            "canRemove": can_manage and player_id in rule.player_ids,
            "playerCount": len(playing_ids),
            "playerCountWithPlayer": player_count_with_player,
        })
    return {
        "player": invite.get("player") or {},
        "canManage": can_manage,
        "message": "" if can_manage else "Je staat al standaard als spelende member of Always add player.",
        "rules": rules,
    }


def update_player_booking_rule(token: str, rule_index: int, action: str) -> dict[str, Any]:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "player_signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    if invite.get("status") != ACTIVE_PLAYER_SIGNUP_STATUS:
        raise HTTPException(status_code=409, detail="Signup link is not active")
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Signup player has no encodedContactId")

    config = load_config()
    if rule_index < 0 or rule_index >= len(config.padel.booking_rules):
        raise HTTPException(status_code=404, detail="Booking rule not found")
    reserved_ids = {member.member_id for member in config.padel.members if member.plays} | set(config.padel.always_add_player_ids)
    if player_id in reserved_ids:
        raise HTTPException(status_code=400, detail="Player is already configured as playing member or always-add player")

    rule = config.padel.booking_rules[rule_index]
    if action == "add":
        if player_id not in rule.player_ids:
            if len(configured_playing_ids_for_rule(config, rule_index, player_id)) > 4:
                raise HTTPException(status_code=409, detail="Booking rule already has 4 playing players")
            rule.player_ids.append(player_id)
            write_config(config)
    elif action == "remove":
        if player_id in rule.player_ids:
            rule.player_ids = [current_id for current_id in rule.player_ids if current_id != player_id]
            write_config(config)
    else:
        raise HTTPException(status_code=400, detail="Unknown booking rule action")

    return player_signup_rules_payload(invite)


def remove_player_from_booking_rules(player_id: str, booking: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    removed_indexes: list[int] = []
    for rule_index in matching_booking_rule_indexes(config, booking):
        rule = config.padel.booking_rules[rule_index]
        if player_id in rule.player_ids:
            rule.player_ids = [current_id for current_id in rule.player_ids if current_id != player_id]
            removed_indexes.append(rule_index)
    if removed_indexes:
        write_config(config)
    return {"removed": bool(removed_indexes), "removedRuleIndexes": removed_indexes}


def update_ids_for_signup(booking: dict[str, Any], player_id: str) -> tuple[list[str], bool]:
    non_playing_ids = configured_non_playing_member_ids()
    booked_member_id = booking.get("bookedMemberEncodedContactId")
    ids = booking_player_ids(booking)
    filtered: list[str] = []
    for current_id in ids:
        if current_id in non_playing_ids and current_id != booked_member_id:
            continue
        if current_id not in filtered:
            filtered.append(current_id)
    if player_id not in filtered:
        filtered.append(player_id)
    return filtered, len(filtered) <= 4


def player_signup_open_groups(player_id: str) -> list[dict[str, Any]]:
    data = client().bookings()
    raw_bookings = data.get("bookings", []) if isinstance(data, dict) else []
    bookings = [normalize_booking(booking) for booking in raw_bookings]
    return player_signup_open_groups_from_bookings(player_id, bookings)


def player_signup_open_groups_from_bookings(player_id: str, bookings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = load_config()
    candidates = [
        booking
        for booking in bookings
        if booking.get("encodedBookingReference")
        and booking_start_datetime(booking) is not None
        and player_id not in effective_booking_player_ids(booking)
        and len(effective_booking_player_ids(booking)) < 4
    ]
    candidates.sort(key=lambda booking: (
        booking.get("date") or "",
        booking.get("clubName") or "",
        booking.get("courtId") or 0,
        booking.get("startTime") or "",
    ))

    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    for booking in candidates:
        ref = booking.get("encodedBookingReference")
        if not ref or ref in used:
            continue
        group = [booking]
        used.add(ref)
        current = booking
        while True:
            current_start = booking_start_datetime(current)
            if current_start is None:
                break
            next_start = current_start + timedelta(minutes=booking_duration_minutes(current))
            next_booking = next(
                (
                    item
                    for item in candidates
                    if item.get("encodedBookingReference") not in used
                    and item.get("date") == current.get("date")
                    and item.get("courtId") == current.get("courtId")
                    and (item.get("clubName") or "") == (current.get("clubName") or "")
                    and booking_start_datetime(item) == next_start
                ),
                None,
            )
            if next_booking is None:
                break
            group.append(next_booking)
            used.add(next_booking["encodedBookingReference"])
            current = next_booking

        groups.append({
            "id": group[0]["encodedBookingReference"],
            "date": group[0].get("date"),
            "dateNl": format_date_nl(group[0].get("date")),
            "weekday": format_weekday_nl(group[0].get("date")),
            "startTime": group[0].get("startTime"),
            "endTime": (
                booking_start_datetime(group[-1]) + timedelta(minutes=booking_duration_minutes(group[-1]))
            ).strftime("%H:%M") if booking_start_datetime(group[-1]) else format_time_nl(group[-1].get("startTime")),
            "clubName": group[0].get("clubName"),
            "courtId": group[0].get("courtId"),
            "courtLabel": court_label(config, group[0].get("courtId")),
            "bookings": group,
            "playerCounts": [len(effective_booking_player_ids(item)) for item in group],
        })
    return groups


def player_signup_existing_groups(invite: dict[str, Any], player_id: str, bookings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = load_config()
    by_ref = {
        booking.get("encodedBookingReference"): booking
        for booking in bookings
        if booking.get("encodedBookingReference")
    }
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for signup in invite.get("signups") or []:
        if signup.get("cancelledAt"):
            continue
        signup_bookings = [
            by_ref[ref]
            for ref in signup.get("bookingReferences") or []
            if ref in by_ref
        ]
        if not signup_bookings:
            continue
        signup_bookings.sort(key=lambda booking: booking_start_datetime(booking) or datetime.max)
        first_booking = signup_bookings[0]
        last_booking = signup_bookings[-1]
        first_start = booking_start_datetime(first_booking)
        if first_start is None:
            continue
        for booking in signup_bookings:
            if booking.get("encodedBookingReference"):
                seen.add(booking["encodedBookingReference"])
        cancel_deadline = first_start - timedelta(hours=72)
        can_cancel = datetime.now() <= cancel_deadline
        groups.append({
            "id": first_booking.get("encodedBookingReference"),
            "date": first_booking.get("date"),
            "dateNl": format_date_nl(first_booking.get("date")),
            "weekday": format_weekday_nl(first_booking.get("date")),
            "startTime": first_booking.get("startTime"),
            "endTime": (
                booking_start_datetime(last_booking) + timedelta(minutes=booking_duration_minutes(last_booking))
            ).strftime("%H:%M") if booking_start_datetime(last_booking) else format_time_nl(last_booking.get("startTime")),
            "clubName": first_booking.get("clubName"),
            "courtId": first_booking.get("courtId"),
            "courtLabel": court_label(config, first_booking.get("courtId")),
            "bookings": signup_bookings,
            "bookingReferences": [booking.get("encodedBookingReference") for booking in signup_bookings],
            "joinedAt": signup.get("joinedAt"),
            "canCancel": can_cancel,
            "cancelUntil": cancel_deadline.isoformat(timespec="seconds"),
        })

    live_bookings = [
        booking
        for booking in bookings
        if booking.get("encodedBookingReference")
        and player_id in effective_booking_player_ids(booking)
        and booking_start_datetime(booking) is not None
    ]
    live_bookings.sort(key=lambda booking: (
        booking.get("date") or "",
        booking.get("clubName") or "",
        booking.get("courtId") or 0,
        booking.get("startTime") or "",
    ))
    for booking in live_bookings:
        ref = booking.get("encodedBookingReference")
        if not ref or ref in seen:
            continue
        group = [booking]
        seen.add(ref)
        current = booking
        while True:
            current_start = booking_start_datetime(current)
            if current_start is None:
                break
            next_start = current_start + timedelta(minutes=booking_duration_minutes(current))
            next_booking = next(
                (
                    item
                    for item in live_bookings
                    if item.get("encodedBookingReference") not in seen
                    and item.get("date") == current.get("date")
                    and item.get("courtId") == current.get("courtId")
                    and (item.get("clubName") or "") == (current.get("clubName") or "")
                    and booking_start_datetime(item) == next_start
                ),
                None,
            )
            if next_booking is None:
                break
            group.append(next_booking)
            seen.add(next_booking["encodedBookingReference"])
            current = next_booking

        first_booking = group[0]
        last_booking = group[-1]
        first_start = booking_start_datetime(first_booking)
        if first_start is None:
            continue
        cancel_deadline = first_start - timedelta(hours=72)
        groups.append({
            "id": first_booking.get("encodedBookingReference"),
            "date": first_booking.get("date"),
            "dateNl": format_date_nl(first_booking.get("date")),
            "weekday": format_weekday_nl(first_booking.get("date")),
            "startTime": first_booking.get("startTime"),
            "endTime": (
                booking_start_datetime(last_booking) + timedelta(minutes=booking_duration_minutes(last_booking))
            ).strftime("%H:%M") if booking_start_datetime(last_booking) else format_time_nl(last_booking.get("startTime")),
            "clubName": first_booking.get("clubName"),
            "courtId": first_booking.get("courtId"),
            "courtLabel": court_label(config, first_booking.get("courtId")),
            "bookings": group,
            "bookingReferences": [item.get("encodedBookingReference") for item in group],
            "joinedAt": None,
            "canCancel": datetime.now() <= cancel_deadline,
            "cancelUntil": cancel_deadline.isoformat(timespec="seconds"),
        })
    return groups


def player_signup_payload(invite: dict[str, Any]) -> dict[str, Any]:
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Signup player has no encodedContactId")
    data = client().bookings()
    raw_bookings = data.get("bookings", []) if isinstance(data, dict) else []
    bookings = [normalize_booking(booking) for booking in raw_bookings]
    return {
        "player": invite.get("player") or {},
        "openGroups": player_signup_open_groups_from_bookings(player_id, bookings),
        "signedUpGroups": player_signup_existing_groups(invite, player_id, bookings),
    }


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


def first_name(value: Any) -> str:
    name = str(value or "").strip()
    return name.split()[0] if name else "-"


def court_label(config: AppConfig, court_id: Any) -> str:
    if court_id in (None, ""):
        return "-"
    court_key = str(court_id)
    return config.padel.court_aliases.get(court_key) or f"Court {court_key}"


PUBLIC_PAGE_STYLE = """
  :root { --bg:#f6f7f8; --surface:#fff; --line:#d8dde3; --text:#171a1f; --muted:#66707f; --accent:#0f766e; --danger:#b42318; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { width: min(620px, calc(100% - 32px)); margin: 36px auto; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 12px 30px rgba(15,23,42,.08); padding: 22px; }
  main.wide-main { width: min(760px, calc(100% - 32px)); }
  h1 { margin: 0 0 14px; font-size: 26px; line-height: 1.15; }
  h2 { margin: 22px 0 8px; font-size: 18px; }
  p { color: var(--muted); line-height: 1.5; }
  .notice { color: var(--text); }
  .page-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
  .brand { display: inline-flex; align-items: center; gap: 10px; margin: 0; }
  .brand-icon { display: inline-grid; place-items: center; width: 42px; height: 42px; border: 1px solid #c6ece6; border-radius: 8px; background: #e9fbf7; font-size: 24px; }
  .user-badge { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; background: #fbfcfd; color: var(--text); font-weight: 650; }
  .user-icon { display: inline-grid; place-items: center; width: 28px; height: 28px; border-radius: 50%; background: var(--accent); color: white; font-size: 14px; }
  .tabs { display: flex; gap: 8px; border-bottom: 1px solid var(--line); margin: 8px 0 18px; }
  .tabs { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { border: 0; border-bottom: 3px solid transparent; border-radius: 0; padding: 10px 4px; background: transparent; color: var(--muted); }
  .tab.active { border-color: var(--accent); color: var(--text); font-weight: 700; }
  .meta { display: grid; gap: 10px; border: 1px solid var(--line); border-radius: 7px; background: #fbfcfd; padding: 14px; margin-bottom: 16px; }
  .meta span { display: block; color: var(--muted); font-size: 13px; }
  .meta strong { display: block; margin-top: 3px; color: var(--text); }
  .warning { border: 1px solid #f1c7c2; border-radius: 7px; background: #fff7f6; color: var(--text); padding: 12px; margin: 14px 0; }
  .loader { display: grid; justify-items: center; gap: 10px; border: 1px solid var(--line); border-radius: 7px; background: #fbfcfd; padding: 20px; margin: 16px 0; transition: opacity .18s ease, transform .18s ease; }
  .loader.is-done { opacity: 0; transform: translateY(-4px); }
  .loader-icons { display: flex; min-height: 38px; align-items: center; gap: 12px; font-size: 30px; }
  .loader-icons span { opacity: .18; transform: scale(.82); transition: opacity .18s ease, transform .18s ease; }
  .loader-icons span.active { opacity: 1; transform: scale(1.16); }
  ul { list-style: none; padding: 0; margin: 16px 0; display: grid; gap: 10px; }
  li { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; border: 1px solid var(--line); border-radius: 7px; background: #fbfcfd; padding: 12px; overflow-wrap: anywhere; }
  li span { display: block; margin-top: 4px; color: var(--muted); }
  form { display: inline-block; margin-right: 8px; }
  button { border: 1px solid var(--line); border-radius: 7px; min-height: 42px; padding: 10px 14px; background: white; color: var(--text); cursor: pointer; font: inherit; }
  button.primary, .primary { background: var(--accent); border-color: var(--accent); color: white; }
  button.secondary, .secondary { border-color: var(--line); background: white; color: var(--text); }
  button:disabled { opacity: .55; cursor: not-allowed; }
  .danger { background: var(--danger); border-color: var(--danger); color: white; }
  .wide { width: 100%; margin-top: 16px; }
  .status { display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; background: #fff; color: var(--text); }
  .modal-backdrop { position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 18px; background: rgba(15,23,42,.45); }
  .modal-backdrop[hidden] { display: none; }
  .modal { width: min(430px, 100%); border: 1px solid var(--line); border-radius: 8px; background: var(--surface); box-shadow: 0 18px 45px rgba(15,23,42,.2); padding: 18px; }
  .modal h2 { margin: 0 0 8px; }
  .modal p { margin: 0 0 16px; }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; }
  @media (max-width: 560px) {
    body { background: var(--surface); }
    main, main.wide-main { width: 100%; min-height: 100vh; margin: 0; border: 0; border-radius: 0; box-shadow: none; padding: 16px 14px 72px; }
    h1 { font-size: 23px; }
    .page-head { align-items: flex-start; flex-direction: column; gap: 10px; }
    .brand-icon { width: 38px; height: 38px; font-size: 22px; }
    .user-badge { max-width: 100%; }
    .tabs { margin-inline: -14px; padding-inline: 14px; }
    .tab { flex: 0 0 auto; white-space: nowrap; }
    .loader { padding: 16px 12px; }
    .loader-icons { font-size: 26px; }
    li { grid-template-columns: 1fr; }
    form, button { width: 100%; margin: 0 0 8px; }
    .modal-backdrop { align-items: end; padding: 10px; }
    .modal { max-height: calc(100vh - 20px); overflow: auto; border-radius: 10px; }
    .modal-actions { display: grid; }
  }
"""


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
        <style>{PUBLIC_PAGE_STYLE}</style>
      </head>
      <body>
        <main>
          <h1>&#127934; Padel uitnodiging</h1>
          <div class="meta">
            <div><span>Speler</span><strong>{escape(first_name(player.get("fullName")))}</strong></div>
            <div><span>Datum en tijd</span><strong>{escape(format_date_nl(booking.get("date")))} om {escape(format_time_nl(booking.get("startTime")))}</strong></div>
            <div><span>Locatie</span><strong>{escape(str(booking.get("clubName") or "David Lloyd"))}</strong></div>
            <div><span>Status</span><strong class="status">{escape(status)}</strong></div>
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
          <span>{escape(first_name(player.get("fullName") or player.get("encodedContactId")))}</span>
          <button type="button" data-copy="{escape(first_name(player.get("fullName") or player.get("encodedContactId")))}">Kopieer naam</button>
        </li>
        """
        for player in participants
    )
    action_html = (
        f"""
        <form method="post" action="/takeover/{invite["token"]}/cancel">
          <button class="danger wide" type="submit">Baan annuleren</button>
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
        <style>{PUBLIC_PAGE_STYLE}</style>
      </head>
      <body>
        <main>
          <h1>&#127934; Baan overnemen</h1>
          <p>Deze baan kan door jou worden overgenomen. Annuleer de baan alleen als je direct daarna zelf in de David Lloyd app opnieuw gaat boeken.</p>
          <div class="warning"><strong>Let op:</strong> zodra je annuleert komt de baan vrij. Boek direct opnieuw met de spelers hieronder, anders kan iemand anders de baan reserveren.</div>
          <div class="meta">
            <div><span>Ontvanger</span><strong>{escape(first_name(recipient.get("fullName")))}</strong></div>
            <div><span>Datum en tijd</span><strong>{escape(format_date_nl(booking.get("date")))} om {escape(format_time_nl(booking.get("startTime")))}</strong></div>
            <div><span>Locatie</span><strong>{escape(str(booking.get("clubName") or "David Lloyd"))} - Court {escape(str(booking.get("courtId") or "-"))}</strong></div>
            <div><span>Status</span><strong class="status">{escape(status)}</strong></div>
          </div>
          {notice}
          <h2>Spelers om opnieuw toe te voegen</h2>
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


def render_signup_page(invite: dict[str, Any], *, message: str | None = None, status_code: int = 200) -> HTMLResponse:
    bookings = invite.get("bookings") or [invite.get("booking") or {}]
    player = invite.get("player") or {}
    status = str(invite.get("status") or "")
    can_respond = active_invite(invite)
    booking_rows = "\n".join(
        f"""
        <li>
          <strong>{escape(format_weekday_nl(booking.get("date")).capitalize())} {escape(format_date_nl(booking.get("date")))} om {escape(format_time_nl(booking.get("startTime")))}</strong>
          <span>{escape(str(booking.get("clubName") or "David Lloyd"))} - Court {escape(str(booking.get("courtId") or "-"))}</span>
        </li>
        """
        for booking in bookings
    )
    action_html = (
        f"""
        <form method="post" action="/signup/{invite["token"]}/accept"><button class="primary" type="submit">Inschrijven</button></form>
        <form method="post" action="/signup/{invite["token"]}/reject"><button type="submit">Niet meedoen</button></form>
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
        <title>Inschrijven voor padel</title>
        <style>{PUBLIC_PAGE_STYLE}</style>
      </head>
      <body>
        <main>
          <h1>&#127934; Inschrijven voor padel</h1>
          <p>Je schrijft je in als <strong>{escape(first_name(player.get("fullName")))}</strong>. Als deze boeking uit meerdere aansluitende uren bestaat, word je voor alle onderstaande uren toegevoegd.</p>
          <p>Status: <span class="status">{escape(status)}</span></p>
          <ul>{booking_rows}</ul>
          {notice}
          {action_html}
        </main>
      </body>
    </html>
    """
    return HTMLResponse(body, status_code=status_code)


def render_player_signup_page(
    invite: dict[str, Any],
    groups: list[dict[str, Any]],
    *,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    player = invite.get("player") or {}
    display_name = first_name(player.get("fullName"))
    initial = escape(display_name[:1].upper() or "?")
    notice = f"<p class='notice'>{escape(message)}</p>" if message else ""
    body = f"""
    <!doctype html>
    <html lang="nl">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>&#129302; Padel Bot</title>
        <style>{PUBLIC_PAGE_STYLE}</style>
      </head>
      <body>
        <main class="wide-main">
          <div class="page-head">
            <h1 class="brand"><span class="brand-icon" aria-hidden="true">&#129302;</span><span>Padel Bot</span></h1>
            <div class="user-badge" title="Ingelogd als {escape(display_name)}">
              <span class="user-icon">{initial}</span>
              <span>{escape(display_name)}</span>
            </div>
          </div>
          {notice}
          <nav class="tabs" aria-label="Pagina onderdelen">
            <button class="tab active" type="button" data-tab="matches">&#127934; Reserveringen</button>
            <button class="tab" type="button" data-tab="rules">&#128197; Wekelijks</button>
          </nav>
          <section id="matchesPage" class="tab-page">
            <div id="loader" class="loader">
              <div id="loaderIcons" class="loader-icons" aria-hidden="true"><span>&#127934;</span><span>&#127939;</span><span>&#127934;</span></div>
              <strong>Wedstrijden laden...</strong>
              <span>Even wachten, de padelbanen worden opgehaald.</span>
            </div>
            <section><h2>Jouw inschrijvingen</h2><ul id="signedUpList"></ul></section>
            <section><h2>Open wedstrijden</h2><ul id="openList"></ul></section>
          </section>
          <section id="rulesPage" class="tab-page" hidden>
            <h2>Vaste speelmomenten</h2>
            <p>Beheer je vaste speelmomenten. Er mogen maximaal 4 spelende spelers in een rule staan.</p>
            <ul id="rulesList"></ul>
          </section>
        </main>
        <div id="confirmModal" class="modal-backdrop" hidden>
          <section class="modal" role="dialog" aria-modal="true" aria-labelledby="confirmTitle">
            <h2 id="confirmTitle">Bevestigen</h2>
            <p id="confirmText"></p>
            <div class="modal-actions">
              <button id="confirmCancel" class="secondary" type="button">Terug</button>
              <button id="confirmSubmit" class="primary" type="button">Bevestigen</button>
            </div>
          </section>
        </div>
        <script>
          const token = {invite["token"]!r};
          const loader = document.getElementById("loader");
          const loaderIcons = [...document.querySelectorAll("#loaderIcons span")];
          const openList = document.getElementById("openList");
          const signedUpList = document.getElementById("signedUpList");
          const rulesList = document.getElementById("rulesList");
          const tabs = [...document.querySelectorAll(".tab")];
          const pages = {{ matches: document.getElementById("matchesPage"), rules: document.getElementById("rulesPage") }};
          const confirmModal = document.getElementById("confirmModal");
          const confirmTitle = document.getElementById("confirmTitle");
          const confirmText = document.getElementById("confirmText");
          const confirmCancel = document.getElementById("confirmCancel");
          const confirmSubmit = document.getElementById("confirmSubmit");
          let pendingForm = null;
          let pendingRuleAction = null;
          let loaderIndex = 0;
          const loaderTimer = window.setInterval(() => {{
            loaderIcons.forEach((icon, index) => icon.classList.toggle("active", index === loaderIndex));
            loaderIndex = (loaderIndex + 1) % loaderIcons.length;
          }}, 260);

          function escapeHtml(value) {{
            return String(value ?? "").replace(/[&<>"']/g, (char) => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[char]));
          }}
          function titleCase(value) {{ return value ? value.charAt(0).toUpperCase() + value.slice(1) : "-"; }}
          function groupTitle(group) {{ return `${{titleCase(group.weekday)}} ${{group.dateNl || group.date || "-"}} om ${{group.startTime || "-"}} - ${{group.endTime || "-"}}`; }}
          function ruleTitle(rule) {{ return `${{titleCase(rule.day)}} ${{(rule.times || []).join(", ")}}`; }}

          function renderOpen(groups) {{
            if (!groups.length) {{
              openList.innerHTML = "<li><div><strong>Geen open wedstrijden</strong><span>Er zijn nu geen geboekte wedstrijden met vrije plekken.</span></div></li>";
              return;
            }}
            openList.innerHTML = groups.map((group) => `
              <li>
                <div>
                  <strong>${{escapeHtml(groupTitle(group))}}</strong>
                  <span>${{escapeHtml(group.clubName || "David Lloyd")}} - ${{escapeHtml(group.courtLabel || `Court ${{group.courtId || "-"}}`)}} - ${{(group.bookings || []).length}} uur/blok</span>
                  <span>Bezetting: ${{escapeHtml((group.playerCounts || []).map((count) => `${{count}}/4`).join(", "))}}</span>
                </div>
                <form method="post" action="/signup/player/${{encodeURIComponent(token)}}/join/${{encodeURIComponent(group.id || "")}}" data-confirm-kind="join" data-confirm-title="${{escapeHtml(groupTitle(group))}}">
                  <button class="primary" type="submit">&#9989; Inschrijven</button>
                </form>
              </li>`).join("");
          }}
          function renderSignedUp(groups) {{
            if (!groups.length) {{
              signedUpList.innerHTML = "<li><div><strong>Nog geen inschrijvingen</strong><span>Je staat nog niet op een aankomende wedstrijd via deze link.</span></div></li>";
              return;
            }}
            signedUpList.innerHTML = groups.map((group) => `
              <li>
                <div>
                  <strong>${{escapeHtml(groupTitle(group))}}</strong>
                  <span>${{escapeHtml(group.clubName || "David Lloyd")}} - ${{escapeHtml(group.courtLabel || `Court ${{group.courtId || "-"}}`)}}</span>
                  <span>${{group.canCancel ? "Je kunt nog annuleren tot 72 uur vooraf." : "Annuleren kan niet meer binnen 72 uur voor start."}}</span>
                </div>
                ${{group.canCancel ? `<form method="post" action="/signup/player/${{encodeURIComponent(token)}}/cancel/${{encodeURIComponent(group.id || "")}}" data-confirm-kind="cancel" data-confirm-title="${{escapeHtml(groupTitle(group))}}"><button class="secondary" type="submit">&#10060; Annuleren</button></form>` : ""}}
              </li>`).join("");
          }}
          function renderRules(payload) {{
            const rules = payload.rules || [];
            if (!rules.length) {{
              rulesList.innerHTML = "<li><div><strong>Geen booking rules</strong><span>Er zijn geen vaste speelmomenten ingesteld.</span></div></li>";
              return;
            }}
            const message = payload.message ? `<li><div><strong>Niet aanpasbaar</strong><span>${{escapeHtml(payload.message)}}</span></div></li>` : "";
            rulesList.innerHTML = message + rules.map((rule) => {{
              const countText = `${{rule.playerCount}}/4 spelende spelers`;
              const action = rule.canRemove
                ? `<button class="secondary" type="button" data-rule-action="remove" data-rule-index="${{rule.index}}" data-rule-title="${{escapeHtml(ruleTitle(rule))}}">&#128465; Verwijderen</button>`
                : rule.canAdd
                  ? `<button class="primary" type="button" data-rule-action="add" data-rule-index="${{rule.index}}" data-rule-title="${{escapeHtml(ruleTitle(rule))}}">&#10133; Toevoegen</button>`
                  : `<button class="secondary" type="button" disabled>${{rule.isAdded ? "&#9989; Toegevoegd" : "&#128274; Vol"}}</button>`;
              return `<li><div><strong>${{escapeHtml(ruleTitle(rule))}}</strong><span>Duur: ${{escapeHtml(rule.duration || "-")}} uur - ${{escapeHtml(countText)}}</span></div>${{action}}</li>`;
            }}).join("");
          }}
          async function loadRules() {{
            rulesList.innerHTML = "<li><div><strong>Booking rules laden...</strong><span>Een moment.</span></div></li>";
            try {{
              const response = await fetch(`/signup/player/${{encodeURIComponent(token)}}/rules`, {{ headers: {{ "Accept": "application/json" }} }});
              if (!response.ok) throw new Error(await response.text());
              renderRules(await response.json());
            }} catch (error) {{
              rulesList.innerHTML = `<li><div><strong>Laden mislukt</strong><span>${{escapeHtml(error.message || error)}}</span></div></li>`;
            }}
          }}
          async function updateRule(index, action) {{
            rulesList.innerHTML = "<li><div><strong>Booking rule bijwerken...</strong><span>Een moment.</span></div></li>";
            try {{
              const response = await fetch(`/signup/player/${{encodeURIComponent(token)}}/rules/${{encodeURIComponent(index)}}/${{action}}`, {{ method: "POST", headers: {{ "Accept": "application/json" }} }});
              if (!response.ok) throw new Error(await response.text());
              renderRules(await response.json());
            }} catch (error) {{
              rulesList.innerHTML = `<li><div><strong>Bijwerken mislukt</strong><span>${{escapeHtml(error.message || error)}}</span></div></li>`;
            }}
          }}
          function stopLoader() {{ window.clearInterval(loaderTimer); loader.classList.add("is-done"); window.setTimeout(() => {{ loader.hidden = true; loader.remove(); }}, 180); }}
          function openConfirm(form) {{
            pendingForm = form;
            const isCancel = form.dataset.confirmKind === "cancel";
            const title = form.dataset.confirmTitle || "deze wedstrijd";
            confirmTitle.textContent = isCancel ? "Inschrijving annuleren?" : "Inschrijving bevestigen?";
            confirmText.textContent = isCancel ? `Weet je zeker dat je jouw inschrijving voor ${{title}} wilt annuleren?` : `Wil je je inschrijven voor ${{title}}?`;
            confirmSubmit.textContent = isCancel ? "❌ Annuleren" : "✅ Inschrijven";
            confirmSubmit.className = isCancel ? "danger" : "primary";
            confirmModal.hidden = false;
          }}
          function openRuleConfirm(index, action, title) {{
            pendingForm = null;
            pendingRuleAction = {{ index, action }};
            const isRemove = action === "remove";
            confirmTitle.textContent = isRemove ? "Vast speelmoment verwijderen?" : "Vast speelmoment toevoegen?";
            confirmText.textContent = isRemove
              ? `Wil je jezelf verwijderen uit ${{title}}?`
              : `Wil je jezelf toevoegen aan ${{title}}?`;
            confirmSubmit.textContent = isRemove ? "🗑️ Verwijderen" : "➕ Toevoegen";
            confirmSubmit.className = isRemove ? "danger" : "primary";
            confirmModal.hidden = false;
          }}
          function closeConfirm() {{ pendingForm = null; pendingRuleAction = null; confirmModal.hidden = true; }}
          document.addEventListener("submit", (event) => {{
            const form = event.target.closest("form[data-confirm-kind]");
            if (!form || form.dataset.confirmed === "true") return;
            event.preventDefault(); openConfirm(form);
          }});
          confirmCancel.addEventListener("click", closeConfirm);
          confirmModal.addEventListener("click", (event) => {{ if (event.target === confirmModal) closeConfirm(); }});
          confirmSubmit.addEventListener("click", () => {{
            if (pendingRuleAction) {{
              const current = pendingRuleAction;
              closeConfirm();
              updateRule(current.index, current.action);
              return;
            }}
            if (!pendingForm) return;
            pendingForm.dataset.confirmed = "true";
            pendingForm.submit();
          }});
          tabs.forEach((tab) => {{
            tab.addEventListener("click", () => {{
              const name = tab.dataset.tab;
              tabs.forEach((item) => item.classList.toggle("active", item === tab));
              Object.entries(pages).forEach(([key, page]) => page.hidden = key !== name);
              if (name === "rules" && !rulesList.dataset.loaded) {{ rulesList.dataset.loaded = "true"; loadRules(); }}
            }});
          }});
          rulesList.addEventListener("click", (event) => {{
            const button = event.target.closest("[data-rule-action]");
            if (button) openRuleConfirm(button.dataset.ruleIndex, button.dataset.ruleAction, button.dataset.ruleTitle || "dit vaste speelmoment");
          }});
          async function loadData() {{
            openList.innerHTML = "";
            signedUpList.innerHTML = "";
            try {{
              const response = await fetch(`/signup/player/${{encodeURIComponent(token)}}/data`, {{ headers: {{ "Accept": "application/json" }} }});
              if (!response.ok) throw new Error(await response.text());
              const data = await response.json();
              renderSignedUp(data.signedUpGroups || []);
              renderOpen(data.openGroups || []);
            }} catch (error) {{
              const message = error.message || error;
              openList.innerHTML = `<li><div><strong>Laden mislukt</strong><span>${{escapeHtml(message)}}</span></div></li>`;
            }} finally {{ stopLoader(); }}
          }}
          loadData();
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
        result = update_booking_players_with_ids(
            payload.encodedBookingReference,
            payload.playersEncodedContactIds,
        )
        update_booking_tool_players(payload.encodedBookingReference, payload.toolOnlyPlayerIds)
        return result
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


@app.post("/bookings/signups")
def create_booking_signup(payload: CreateSignupRequest, request: Request) -> dict:
    player = payload.player.model_dump()
    if not player.get("encodedContactId"):
        raise HTTPException(status_code=400, detail="Player has no encodedContactId")
    cfg = load_config()
    bookings = consecutive_bookings_for(payload.booking)
    invite = create_invite(
        encoded_booking_reference=payload.encodedBookingReference,
        player=player,
        booking=payload.booking,
    )
    invite = update_invite(
        invite["token"],
        kind="signup",
        bookings=[
            {
                "date": booking.get("date"),
                "startTime": booking.get("startTime"),
                "duration": booking.get("duration"),
                "courtId": booking.get("courtId"),
                "clubName": booking.get("clubName"),
                "activityName": booking.get("activityName"),
                "encodedBookingReference": booking.get("encodedBookingReference"),
                "players": booking.get("players") or [],
            }
            for booking in bookings
        ],
    )
    signup_url = public_url(cfg, request, "signup_page", token=invite["token"])
    return {"ok": True, "invite": invite, "signupUrl": signup_url}


@app.post("/players/signup-link")
def create_player_signup_link(payload: InvitePlayer, request: Request) -> dict:
    player = payload.model_dump()
    if not player.get("encodedContactId"):
        raise HTTPException(status_code=400, detail="Player has no encodedContactId")
    cfg = load_config()
    existing = next(
        (
            invite
            for invite in read_invites()
            if invite.get("kind") == "player_signup"
            and (invite.get("player") or {}).get("encodedContactId") == player["encodedContactId"]
            and invite.get("status") == ACTIVE_PLAYER_SIGNUP_STATUS
        ),
        None,
    )
    if existing:
        signup_url = public_url(cfg, request, "player_signup_page", token=existing["token"])
        return {"ok": True, "invite": existing, "signupUrl": signup_url}

    invite = create_invite(
        encoded_booking_reference="",
        player=player,
        booking={},
    )
    invite = update_invite(invite["token"], kind="player_signup", status=ACTIVE_PLAYER_SIGNUP_STATUS, signups=[])
    signup_url = public_url(cfg, request, "player_signup_page", token=invite["token"])
    return {"ok": True, "invite": invite, "signupUrl": signup_url}


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


@app.get("/signup/{token}", response_class=HTMLResponse)
def signup_page(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    changes: dict[str, Any] = {"openCount": int(invite.get("openCount") or 0) + 1}
    if not invite.get("openedAt"):
        changes["openedAt"] = datetime.now().isoformat(timespec="seconds")
    invite = update_invite(token, **changes)
    message = None if active_invite(invite) else "Deze inschrijflink is niet meer actief."
    return render_signup_page(invite, message=message)


@app.get("/signup/player/{token}", response_class=HTMLResponse)
def player_signup_page(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "player_signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    changes: dict[str, Any] = {"openCount": int(invite.get("openCount") or 0) + 1}
    if not invite.get("openedAt"):
        changes["openedAt"] = datetime.now().isoformat(timespec="seconds")
    invite = update_invite(token, **changes)
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Signup player has no encodedContactId")
    return render_player_signup_page(invite, [])


@app.get("/signup/player/{token}/data")
def player_signup_data(token: str) -> dict:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "player_signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    if invite.get("status") != ACTIVE_PLAYER_SIGNUP_STATUS:
        raise HTTPException(status_code=409, detail="Signup link is not active")
    return player_signup_payload(invite)


@app.get("/signup/player/{token}/rules")
def player_signup_rules(token: str) -> dict:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "player_signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    if invite.get("status") != ACTIVE_PLAYER_SIGNUP_STATUS:
        raise HTTPException(status_code=409, detail="Signup link is not active")
    return player_signup_rules_payload(invite)


@app.post("/signup/player/{token}/rules/{rule_index}/add")
def player_signup_rule_add(token: str, rule_index: int) -> dict:
    return update_player_booking_rule(token, rule_index, "add")


@app.post("/signup/player/{token}/rules/{rule_index}/remove")
def player_signup_rule_remove(token: str, rule_index: int) -> dict:
    return update_player_booking_rule(token, rule_index, "remove")


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


@app.post("/signup/{token}/accept", response_class=HTMLResponse)
def accept_signup(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    if not active_invite(invite):
        return render_signup_page(invite, message="Deze inschrijflink is al verwerkt of ingetrokken.", status_code=409)
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Signup player has no encodedContactId")

    updated_bookings = []
    signup_bookings = []
    for booking in invite.get("bookings") or [invite.get("booking") or {}]:
        booking_ref = booking.get("encodedBookingReference")
        if not booking_ref:
            continue
        current = find_booking(booking_ref) or booking
        signup_bookings.append(current)
    blockers = booking_rule_add_blockers(player_id, signup_bookings)
    if blockers:
        return render_signup_page(
            invite,
            message=(
                "Deze booking rule heeft al 4 spelende spelers in de config. "
                "Verwijder eerst een speler uit de rule of zet een member op 'Speelt niet mee'."
            ),
            status_code=409,
        )

    for current in signup_bookings:
        booking_ref = current.get("encodedBookingReference")
        if not booking_ref:
            continue
        effective_ids = effective_booking_player_ids(current)
        if player_id not in effective_ids:
            player_ids, can_update_david_lloyd = update_ids_for_signup(current, player_id)
            if not can_update_david_lloyd:
                update_invite(token, status="full")
                return render_signup_page(
                    get_invite(token) or invite,
                    message="Je inschrijving past in de tool, maar David Lloyd heeft geen vrije spelerplek omdat een niet-spelende boeker technisch in de boeking blijft staan.",
                    status_code=409,
                )
            result = update_booking_players_with_ids(booking_ref, player_ids)
            updated_bookings.append(result.get("booking") or current)
        else:
            updated_bookings.append(current)
        add_player_to_booking_rules(player_id, current)

    update_invite(token, status="accepted", acceptedAt=datetime.now().isoformat(timespec="seconds"))
    return render_signup_page(
        get_invite(token) or invite,
        message=f"Je bent toegevoegd aan {len(updated_bookings)} boeking(en).",
    )


@app.post("/signup/{token}/reject", response_class=HTMLResponse)
def reject_signup(token: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    if not active_invite(invite):
        return render_signup_page(invite, message="Deze inschrijflink is al verwerkt of ingetrokken.", status_code=409)
    update_invite(token, status="rejected")
    return render_signup_page(get_invite(token) or invite, message="Je hebt de inschrijving geweigerd.")


@app.post("/signup/player/{token}/cancel/{booking_reference:path}", response_class=HTMLResponse)
def cancel_player_signup(token: str, booking_reference: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "player_signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    if invite.get("status") != ACTIVE_PLAYER_SIGNUP_STATUS:
        raise HTTPException(status_code=409, detail="Signup link is not active")
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Signup player has no encodedContactId")

    signups = list(invite.get("signups") or [])
    signup_index = next(
        (
            index
            for index, signup in enumerate(signups)
            if not signup.get("cancelledAt")
            and booking_reference in (signup.get("bookingReferences") or [])
        ),
        None,
    )
    if signup_index is None:
        base = find_booking(booking_reference)
        if base is None or player_id not in effective_booking_player_ids(base):
            return render_player_signup_page(invite, [], message="Deze inschrijving is niet gevonden.", status_code=404)
        bookings = consecutive_bookings_for(base)
        bookings = [
            booking
            for booking in bookings
            if player_id in effective_booking_player_ids(booking)
        ]
    else:
        bookings = [
            find_booking(ref)
            for ref in signups[signup_index].get("bookingReferences") or []
        ]
    bookings = [booking for booking in bookings if booking]
    first_start = min((booking_start_datetime(booking) for booking in bookings), default=None)
    if first_start is None:
        return render_player_signup_page(invite, [], message="Deze wedstrijd bestaat niet meer.", status_code=404)
    if datetime.now() > first_start - timedelta(hours=72):
        return render_player_signup_page(
            invite,
            [],
            message="Annuleren kan niet meer binnen 72 uur voor de start.",
            status_code=409,
        )

    for booking in bookings:
        ids = [
            current_id
            for current_id in booking_player_ids(booking)
            if current_id != player_id
        ]
        if len(ids) != len(booking_player_ids(booking)):
            update_booking_players_with_ids(booking["encodedBookingReference"], ids)
        remove_player_from_booking_rules(player_id, booking)

    if signup_index is not None:
        signups[signup_index]["cancelledAt"] = datetime.now().isoformat(timespec="seconds")
        invite = update_invite(token, signups=signups)
    return render_player_signup_page(invite, [], message="Je inschrijving is geannuleerd.")


@app.post("/signup/player/{token}/join/{booking_reference:path}", response_class=HTMLResponse)
def join_player_signup(token: str, booking_reference: str) -> HTMLResponse:
    invite = get_invite(token)
    if invite is None or invite.get("kind") != "player_signup":
        raise HTTPException(status_code=404, detail="Signup link not found")
    if invite.get("status") != ACTIVE_PLAYER_SIGNUP_STATUS:
        raise HTTPException(status_code=409, detail="Signup link is not active")
    player_id = (invite.get("player") or {}).get("encodedContactId")
    if not player_id:
        raise HTTPException(status_code=400, detail="Signup player has no encodedContactId")

    base = find_booking(booking_reference)
    if base is None:
        return render_player_signup_page(invite, [], message="Deze wedstrijd bestaat niet meer.", status_code=404)

    group = consecutive_bookings_for(base)
    for booking in group:
        player_ids = effective_booking_player_ids(booking)
        if player_id in player_ids:
            continue
        if len(player_ids) >= 4:
            return render_player_signup_page(invite, [], message="Deze wedstrijd zit inmiddels vol.", status_code=409)

    blockers = booking_rule_add_blockers(player_id, group)
    if blockers:
        return render_player_signup_page(
            invite,
            [],
            message=(
                "Deze booking rule heeft al 4 spelende spelers in de config. "
                "Verwijder eerst een speler uit de rule of zet een member op 'Speelt niet mee'."
            ),
            status_code=409,
        )

    updated = []
    not_added_to_david_lloyd = []
    config_rule_updates = []
    for booking in group:
        effective_ids = effective_booking_player_ids(booking)
        if player_id not in effective_ids:
            player_ids, can_update_david_lloyd = update_ids_for_signup(booking, player_id)
            if can_update_david_lloyd:
                result = update_booking_players_with_ids(booking["encodedBookingReference"], player_ids)
                updated.append(result.get("booking") or booking)
            else:
                not_added_to_david_lloyd.append(booking)
                updated.append(booking)
        else:
            updated.append(booking)
        config_rule_updates.append(add_player_to_booking_rules(player_id, booking))

    signups = list(invite.get("signups") or [])
    signups.append({
        "joinedAt": datetime.now().isoformat(timespec="seconds"),
        "bookingReferences": [booking.get("encodedBookingReference") for booking in updated],
        "configRuleUpdates": config_rule_updates,
    })
    invite = update_invite(token, signups=signups)
    if not_added_to_david_lloyd:
        return render_player_signup_page(
            invite,
            [],
            message=(
                "Je bent ingeschreven in de tool, maar nog niet toegevoegd in David Lloyd. "
                "Er staat nog een niet-spelende boeker in de boeking. Dit moet later via overname/takeover worden rechtgezet."
            ),
        )
    return render_player_signup_page(
        invite,
        [],
        message=f"Je bent ingeschreven voor {len(updated)} uur/blok(ken).",
    )


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
