# wyze-garmin-sync

A self-hosted replacement for SmartScaleSync. It copies your Wyze Scale weigh-ins
(weight plus body fat, water, muscle, bone, BMR, metabolic age, visceral fat and BMI)
into Garmin Connect, where they show up like a reading from a Garmin Index scale.

It uses two unofficial libraries: `wyze-sdk` and `python-garminconnect` (0.3.x,
which has Garmin's new 2026 login). Neither Wyze nor Garmin supports them, so
an update on either side can break them for a while.

## Setup (once)

Requires Python 3.12 or newer.

```bash
cd wyze-garmin-sync
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in .env
```

1. **Wyze API key:** sign in at https://developer-api-console.wyze.com/#/apikey/view,
   create a key, and put the Key ID and API Key in `.env`.
2. **Garmin login:** run `python sync.py --login` and enter your password and MFA code
   if asked. Tokens are saved to `~/.garminconnect/` and refreshed automatically.
   Treat that folder like a password.
3. **Check:** run `python sync.py --dry-run --days 30` to list what would be uploaded,
   with nothing sent to Garmin. Make sure the numbers match the Wyze app.
4. **First real sync:** run `python sync.py --days 30` (or however far back you want).

## Running it automatically

Every run only uploads weigh-ins it hasn't uploaded before. It keeps track of them in
`state.json` and also skips any reading Garmin already has within 2 minutes.

**macOS / Linux (cron), every 30 minutes.** Run `crontab -e` and add:

```
*/30 * * * * cd /path/to/wyze-garmin-sync && .venv/bin/python sync.py >> sync.log 2>&1
```

**Windows (Task Scheduler):** create a task that runs every 30 minutes with
program `C:\path\to\wyze-garmin-sync\.venv\Scripts\python.exe`, arguments
`sync.py`, and start-in folder `C:\path\to\wyze-garmin-sync`.

A Raspberry Pi or any computer that's always on works well for this.

## Commands

| Command | What it does |
|---|---|
| `python sync.py` | Sync new weigh-ins |
| `python sync.py --dry-run` | Show what would be uploaded |
| `python sync.py --days 90` | Look back 90 days (backfill) |
| `python sync.py --list-scales` | List your Wyze scales and model codes |
| `python sync.py --login` | Log in to Garmin again (if tokens expire) |
| `-v` | Verbose logging (add to any command) |

## Field mapping

| Wyze | Garmin |
|---|---|
| weight (kg) | Weight |
| body_fat | Body fat % |
| body_water | Body water % |
| muscle (or skeletal_muscle, set with `MUSCLE_FIELD`) | Skeletal muscle mass |
| bone_mineral | Bone mass |
| bmr | BMR |
| metabolic_age | Metabolic age |
| body_vfr | Visceral fat rating |
| body_type | Physique rating (only if 1–9) |
| bmi | BMI |

If you weigh in with socks on, Wyze records 0 for the impedance-based metrics.
Those fields are left out, so Garmin gets the weight only.

## Troubleshooting

- **Garmin login failed:** run `python sync.py --login` again. If Garmin changed its
  login again, upgrade the library with `pip install -U garminconnect`.
- **Wyze errors / no scale found:** check the API key, and run
  `pip install -U wyze-sdk`. Use `--list-scales` to confirm the scale is on your account.
- **Muscle mass looks wrong in Garmin:** set `MUSCLE_FIELD=skeletal_muscle` in `.env`,
  then compare with `--dry-run -v`.
- **Undo an upload:** delete the weigh-in in Garmin Connect, remove its ID from
  `state.json`, and run again.
