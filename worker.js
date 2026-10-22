// Space weather Telegram bot: replies to /status instantly with live readings.
// Cloudflare Worker (module syntax). Secrets: BOT_TOKEN, CHAT_ID, WEBHOOK_SECRET.
// Storm alerts themselves still come from tracker.py on GitHub Actions.

const WIND_URL = "https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json";
const MAG_URL = "https://services.swpc.noaa.gov/json/rtsw/rtsw_mag_1m.json";
const KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json";
const ALERTS_URL = "https://services.swpc.noaa.gov/products/alerts.json";
const STATE_URL = "https://raw.githubusercontent.com/zorro1906/space-weather-tracker/main/state.json";

const SPEED_MIN = 500;      // km/s
const BZ_MAX = -10;         // nT
const AVG_MIN = 10;         // median window, minutes
const BIN_MIN = 10;         // Earth forecast block, minutes
const DIST_KM = 1.35e6;     // L1 to bow shock
const MIN_DRIVER = SPEED_MIN * Math.abs(BZ_MAX) / 1000;  // 5 mV/m, your minimum
const TZ = "Europe/Prague";
const G_NAMES = {1: "Minor", 2: "Moderate", 3: "Strong", 4: "Severe", 5: "Extreme"};
const MONTHS = {Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET") {
      if (url.pathname === "/status" || url.pathname === "/details") {
        const text = await statusText(Date.now(), url.pathname === "/details");
        return new Response(text, {headers: {"content-type": "text/plain; charset=utf-8"}});
      }
      return new Response("space weather bot is running");
    }
    if (env.WEBHOOK_SECRET && request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", {status: 403});
    }
    let update = null;
    try { update = await request.json(); } catch (e) { return new Response("ok"); }
    const msg = update && (update.message || update.edited_message);
    if (!msg || !msg.text || String(msg.chat.id) !== String(env.CHAT_ID)) return new Response("ok");

    const cmd = msg.text.trim().toLowerCase().replace(/^\//, "").split(/[\s@]/)[0];
    let reply;
    if (cmd === "status" || cmd === "s" || cmd === "now" || cmd === "details" || cmd === "d") {
      try { reply = await statusText(Date.now(), cmd === "details" || cmd === "d"); }
      catch (e) { reply = "Could not reach NOAA just now (" + e.message + "). Try again in a minute."; }
    } else if (cmd === "start" || cmd === "help") {
      reply = "Commands:\n/status - Bz and wind at Earth now and for the next ~45 min\n/details - the full numbers\n\n" +
        "🟢 Bz -5 or lower\n🟡 Bz -1 to -5\n⚪ Bz around 0 or north\n🌩 storm level (Bz -10 or lower and wind over 500 km/s)\n\n" +
        "Storm alerts arrive on their own.";
    } else {
      return new Response("ok");
    }
    await fetch("https://api.telegram.org/bot" + env.BOT_TOKEN + "/sendMessage", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({chat_id: msg.chat.id, text: reply}),
    });
    return new Response("ok");
  },
};

// ---------------------------------------------------------------- data

async function getJson(url) {
  const r = await fetch(url, {headers: {"User-Agent": "space-weather-bot/1.0"}, cf: {cacheTtl: 30}});
  if (!r.ok) throw new Error(url.split("/").pop() + " " + r.status);
  return r.json();
}

async function getOptional(url) {
  try { return await getJson(url); } catch (e) { return null; }
}

function parseTag(tag) {
  return Date.parse(String(tag).replace(" ", "T").slice(0, 19) + "Z");
}

