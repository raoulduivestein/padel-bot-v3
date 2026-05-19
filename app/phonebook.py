from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import ROOT


STATE_DIR = ROOT / "state"
PHONEBOOK_PATH = STATE_DIR / "phonebook.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_raw() -> dict[str, dict[str, Any]]:
    if not PHONEBOOK_PATH.exists():
        return {}
    with PHONEBOOK_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _write_raw(entries: dict[str, dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PHONEBOOK_PATH.open("w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2, sort_keys=True)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def read_phonebook() -> list[dict[str, Any]]:
    entries = _read_raw()
    return sorted(
        entries.values(),
        key=lambda entry: (entry.get("fullName") or entry.get("encodedContactId") or "").lower(),
    )


def upsert_player(
    *,
    encoded_contact_id: str,
    full_name: str | None = None,
    member_reference_number: str | None = None,
    home_club_site_id: int | None = None,
    source: str = "tool",
) -> dict[str, Any]:
    player_id = _clean(encoded_contact_id)
    if not player_id:
        raise ValueError("encodedContactId is required")

    entries = _read_raw()
    current = entries.get(player_id, {})
    created_at = current.get("createdAt") or _now()
    sources = set(current.get("sources") or [])
    sources.add(source)

    entry = {
        "encodedContactId": player_id,
        "fullName": _clean(full_name) or current.get("fullName") or player_id,
        "memberReferenceNumber": _clean(member_reference_number) or current.get("memberReferenceNumber"),
        "homeClubSiteId": home_club_site_id if home_club_site_id is not None else current.get("homeClubSiteId"),
        "phone": current.get("phone") or "",
        "notes": current.get("notes") or "",
        "sources": sorted(sources),
        "createdAt": created_at,
        "updatedAt": _now(),
    }
    entries[player_id] = entry
    _write_raw(entries)
    return entry


def update_entry(
    *,
    encoded_contact_id: str,
    full_name: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    player_id = _clean(encoded_contact_id)
    if not player_id:
        raise ValueError("encodedContactId is required")

    entries = _read_raw()
    current = entries.get(player_id, {})
    entry = {
        "encodedContactId": player_id,
        "fullName": _clean(full_name) or current.get("fullName") or player_id,
        "memberReferenceNumber": current.get("memberReferenceNumber"),
        "homeClubSiteId": current.get("homeClubSiteId"),
        "phone": "" if phone is None else str(phone).strip(),
        "notes": "" if notes is None else str(notes).strip(),
        "sources": current.get("sources") or ["manual"],
        "createdAt": current.get("createdAt") or _now(),
        "updatedAt": _now(),
    }
    entries[player_id] = entry
    _write_raw(entries)
    return entry


def sync_booking_players(bookings: list[dict[str, Any]]) -> None:
    for booking in bookings:
        details = booking.get("details") or {}
        players = details.get("players") or booking.get("players") or []
        for player in players:
            if not isinstance(player, dict):
                continue
            player_id = player.get("encodedContactId")
            if player_id:
                upsert_player(
                    encoded_contact_id=player_id,
                    full_name=player.get("fullName") or player.get("name"),
                    member_reference_number=player.get("memberReferenceNumber"),
                    home_club_site_id=player.get("homeClubSiteId"),
                    source="booking",
                )


def sync_config_players(config: Any) -> None:
    padel = config.padel
    known = padel.known_players or {}
    for member in padel.members:
        upsert_player(encoded_contact_id=member.member_id, full_name=member.name, source="config")
    for player_id in padel.always_add_player_ids:
        upsert_player(encoded_contact_id=player_id, full_name=known.get(player_id), source="config")
    for rule in padel.booking_rules:
        for player_id in rule.player_ids:
            upsert_player(encoded_contact_id=player_id, full_name=known.get(player_id), source="config")
