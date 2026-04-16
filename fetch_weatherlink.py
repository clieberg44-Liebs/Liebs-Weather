#!/usr/bin/env python3
"""
fetch_weatherlink.py — pull station data from WeatherLink v2 API and write data/weather.json

Modes:
  --backfill   : pull full year-to-date history (one API call per day, slow)
                 plus optional historical year from a local workbook
  --refresh    : pull only current conditions + today's history (fast, for 15-min cron)
  --full       : equivalent to --backfill (for first run or reset)

Env vars (set as GitHub Secrets):
  WEATHERLINK_API_KEY     — v2 API key
  WEATHERLINK_API_SECRET  — v2 API secret
  WEATHERLINK_STATION_ID  — integer station id (optional; auto-detects if one station)

Optional:
  WEATHERLINK_TZ          — IANA timezone (default: America/Chicago)
  HISTORICAL_WORKBOOK     — path to .xlsm/.xlsx for prior-year data (default: data/Weather_Data.xlsm if it exists)
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

# ─────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────
API_BASE = "https://api.weatherlink.com/v2"
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "weather.json"
HTML_FILE = Path(__file__).resolve().parent.parent / "index.html"

API_KEY    = os.environ.get("WEATHERLINK_API_KEY", "")
API_SECRET = os.environ.get("WEATHERLINK_API_SECRET", "")
STATION_ID = os.environ.get("WEATHERLINK_STATION_ID", "")
TZ_NAME    = os.environ.get("WEATHERLINK_TZ", "America/Chicago")
WORKBOOK   = os.environ.get("HISTORICAL_WORKBOOK", "")

TZ = ZoneInfo(TZ_NAME)

# Sensor categories / data structure types we care about
# See https://weatherlink.github.io/v2-api/data-structure-types
# ISS / Vue / Vantage archive records:  data_structure_type = 4 (VP2) or 11 (WLL hourly)
# Current conditions from ISS         :  data_structure_type = 2, 10, 23
# We'll accept any structure containing the temp/rain/wind fields.

# ─────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────
def api_get(path: str, params: Optional[dict] = None) -> dict:
    if not API_KEY or not API_SECRET:
        raise SystemExit("ERROR: WEATHERLINK_API_KEY and WEATHERLINK_API_SECRET must be set")
    params = dict(params or {})
    params["api-key"] = API_KEY
    headers = {"X-Api-Secret": API_SECRET, "Accept": "application/json"}
    url = f"{API_BASE}{path}"
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code == 429:
        print("WARN: rate limited, sleeping 30s", file=sys.stderr)
        time.sleep(30)
        r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def discover_station_id() -> str:
    """If STATION_ID not set and the account has exactly one station, use it."""
    global STATION_ID
    if STATION_ID:
        return STATION_ID
    data = api_get("/stations")
    stations = data.get("stations", [])
    if not stations:
        raise SystemExit("ERROR: no stations found on this account")
    if len(stations) > 1:
        names = ", ".join(f"{s.get('station_id')}:{s.get('station_name')}" for s in stations)
        raise SystemExit(f"ERROR: multiple stations found, set WEATHERLINK_STATION_ID. Options: {names}")
    STATION_ID = str(stations[0]["station_id"])
    print(f"Auto-detected station_id={STATION_ID} ({stations[0].get('station_name','')})")
    return STATION_ID


# ─────────────────────────────────────────────────────────────────────────
# Sensor field extraction
# ─────────────────────────────────────────────────────────────────────────
# Field-name candidates vary by station/console type. We pick first present.
TEMP_KEYS        = ("temp", "temp_out", "temp_last")
HUM_KEYS         = ("hum", "hum_out", "hum_last")
DEW_KEYS         = ("dew_point", "dew_point_last")
WIND_KEYS        = ("wind_speed_last", "wind_speed_avg_last_10_min", "wind_speed")
WINDDIR_KEYS     = ("wind_dir_last", "wind_dir_scalar_avg_last_10_min", "wind_dir")
PRESSURE_KEYS    = ("bar", "bar_sea_level", "bar_absolute")
RAIN_TODAY_KEYS  = ("rainfall_daily_in", "rainfall_day_in", "rain_day_in", "rainfall_daily",
                    "rainfall_daily_clicks")  # clicks variant requires conversion
RAIN_RATE_KEYS   = ("rain_rate_last_in", "rain_rate_hi_last_15_min_in", "rain_rate_last",
                    "rain_rate_last_clicks")
UV_KEYS          = ("uv_index", "uv_last", "uv_index_last")
SOLAR_KEYS       = ("solar_rad", "solar_rad_last", "solar_radiation")
FEELS_KEYS       = ("thsw_index", "thw_index", "heat_index", "wind_chill")

# Archive (daily) fields
ARC_TEMP_HI_KEYS = ("temp_hi", "temp_out_hi", "temp_hi_out")
ARC_TEMP_LO_KEYS = ("temp_lo", "temp_out_lo", "temp_lo_out")
ARC_RAIN_KEYS    = ("rainfall_in", "rain_clicks", "rainfall")


def pick(record: dict, keys: tuple) -> Any:
    for k in keys:
        if k in record and record[k] is not None:
            return record[k]
    return None


def clicks_to_inches(clicks: Optional[float], rain_collector: int = 1) -> Optional[float]:
    """rain_collector: 1=0.01in, 2=0.2mm, 3=0.1mm, 4=0.001in"""
    if clicks is None:
        return None
    cal = {1: 0.01, 2: 0.2 / 25.4, 3: 0.1 / 25.4, 4: 0.001}.get(rain_collector, 0.01)
    return round(clicks * cal, 3)


def extract_current(api_json: dict) -> dict:
    """Flatten current-conditions response into our schema."""
    out = {}
    for sensor in api_json.get("sensors", []):
        for record in sensor.get("data", []):
            # Merge known fields across all sensor records (ISS, barometer, WLL, etc.)
            for keys, target in [
                (TEMP_KEYS,    "temp"),
                (HUM_KEYS,     "hum"),
                (DEW_KEYS,     "dew"),
                (WIND_KEYS,    "wind"),
                (WINDDIR_KEYS, "windDir"),
                (PRESSURE_KEYS,"pressure"),
                (RAIN_TODAY_KEYS, "rainToday"),
                (RAIN_RATE_KEYS,  "rainRate"),
                (UV_KEYS,      "uv"),
                (SOLAR_KEYS,   "solar"),
                (FEELS_KEYS,   "feels"),
            ]:
                v = pick(record, keys)
                if v is not None and target not in out:
                    out[target] = v
    # Handle rain given as clicks (some station types)
    if isinstance(out.get("rainToday"), (int, float)) and out["rainToday"] > 20:
        # Heuristic: if value looks like clicks (>20), convert
        out["rainToday"] = clicks_to_inches(out["rainToday"])
    if isinstance(out.get("rainRate"), (int, float)) and out["rainRate"] > 20:
        out["rainRate"] = clicks_to_inches(out["rainRate"])
    return out


def aggregate_day(records: list, day_iso: str) -> Optional[dict]:
    """Given all archive records for a single day, compute daily hi/lo/rain."""
    highs, lows, rains = [], [], []
    for r in records:
        th = pick(r, ARC_TEMP_HI_KEYS)
        tl = pick(r, ARC_TEMP_LO_KEYS)
        rn = pick(r, ARC_RAIN_KEYS)
        if isinstance(th, (int, float)): highs.append(th)
        if isinstance(tl, (int, float)): lows.append(tl)
        if isinstance(rn, (int, float)): rains.append(rn)
    if not highs and not lows and not rains:
        return None
    hi = max(highs) if highs else None
    lo = min(lows) if lows else None
    rain_total = sum(rains) if rains else 0.0
    # If rain looks like clicks, convert
    if rain_total > 10:
        rain_total = clicks_to_inches(rain_total) or 0.0
    return {
        "d": day_iso,
        "hi": round(hi, 1) if hi is not None else None,
        "lo": round(lo, 1) if lo is not None else None,
        "avgHi": None,  # filled from workbook/historical when available
        "avgLo": None,
        "rain": round(rain_total, 2),
    }


# ─────────────────────────────────────────────────────────────────────────
# Historical fetching
# ─────────────────────────────────────────────────────────────────────────
def fetch_day(station_id: str, day: datetime) -> Optional[dict]:
    """Fetch one day's archive records. day is a datetime at 00:00 local time."""
    start = int(day.replace(tzinfo=TZ).astimezone(timezone.utc).timestamp())
    end   = int((day + timedelta(days=1)).replace(tzinfo=TZ).astimezone(timezone.utc).timestamp())
    try:
        data = api_get(f"/historic/{station_id}", {"start-timestamp": start, "end-timestamp": end})
    except requests.HTTPError as e:
        print(f"WARN: historic fetch failed for {day.date()}: {e}", file=sys.stderr)
        return None

    all_records = []
    for sensor in data.get("sensors", []):
        for r in sensor.get("data", []):
            all_records.append(r)
    return aggregate_day(all_records, day.date().isoformat())


