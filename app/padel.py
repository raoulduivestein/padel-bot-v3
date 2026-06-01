from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.config import PadelConfig
from app.davidlloyd_client import DavidLloydClient, DavidLloydError


@dataclass
class Slot:
    date: str
    time: str
    player_ids: list[str] | None = None


def generate_slots(config: PadelConfig) -> list[Slot]:
    target_date = datetime.today() + timedelta(days=config.days_ahead)
    weekday = target_date.strftime("%A").lower()
    slots: list[Slot] = []

    for rule in config.booking_rules:
        if rule.day.lower() != weekday:
            continue
        for base_time in rule.times:
            hour, minute = map(int, base_time.split(":"))
            for offset in range(rule.duration):
                slots.append(
                    Slot(
                        date=target_date.strftime("%Y-%m-%d"),
                        time=f"{hour + offset:02d}:{minute:02d}",
                        player_ids=list(rule.player_ids),
                    )
                )

    return slots


class PadelBookingService:
    def __init__(self, client: DavidLloydClient, config: PadelConfig):
        self.client = client
        self.config = config

    def slots(self) -> list[dict[str, str]]:
        return [slot.__dict__ for slot in generate_slots(self.config)]

    def availability(self, *, date: str, member_id: str | None = None) -> dict:
        member = self._member(member_id)
        path = f"/clubs/{self.config.club_id}/court-slots/{date}/{self.config.sports_package_id}"
        return self.client.mobile_get(
            path,
            params={"encodedContactId": member["member_id"]},
        )

    def available_courts_for_slots(self, slots: list[Slot]) -> dict[str, list[int]]:
        if not slots:
            return {}

        member = self.config.members[0]
        data = self.availability(date=slots[0].date, member_id=member.member_id)
        result: dict[str, list[int]] = {}

        for slot in slots:
            result[slot.time] = [
                int(court_slot["courtId"])
                for court_slot in data.get("slots", [])
                if court_slot.get("startTime") == slot.time and court_slot.get("courtId") is not None
            ]

        return result

    def book_generated_slots(self, *, attempts: int = 1) -> dict:
        slots = generate_slots(self.config)
        if not slots:
            return {"ok": False, "reason": "No slots generated", "results": []}

        results: list[dict[str, Any]] = []
        booked_any = False
        for attempt in range(attempts):
            attempt_result = self.book_slots(slots)
            attempt_result["attempt"] = attempt + 1
            results.append(attempt_result)
            booked_any = bool(attempt_result.get("ok"))

            if booked_any:
                break

        return {"ok": booked_any, "results": results}

    def book_slots(self, slots: list[Slot]) -> dict:
        availability = self.available_courts_for_slots(slots)
        selected = self.select_courts_smart(slots, availability)

        if not selected:
            return {
                "ok": False,
                "reason": "No courts available",
                "availability": availability,
            }

        max_workers = max(1, len(selected))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for index, (slot, court_id) in enumerate(selected):
                member = self.config.members[index % len(self.config.members)]
                futures.append(
                    executor.submit(self.try_book, slot=slot, court_id=court_id, member_id=member.member_id)
                )
            booking_results = [future.result() for future in futures]

        return {
            "ok": any(result.get("ok") for result in booking_results),
            "availability": availability,
            "selected": [
                {"date": slot.date, "time": slot.time, "court_id": court_id}
                for slot, court_id in selected
            ],
            "bookings": booking_results,
        }

    def try_book(self, *, slot: Slot, court_id: int | None = None, member_id: str | None = None) -> dict:
        member = self._member(member_id)
        if court_id is None:
            availability = self.availability(date=slot.date, member_id=member["member_id"])
            court_id = self.select_court(availability, slot.time)
        if not court_id:
            return {
                "ok": False,
                "date": slot.date,
                "time": slot.time,
                "member": member["name"],
                "reason": "No court available",
            }

        create_payload = {
            "bookedMemberEncodedContactId": member["member_id"],
            "courtId": court_id,
            "date": slot.date,
            "startTime": slot.time,
            "sportsPackageId": self.config.sports_package_id,
            "playersEncodedContactIds": [],
        }
        created = self.client.mobile_post(
            f"/clubs/{self.config.club_id}/bookings/court",
            payload=create_payload,
        )
        booking_ref = created.get("encodedBookingReference")
        if not booking_ref:
            raise DavidLloydError("Booking create response did not contain encodedBookingReference", body=created)

        self.update_booking_players(booking_ref, member, slot.player_ids or [])

        confirmation_type = self.config.booking_confirmation_type or "confirmed"
        confirmed = self.client.mobile_post(
            f"/clubs/{self.config.club_id}/members/me/bookings/{booking_ref}/confirmCourt?return-booking=true",
            payload={"courtConfirmationType": confirmation_type},
        )
        return {
            "ok": True,
            "date": slot.date,
            "time": slot.time,
            "member": member["name"],
            "court_id": court_id,
            "booking_reference": booking_ref,
            "booking": confirmed,
        }

    def update_booking_players(self, booking_ref: str, member: dict[str, str], rule_player_ids: list[str]) -> dict:
        all_player_ids = [member["member_id"], *self.build_booking_player_ids(member, rule_player_ids)]
        if len(all_player_ids) > 4:
            raise DavidLloydError(
                "A booking can have at most 4 players",
                body={"playersEncodedContactIds": all_player_ids},
            )
        return self.client.mobile_put(
            f"/clubs/{self.config.club_id}/members/me/bookings/{booking_ref}/players?return-booking=true",
            payload={"playersEncodedContactIds": all_player_ids},
        )

    def build_booking_player_ids(self, booked_member: dict[str, str], rule_player_ids: list[str]) -> list[str]:
        booked_member_id = booked_member["member_id"]
        seen = {booked_member_id}
        player_ids: list[str] = []

        configured_ids = [
            member.member_id
            for member in self.config.members
            if member.member_id != booked_member_id and member.plays
        ]

        for player_id in [*configured_ids, *self.config.always_add_player_ids, *rule_player_ids]:
            if player_id and player_id not in seen:
                player_ids.append(player_id)
                seen.add(player_id)

        return player_ids

    def select_court(self, availability: dict, target_time: str) -> int | None:
        available = [
            slot.get("courtId")
            for slot in availability.get("slots", [])
            if slot.get("startTime") == target_time and slot.get("courtId") is not None
        ]

        for preferred in self.config.preferred_courts:
            if preferred in available:
                return preferred

        if self.config.fallback_to_any and available:
            return int(available[0])

        return None

    def select_courts_smart(
        self,
        slots: list[Slot],
        availability: dict[str, list[int]],
    ) -> list[tuple[Slot, int]] | None:
        common: set[int] | None = None

        for slot in slots:
            courts = set(availability.get(slot.time, []))
            common = courts if common is None else common.intersection(courts)

        if common:
            for preferred_court in self.config.preferred_courts:
                if preferred_court in common:
                    return [(slot, preferred_court) for slot in slots]
            chosen = next(iter(common))
            return [(slot, chosen) for slot in slots]

        selected: list[tuple[Slot, int]] = []
        used: set[int] = set()

        for slot in slots:
            possible = availability.get(slot.time, [])
            for preferred_court in self.config.preferred_courts:
                if preferred_court in possible and preferred_court not in used:
                    selected.append((slot, preferred_court))
                    used.add(preferred_court)
                    break
            else:
                for court_id in possible:
                    if court_id not in used:
                        selected.append((slot, court_id))
                        used.add(court_id)
                        break

        if len(selected) == len(slots):
            return selected

        for slot in slots:
            possible = availability.get(slot.time, [])
            if possible:
                return [(slot, int(possible[0]))]

        return None

    def _member(self, member_id: str | None = None) -> dict[str, str]:
        if not self.config.members:
            raise DavidLloydError("No members configured")

        if member_id is None:
            member = self.config.members[0]
            return {"member_id": member.member_id, "name": member.name}

        for member in self.config.members:
            if member.member_id == member_id:
                return {"member_id": member.member_id, "name": member.name}

        raise DavidLloydError(f"Unknown member_id: {member_id}")
