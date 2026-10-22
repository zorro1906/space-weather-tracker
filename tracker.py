#!/usr/bin/env python3
"""Space weather tracker: phone alerts for geomagnetic storms, matched to strength.

NOAA's real-time solar wind data comes from the operational satellite at L1,
about 1.5 million km toward the Sun. What it measures reaches Earth roughly
30-60 minutes later. Your storm minimum (both, as 10-minute medians):

    solar wind speed  > SPEED_MIN  (default 500 km/s)
    IMF Bz (GSM)     <= BZ_MAX     (default -10 nT, southward)

Messages, each sent once:
  NOAA WATCH       days ahead: NOAA forecasters predict a G2+ storm (CME on the way)
  NOAA WARNING     NOAA expects K-index 6+ (G2+) within hours
  INCOMING         the satellite sees your minimum; gives arrival time and strength
  STRENGTHENING    the driver at the satellite reaches 2x, 3x, 4x your minimum
  HERE             the storm is reaching Earth; current Kp and G level
  G-LEVEL UP       Kp at Earth reaches a new level: G1 Minor ... G5 Extreme
  OVER             calm at Earth for 30 min; duration and peak level
  CANCELLED        a warning fizzled, or NOAA cancelled a watch/warning

Strength of the driver = speed x southward Bz (the solar wind electric field,
mV/m). Your minimum, 500 km/s x 10 nT = 5 mV/m, is the classic threshold for
intense storms when it lasts 3+ hours (Gonzalez & Tsurutani 1987).

Python 3.9+ standard library only. Configure via environment variables
(see README.md).

Usage:
    python3 tracker.py                 # one check (cron / GitHub Actions)
    python3 tracker.py --loop          # check every 5 min forever (server)
    python3 tracker.py --test          # send a test message with live readings
    python3 tracker.py --find-chat-id  # print your Telegram chat ID
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

WIND_URL = "https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json"
MAG_URL = "https://services.swpc.noaa.gov/json/rtsw/rtsw_mag_1m.json"
KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
ALERTS_URL = "https://services.swpc.noaa.gov/products/alerts.json"

G_NAMES = {1: "Minor", 2: "Moderate", 3: "Strong", 4: "Severe", 5: "Extreme"}


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def load_config() -> dict:
    return {
        "speed_min": env_float("SPEED_MIN", 500.0),          # km/s
        "bz_max": env_float("BZ_MAX", -10.0),                # nT
        "avg_minutes": env_float("AVG_MINUTES", 10.0),       # median window
        "clear_minutes": env_float("CLEAR_MINUTES", 30.0),   # calm at Earth before "over"
        "stale_minutes": env_float("STALE_MINUTES", 30.0),   # ignore older data
        "cancel_minutes": env_float("CANCEL_MINUTES", 30.0), # grace after ETA before "cancelled"
        "distance_km": env_float("L1_DISTANCE_KM", 1.35e6),  # L1 to bow shock
        "noaa_min_g": int(env_float("NOAA_MIN_G", 2)),       # relay NOAA watches/warnings from this G
        "tz": os.environ.get("TZ_NAME", "Europe/Prague").strip() or "UTC",
    }


def g_level(kp_index) -> int:
    """NOAA G scale: Kp 5 = G1 ... Kp 9 = G5 (0 = below storm level)."""
    if kp_index is None:
        return 0
    return max(0, min(5, int(kp_index) - 4))


def g_label(g: int) -> str:
    return f"G{g} {G_NAMES[g]}" if g >= 1 else "below storm level"


# --------------------------------------------------------------------------
# Solar wind data
# --------------------------------------------------------------------------

def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "space-weather-tracker/3.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_time(tag: str) -> datetime:
    """NOAA time tags are UTC without a zone suffix."""
    dt = datetime.fromisoformat(tag.replace("Z", "").replace(" ", "T")[:19])
    return dt.replace(tzinfo=timezone.utc)


def minute_key(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def active_rows(rows, field: str) -> list:
    """Rows from the operational ("active") satellite that carry the given field.

    The real-time feeds mix several spacecraft; NOAA flags the one in
    operational use with active=true. If none is flagged (format change),
    fall back to every row so the tracker keeps working.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get(field) is not None]
    active = [r for r in rows if r.get("active") is True]
    return active or rows


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def build_samples(wind_rows, mag_rows, cfg) -> list:
    """Join wind and field by minute; add each sample's estimated arrival at Earth.

    Travel time uses the 10-minute running median speed, so a brief speed
    spike can't make a few samples jump ahead of the rest.
    """
    wind = {}
    for r in active_rows(wind_rows, "proton_speed"):
        wind[minute_key(parse_time(r["time_tag"]))] = r
    samples = []
    for r in active_rows(mag_rows, "bz_gsm"):
        t = minute_key(parse_time(r["time_tag"]))
        w = wind.get(t)
        if w is None or not w.get("proton_speed"):
            continue
        samples.append({
            "t": t,
            "speed": float(w["proton_speed"]),
            "bz": float(r["bz_gsm"]),
            "bt": float(r["bt"]) if r.get("bt") is not None else None,
            "density": float(w["proton_density"]) if w.get("proton_density") is not None else None,
            "source": r.get("source", "?"),
        })
    samples.sort(key=lambda s: s["t"])

    span = timedelta(minutes=cfg["avg_minutes"])
    for i, s in enumerate(samples):
        recent = [x["speed"] for x in samples[max(0, i - 60):i + 1] if x["t"] > s["t"] - span]
        s["arrival"] = s["t"] + timedelta(seconds=cfg["distance_km"] / median(recent))
    return samples


