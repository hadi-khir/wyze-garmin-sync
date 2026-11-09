#!/usr/bin/env python3
"""
wyze-garmin-sync: copy Wyze Scale weigh-ins to Garmin Connect.

Usage:
    python sync.py --login            # one-time interactive Garmin login (handles MFA)
    python sync.py --dry-run          # show what would be uploaded, upload nothing
    python sync.py                    # sync new weigh-ins since the last run
    python sync.py --days 90          # backfill the last 90 days
    python sync.py --list-scales      # show your Wyze scales and their models

Config is read from environment variables (or a .env file next to this script).
See .env.example.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
log = logging.getLogger("wyze-garmin-sync")


# --------------------------------------------------------------------------- config

def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so we don't need an extra dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required setting {name}. Add it to .env (see .env.example).")
    return val


# --------------------------------------------------------------------------- state

class State:
    """Remembers which Wyze records were already uploaded, so runs are idempotent."""

    def __init__(self, path: Path):
        self.path = path
        data = json.loads(path.read_text()) if path.exists() else {}
        self.synced: dict[str, str] = data.get("synced", {})  # wyze record id -> iso time
        self.last_measure_ts: int | None = data.get("last_measure_ts")

    def has(self, record_id: str) -> bool:
        return record_id in self.synced

    def mark(self, record_id: str, measure_ts: int) -> None:
        self.synced[record_id] = datetime.now(timezone.utc).isoformat()
        if self.last_measure_ts is None or measure_ts > self.last_measure_ts:
            self.last_measure_ts = measure_ts
        self.save()  # save after every upload so a crash never causes duplicates

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"synced": self.synced, "last_measure_ts": self.last_measure_ts}, indent=2))
        tmp.replace(self.path)


# --------------------------------------------------------------------------- data model

@dataclass
class WeighIn:
    record_id: str
    measured_at: datetime          # timezone-aware, UTC
    measure_ts_ms: int
    weight_kg: float
    percent_fat: float | None = None
    percent_hydration: float | None = None
    bone_mass_kg: float | None = None
    muscle_mass_kg: float | None = None
    basal_met: float | None = None
    metabolic_age: float | None = None
    visceral_fat_rating: float | None = None
    physique_rating: float | None = None
    bmi: float | None = None

    def summary(self) -> str:
        local = self.measured_at.astimezone()
        parts = [f"{local:%Y-%m-%d %H:%M}", f"{self.weight_kg:.2f} kg ({self.weight_kg * 2.20462:.1f} lb)"]
        if self.percent_fat is not None:
            parts.append(f"fat {self.percent_fat:.1f}%")
        if self.muscle_mass_kg is not None:
            parts.append(f"muscle {self.muscle_mass_kg:.1f} kg")
        if self.percent_hydration is not None:
            parts.append(f"water {self.percent_hydration:.1f}%")
        if self.bmi is not None:
            parts.append(f"BMI {self.bmi:.1f}")
        return ", ".join(parts)


def _num(value) -> float | None:
    """Wyze returns 0 or None when a metric wasn't measured (e.g. wearing socks)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _pct(value) -> float | None:
    v = _num(value)
    return v if v is not None and 0 < v < 100 else None


