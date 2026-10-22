# Space weather tracker

Telegram alerts on your phone for geomagnetic storms, matched to how strong they are.

Your minimum for a storm: **solar wind above 500 km/s with IMF Bz at −10 nT or lower** (southward), using 10-minute medians so brief spikes don't count. Everything stronger gets its own alerts on top.

NOAA's real-time data comes from the satellite at L1, about 1.5 million km toward the Sun. What it measures reaches Earth roughly 30–60 minutes later.

| Message | When |
| --- | --- |
| 👀 **NOAA watch** | Days ahead: NOAA forecasters predict a G2+ storm (usually a CME on its way), with the predicted level per day. |
| 📢 **NOAA warning** | Hours ahead: NOAA expects K-index 6+ (G2+). |
| ⚠️ **Storm incoming** | The satellite sees your minimum. Gives arrival time, minutes to go (usually 25–40), and strength as a multiple of your minimum. |
| ⬆️ **Strengthening** | The driver at the satellite reaches 2×, 3× or 4× your minimum, with when that stronger part reaches Earth. |
| 🌩 **Storm here** | It's reaching Earth. Shows Kp and G level, and whether the satellite is still stormy (at least ~35–45 more min on the way). |
| ⬆️ **G level up** | Kp at Earth reaches a new level: G1 Minor, G2 Moderate, G3 Strong, G4 Severe, G5 Extreme. |
| ✅ **Storm over** | Calm at Earth for 30 minutes. Duration and peak level. |
| ↩️ **Cancelled** | A warning fizzled before reaching Earth, or NOAA cancelled its watch/warning. |

Each alert is sent once; levels only ever escalate within a storm.

**Strength** is the solar wind electric field, speed × southward Bz, in mV/m. Your minimum, 500 km/s × 10 nT = 5 mV/m, is the classic driver of intense storms when it lasts 3+ hours (Gonzalez & Tsurutani 1987). The **G level** is NOAA's official scale from the Kp index measured at Earth: Kp 5 = G1 up to Kp 9 = G5.

Example run (simulated storm that strengthens and pushes Kp to 7):

```
⚠️ STORM INCOMING — arrives ~13:36 (in ~26 min)
At the satellite: 620 km/s, Bz -12.0 nT → 7.4 mV/m (1.5× your minimum)
NOAA expects: G2 Moderate (K6 warning)
Kp now: 3.33 (below storm level)

⬆️ STRENGTHENING — 3× your minimum at the satellite
750 km/s, Bz -25.0 nT → 18.8 mV/m (3.8× your minimum)
Reaches Earth ~14:00

🌩 STORM HERE — hitting Earth now (front arrived ~13:36)
Arriving: 620 km/s, Bz -12.0 nT → 7.4 mV/m (1.5× your minimum)
Kp now: 4.00 (below storm level) · Kp rises with a lag
Satellite still stormy: at least ~30 more min on the way

⬆️ G1 MINOR storm now — Kp 5.00
⬆️ G2 MODERATE storm now — Kp 6.33
⬆️ G3 STRONG storm now — Kp 7.00

✅ Storm over at Earth
Lasted ~2.1 h · peak Kp 7.00 (G3 Strong)
Now arriving: 400 km/s, Bz -2.0 nT
```

How "arrival" is worked out: each minute of satellite data is shifted by its travel time (distance ÷ speed, to Earth's bow shock about 1.35 million km away), the same idea as NOAA's propagated solar wind. Expect it to be accurate to within about 5–10 minutes; the "here" message itself can come up to ~15 minutes after the front, because it waits for a 10-minute median and checks every 5 minutes. Kp is measured on the ground, so it climbs after the storm has arrived.

## 1. Make a Telegram bot (3 minutes)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
2. BotFather replies with a **token** like `123456789:AAH...`. Keep it private.
3. Open your new bot and send it any message (e.g. `hi`). Bots can only message people who wrote to them first.
4. Get your **chat ID**: open this in a browser, with your token pasted in:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find `"chat":{"id":123456789` — that number is your chat ID.

## 2. Run it free on GitHub (no computer needed)

1. Create a new **public** repository on GitHub. Actions are free for public repos; a private repo's free monthly minutes would run out at a check every 5 minutes. Nothing secret goes in the repo.
2. Upload `tracker.py`.
3. Add the workflow: **Add file → Create new file**, type the name `.github/workflows/space-weather.yml` (the slashes create the folders), and paste in the contents of `space-weather.yml`. Commit.
4. **Settings → Secrets and variables → Actions → New repository secret**, add two:
   - `TELEGRAM_BOT_TOKEN` = your token
   - `TELEGRAM_CHAT_ID` = your chat ID
5. **Actions** tab → **Space weather check** → **Run workflow** (leave "Send a test message" ticked). Within a minute your phone should get a test message with the live readings, both at the satellite and arriving at Earth.

From then on it checks every 5 minutes on its own.

Two GitHub quirks:
- Scheduled runs can start a few minutes late when GitHub is busy.
- In a public repo, GitHub pauses scheduled workflows after 60 days with no activity in the repo. It emails you first; open the Actions tab and click **Enable workflow** to restart it.

## Or run it on your own server

Any always-on machine with Python 3.9+ or Docker:

```bash
export TELEGRAM_BOT_TOKEN=123456789:AAH...
export TELEGRAM_CHAT_ID=123456789
python3 tracker.py --test --loop       # test message, then a check every 5 min
```

With Docker, from the folder containing `tracker.py`:

```bash
docker run -d --name space-weather --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_CHAT_ID=... \
  -v "$PWD":/app -w /app python:3.12-slim python tracker.py --loop
```

## Settings

Set as environment variables (on GitHub, edit the `env:` block in the workflow):

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPEED_MIN` | 500 | Storm when speed is above this (km/s) |
| `BZ_MAX` | -10 | ...and Bz is at or below this (nT) |
| `AVG_MINUTES` | 10 | Median window, so brief spikes don't trigger |
| `CLEAR_MINUTES` | 30 | Calm at Earth for this long before "storm over" |
| `CANCEL_MINUTES` | 30 | How long past the ETA before a fizzled warning is cancelled |
| `NOAA_MIN_G` | 2 | Relay NOAA watches and warnings from this G level up (1 = include the frequent G1s) |
| `TZ_NAME` | Europe/Prague | Time zone for arrival times |
| `L1_DISTANCE_KM` | 1350000 | Travel distance used for the arrival estimate |
| `STALE_MINUTES` | 30 | Ignore data older than this (satellite outage) |

**ntfy instead of Telegram:** install the free ntfy app, subscribe to a hard-to-guess topic name, and set `NTFY_TOPIC` instead of the two Telegram values.

## Data sources

- Solar wind: https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json
- Magnetic field: https://services.swpc.noaa.gov/json/rtsw/rtsw_mag_1m.json
- Kp (1-minute estimate): https://services.swpc.noaa.gov/json/planetary_k_index_1m.json
- NOAA watches and warnings: https://services.swpc.noaa.gov/products/alerts.json
- G scale: https://www.swpc.noaa.gov/noaa-scales-explanation

These feeds carry several spacecraft at once; the tracker uses only the one NOAA flags as active.