def drop_overtaken(samples: list) -> list:
    """Remove slow samples that faster, later wind would overtake.

    Ballistically, fast wind measured later can 'arrive' before slow wind
    measured earlier. Physically the fast stream sweeps that slow plasma up
    (the compressed front of a storm), so overtaken samples are dropped and
    arrival times stay in order.
    """
    kept, earliest = [], None
    for s in reversed(samples):
        if earliest is None or s["arrival"] < earliest:
            kept.append(s)
            earliest = s["arrival"]
    kept.reverse()
    return kept


def driver(speed, bz) -> float:
    """Solar wind electric field from the southward field, mV/m."""
    if speed is None or bz is None:
        return 0.0
    return speed * max(0.0, -bz) / 1000.0


def summarize(window: list) -> dict | None:
    """Median speed and Bz (a brief spike can't trigger an alert); mean for the rest."""
    if not window:
        return None
    speed = median(s["speed"] for s in window)
    bz = median(s["bz"] for s in window)
    return {
        "speed": speed,
        "bz": bz,
        "bt": mean(s["bt"] for s in window),
        "density": mean(s["density"] for s in window),
        "driver": driver(speed, bz),
        "first_arrival": min(s["arrival"] for s in window),
        "last_arrival": max(s["arrival"] for s in window),
    }


def latest_kp(rows):
    """(estimated Kp, NOAA's integer Kp) from the newest 1-minute row."""
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("estimated_kp") is not None]
    if not rows:
        return None, None
    newest = max(rows, key=lambda r: r["time_tag"])
    kp_index = newest.get("kp_index")
    if kp_index is None:
        kp_index = round(float(newest["estimated_kp"]))
    return float(newest["estimated_kp"]), int(kp_index)


def build_readings(wind_rows, mag_rows, kp_rows, now: datetime, cfg: dict) -> dict:
    samples = build_samples(wind_rows, mag_rows, cfg)
    avg = timedelta(minutes=cfg["avg_minutes"])
    kp, kp_index = latest_kp(kp_rows)
    readings = {"l1": None, "earth": None, "eta": None, "data_time": None, "source": None,
                "kp": kp, "kp_index": kp_index, "noaa": [], "avg_minutes": cfg["avg_minutes"]}
    if not samples:
        return readings

    newest = samples[-1]["t"]
    readings["data_time"] = newest
    readings["source"] = samples[-1]["source"]

    # What the satellite sees now (reaches Earth later)
    l1_window = [s for s in samples if s["t"] > newest - avg]
    readings["l1"] = summarize(l1_window)
    readings["l1_samples"] = l1_window

    # ETA of the storm front: first stormy minute at the satellite + travel time at storm speed
    front = [s["t"] for s in l1_window if s["speed"] > cfg["speed_min"] and s["bz"] <= cfg["bz_max"]]
    if front and readings["l1"]["speed"]:
        readings["eta"] = min(front) + timedelta(seconds=cfg["distance_km"] / readings["l1"]["speed"])

    # What is reaching Earth now: samples whose estimated arrival falls in the last window
    propagated = drop_overtaken(samples)
    at_earth = [s for s in propagated if now - avg < s["arrival"] <= now]
    if not at_earth:
        pending = [s for s in propagated if s["arrival"] > now]
        if pending and len(pending) == len(propagated):
            # History shorter than the travel time: the earliest-arriving data is the best proxy
            first = min(s["arrival"] for s in pending)
            at_earth = [s for s in pending if s["arrival"] < first + avg]
    readings["earth"] = summarize(at_earth)
    return readings