def fetch_year_to_date(station_id: str, year: int) -> list:
    """Fetch every day from Jan 1 of `year` through today."""
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    start = datetime(year, 1, 1)
    days = []
    cur = start
    day_count = (today - start).days + 1
    print(f"Backfilling {day_count} days for {year}...")
    n = 0
    while cur <= today:
        rec = fetch_day(station_id, cur)
        if rec:
            days.append(rec)
        n += 1
        if n % 10 == 0:
            print(f"  {n}/{day_count} days fetched")
        time.sleep(0.3)  # be polite
        cur += timedelta(days=1)
    return days


# ─────────────────────────────────────────────────────────────────────────
# Workbook historical import (optional)
# ─────────────────────────────────────────────────────────────────────────
def load_from_workbook(path: str, years: list[int]) -> dict[int, list]:
    """Load prior years from the Excel workbook if present. Returns {year: [day dicts]}."""
    try:
        import pandas as pd
        import warnings
        warnings.filterwarnings("ignore")
    except ImportError:
        print("WARN: pandas not installed, skipping workbook import", file=sys.stderr)
        return {}

    p = Path(path)
    if not p.exists():
        print(f"INFO: workbook {p} not found, skipping")
        return {}

    out = {}
    for year in years:
        sheet = f"Raw Data {year}"
        try:
            df = pd.read_excel(p, sheet_name=sheet)
        except Exception as e:
            print(f"WARN: couldn't read sheet '{sheet}' from {p}: {e}")
            continue
        df = df[df["Day"].apply(lambda x: hasattr(x, "year"))].reset_index(drop=True)

        # Trim trailing zero rows for current year (unrecorded days)
        last_idx = len(df) - 1
        while last_idx >= 0:
            r = df.iloc[last_idx]
            hi = r.get("High") or 0
            lo = r.get("Low") or 0
            rain = r.get("Rain") or 0
            if (isinstance(hi, (int, float)) and hi > 0) or \
               (isinstance(lo, (int, float)) and lo != 0) or \
               (isinstance(rain, (int, float)) and rain > 0):
                break
            last_idx -= 1

        days = []
        for i in range(last_idx + 1):
            r = df.iloc[i]
            def num(v):
                if v is None: return None
                if isinstance(v, float) and math.isnan(v): return None
                try: return float(v)
                except: return None
            days.append({
                "d": r["Day"].strftime("%Y-%m-%d"),
                "hi": num(r.get("High")),
                "lo": num(r.get("Low")),
                "avgHi": num(r.get("Average High")),
                "avgLo": num(r.get("Average Low")),
                "rain": num(r.get("Rain")) or 0,
            })
        out[year] = days
        print(f"Loaded {len(days)} days for {year} from workbook")
    return out


