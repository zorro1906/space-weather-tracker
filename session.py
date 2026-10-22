#!/usr/bin/env python3
"""Session pings and the data behind the bot's /morning outlook.

Runs after tracker.py on every GitHub Actions check.

Session ping: when the satellite sees wind of at least SESSION_SPEED_MIN
(default 400 km/s) with Bz at or below SESSION_BZ_MAX (default -5 nT), both as
10-minute medians, Telegram gets one ping saying when it reaches Earth. During
quiet hours (default 00:00-08:00 local) the ping is held; if the window is
still on when they end, it is sent then. A window ends after 30 minutes below
the minimum, and the next one gets a new ping.

Outlook: NOAA's hourly solar wind speed forecast (WSA-Enlil model) and NOAA's
3-day Kp forecast, saved to outlook.json about once an hour. The Telegram bot
reads that file for /morning. Bz itself can't be forecast hours ahead; the
pings catch it live.

Usage:
    python3 session.py --state session.json --outlook outlook.json
    python3 session.py --test     # send a sample ping
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone

import tracker as T

ENLIL_URL = "https://services.swpc.noaa.gov/json/enlil_time_series.json"
FORECAST_URL = "https://services.swpc.noaa.gov/text/3-day-forecast.txt"
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def session_config() -> dict:
    return {
        "speed": T.env_float("SESSION_SPEED_MIN", 400.0),   # km/s, at least
        "bz": T.env_float("SESSION_BZ_MAX", -5.0),          # nT, at or below
        "clear_minutes": T.env_float("SESSION_CLEAR_MINUTES", 30.0),
        "quiet_start": int(T.env_float("QUIET_START_HOUR", 0)),
        "quiet_end": int(T.env_float("QUIET_END_HOUR", 8)),
    }


# --------------------------------------------------------------------------
# Session pings
# --------------------------------------------------------------------------

def local_hour(dt: datetime, cfg: dict) -> int:
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(cfg["tz"])).hour
    except Exception:
        return dt.hour


def is_quiet(dt: datetime, cfg: dict, scfg: dict) -> bool:
    start, end, hour = scfg["quiet_start"], scfg["quiet_end"], local_hour(dt, cfg)
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def session_ok(w, scfg: dict) -> bool:
    return bool(w) and w["speed"] >= scfg["speed"] and w["bz"] <= scfg["bz"]


def l1_now(samples: list, now: datetime, cfg: dict):
    """10-minute medians at the satellite, or None if the data is stale."""
    if not samples or now - samples[-1]["t"] > timedelta(minutes=cfg["stale_minutes"]):
        return None, []
    newest = samples[-1]["t"]
    window = [s for s in samples if s["t"] > newest - timedelta(minutes=cfg["avg_minutes"])]
    return T.summarize(window), window


def format_ping(w: dict, eta: datetime, now: datetime, cfg: dict) -> str:
    reading = f"Bz {w['bz']:+.1f} · {w['speed']:.0f} km/s"
    if eta > now + timedelta(minutes=2):
        mins = round((eta - now).total_seconds() / 60)
        head = f"🟢 Session window: {reading}\nreaches Earth ~{T.local(eta, cfg)} (in ~{mins} min)"
    else:
        head = f"🟢 Session window on now at Earth: {reading}"
    return head + "\n/status shows how long it holds"


def session_step(state: dict, samples: list, now: datetime, cfg: dict, scfg: dict):
    """Returns (new_state, messages)."""
    st = dict(state.get("session") or {"phase": "idle"})
    messages = []
    w, window = l1_now(samples, now, cfg)
    ok = session_ok(w, scfg)

    if ok:
        st["last_ok"] = now.isoformat()
        if st.get("phase") != "on":
            front = [s["t"] for s in window if s["speed"] >= scfg["speed"] and s["bz"] <= scfg["bz"]]
            eta = min(front or [window[0]["t"]]) + timedelta(seconds=cfg["distance_km"] / w["speed"])
            st = {"phase": "on", "notified": False, "eta": eta.isoformat(), "last_ok": now.isoformat()}
    elif st.get("phase") == "on":
        last_ok = datetime.fromisoformat(st["last_ok"])
        if now - last_ok > timedelta(minutes=scfg["clear_minutes"]):
            st = {"phase": "idle"}

    if st.get("phase") == "on" and not st.get("notified") and not is_quiet(now, cfg, scfg):
        if ok:  # only ping while the satellite still sees it, so the reading is current
            messages.append(("session", format_ping(w, datetime.fromisoformat(st["eta"]), now, cfg)))
            st["notified"] = True

    new_state = dict(state)
    new_state["session"] = st
    return new_state, messages


# --------------------------------------------------------------------------
# Outlook for /morning
# --------------------------------------------------------------------------

def hourly_speed(enlil) -> dict:
    """Mean modelled speed per UTC hour, keyed 'YYYY-MM-DDTHH'."""
    hours = {}
    for row in enlil or []:
        try:
            t = T.parse_time(row["time_tag"])
            v = float(row["v_r"])
        except (KeyError, TypeError, ValueError):
            continue
        hours.setdefault(t.strftime("%Y-%m-%dT%H"), []).append(v)
    return {k: round(sum(v) / len(v)) for k, v in sorted(hours.items())}


def parse_forecast(text: str) -> dict:
    """Kp blocks (UTC start, value) and the rationale from NOAA's 3-day forecast."""
    out = {"issued": None, "kp_blocks": [], "note": None}
    issued = re.search(r":Issued:\s*(\d{4}) (\w{3}) (\d{1,2}) (\d{2})(\d{2}) UTC", text)
    if issued:
        y, mon, d, hh, mm = issued.groups()
        out["issued"] = datetime(int(y), MONTHS[mon], int(d), int(hh), int(mm), tzinfo=timezone.utc).isoformat()

    head = re.search(r"NOAA Kp index breakdown .*?(\d{4})\s*\n\s*\n\s*((?:\w{3} \d{1,2}\s+){2,3})", text)
    if head:
        days = [(MONTHS.get(mon), int(d)) for mon, d in re.findall(r"(\w{3}) (\d{1,2})", head.group(2))]
        if any(m is None for m, _ in days):
            return out
        # The title year belongs to the last day; step back a year across Dec -> Jan.
        year, dates = int(head.group(1)), []
        for i in range(len(days) - 1, -1, -1):
            if i < len(days) - 1 and days[i][0] > days[i + 1][0]:
                year -= 1
            dates.insert(0, datetime(year, days[i][0], days[i][1], tzinfo=timezone.utc))
        for line in text.splitlines():
            row = re.match(r"\s*(\d{2})-(\d{2})UT\s+(.*)$", line)
            if not row:
                continue
            values = re.findall(r"\d+\.\d+", row.group(3))
            for day, val in zip(dates, values):
                start = day + timedelta(hours=int(row.group(1)))
                out["kp_blocks"].append({"start": start.isoformat(), "kp": float(val)})
        out["kp_blocks"].sort(key=lambda b: b["start"])

    note = re.search(r"A\. NOAA Geomagnetic Activity.*?Rationale:\s*(.*?)\n\s*\n", text, re.S)
    if note:
        out["note"] = " ".join(note.group(1).split())
    return out


