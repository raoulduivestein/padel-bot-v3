from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.json"
STATE_DIR = ROOT / "state"
STATE_PATH = STATE_DIR / "session.json"


SignatureMode = Literal[
    "davidlloyd_v1",
    "method_path_timestamp_body",
    "timestamp_body",
    "timestamp_method_path_body",
]


class MemberConfig(BaseModel):
    member_id: str
    name: str


class BookingRule(BaseModel):
    day: str
    times: list[str]
    duration: int = Field(ge=1, le=4)


class PadelConfig(BaseModel):
    run_time: dict[str, str] = {}
    club_id: int
    sports_package_id: int
    days_ahead: int = Field(ge=0)
    members: list[MemberConfig]
    always_add_player_ids: list[str] = []
    preferred_courts: list[int] = []
    fallback_to_any: bool = True
    booking_rules: list[BookingRule]


def default_padel_config() -> PadelConfig:
    return PadelConfig(
        run_time={"prep": "07:59:55", "booking": "08:00:00"},
        days_ahead=6,
        club_id=94,
        sports_package_id=63,
        members=[
            MemberConfig(member_id="bUpDTmkxR2kwMDZWNUk2SVQ5QithUQ==", name="Marvin"),
            MemberConfig(member_id="ZktCelEvaGFZRStuaW9kNUE0cytpdw==", name="Senn"),
        ],
        always_add_player_ids=["SHlQK3EvQVU3VXk4QTFraXN1WWoxdw=="],
        preferred_courts=[737381, 737383, 737385],
        fallback_to_any=True,
        booking_rules=[
            BookingRule(day="monday", times=["21:00"], duration=2),
            BookingRule(day="tuesday", times=["18:00"], duration=2),
            BookingRule(day="wednesday", times=["18:00"], duration=2),
            BookingRule(day="thursday", times=["19:00"], duration=2),
            BookingRule(day="friday", times=["08:00"], duration=1),
            BookingRule(day="saturday", times=["07:00"], duration=1),
            BookingRule(day="sunday", times=["10:00"], duration=2),
        ],
    )


class AppConfig(BaseModel):
    username: str
    password: str
    device_id: str = Field(min_length=8)
    user_agent: str
    okta_user_agent: str
    okta_authn_user_agent: str
    client_id: str
    redirect_uri: str
    scope: str
    signature_mode: SignatureMode = "davidlloyd_v1"
    padel: PadelConfig = Field(default_factory=default_padel_config)


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing config file: {path}. Copy config/config.example.json to config/config.json."
        )
    with path.open("r", encoding="utf-8") as handle:
        return AppConfig.model_validate(json.load(handle))


def write_config(config: AppConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(config.model_dump(), handle, indent=2)
    tmp.replace(path)


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    with STATE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    tmp.replace(STATE_PATH)