# --------------------------------------------------------------------------
# NOAA forecaster products (watches = days ahead, warnings = hours ahead)
# --------------------------------------------------------------------------

def parse_noaa_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y %b %d %H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_noaa(alerts) -> list:
    """Geomagnetic watches, K-index warnings and cancellations from alerts.json."""
    items = []
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        pid = str(a.get("product_id", ""))
        msg = str(a.get("message", "")).replace("\r", "")
        serial = re.search(r"Serial Number:\s*(\d+)", msg)
        try:
            issued = parse_time(str(a.get("issue_datetime", "")))
        except ValueError:
            continue
        item = {"key": f"{pid}-{serial.group(1) if serial else a.get('issue_datetime')}",
                "pid": pid, "issued": issued}

        cancel = re.search(r"Cancel Serial Number:\s*(\d+)", msg)
        watch = re.search(r"WATCH:\s*Geomagnetic Storm Category G(\d)", msg, re.I)
        warning = re.search(r"WARNING:\s*Geomagnetic K-index of (\d)", msg, re.I)

        if cancel and "CANCEL" in msg.upper():
            item.update(kind="cancel", ref=f"{pid}-{cancel.group(1)}",
                        what="watch" if watch else "warning")
        elif watch:
            item.update(kind="watch", g=int(watch.group(1)), detail=watch_detail(msg))
        elif warning:
            valid = re.search(r"(?:Valid To|Now Valid Until):\s*(\d{4} \w{3} \d{1,2} \d{4})", msg)
            item.update(kind="warning", k=int(warning.group(1)),
                        valid_to=parse_noaa_time(valid.group(1)) if valid else issued + timedelta(hours=12))
        else:
            continue
        items.append(item)
    return items


def watch_detail(msg: str) -> str:
    lines = [ln.strip() for ln in msg.split("\n")]
    out = []
    for i, ln in enumerate(lines):
        if ln.lower().startswith("highest storm level predicted by day"):
            nxt = next((x for x in lines[i + 1:] if x), "")
            out.append(re.sub(r"\s{2,}", "  ", nxt))
        elif ln.lower().startswith("comment:"):
            out.append(ln)
    return "\n".join(out)


def active_warning(items: list, now: datetime) -> dict | None:
    cancelled = {i["ref"] for i in items if i["kind"] == "cancel"}
    live = [i for i in items if i["kind"] == "warning" and i["key"] not in cancelled
            and i["valid_to"] and i["valid_to"] >= now]
    return max(live, key=lambda i: i["k"]) if live else None


def noaa_step(noaa_state: dict, items: list, now: datetime, cfg: dict):
    """NOAA watches/warnings at G >= noaa_min_g, each sent once. Returns (state, messages)."""
    st = dict(noaa_state or {})
    seen = list(st.get("seen", []))
    messages = []
    min_g = cfg["noaa_min_g"]

    for it in sorted(items, key=lambda i: i["issued"]):
        if it["key"] in seen:
            continue
        if it["kind"] == "watch":
            fresh = now - it["issued"] <= timedelta(hours=24)
            last_time = datetime.fromisoformat(st["watch_time"]) if st.get("watch_time") else None
            higher = it["g"] > st.get("watch_g", 0) or not last_time or now - last_time > timedelta(hours=24)
            if fresh and it["g"] >= min_g:
                if higher:
                    messages.append(("watch", format_watch(it)))
                    st["watch_g"], st["watch_time"] = it["g"], now.isoformat()
                seen.append(it["key"])  # a same-level reissue is marked seen, never sent late
        elif it["kind"] == "warning":
            g = it["k"] - 4
            last_time = datetime.fromisoformat(st["warn_time"]) if st.get("warn_time") else None
            higher = g > st.get("warn_g", 0) or not last_time or now - last_time > timedelta(hours=12)
            if it["valid_to"] and it["valid_to"] >= now and g >= min_g:
                if higher:
                    messages.append(("warning", format_warning(it, cfg)))
                    st["warn_g"], st["warn_time"] = g, now.isoformat()
                seen.append(it["key"])
        elif it["kind"] == "cancel" and it["ref"] in seen:
            messages.append(("cancelled", f"↩️ NOAA cancelled its storm {it['what']}"))
            seen.append(it["key"])
            if it["what"] == "watch":
                st["watch_g"] = 0
            else:
                st["warn_g"] = 0

    st["seen"] = seen[-100:]
    return st, messages