def build_outlook(enlil, forecast_text: str) -> dict:
    outlook = {"speed_hourly": hourly_speed(enlil)}
    outlook.update(parse_forecast(forecast_text or ""))
    return outlook


def refresh_outlook(path: str, now: datetime, force: bool = False) -> bool:
    """Refresh about once an hour (runs at :00-:09) or when the file is missing."""
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        old = None
    if old is not None and not force and now.minute >= 10:
        return False
    try:
        enlil = T.fetch_json(ENLIL_URL)
    except Exception as exc:
        print(f"Enlil fetch failed: {exc}")
        enlil = None
    try:
        import urllib.request
        req = urllib.request.Request(FORECAST_URL, headers={"User-Agent": "space-weather-tracker/3.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"3-day forecast fetch failed: {exc}")
        text = ""
    new = build_outlook(enlil, text)
    if old:  # keep the last good part if one source failed
        if not new["speed_hourly"]:
            new["speed_hourly"] = old.get("speed_hourly", {})
        if not new["kp_blocks"]:
            for key in ("issued", "kp_blocks", "note"):
                new[key] = old.get(key)
    if new == old:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(new, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    print(f"Outlook updated: {len(new['speed_hourly'])} speed hours, {len(new['kp_blocks'])} Kp blocks")
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Session pings and outlook data")
    parser.add_argument("--state", default="session.json")
    parser.add_argument("--outlook", default="outlook.json")
    parser.add_argument("--force-outlook", action="store_true")
    parser.add_argument("--test", action="store_true", help="send a sample session ping")
    args = parser.parse_args()

    cfg, scfg = T.load_config(), session_config()
    now = datetime.now(timezone.utc)

    try:
        refresh_outlook(args.outlook, now, force=args.force_outlook)
    except Exception as exc:  # the outlook must never block the pings
        print(f"Outlook refresh failed: {exc}")

    try:
        samples = T.build_samples(T.fetch_json(T.WIND_URL), T.fetch_json(T.MAG_URL), cfg)
    except Exception as exc:
        print(f"Fetch failed: {exc}")
        return

    if args.test:
        w, _ = l1_now(samples, now, cfg)
        if w:
            eta = samples[-1]["t"] + timedelta(seconds=cfg["distance_km"] / w["speed"])
            T.send("test", "(test) " + format_ping(w, eta, now, cfg))

    try:
        with open(args.state, encoding="utf-8") as fh:
            state = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    new_state, messages = session_step(state, samples, now, cfg, scfg)
    for kind, text in messages:
        T.send(kind, text)
    if new_state != state:
        T.save_state(args.state, new_state)


if __name__ == "__main__":
    main()