# ─────────────────────────────────────────────────────────────────────────
# Stats + monthly aggregation
# ─────────────────────────────────────────────────────────────────────────
def stats(days: list) -> dict:
    if not days: return {}
    highs = [d["hi"] for d in days if d.get("hi") is not None and d["hi"] > 0]
    lows  = [d["lo"] for d in days if d.get("lo") is not None]
    rains = [d["rain"] or 0 for d in days]
    if not highs or not lows:
        return {"daysRecorded": len(days)}
    max_hi = max((d for d in days if d.get("hi") is not None), key=lambda x: x["hi"])
    min_lo = min((d for d in days if d.get("lo") is not None), key=lambda x: x["lo"])
    max_r  = max((d for d in days if d.get("rain") is not None), key=lambda x: x["rain"])
    return {
        "daysRecorded": len(days),
        "maxHigh":     max(highs),
        "maxHighDate": max_hi["d"],
        "minLow":      min(lows),
        "minLowDate":  min_lo["d"],
        "avgHigh":     round(sum(highs)/len(highs), 1),
        "avgLow":      round(sum(lows)/len(lows), 1),
        "totalRain":   round(sum(rains), 2),
        "wetDays":     sum(1 for r in rains if r > 0),
        "dryDays":     sum(1 for r in rains if r == 0),
        "maxRainDay":  round(max(rains), 2),
        "maxRainDate": max_r["d"],
    }