# --------------------------------------------------------------------------
# Decision logic (pure: no network, easy to test)
# --------------------------------------------------------------------------

def stormy(window, cfg) -> bool:
    return bool(window) and window["speed"] > cfg["speed_min"] and window["bz"] <= cfg["bz_max"]


def strength_multiple(window, cfg) -> int:
    """Whole multiples of your minimum driver (1 = minimum, capped at 4)."""
    minimum = driver(cfg["speed_min"], cfg["bz_max"])
    if not window or minimum <= 0:
        return 0
    return min(4, int(window["driver"] / minimum))


def step(state: dict, readings: dict, now: datetime, cfg: dict):
    """Advance the alert state. Returns (new_state, list of (kind, message))."""
    state = dict(state or {})
    phase = state.get("phase", "idle")
    messages = []

    noaa_items = readings.get("noaa") or []
    state["noaa"], noaa_msgs = noaa_step(state.get("noaa"), noaa_items, now, cfg)
    messages.extend(noaa_msgs)
    expected = active_warning(noaa_items, now)

    data_time = readings.get("data_time")
    if data_time is None or now - data_time > timedelta(minutes=cfg["stale_minutes"]):
        return state, messages  # no fresh solar wind data: storm phases unchanged

    l1_storm = stormy(readings["l1"], cfg)
    earth_storm = stormy(readings["earth"], cfg)
    multiple = strength_multiple(readings["l1"], cfg) if l1_storm else 0
    g_now = g_level(readings.get("kp_index"))
    noaa_state = state["noaa"]

    def reset():
        return {"phase": "idle", "noaa": noaa_state}

    if phase in ("idle", "incoming") and earth_storm:
        front = datetime.fromisoformat(state["eta"]) if phase == "incoming" else None
        state = {"phase": "here", "noaa": noaa_state, "started": (front or now).isoformat(),
                 "clear_since": None, "strength": max(state.get("strength", 0), multiple),
                 "g": g_now, "peak_kp": readings.get("kp") or 0.0}
        messages.append(("here", format_here(readings, l1_storm, front, g_now, cfg)))

    elif phase == "idle" and l1_storm:
        eta = readings.get("eta") or readings["l1"]["first_arrival"]
        state = {"phase": "incoming", "noaa": noaa_state, "eta": eta.isoformat(),
                 "strength": multiple}
        messages.append(("incoming", format_incoming(readings, eta, now, expected, cfg)))

    elif phase == "incoming":
        eta = datetime.fromisoformat(state["eta"])
        if not l1_storm and now > eta + timedelta(minutes=cfg["cancel_minutes"]):
            state = reset()
            messages.append(("cancelled", "↩️ Storm warning cancelled\n"
                                          "Conditions faded before reaching Earth."))

    elif phase == "here":
        state["peak_kp"] = max(state.get("peak_kp") or 0.0, readings.get("kp") or 0.0)
        if g_now > state.get("g", 0):
            state["g"] = g_now
            messages.append(("g_up", format_g_up(readings, g_now)))
        if earth_storm:
            state["clear_since"] = None
        elif not state.get("clear_since"):
            state["clear_since"] = now.isoformat()
        elif now - datetime.fromisoformat(state["clear_since"]) >= timedelta(minutes=cfg["clear_minutes"]):
            started = datetime.fromisoformat(state["started"])
            lasted = datetime.fromisoformat(state["clear_since"]) - started
            peak = state.get("peak_kp") or 0.0
            state = reset()
            messages.append(("over", format_over(readings, lasted, peak)))

    # Strengthening at the satellite (incoming or here): 2x, 3x, 4x your minimum
    if state.get("phase") in ("incoming", "here") and l1_storm and multiple > state.get("strength", 0):
        if multiple >= 2:
            messages.append(("stronger", format_stronger(readings, multiple, cfg)))
        state["strength"] = multiple

    return state, messages


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def fmt(value, digits=1, unit=""):
    return "n/a" if value is None else f"{value:.{digits}f}{unit}"