def to_weigh_in(rec, muscle_field: str) -> WeighIn | None:
    weight = _num(getattr(rec, "_weight", None))  # raw value from the API, in kg
    ts = getattr(rec, "measure_ts", None)
    if weight is None or ts is None:
        return None
    ts = int(ts)
    ts_ms = ts if ts > 10**12 else ts * 1000  # tolerate seconds or milliseconds
    measured_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    muscle = _num(getattr(rec, muscle_field, None))
    bone = _num(getattr(rec, "bone_mineral", None))
    # Guard against fields that turn out to be percentages instead of kg.
    if muscle is not None and muscle >= weight:
        muscle = None
    if bone is not None and bone >= weight:
        bone = None

    physique = _num(getattr(rec, "body_type", None))
    if physique is not None and not (1 <= physique <= 9):
        physique = None

    return WeighIn(
        record_id=str(getattr(rec, "id", None) or ts_ms),
        measured_at=measured_at,
        measure_ts_ms=ts_ms,
        weight_kg=round(weight, 2),
        percent_fat=_pct(getattr(rec, "body_fat", None)),
        percent_hydration=_pct(getattr(rec, "body_water", None)),
        bone_mass_kg=bone,
        muscle_mass_kg=muscle,
        basal_met=_num(getattr(rec, "bmr", None)),
        metabolic_age=_num(getattr(rec, "metabolic_age", None)),
        visceral_fat_rating=_num(getattr(rec, "body_vfr", None)),
        physique_rating=physique,
        bmi=_num(getattr(rec, "bmi", None)),
    )


# --------------------------------------------------------------------------- Wyze

def wyze_client():
    from wyze_sdk import Client

    return Client(
        email=env("WYZE_EMAIL", required=True),
        password=env("WYZE_PASSWORD", required=True),
        key_id=env("WYZE_KEY_ID", required=True),
        api_key=env("WYZE_API_KEY", required=True),
        totp_key=env("WYZE_TOTP_KEY") or None,
    )


def pick_scale_model(client) -> str:
    override = env("WYZE_SCALE_MODEL")
    if override:
        return override
    scales = client.scales.list()
    if not scales:
        sys.exit("No Wyze scale found on this account.")
    if len(scales) > 1:
        log.info("Several scales found; using %s (%s). Set WYZE_SCALE_MODEL to choose.",
                 scales[0].nickname, scales[0].product.model)
    return scales[0].product.model


def fetch_wyze(client, model: str, since: datetime, until: datetime, user_id: str | None):
    """Fetch records in 30-day windows (the API returns a range per call)."""
    records, window = [], timedelta(days=30)
    start = since
    while start < until:
        end = min(start + window, until)
        batch = client.scales.get_records(
            device_model=model, user_id=user_id, start_time=start, end_time=end)
        log.debug("Wyze %s -> %s: %d records", start, end, len(batch))
        records.extend(batch)
        start = end
    return records


# --------------------------------------------------------------------------- Garmin

def garmin_token_dir() -> str:
    return os.path.expanduser(env("GARMIN_TOKEN_DIR", "~/.garminconnect"))


def garmin_login_interactive() -> None:
    from getpass import getpass
    from garminconnect import Garmin

    email = env("GARMIN_EMAIL") or input("Garmin email: ")
    password = env("GARMIN_PASSWORD") or getpass("Garmin password: ")
    client = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: "))
    client.login(garmin_token_dir())
    print(f"Logged in to Garmin as {client.get_full_name()}. Tokens saved to {garmin_token_dir()}.")


def garmin_client():
    from garminconnect import Garmin

    token_dir = garmin_token_dir()
    try:
        client = Garmin()
        client.login(token_dir)
        return client
    except Exception as exc:  # tokens missing/expired: fall back to credentials if given
        email, password = env("GARMIN_EMAIL"), env("GARMIN_PASSWORD")
        if not (email and password):
            sys.exit(f"Garmin login failed ({exc}). Run `python sync.py --login` once first.")
        log.info("Saved Garmin tokens didn't work; logging in with credentials.")
        client = Garmin(email, password)  # no MFA prompt possible when unattended
        client.login(token_dir)
        return client


def garmin_existing_times(client, since: datetime, until: datetime) -> list[int]:
    """Timestamps (ms, UTC) of weigh-ins already in Garmin, to avoid duplicates."""
    try:
        data = client.get_body_composition(
            since.astimezone().date().isoformat(), until.astimezone().date().isoformat())
    except Exception as exc:
        log.warning("Couldn't read existing Garmin weigh-ins (%s); relying on local state.", exc)
        return []
    times = []
    for item in (data or {}).get("dateWeightList", []) or []:
        for key in ("timestampGMT", "date"):
            if isinstance(item.get(key), (int, float)):
                times.append(int(item[key]))
                break
    return times