def monthly(days: list) -> list:
    m: dict[str, dict] = {}
    for d in days:
        mo = d["d"][:7]
        if mo not in m: m[mo] = {"highs": [], "lows": [], "rain": 0.0, "days": 0}
        if d.get("hi") is not None and d["hi"] > 0: m[mo]["highs"].append(d["hi"])
        if d.get("lo") is not None: m[mo]["lows"].append(d["lo"])
        m[mo]["rain"] += d.get("rain") or 0
        m[mo]["days"] += 1
    out = []
    for k in sorted(m.keys()):
        v = m[k]
        if not v["highs"]: continue
        out.append({
            "month":   k,
            "avgHigh": round(sum(v["highs"])/len(v["highs"]), 1),
            "avgLow":  round(sum(v["lows"])/len(v["lows"]), 1) if v["lows"] else None,
            "rain":    round(v["rain"], 2),
            "maxHigh": max(v["highs"]),
            "minLow":  min(v["lows"]) if v["lows"] else None,
            "days":    v["days"],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# Merge logic — update-in-place without full refetch
# ─────────────────────────────────────────────────────────────────────────
def load_existing() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {}


def merge_day(days: list, new_day: dict) -> list:
    """Replace or append a day by date key."""
    iso = new_day["d"]
    found = False
    for i, d in enumerate(days):
        if d["d"] == iso:
            # Preserve avgHi/avgLo from existing (workbook-sourced) data
            for k in ("avgHi", "avgLo"):
                if d.get(k) is not None and new_day.get(k) is None:
                    new_day[k] = d[k]
            days[i] = new_day
            found = True
            break
    if not found:
        days.append(new_day)
        days.sort(key=lambda x: x["d"])
    return days


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────
def write_output(export: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(export, default=str, separators=(",", ":")))
    print(f"✓ wrote {DATA_FILE} ({DATA_FILE.stat().st_size:,} bytes)")


def run_refresh(station_id: str) -> None:
    """Fast path for every-15-min cron: current + today only."""
    existing = load_existing()
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    year = today.year

    # Current conditions
    print("Fetching current conditions...")
    cur_json = api_get(f"/current/{station_id}")
    current = extract_current(cur_json)
    current_ts = cur_json.get("generated_at")

    # Today's daily aggregate (so the chart extends through today)
    print("Fetching today's archive...")
    today_rec = fetch_day(station_id, today)

    # Build output from existing + updates
    daily_current = existing.get(f"daily{year}", [])
    if today_rec:
        daily_current = merge_day(daily_current, today_rec)

    export = dict(existing)
    export[f"daily{year}"] = daily_current
    export[f"stats{year}"] = stats(daily_current)
    export[f"monthly{year}"] = monthly(daily_current)

    # Also expose under canonical keys the HTML expects
    export["daily2026"]   = export.get(f"daily{year}", existing.get("daily2026", []))
    export["stats2026"]   = export.get(f"stats{year}", existing.get("stats2026", {}))
    export["monthly2026"] = export.get(f"monthly{year}", existing.get("monthly2026", []))
    export.setdefault("daily2025",   existing.get("daily2025", []))
    export.setdefault("stats2025",   existing.get("stats2025", {}))
    export.setdefault("monthly2025", existing.get("monthly2025", []))

    export["current"]   = current
    export["live"]      = True
    export["station"]   = export.get("station", {"name": "Foxen Canyon"})
    if current_ts:
        export["lastFetch"] = datetime.fromtimestamp(current_ts, TZ).isoformat()
    else:
        export["lastFetch"] = now.isoformat()
    export["lastUpdate"] = today.date().isoformat()

    write_output(export)


def run_backfill(station_id: str) -> None:
    """Slow path: full YTD + prior year from workbook if available."""
    now = datetime.now(TZ)
    year = now.year
    prior = year - 1

    print(f"Full backfill: {year} YTD + historical {prior}")

    # Start with workbook data if present (provides avgHi/avgLo normals for both years)
    wb_data = {}
    if WORKBOOK:
        wb_data = load_from_workbook(WORKBOOK, [prior, year])

    # Fetch current year from API
    daily_current = fetch_year_to_date(station_id, year)
    # Merge workbook normals into API data
    if year in wb_data:
        wb_idx = {d["d"]: d for d in wb_data[year]}
        for d in daily_current:
            if d["d"] in wb_idx:
                for k in ("avgHi", "avgLo"):
                    if wb_idx[d["d"]].get(k) is not None and d.get(k) is None:
                        d[k] = wb_idx[d["d"]][k]

    # Prior year: prefer workbook; fall back to API if not present
    daily_prior = wb_data.get(prior, [])
    if not daily_prior:
        print(f"No workbook data for {prior}; fetching from API (this will take ~6 min)...")
        daily_prior = fetch_year_to_date(station_id, prior)

    # Current conditions
    print("Fetching current conditions...")
    cur_json = api_get(f"/current/{station_id}")
    current = extract_current(cur_json)

    export = {
        "station":     {"name": "Foxen Canyon", "lat": 35.923652, "lon": -86.867827},
        "current":     current,
        "live":        True,
        "lastFetch":   now.isoformat(),
        "lastUpdate":  now.date().isoformat(),
        "daily2025":   daily_prior,
        "daily2026":   daily_current,
        "stats2025":   stats(daily_prior),
        "stats2026":   stats(daily_current),
        "monthly2025": monthly(daily_prior),
        "monthly2026": monthly(daily_current),
    }
    write_output(export)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["refresh", "backfill", "full"], default="refresh",
                        help="refresh=today only (fast); backfill/full=entire YTD (slow)")
    args = parser.parse_args()

    station_id = discover_station_id()
    if args.mode == "refresh":
        run_refresh(station_id)
    else:
        run_backfill(station_id)


if __name__ == "__main__":
    main()