function median(values) {
  const v = values.filter(x => x !== null && x !== undefined).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

function mean(values) {
  const v = values.filter(x => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

function activeRows(rows, field) {
  const all = (rows || []).filter(r => r && typeof r === "object" && r[field] !== null && r[field] !== undefined);
  const active = all.filter(r => r.active === true);
  return active.length ? active : all;
}

function buildSamples(windRows, magRows) {
  const wind = new Map();
  for (const r of activeRows(windRows, "proton_speed")) wind.set(Math.floor(parseTag(r.time_tag) / 60000), r);
  const samples = [];
  for (const r of activeRows(magRows, "bz_gsm")) {
    const minute = Math.floor(parseTag(r.time_tag) / 60000);
    const w = wind.get(minute);
    if (!w || !w.proton_speed) continue;
    samples.push({
      t: minute * 60000,
      speed: Number(w.proton_speed),
      bz: Number(r.bz_gsm),
      bt: r.bt === null || r.bt === undefined ? null : Number(r.bt),
      source: r.source || "?",
    });
  }
  samples.sort((a, b) => a.t - b.t);
  const span = AVG_MIN * 60000;
  for (let i = 0; i < samples.length; i++) {
    const s = samples[i];
    const recent = samples.slice(Math.max(0, i - 60), i + 1).filter(x => x.t > s.t - span).map(x => x.speed);
    s.arrival = s.t + (DIST_KM / median(recent)) * 1000;
  }
  return samples;
}

function dropOvertaken(samples) {
  const kept = [];
  let earliest = null;
  for (let i = samples.length - 1; i >= 0; i--) {
    if (earliest === null || samples[i].arrival < earliest) {
      kept.push(samples[i]);
      earliest = samples[i].arrival;
    }
  }
  return kept.reverse();
}

function summarize(win) {
  if (!win.length) return null;
  const speed = median(win.map(s => s.speed));
  const bz = median(win.map(s => s.bz));
  return {speed: speed, bz: bz, bt: mean(win.map(s => s.bt)), driver: speed * Math.max(0, -bz) / 1000};
}

function readings(windRows, magRows, kpRows, now) {
  const samples = buildSamples(windRows, magRows);
  const avg = AVG_MIN * 60000;
  const out = {l1: null, earth: null, dataTime: null, source: null, kp: null, kpIndex: null,
               kpPrev: null, kpWindowMin: null, l1Eta: null, blocks: []};
  const kpOk = (kpRows || []).filter(r => r && r.estimated_kp !== null && r.estimated_kp !== undefined);
  if (kpOk.length) {
    const newest = kpOk.reduce((a, b) => (a.time_tag > b.time_tag ? a : b));
    out.kp = Number(newest.estimated_kp);
    out.kpIndex = newest.kp_index !== null && newest.kp_index !== undefined ? Number(newest.kp_index) : Math.round(out.kp);
    // NOAA's estimate restarts at every 3-hour UTC block, so keep the last block's final value too.
    const block = 3 * 3600000;
    const tNew = parseTag(newest.time_tag);
    const win = Math.floor(tNew / block);
    const prev = kpOk.filter(x => Math.floor(parseTag(x.time_tag) / block) === win - 1)
      .sort((a, b) => (a.time_tag < b.time_tag ? -1 : 1));
    out.kpPrev = prev.length ? Number(prev[prev.length - 1].estimated_kp) : null;
    out.kpWindowMin = (tNew - win * block) / 60000;
  }
  if (!samples.length) return out;
  const newest = samples[samples.length - 1].t;
  out.dataTime = newest;
  out.source = samples[samples.length - 1].source;
  out.l1 = summarize(samples.filter(s => s.t > newest - avg));
  const prop = dropOvertaken(samples);
  let atEarth = prop.filter(s => s.arrival > now - avg && s.arrival <= now);
  if (!atEarth.length) {
    const pending = prop.filter(s => s.arrival > now);
    if (pending.length && pending.length === prop.length) {
      const first = Math.min.apply(null, pending.map(s => s.arrival));
      atEarth = pending.filter(s => s.arrival < first + avg);
    }
  }
  out.earth = summarize(atEarth);
  out.l1Eta = prop[prop.length - 1].arrival;
  out.blocks = forecast(prop, now);
  return out;
}

// Wind already past the satellite but not yet at Earth, in clock-aligned blocks.
function forecast(prop, now) {
  const span = BIN_MIN * 60000;
  const bins = new Map();
  for (const s of prop) {
    if (s.arrival <= now) continue;
    const k = Math.floor(s.arrival / span);
    if (!bins.has(k)) bins.set(k, []);
    bins.get(k).push(s);
  }
  const keys = Array.from(bins.keys()).sort((a, b) => a - b);
  return keys.map((k, i) => {
    const win = bins.get(k);
    const block = summarize(win);
    block.samples = win;
    block.from = Math.max(k * span, now);
    block.to = i < keys.length - 1 ? keys[i + 1] * span : Math.max.apply(null, win.map(s => s.arrival));
    return block;
  });
}

// When the first storm-level wind lands: earliest qualifying minute around the first storm block.
function frontTime(blocks, i) {
  const pool = (i > 0 ? blocks[i - 1].samples : []).concat(blocks[i].samples);
  const hits = pool.filter(isStorm).map(s => s.arrival);
  return hits.length ? Math.min.apply(null, hits) : blocks[i].from;
}

function parseNoaaTime(text) {
  const m = /^(\d{4}) (\w{3}) (\d{1,2}) (\d{2})(\d{2})$/.exec(String(text).trim());
  if (!m || MONTHS[m[2]] === undefined) return null;
  return Date.UTC(Number(m[1]), MONTHS[m[2]], Number(m[3]), Number(m[4]), Number(m[5]));
}

function activeWarning(alerts, now) {
  const cancelled = new Set();
  const warnings = [];
  for (const a of alerts || []) {
    if (!a || typeof a !== "object") continue;
    const msg = String(a.message || "").replace(/\r/g, "");
    const pid = String(a.product_id || "");
    const serial = /Serial Number:\s*(\d+)/.exec(msg);
    const cancel = /Cancel Serial Number:\s*(\d+)/.exec(msg);
    if (cancel && msg.toUpperCase().indexOf("CANCEL") >= 0) { cancelled.add(pid + "-" + cancel[1]); continue; }
    const w = /WARNING:\s*Geomagnetic K-index of (\d)/i.exec(msg);
    if (!w) continue;
    const valid = /(?:Valid To|Now Valid Until):\s*(\d{4} \w{3} \d{1,2} \d{4})/.exec(msg);
    const validTo = valid ? parseNoaaTime(valid[1]) : parseTag(a.issue_datetime) + 12 * 3600000;
    warnings.push({key: pid + "-" + (serial ? serial[1] : a.issue_datetime), k: Number(w[1]), validTo: validTo});
  }
  const live = warnings.filter(w => !cancelled.has(w.key) && w.validTo && w.validTo >= now);
  return live.length ? live.reduce((a, b) => (b.k > a.k ? b : a)) : null;
}

// ---------------------------------------------------------------- text

function fmt(v, digits, unit) {
  return v === null || v === undefined ? "n/a" : v.toFixed(digits) + (unit || "");
}

function gLabel(g) {
  return g >= 1 ? "G" + g + " " + G_NAMES[g] : "below storm level";
}

function local(ms, withDay) {
  const opts = {timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false};
  if (withDay) opts.weekday = "short";
  return new Intl.DateTimeFormat("en-GB", opts).format(new Date(ms));
}

function utcHM(ms) {
  const d = new Date(ms);
  return String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
}

function isStorm(w) {
  return !!w && w.speed > SPEED_MIN && w.bz <= BZ_MAX;
}

function ratio(w) {
  return (w.driver / MIN_DRIVER).toFixed(1) + "×";
}

function signed(v) {
  return (v > 0 ? "+" : "") + v.toFixed(1);
}

function minsUntil(ms, now) {
  return Math.max(0, Math.round((ms - now) / 60000));
}

function windLine(w) {
  if (!w) return "n/a";
  const dir = w.bz < 0 ? "south" : "north";
  return fmt(w.speed, 0, " km/s") + ", Bz " + fmt(w.bz, 1, " nT") + " (" + dir + ") → " +
    w.driver.toFixed(1) + " mV/m (" + ratio(w) + " your minimum)";
}

function blockLine(b) {
  return local(b.from) + "–" + local(b.to) + "  " + fmt(b.speed, 0, " km/s") + "  Bz " + signed(b.bz) +
    "  " + ratio(b) + (isStorm(b) ? " 🌩" : "");
}

function outlook(blocks, earthNow, now) {
  const last = blocks[blocks.length - 1];
  const i = blocks.findIndex(isStorm);
  if (i < 0) {
    const peak = blocks.reduce((a, b) => (b.driver > a.driver ? b : a));
    return "No storm-level wind before ~" + local(last.to) + " (strongest " + ratio(peak) + " at " + local(peak.from) + ")";
  }
  if (i === 0 && isStorm(earthNow)) {
    const j = blocks.findIndex(b => !isStorm(b));
    return j < 0 ? "🌩 Storm-level wind keeps coming through ~" + local(last.to) + " (as far as the satellite sees)"
                 : "🌩 Storm-level wind keeps coming until ~" + local(blocks[j].from);
  }
  const front = frontTime(blocks, i);
  return "🌩 Storm-level wind reaches Earth ~" + local(front) + " (in ~" + minsUntil(front, now) + " min)";
}

function stateLine(state, now) {
  const phase = state && state.phase;
  if (phase === "incoming" && state.eta) {
    const eta = Date.parse(state.eta);
    return "INCOMING, arrives ~" + local(eta) + " (in ~" + minsUntil(eta, now) + " min)";
  }
  if (phase === "here") {
    return "STORM HERE" + (state.started ? " since ~" + local(Date.parse(state.started)) : "");
  }
  if (phase === "idle") return "quiet (no storm in progress)";
  return "unknown";
}

function dayKey(ms) {
  return new Intl.DateTimeFormat("en-GB", {timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date(ms));
}

function kpNum(v) {
  return Math.abs(v - Math.round(v)) < 0.01 ? String(Math.round(v)) : v.toFixed(1);
}

// Kp as a person would read it: right after a 3-hour reset, show the block that just ended.
function kpText(r) {
  if (r.kp === null || r.kp === undefined) return "Kp n/a";
  const usePrev = r.kpPrev !== null && r.kpPrev > r.kp && r.kpWindowMin < 60;
  const v = usePrev ? r.kpPrev : r.kp;
  const g = Math.round(v) - 4;
  return "Kp " + kpNum(v) + (g >= 1 ? " (G" + g + ")" : "") + (usePrev ? " (last 3 h)" : "");
}

function dot(w) {
  if (isStorm(w)) return "🌩";
  if (w.bz <= -5) return "🟢";
  if (w.bz <= -1) return "🟡";
  return "⚪";
}

function bzSpeed(w) {
  return dot(w) + " Bz " + signed(w.bz) + " · " + fmt(w.speed, 0, " km/s");
}

function headline(r, now) {
  const blocks = r.blocks || [];
  const earth = r.earth;
  const last = blocks.length ? blocks[blocks.length - 1] : null;
  if (isStorm(earth)) {
    if (!last) return "🌩 STORM at Earth now";
    const j = blocks.findIndex(b => !isStorm(b));
    return "🌩 STORM at Earth now, " + (j < 0 ? "holds through ~" + local(last.to) : "until ~" + local(blocks[j].from));
  }
  const i = blocks.findIndex(isStorm);
  if (i >= 0) {
    const front = frontTime(blocks, i);
    return "🌩 STORM arrives ~" + local(front) + " (in ~" + minsUntil(front, now) + " min)";
  }
  if (!earth) return "No recent satellite data";
  const south = w => w.bz <= -1;
  if (south(earth)) {
    if (!last) return "South Bz now";
    const j = blocks.findIndex(b => !south(b));
    return "South Bz now, " + (j < 0 ? "holds through ~" + local(last.to) : "until ~" + local(blocks[j].from));
  }
  const j = blocks.findIndex(south);
  return j >= 0 ? "Bz north/neutral now, turns south ~" + local(blocks[j].from) : "Quiet, Bz north/neutral";
}

function stormWord(state) {
  const phase = state && state.phase;
  if (phase === "incoming" && state.eta) return "INCOMING ~" + local(Date.parse(state.eta));
  if (phase === "here") return "HERE" + (state.started ? " since ~" + local(Date.parse(state.started)) : "");
  return "none";
}

function formatSimple(r, warn, state, now) {
  const lines = ["🛰 " + local(now) + " · " + headline(r, now), ""];
  lines.push("Earth now:  " + (r.earth ? bzSpeed(r.earth) : "no recent data"));
  if (r.blocks && r.blocks.length) {
    const last = r.blocks[r.blocks.length - 1];
    lines.push("");
    lines.push("Next " + minsUntil(last.to, now) + " min:");
    for (const b of r.blocks) lines.push(local(b.from) + "  " + bzSpeed(b));
    lines.push("After " + local(last.to) + ": not visible yet");
  }
  lines.push("");
  lines.push("Storm: " + stormWord(state) + " · " + kpText(r));
  if (warn) {
    lines.push("NOAA: K" + warn.k + " warning" + (warn.k >= 5 ? " (G" + (warn.k - 4) + ")" : "") +
      " until " + local(warn.validTo, dayKey(warn.validTo) !== dayKey(now)));
  }
  if (r.dataTime && now - r.dataTime > 15 * 60000) {
    lines.push("⚠️ Satellite data is " + Math.round((now - r.dataTime) / 60000) + " min old");
  }
  return lines.join("\n");
}

async function statusText(now, detailed) {
  const [wind, mag, kp, alerts, state] = await Promise.all([
    getJson(WIND_URL), getJson(MAG_URL), getOptional(KP_URL), getOptional(ALERTS_URL), getOptional(STATE_URL),
  ]);
  const format = detailed ? formatStatus : formatSimple;
  return format(readings(wind, mag, kp, now), activeWarning(alerts, now), state, now);
}

function formatStatus(r, warn, state, now) {
  const lines = ["🛰 Space weather · " + local(now), ""];
  if (r.l1) {
    lines.push("Satellite (L1), latest " + local(r.dataTime) + ":");
    lines.push(windLine(r.l1) + (isStorm(r.l1) ? " ⚠️" : ""));
    if (r.l1Eta > now) lines.push("→ hits Earth ~" + local(r.l1Eta) + " (in ~" + minsUntil(r.l1Eta, now) + " min)");
    lines.push("");
  }
  lines.push("Hitting Earth now:");
  lines.push(r.earth ? windLine(r.earth) + (isStorm(r.earth) ? " 🌩" : "") : "n/a (no recent satellite data)");
  lines.push("");
  if (r.blocks && r.blocks.length) {
    lines.push("Coming to Earth (" + BIN_MIN + "-min blocks):");
    for (const b of r.blocks) lines.push(blockLine(b));
    lines.push(outlook(r.blocks, r.earth, now));
  } else {
    lines.push("Coming to Earth: nothing in the pipeline (satellite data may be stale)");
  }
  lines.push("");
  lines.push(kpText(r));
  lines.push("Storm state: " + stateLine(state, now));
  lines.push(warn ? "NOAA: K" + warn.k + " warning (" + gLabel(warn.k - 4) + ") until " + local(warn.validTo, true)
                  : "NOAA: no active K-index warning");
  if (r.dataTime) {
    lines.push("Data: " + Math.round((now - r.dataTime) / 60000) + " min old (" + r.source + ", " + utcHM(r.dataTime) + " UTC)");
  }
  return lines.join("\n");
}