def upload(client, w: WeighIn) -> None:
    client.add_body_composition(
        w.measured_at.isoformat(),  # timezone-aware, so Garmin places it at the right time
        weight=w.weight_kg,
        percent_fat=w.percent_fat,
        percent_hydration=w.percent_hydration,
        bone_mass=w.bone_mass_kg,
        muscle_mass=w.muscle_mass_kg,
        basal_met=w.basal_met,
        metabolic_age=w.metabolic_age,
        visceral_fat_rating=w.visceral_fat_rating,
        physique_rating=w.physique_rating,
        bmi=w.bmi,
    )


# --------------------------------------------------------------------------- main

def sync(args) -> int:
    state = State(Path(os.path.expanduser(env("STATE_FILE", str(HERE / "state.json")))))
    until = datetime.now()
    if args.days:
        since = until - timedelta(days=args.days)
    elif state.last_measure_ts:
        since = datetime.fromtimestamp(state.last_measure_ts / 1000) - timedelta(days=2)
    else:
        since = until - timedelta(days=int(env("INITIAL_BACKFILL_DAYS", "7")))

    wyze = wyze_client()
    model = pick_scale_model(wyze)
    raw = fetch_wyze(wyze, model, since, until, env("WYZE_FAMILY_MEMBER_ID") or None)

    muscle_field = env("MUSCLE_FIELD", "muscle")  # or "skeletal_muscle"
    weigh_ins = sorted(
        (w for w in (to_weigh_in(r, muscle_field) for r in raw) if w),
        key=lambda w: w.measure_ts_ms)
    pending = [w for w in weigh_ins if not state.has(w.record_id)]
    log.info("Wyze: %d weigh-ins since %s, %d not yet synced.",
             len(weigh_ins), since.strftime("%Y-%m-%d"), len(pending))

    if args.dry_run:
        for w in pending:
            print("would upload:", w.summary())
            if args.verbose:
                print("   ", json.dumps(asdict(w), default=str))
        return 0
    if not pending:
        return 0

    garmin = garmin_client()
    existing = garmin_existing_times(garmin, since, until)
    uploaded = 0
    for w in pending:
        if any(abs(t - w.measure_ts_ms) < 120_000 for t in existing):
            log.info("Already in Garmin, skipping: %s", w.summary())
            state.mark(w.record_id, w.measure_ts_ms)
            continue
        try:
            upload(garmin, w)
        except Exception as exc:
            log.error("Upload failed for %s: %s", w.summary(), exc)
            return 1  # stop; the next run retries from here
        state.mark(w.record_id, w.measure_ts_ms)
        uploaded += 1
        log.info("Uploaded: %s", w.summary())
        time.sleep(1)  # be gentle with Garmin
    log.info("Done. %d uploaded.", uploaded)
    return 0


def main() -> int:
    load_dotenv(HERE / ".env")
    p = argparse.ArgumentParser(description="Sync Wyze Scale weigh-ins to Garmin Connect.")
    p.add_argument("--login", action="store_true", help="interactive Garmin login (run once)")
    p.add_argument("--list-scales", action="store_true", help="list Wyze scales and exit")
    p.add_argument("--dry-run", action="store_true", help="show what would be uploaded")
    p.add_argument("--days", type=int, help="look back this many days (backfill)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.verbose:
        for noisy in ("wyze_sdk", "garminconnect", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.login:
        garmin_login_interactive()
        return 0
    if args.list_scales:
        for s in wyze_client().scales.list():
            print(f"{s.nickname}: model={s.product.model} mac={s.mac}")
        return 0
    return sync(args)


if __name__ == "__main__":
    sys.exit(main())