def local(dt: datetime, cfg: dict) -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(cfg["tz"])).strftime("%H:%M")
    except Exception:
        return dt.strftime("%H:%M UTC")


def local_day(dt: datetime, cfg: dict) -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(cfg["tz"])).strftime("%a %H:%M")
    except Exception:
        return dt.strftime("%a %H:%M UTC")


def strength_line(w: dict, cfg: dict) -> str:
    minimum = driver(cfg["speed_min"], cfg["bz_max"])
    ratio = w["driver"] / minimum if minimum else 0
    return (f"{fmt(w['speed'], 0, ' km/s')}, Bz {fmt(w['bz'], 1, ' nT')} → "
            f"{w['driver']:.1f} mV/m ({ratio:.1f}× your minimum)")


def kp_line(r: dict) -> str:
    return f"Kp now: {fmt(r['kp'], 2)} ({g_label(g_level(r.get('kp_index')))})"


def format_incoming(r: dict, eta: datetime, now: datetime, expected, cfg: dict) -> str:
    minutes = max(0, round((eta - now).total_seconds() / 60))
    lines = [
        f"⚠️ STORM INCOMING — arrives ~{local(eta, cfg)} (in ~{minutes} min)",
        f"At the satellite: {strength_line(r['l1'], cfg)}",
    ]
    if expected:
        g = expected["k"] - 4
        lines.append(f"NOAA expects: {g_label(g)} (K{expected['k']} warning)")
    lines.append(kp_line(r))
    return "\n".join(lines)


def format_stronger(r: dict, multiple: int, cfg: dict) -> str:
    l1 = r["l1"]
    label = f"{multiple}×" if multiple < 4 else "4×+"
    # arrival of the stronger part: its first minute at the satellite + travel time
    level = multiple * driver(cfg["speed_min"], cfg["bz_max"])
    firsts = [s["t"] for s in r.get("l1_samples") or [] if driver(s["speed"], s["bz"]) >= level]
    arrives = (min(firsts) + timedelta(seconds=cfg["distance_km"] / l1["speed"])) if firsts else l1["last_arrival"]
    return "\n".join([
        f"⬆️ STRENGTHENING — {label} your minimum at the satellite",
        strength_line(l1, cfg),
        f"Reaches Earth ~{local(arrives, cfg)}",
    ])


def format_here(r: dict, l1_storm: bool, front, g_now: int, cfg: dict) -> str:
    e = r["earth"]
    head = "🌩 STORM HERE — hitting Earth now"
    if front:
        head += f" (front arrived ~{local(front, cfg)})"
    lines = [head, f"Arriving: {strength_line(e, cfg)}", kp_line(r) + " · Kp rises with a lag"]
    if l1_storm and r["l1"]:
        ahead = round(cfg["distance_km"] / r["l1"]["speed"] / 60)
        lines.append(f"Satellite still stormy: at least ~{ahead} more min on the way")
    else:
        lines.append("Satellite already calmer: this one may be short")
    return "\n".join(lines)


def format_g_up(r: dict, g: int) -> str:
    return f"⬆️ {g_label(g).upper()} storm now — Kp {fmt(r['kp'], 2)}"


def format_over(r: dict, lasted: timedelta, peak_kp: float) -> str:
    e = r["earth"] or {}
    peak_g = g_level(round(peak_kp)) if peak_kp else 0
    return "\n".join([
        "✅ Storm over at Earth",
        f"Lasted ~{max(lasted.total_seconds(), 0) / 3600:.1f} h · peak Kp {peak_kp:.2f} ({g_label(peak_g)})",
        f"Now arriving: {fmt(e.get('speed'), 0, ' km/s')}, Bz {fmt(e.get('bz'), 1, ' nT')}",
    ])


