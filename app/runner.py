from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

from app.config import load_config
from app.davidlloyd_client import DavidLloydClient, DavidLloydError
from app.padel import PadelBookingService


def parse_time(value: str) -> tuple[int, int, int]:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0], parts[1], 0
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"Invalid time: {value}")


def next_datetime(value: str) -> datetime:
    hour, minute, second = parse_time(value)
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def wait_until(target: datetime) -> None:
    while True:
        seconds = (target - datetime.now()).total_seconds()
        if seconds <= 0:
            return
        time.sleep(min(seconds, 1))


def run_once(*, attempts: int | None = None, wait: bool = False) -> dict:
    config = load_config()
    client = DavidLloydClient(config)
    service = PadelBookingService(client, config.padel)
    run_attempts = attempts if attempts is not None else 10

    if wait:
        prep = next_datetime(config.padel.run_time.get("prep", "07:59:55"))
        booking = next_datetime(config.padel.run_time.get("booking", "08:00:00"))
        wait_until(prep)
        try:
            client.refresh_token()
            client.refresh_hmac()
        except DavidLloydError:
            client.login()
        wait_until(booking)
    else:
        try:
            client.refresh_token()
            client.refresh_hmac()
        except DavidLloydError:
            client.login()

    return service.book_generated_slots(attempts=run_attempts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the padel booking flow.")
    parser.add_argument("--attempts", type=int, default=None)
    parser.add_argument("--wait", action="store_true", help="Wait for config.padel.run_time before booking.")
    args = parser.parse_args()

    result = run_once(attempts=args.attempts, wait=args.wait)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
