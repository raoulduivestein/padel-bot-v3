from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from app.config import ROOT


STATE_DIR = ROOT / "state"
INVITES_PATH = STATE_DIR / "invites.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_raw() -> dict[str, dict[str, Any]]:
    if not INVITES_PATH.exists():
        return {}
    with INVITES_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _write_raw(invites: dict[str, dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with INVITES_PATH.open("w", encoding="utf-8") as handle:
        json.dump(invites, handle, indent=2, sort_keys=True)


def create_invite(
    *,
    encoded_booking_reference: str,
    player: dict[str, Any],
    booking: dict[str, Any],
) -> dict[str, Any]:
    token = secrets.token_urlsafe(24)
    invites = _read_raw()
    invite = {
        "token": token,
        "status": "pending",
        "encodedBookingReference": encoded_booking_reference,
        "player": player,
        "booking": {
            "date": booking.get("date"),
            "startTime": booking.get("startTime"),
            "courtId": booking.get("courtId"),
            "clubName": booking.get("clubName"),
            "activityName": booking.get("activityName"),
        },
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    invites[token] = invite
    _write_raw(invites)
    return invite


def get_invite(token: str) -> dict[str, Any] | None:
    return _read_raw().get(token)


def read_invites() -> list[dict[str, Any]]:
    invites = _read_raw()
    return sorted(invites.values(), key=lambda invite: invite.get("createdAt") or "", reverse=True)


def update_invite(token: str, **changes: Any) -> dict[str, Any]:
    invites = _read_raw()
    invite = invites.get(token)
    if invite is None:
        raise KeyError(token)
    invite.update(changes)
    invite["updatedAt"] = _now()
    invites[token] = invite
    _write_raw(invites)
    return invite


def cancel_invite(token: str) -> dict[str, Any]:
    return update_invite(token, status="cancelled")