def format_watch(it: dict) -> str:
    lines = [f"👀 NOAA WATCH — {g_label(it['g'])} storm predicted"]
    if it.get("detail"):
        lines.append(it["detail"])
    lines.append("(forecast days ahead; exact timing comes from the incoming alert)")
    return "\n".join(lines)


def format_warning(it: dict, cfg: dict) -> str:
    g = it["k"] - 4
    until = f" until {local_day(it['valid_to'], cfg)}" if it.get("valid_to") else ""
    return f"📢 NOAA WARNING — K-index {it['k']} expected ({g_label(g)}){until}"


def format_status(r: dict) -> str:
    def part(name, w):
        if not w:
            return f"{name}: n/a"
        return f"{name}: {fmt(w['speed'], 0, ' km/s')}, Bz {fmt(w['bz'], 1, ' nT')}"
    when = f"{r['data_time']:%Y-%m-%d %H:%M} UTC" if r.get("data_time") else "no data"
    return (f"{part('Satellite', r['l1'])} | {part('Arriving at Earth', r['earth'])} | "
            f"Kp {fmt(r['kp'], 2)} | data {when} ({r.get('source')})")


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

TITLES = {"incoming": "Storm incoming", "here": "Storm here", "over": "Storm over",
          "stronger": "Storm strengthening", "g_up": "Storm level up", "watch": "NOAA storm watch",
          "warning": "NOAA storm warning", "cancelled": "Cancelled", "test": "Tracker test"}
URGENT = {"incoming", "here", "stronger", "g_up", "warning"}


def send(kind: str, text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    topic = os.environ.get("NTFY_TOPIC", "").strip()

    if token and chat_id:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"Sent {kind} to Telegram")
    elif topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(topic)}",
            data=text.encode("utf-8"),
            headers={"Title": TITLES.get(kind, "Space weather"),
                     "Priority": "urgent" if kind in URGENT else "default"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"Sent {kind} to ntfy")
    else:
        print(f"[no TELEGRAM_* or NTFY_TOPIC set; would send {kind}]\n{text}")


def find_chat_id() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN first.")
    data = fetch_json(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        sys.exit("No messages found. Send your bot any message in Telegram, then run this again.")
    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}   ({name})")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def fetch_readings(now: datetime, cfg: dict) -> dict:
    wind = fetch_json(WIND_URL)
    mag = fetch_json(MAG_URL)
    optional = {}
    for name, url in (("kp", KP_URL), ("alerts", ALERTS_URL)):
        try:
            optional[name] = fetch_json(url)
        except Exception as exc:  # informational feeds: carry on without them
            print(f"{name} fetch failed: {exc}")
            optional[name] = None
    readings = build_readings(wind, mag, optional["kp"], now, cfg)
    readings["noaa"] = parse_noaa(optional["alerts"])
    return readings


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"phase": "idle"}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def check_once(state_path: str, cfg: dict, test: bool = False) -> None:
    now = datetime.now(timezone.utc)
    try:
        readings = fetch_readings(now, cfg)
    except Exception as exc:  # network hiccup: try again next run, don't fail the job
        print(f"Fetch failed: {exc}")
        return
    print(format_status(readings))

    if test:
        warn = active_warning(readings["noaa"], now)
        noaa = f"NOAA: K{warn['k']} warning active" if warn else "NOAA: no active K-index warning"
        send("test", "🛰 Tracker test: delivery works.\n"
             + format_status(readings).replace(" | ", "\n") + "\n" + noaa)

    state = load_state(state_path)
    new_state, messages = step(state, readings, now, cfg)
    for kind, text in messages:
        send(kind, text)
    if new_state != state:
        save_state(state_path, new_state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Solar wind storm alerts")
    parser.add_argument("--state", default="state.json", help="state file path")
    parser.add_argument("--loop", action="store_true", help="check every --interval seconds")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--test", action="store_true", help="send a test message first")
    parser.add_argument("--find-chat-id", action="store_true", help="print your Telegram chat ID")
    args = parser.parse_args()

    if args.find_chat_id:
        find_chat_id()
        return

    cfg = load_config()
    check_once(args.state, cfg, test=args.test)
    while args.loop:
        time.sleep(args.interval)
        check_once(args.state, cfg)


if __name__ == "__main__":
    main()
