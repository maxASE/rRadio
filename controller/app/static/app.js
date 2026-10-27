"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const VIEWS = ["home", "channel", "schedule", "sources", "settings"];
let cfg = null;
let status = null;
let mediaLib = { files: [], folders: [] };
let playlists = null; // Navidrome playlists, loaded on demand
let channelId = null;
try { channelId = localStorage.getItem("channel"); } catch {}
let view = "home";
let sourceFilter = "all";
// Who is logged in: the admin (or anyone, without a UI password) or a user with own channels.
let me = { admin: true, user: null, admin_name: "admin" };
let userList = null; // the admin's list of users, loaded on demand
let openSource = null; // key of the source row whose editor is open

// --- helpers -------------------------------------------------------------------

async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch(path, {
    headers: isForm ? {} : { "Content-Type": "application/json" },
    ...opts,
    body: opts.body === undefined ? undefined : isForm ? opts.body : JSON.stringify(opts.body),
  });
  if (res.status === 401) {
    // Logged out (or the session expired): go to the log-in page and come back here after.
    location.href = `/login?next=${encodeURIComponent(location.pathname + location.hash)}`;
    throw new Error("Please log in");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail || res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => `${d.loc}: ${d.msg}`).join("; ");
    throw new Error(msg);
  }
  return data;
}

let toastTimer;
function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), error ? 6000 : 2500);
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) e.append(c);
  return e;
}

const pad2 = (n) => String(n).padStart(2, "0");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const fmtTime = (s) => {
  if (s == null || !isFinite(s)) return "";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${pad2(m)}` : m) + ":" + pad2(sec);
};
const fmtAgo = (ts) => {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};
const fmtSize = (b) => (b > 1 << 20 ? `${(b / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} kB`);
const chq = () => (channelId ? `channel=${encodeURIComponent(channelId)}` : "");
// navigator.clipboard only exists on https or localhost, so plain http://rradio.hp
// falls back to the old execCommand route, and finally to a prompt to copy by hand.
function copy(text) {
  const manual = () => window.prompt("Copy this link:", text);
  if (window.isSecureContext && navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => toast("Copied"), manual);
    return;
  }
  const ta = el("textarea", { readonly: "", style: "position:fixed;top:0;left:0;opacity:0" });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  ok ? toast("Copied") : manual();
}

// --- the radio's clock -------------------------------------------------------------
//
// Programs and spots run on the radio's time zone, which may differ from the
// browser's. "Wall" times are milliseconds whose UTC fields read as the radio's
// local time.

let clockBase = null;
function setServerTime(iso) {
  const m = iso && iso.match(/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)/);
  if (m) clockBase = { wall: Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]), at: Date.now() };
}
const wallNow = () => (clockBase ? clockBase.wall + Date.now() - clockBase.at : Date.now() - new Date().getTimezoneOffset() * 60000);
const wallOfTs = (ts) => ts * 1000 + (wallNow() - Date.now()); // epoch seconds -> wall
const dayOf = (wall) => (new Date(wall).getUTCDay() + 6) % 7; // Monday = 0
const minuteOf = (wall) => { const d = new Date(wall); return d.getUTCHours() * 60 + d.getUTCMinutes(); };
const dateOf = (wall) => new Date(wall).toISOString().slice(0, 10);
const hhmm = (min) => `${pad2(Math.floor(min / 60) % 24)}:${pad2(min % 60)}`;
const toMin = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
const isoClock = (iso) => (iso ? iso.slice(11, 16) : "");
// "today 18:00", "tomorrow 18:00" or "Fri 18:00"
function whenLabel(iso) {
  if (!iso) return "";
  const d = Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10));
  const today = Date.parse(dateOf(wallNow()) + "T00:00:00Z");
  const days = Math.round((d - today) / 86400000);
  const day = days === 0 ? "today" : days === 1 ? "tomorrow" : DAY_NAMES[dayOf(d)];
  return `${day} ${isoClock(iso)}`;
}

// "Next program: …", saying first when a program in an open window is already over.
function nextProgramLine(windows, nxt, spot) {
  const parts = [];
  // A spot or news bulletin coming before the next program is what plays next.
  if (spot && (!nxt || new Date(spot.at) < new Date(nxt.start))) parts.push(`Next up: ${spot.name} · ${whenLabel(spot.at)}. `);
  for (const w of windows || []) {
    if (w.state === "ended") parts.push(`${w.name} ended at ${isoClock(w.ended_at)}. `);
    else if (w.state === "waiting") parts.push(`${w.name} starts with the show, if it comes before ${isoClock(w.until)}. `);
  }
  parts.push(`Next program: ${nxt ? `${nxt.name} · ${whenLabel(nxt.start)}` : "none scheduled"}`);
  return parts.join("");
}

// The parts of day `d` (0 = Monday) that program `p` covers, in minutes.
function programSpans(p, d) {
  const a = toMin(p.start), b = toMin(p.end);
  const spans = [];
  if (p.days.includes(DAYS[d])) spans.push([a, b > a ? b : 1440]);
  if (b <= a && b > 0 && p.days.includes(DAYS[(d + 6) % 7])) spans.push([0, b]); // yesterday's, past midnight
  return spans;
}

// The minutes of day `d` a timed spot fires at.
function spotMinutes(s, d) {
  if (!s.enabled || s.trigger === "songs" || !s.days.includes(DAYS[d])) return [];
  const a = toMin(s.active_from), b = toMin(s.active_to);
  const active = (m) => a === b || (a < b ? m >= a && m < b : m >= a || m < b);
  let mins;
  if (s.trigger === "times") mins = s.times.map(toMin);
  else {
    mins = [];
    for (let m = 0; m < 1440; m++) if ((((m - s.offset_minutes) % s.every_minutes) + s.every_minutes) % s.every_minutes === 0) mins.push(m);
  }
  return mins.filter(active);
}

// The channel being looked at.
function channel() {
  return cfg.channels.find((c) => c.id === channelId) || mine()[0] || cfg.channels[0];
}

// Channels this person may change: all for the admin, else their own.
const canEdit = (c) => !!c && (me.admin || (!!me.user && c.owner === me.user.id));
const mine = () => (cfg ? cfg.channels.filter(canEdit) : []);
const ownsSource = (src) => me.admin || (!!me.user && src.owner === me.user.id);
function ownerName(id) {
  if (!id) return me.admin_name;
  if (me.user && id === me.user.id) return me.user.name;
  const u = (userList || []).find((x) => x.id === id);
  return u ? u.name : status?.channels.find((c) => c.owner === id)?.owner_name || "another user";
}

async function saveConfig(msg = "Saved") {
  try {
    cfg = await api("/api/config", { method: "PUT", body: cfg });
    renderConfig();
    toast(msg);
  } catch (e) {
    toast(e.message, true);
    await loadConfig();
  }
}

// --- views ------------------------------------------------------------------------
//
// #/home, #/channel/<id>, #/schedule/<id>, #/sources/<type>, #/settings

function setChannel(id) {
  if (!cfg || id === channelId || !cfg.channels.some((c) => c.id === id)) return;
  channelId = id;
  try { localStorage.setItem("channel", id); } catch {}
  status = null;
  renderConfig();
  connectLive();
  if (!live) refresh();
}

function route() {
  const [, v, arg] = location.hash.split("/");
  view = VIEWS.includes(v) ? v : "home";
  if (arg && (view === "channel" || view === "schedule")) setChannel(decodeURIComponent(arg));
  if (view === "sources") sourceFilter = arg || "all";
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${view}`));
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === (view === "channel" ? "home" : view)));
  if (view === "sources" || view === "schedule") loadMedia();
  if (view === "settings") { loadDevices(); if (me.admin && me.login) loadUsers(); }
  else if (pairing) hidePairing();
  if (cfg) renderConfig();
  if (status) renderStatus();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// Chips that switch the channel within a view.
function renderChannelChips(box, target) {
  box.replaceChildren(
    ...(cfg.channels.length > 1
      ? cfg.channels.map((c) => el("a", { href: `#/${target}/${encodeURIComponent(c.id)}`, class: "chip" + (c.id === channel().id ? " active" : "") }, c.name))
      : []),
  );
}

// Close buttons on the add cards.
document.addEventListener("click", (e) => {
  const close = e.target.closest("[data-close]");
  if (close) close.closest(".add-card").classList.add("hidden");
});

// --- in-page player ----------------------------------------------------------------
//
// The stream is live, so "stop" drops the connection and "listen" opens a fresh
// one. Buttons follow the audio element's own events, and a dropped stream
// (e.g. the audio engine restarting after a channel change) reconnects by itself.

const player = $("#player");
let listenCh = null; // the channel the player is on; null = off
let listenSrc = "";
let playerState = "off";
let retries = 0;
let retryTimer = null;

const streamOf = (cid) => status?.channels.find((c) => c.id === cid)?.stream || "";

function updateListenButtons() {
  for (const b of $$("[data-listen]")) {
    const cid = b.dataset.listen || channelId;
    const st = cid === listenCh ? playerState : "off";
    if (b.classList.contains("big-play")) {
      b.textContent = { off: "▶", connecting: "…", on: "■" }[st];
      b.setAttribute("aria-label", { off: "Listen here", connecting: "Connecting, click to stop", on: "Stop listening" }[st]);
    } else {
      b.textContent = { off: "▶ Listen", connecting: "… Connecting", on: "■ Stop" }[st];
    }
    b.classList.toggle("playing", st !== "off");
  }
}

function setPlayerState(st) {
  playerState = st;
  updateListenButtons();
}

function startListening(cid = listenCh) {
  clearTimeout(retryTimer);
  const src = streamOf(cid);
  if (!src) return toast("No stream is switched on. Turn on a format under Settings.", true);
  listenCh = cid;
  listenSrc = src;
  setPlayerState("connecting");
  player.src = src + (src.includes("?") ? "&" : "?") + "t=" + Date.now(); // never reuse a stale buffer
  player.play().catch((e) => {
    // AbortError: stopped, or a new stream was opened, before the old one started.
    // Load failures are reported once by the "error" event below.
    if (e.name === "NotAllowedError") {
      stopListening();
      toast("The browser blocked playback. Click Listen again.", true);
    }
  });
}

function stopListening() {
  listenCh = null;
  retries = 0;
  clearTimeout(retryTimer);
  player.pause();
  player.removeAttribute("src");
  player.load();
  setPlayerState("off");
}

function streamFailed(why) {
  if (!listenCh) return;
  if (retries < 5) {
    retries++;
    setPlayerState("connecting");
    retryTimer = setTimeout(startListening, 1000 * 2 ** retries);
    return;
  }
  const src = listenSrc;
  stopListening();
  toast(`Can't play ${src}: ${why}. Open the link in a new tab to check it. Behind a reverse proxy, the stream path must be forwarded to Icecast.`, true);
}

player.addEventListener("playing", () => { if (listenCh) { retries = 0; setPlayerState("on"); } });
player.addEventListener("waiting", () => { if (listenCh) setPlayerState("connecting"); });
player.addEventListener("ended", () => streamFailed("the stream ended"));
player.addEventListener("error", () => {
  const code = player.error && player.error.code;
  streamFailed(code === 2 ? "network error" : code === 4 ? "the stream is not available" : "playback error");
});
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-listen]");
  if (!b) return;
  const cid = b.dataset.listen || channelId;
  if (cid === listenCh) stopListening();
  else startListening(cid);
});

// --- status ------------------------------------------------------------------------

const MODE_LABEL = {
  station: "On air · Station",
  program: "On air · Program",
  cutin: "Cut-in",
  silence: "Waiting for stream",
  offline: "Audio engine offline",
};
const PILL = { station: "RADIO", program: "PROGRAM", cutin: "CUT-IN", silence: "WAITING", offline: "OFFLINE" };
const KIND_NAME = { podcast: "Podcast", video: "Video", spot: "Spot", media: "Own media", inbox: "Inbox", url: "Link", program: "Program", voice: "Voice message" };

function nowTitle(np) {
  if (np.mode === "silence" && !np.source) return "No station";
  if (np.mode === "cutin" || np.mode === "program") return np.title && np.mode === "cutin" ? np.title : np.source;
  return np.artist && np.title ? `${np.artist} – ${np.title}` : np.title || np.source || "—";
}

function nowWhy(np) {
  if (np.mode === "cutin") return `${KIND_NAME[np.kind] || "Clip"} · ${np.source}${np.duration ? ` · ${fmtTime(np.elapsed)} of ${fmtTime(np.duration)}` : ""}`;
  if (np.mode === "program") {
    return np.by_title
      ? `Started by the stream title at ${isoClock(np.since)} · ends with the show, at the latest ${isoClock(np.until)}`
      : `Scheduled program until ${isoClock(np.until)}`;
  }
  if (np.mode === "station") return np.title ? `Station · ${np.source}` : "Station";
  if (np.mode === "silence") return np.error || `Waiting for ${np.source || "the stream"} to send audio`;
  return "The audio engine is not reachable";
}

function renderStatus() {
  const s = status;
  if (!s || !cfg) return;
  const np = s.now;
  const multi = s.channels.length > 1;
  const title0 = s.station_name;
  $("#station-name").textContent = title0;
  document.title = s.channel && (np.title || np.source) ? `${nowTitle(np)} · ${multi ? s.channel.name : title0}` : title0;
  $("#live-dot").className = "dot " + (np.mode === "cutin" ? "cutin" : s.engine_up && np.mode !== "silence" ? "live" : "");

  // The player follows a renamed stream.
  if (listenCh && streamOf(listenCh) && streamOf(listenCh) !== listenSrc) startListening(listenCh);

  if (view === "home") renderHome();
  if (view === "channel" && s.channel) renderChannelStatus();
  if (view === "schedule" && s.channel) { renderTimeline(true); renderSpotMeta(); renderProgramMeta(); }
  if (view === "sources") renderSourcesTable(true);
  updateListenButtons();
}

// --- live feed -----------------------------------------------------------------------
// /api/events sends the status once and then only when something changes. While it
// is connected nothing polls; clip times are counted up here. Without it (old
// browser, proxy trouble) the page falls back to asking every 3 seconds.

let live = null; // the EventSource while one is open or connecting
let liveOk = false; // it is connected and delivering
let advancedAt = 0;

function gotStatus(s) {
  status = s;
  advancedAt = Date.now();
  setServerTime(s.time);
  renderStatus();
}

function connectLive() {
  if (live) live.close();
  live = null;
  liveOk = false;
  if (!window.EventSource || document.hidden) return;
  const es = new EventSource(`/api/events?${chq()}`);
  live = es;
  es.onopen = () => { if (live === es) liveOk = true; };
  es.onmessage = (ev) => { if (live === es) { liveOk = true; gotStatus(JSON.parse(ev.data)); } };
  es.onerror = () => {
    if (live !== es) return;
    liveOk = false; // the browser retries by itself; until then, poll
    if (es.readyState === EventSource.CLOSED) { live = null; refresh(); } // e.g. logged out: refresh() goes to the log-in page
  };
}

function tick() {
  if (document.hidden) return;
  if (!liveOk) { refresh(); return; }
  if (!status) return;
  const step = (Date.now() - advancedAt) / 1000;
  advancedAt = Date.now();
  for (const np of [status.now, ...status.channels.map((c) => c.now)]) {
    if (np && np.mode === "cutin" && np.duration) np.elapsed = Math.min(np.duration, np.elapsed + step);
  }
  renderStatus();
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { if (live) live.close(); live = null; liveOk = false; }
  else { connectLive(); if (!live) refresh(); }
});

async function refresh() {
  try {
    gotStatus(await api(`/api/status?${chq()}`));
  } catch (e) {
    $("#np-mode").textContent = "Controller unreachable";
    $("#live-dot").className = "dot";
  }
}

// --- home --------------------------------------------------------------------------

// What a channel plays over the next `hours`, as strip segments and spot ticks.
function lookahead(c, np, hours = 6) {
  const ch = cfg.channels.find((x) => x.id === c.id);
  if (!ch) return { segs: [], ticks: [] };
  const STEP = 5, n = (hours * 60) / STEP;
  const start = wallNow();
  let clipLeft = np.mode === "cutin" && np.duration ? Math.max(STEP, np.duration - np.elapsed) / 60 : 0;
  const inSpan = (p, w) => programSpans(p, dayOf(w)).some(([a, b]) => minuteOf(w) >= a && minuteOf(w) < b);
  // A program whose show is over for today leaves the rest of its window to the station.
  const skipped = new Set((c.windows || []).filter((w) => w.state === "ended").map((w) => w.id));
  const slots = [];
  for (let i = 0; i < n; i++) {
    const w = start + i * STEP * 60000;
    let kind = "radio";
    for (const p of ch.programs) {
      if (!p.enabled) continue;
      if (!inSpan(p, w)) { skipped.delete(p.id); continue; }
      if (!skipped.has(p.id)) kind = p.trigger === "title" ? "title" : "prog";
    }
    if (np.mode === "program" && i * STEP < (Date.parse(np.until) - Date.now()) / 60000) kind = np.by_title ? "title" : "prog";
    if (i * STEP < clipLeft) kind = "clip";
    slots.push(kind);
  }
  const segs = [];
  for (const k of slots) {
    if (segs.length && segs[segs.length - 1].kind === k) segs[segs.length - 1].n++;
    else segs.push({ kind: k, n: 1 });
  }
  const ticks = [];
  const d0 = dayOf(start), m0 = minuteOf(start);
  for (const sp of ch.spots) {
    for (const off of [0, 1]) {
      for (const m of spotMinutes(sp, (d0 + off) % 7)) {
        const ahead = m + off * 1440 - m0;
        if (ahead > 0 && ahead < hours * 60) ticks.push((100 * ahead) / (hours * 60));
      }
    }
  }
  return { segs, ticks };
}

// Cover, channel picture or station logo, served by rRadio. Hidden when there is
// none or it fails to load (and not asked for again).
const badArt = new Set();
function artImg(src, cls) {
  if (!src || badArt.has(src)) return null;
  return el("img", { class: cls, src, alt: "", loading: "lazy", onerror: (e) => { badArt.add(src); e.target.remove(); } });
}

function setArt(img, src) {
  img.onerror = () => { badArt.add(img.getAttribute("src")); img.classList.add("hidden"); };
  const show = !!src && !badArt.has(src);
  img.classList.toggle("hidden", !show);
  if (show && img.getAttribute("src") !== src) img.src = src;
  if (!show) img.removeAttribute("src");
}

function renderHome() {
  const own = status.channels.filter((c) => c.can_edit !== false);
  const others = status.channels.filter((c) => c.can_edit === false);
  const head = (text) => el("h3", { class: "grid-head" }, text);
  $("#channel-cards").replaceChildren(
    ...(me.admin ? [] : [head("My channels")]),
    ...(!me.admin && !own.length ? [el("p", { class: "muted grid-head" }, "No channels of your own yet. Make one with + New channel.")] : []),
    ...own.map(channelCard),
    ...(others.length ? [head("Public channels")] : []),
    ...others.map(channelCard),
  );
  renderIncoming();
}

function channelCard(c) {
  const np = c.now || { mode: c.mode };
  const { segs, ticks } = lookahead(c, np);
  const nxt = c.next_program;
  // Whose it is, for anyone else's channel; "Public" when others can listen.
  const whose = c.owner && !(me.user && c.owner === me.user.id) ? `${c.owner_name || "a user"}'s` : "";
  const tags = [whose && el("span", { class: "owner-tag" }, whose), c.public && el("span", { class: "owner-tag public" }, "Public")].filter(Boolean);
  return el("article", { class: "card channel-card" },
    el("div", { class: "row between" },
      el("span", { class: "row gap" }, el("a", { href: `#/channel/${encodeURIComponent(c.id)}`, class: "card-title" }, c.name), ...tags),
      el("span", { class: `pill ${np.mode}` }, PILL[np.mode] || np.mode)),
    el("div", { class: "cc-now" },
      artImg(np.art, "cc-art"),
      el("div", { class: "grow" },
        el("div", { class: "cc-title" }, nowTitle(np)),
        el("div", { class: "muted small" }, nowWhy(np)))),
    el("div", { class: "strip", "aria-hidden": "true" },
      ...segs.map((g) => el("i", { class: g.kind, style: `flex:${g.n}` })),
      ...ticks.map((t) => el("b", { style: `left:${t}%` }))),
    el("div", { class: "strip-scale", "aria-hidden": "true" }, el("span", {}, "now"), el("span", {}, "+3 h"), el("span", {}, "+6 h")),
    el("div", { class: "muted small" }, nextProgramLine(c.windows, nxt, c.next_spot)),
    el("div", { class: "row gap" },
      el("button", { class: "listen", "data-listen": c.id }),
      c.stream ? el("button", { onclick: () => copy(c.stream) }, "Copy link") : null,
      el("span", { class: "muted small push" }, c.listeners == null ? "" : `${c.listeners} listening`)),
  );
}

function renderIncoming() {
  // New items, one row per clip with every channel it goes to.
  const byClip = new Map();
  for (const i of status.incoming || []) {
    const k = `${i.source}\u0000${i.title}`;
    if (!byClip.has(k)) byClip.set(k, { ...i, chans: [] });
    byClip.get(k).chans.push(cfg.channels.find((c) => c.id === i.channel)?.name || i.channel);
  }
  const rows = [...byClip.values()];
  $("#incoming").replaceChildren(
    ...(rows.length
      ? rows.map((i) =>
          el("li", { class: "item-row" },
            el("div", { class: "grow" },
              el("div", { class: "title" }, i.title),
              el("div", { class: "small muted" }, `${KIND_NAME[i.kind] || i.kind} · ${i.source}${i.duration ? " · " + fmtTime(i.duration) : ""}`)),
            el("span", { class: "tag" }, i.chans.join(", ")),
            el("span", { class: `badge ${i.state}` }, i.state === "downloading" && i.progress != null ? `downloading ${Math.round(i.progress * 100)}%` : i.state)))
      : [el("li", { class: "empty" }, "Nothing waiting. New uploads, episodes and spots will appear here.")]),
  );
}

$("#btn-new-channel").addEventListener("click", () => {
  $("#form-channel").classList.remove("hidden");
  $("#form-channel").name.focus();
});
$("#form-channel").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const first = mine()[0];
  // Start with the first channel's stations, so the new one plays something right away.
  cfg.channels.push({
    name: f.name.value,
    mount: f.mount.value.trim().toLowerCase(),
    stations: first ? first.stations.map(({ id, ...s }) => s) : [],
    programs: [], spots: [], all_sources: true, sources: [],
  });
  f.reset();
  f.classList.add("hidden");
  saveConfig("Channel added. The audio engine restarts for a moment.");
});
$("#btn-check").addEventListener("click", () => api("/api/check", { method: "POST" }).then(() => toast("Checking all sources…")));

// --- one channel ---------------------------------------------------------------------

function renderChannelStatus() {
  const s = status;
  const np = s.now;
  let label = MODE_LABEL[np.mode] || np.mode;
  if (np.mode === "cutin" && KIND_NAME[np.kind]) label += ` · ${KIND_NAME[np.kind]}`;
  $("#np-mode").textContent = label;
  $("#np-title").textContent = nowTitle(np);
  setArt($("#np-art"), np.art);
  const sub = np.mode === "station" || np.mode === "program" ? (np.title ? `Stream title: “${np.artist ? np.artist + " – " : ""}${np.title}”` : "") : np.mode === "cutin" ? np.source : nowWhy(np);
  $("#np-source").textContent = !s.station && np.mode === "silence" ? "Add a station under Sources to get started." : np.error && !sub.includes(np.error) ? [sub, np.error].filter(Boolean).join(" · ") : sub;
  $("#np-source").classList.toggle("bad", !!np.error);
  const isClip = np.mode === "cutin";
  $("#np-progress").classList.toggle("hidden", !isClip);
  $("#np-times").classList.toggle("hidden", !isClip);
  $("#btn-skip").classList.toggle("hidden", !isClip);
  $("#btn-skip-song").classList.toggle("hidden", isClip || np.base_kind !== "navidrome");
  if (isClip && np.duration) {
    $("#np-bar").style.width = `${Math.min(100, (100 * np.elapsed) / np.duration)}%`;
    $("#np-elapsed").textContent = fmtTime(np.elapsed);
    $("#np-duration").textContent = fmtTime(np.duration);
  }

  renderWhy();
  renderStationChips();
  renderRundown();

  // queue
  const queue = s.queue.filter((i) => i.state !== "playing");
  $("#queue-count").textContent = queue.length ? `${queue.length} waiting` : "";
  const held = np.mode === "program";
  $("#queue").replaceChildren(
    ...(queue.length
      ? queue.map((i) =>
          el("li", { class: "item-row" },
            el("div", { class: "grow" },
              el("div", { class: "title" }, i.title),
              el("div", { class: "small muted" },
                `${KIND_NAME[i.kind] || i.kind} · ${i.source}${i.duration ? " · " + fmtTime(i.duration) : ""} · ${fmtAgo(i.arrived)}`,
                held && !i.during_programs && i.state !== "failed" ? " · held until the program ends" : "",
                i.error ? el("span", { class: "bad" }, ` · ${i.error}`) : "")),
            el("span", { class: `badge ${i.state}` }, i.state === "downloading" && i.progress != null ? `${Math.round(i.progress * 100)}%` : i.state),
            ["waiting", "downloading", "ready", "failed"].includes(i.state)
              ? el("button", { class: "small-btn", title: "Remove", "aria-label": `Remove ${i.title}`, onclick: () => api(`/api/queue/${encodeURIComponent(i.key)}`, { method: "DELETE" }).then(refresh).catch((e) => toast(e.message, true)) }, "✕")
              : null))
      : [el("li", { class: "empty" }, "Nothing waiting.")]),
  );

  // listen links
  const q = `&channel=${encodeURIComponent(s.channel.id)}`;
  $("#listen-links").replaceChildren(
    ...Object.entries(s.streams).map(([fmt, url]) =>
      el("li", {},
        el("strong", {}, fmt.toUpperCase()),
        el("code", {}, url),
        el("button", { class: "small-btn", onclick: () => copy(url) }, "Copy"),
        el("a", { href: `/listen.m3u?fmt=${fmt}${q}`, class: "small" }, ".m3u"),
        el("a", { href: `/listen.pls?fmt=${fmt}${q}`, class: "small" }, ".pls"),
      ),
    ),
  );
  $("#listeners").textContent = s.listeners == null ? "" : `${plural(s.listeners, "listener")} right now`;

  $("#history").replaceChildren(
    ...(s.history.length
      ? s.history.map((h) =>
          el("li", { class: "item-row" },
            el("div", { class: "grow" },
              el("span", { class: "title" }, h.title),
              el("span", { class: "small muted" }, ` · ${h.source}${h.detail ? " · " + h.detail : ""}`)),
            el("span", { class: `badge ${h.status}` }, h.status),
            el("span", { class: "small muted" }, fmtAgo(h.ts))))
      : [el("li", { class: "empty" }, "Nothing played yet.")]),
  );
}

function layer(kind, active, title, text, action = null) {
  return el("div", { class: `layer ${kind}${active ? " active" : ""}` },
    el("span", { class: "ldot", "aria-hidden": "true" }),
    el("div", { class: "grow" }, el("strong", {}, title), el("span", { class: "muted" }, ` · ${text}`)),
    action);
}

function renderWhy() {
  const s = status, np = s.now, ch = channel();
  const layers = [];
  const waiting = s.queue.filter((i) => !["playing", "failed"].includes(i.state));

  if (np.mode === "cutin") {
    layers.push(layer("clip", true, `Cut-in · ${np.title}`,
      `${KIND_NAME[np.kind] || "Clip"} from ${np.source}. ${waiting.length ? `${waiting.length} more waiting.` : "The station comes back when it ends."}`));
  } else {
    const onProg = np.mode === "program" && ch.programs.find((p) => p.id === np.program_id);
    const heldN = onProg && !onProg.cutins_interrupt ? waiting.filter((i) => !i.during_programs).length : 0;
    layers.push(layer("clip", false, "Cut-ins",
      waiting.length
        ? `${waiting.length} waiting${heldN ? `, ${heldN} held until the program ends` : ""}.`
        : "Nothing waiting. New uploads and episodes interrupt as soon as they are downloaded."));
  }

  const spot = ((s.channels || []).find((c) => c.id === ch.id) || {}).next_spot;
  layers.push(layer("spot", false, "Spots and news",
    spot
      ? `Next: ${spot.name} · ${whenLabel(spot.at)}.`
      : ch.spots && ch.spots.length ? "None due in the next days." : "None picked for this channel."));

  const prog = np.program_id && ch.programs.find((p) => p.id === np.program_id);
  if (np.mode === "program" || (np.mode === "silence" && np.program_id)) {
    const why = np.by_title && prog
      ? `Started ${isoClock(np.since)} when the stream title matched “${prog.title_start}”. Ends when the title stops matching, at the latest ${isoClock(np.until)}.`
      : `Scheduled ${prog ? `${prog.start}–${prog.end}` : `until ${isoClock(np.until)}`}.`;
    layers.push(layer("prog", true, `Program · ${prog ? prog.name : np.source}`, why,
      el("button", { class: "small-btn", onclick: endProgram }, "End now")));
  } else {
    const open = Object.entries(s.programs || {}).find(([, p]) => p.window_open && !p.on_air);
    const p = open && ch.programs.find((x) => x.id === open[0]);
    const over = (s.windows || []).find((w) => w.state === "ended");
    const next = s.next_program ? ` Next: ${s.next_program.name} · ${whenLabel(s.next_program.start)}.` : "";
    if (over) {
      layers.push(layer("prog", false, `Program · ${over.name}`,
        `Ended at ${isoClock(over.ended_at)}. ${over.by_title ? `Its window runs until ${isoClock(over.until)}, but it only starts again if the stream title matches once more.` : `It stays off until its window closes at ${isoClock(over.until)}.`}${next}`));
    } else if (p && open[1].window_open) {
      layers.push(layer("prog", false, `Program · ${p.name}`,
        `Window open until ${p.end}, waiting for the stream title to match “${p.title_start}”${open[1].title ? `. Now: “${open[1].title}”` : ""}.`));
    } else {
      layers.push(layer("prog", false, "Programs", s.next_program ? `Next: ${s.next_program.name} · ${whenLabel(s.next_program.start)}.` : "None scheduled."));
    }
  }

  const st = s.station;
  if (np.mode === "station") layers.push(layer("station", true, `Station · ${st ? st.name : np.source}`, st ? baseLabel(st) : ""));
  else if (np.mode === "silence") layers.push(layer("station warn", true, `Waiting for ${np.source || "the stream"}`, np.error || "It is not sending audio yet."));
  else if (np.mode === "offline") layers.push(layer("station warn", true, "Audio engine offline", "Check the liquidsoap container."));
  else layers.push(layer("station", false, `Station · ${st ? st.name : "none"}`, np.mode === "program" ? "Resumes after the program." : "Resumes after the clip."));

  $("#why").replaceChildren(...layers);
}

function endProgram() {
  api(`/api/program/end?${chq()}`, { method: "POST" })
    .then((r) => { toast(`${r.name} ended. It stays off until its window closes.`); refresh(); })
    .catch((e) => toast(e.message, true));
}

function renderStationChips() {
  const ch = channel();
  const np = status ? status.now : {};
  $("#station-chips").replaceChildren(
    ...ch.stations.map((s) =>
      el("button", {
        role: "radio", "aria-checked": s.id === ch.current_station ? "true" : "false",
        class: "station-chip" + (s.id === ch.current_station ? " active" : ""),
        title: baseLabel(s),
        onclick: () => {
          if (s.id === ch.current_station) return;
          api("/api/station", { method: "POST", body: { id: s.id, channel: ch.id } }).then(() => {
            ch.current_station = s.id;
            toast(np.mode === "program" ? `${s.name} plays once the program on air ends` : np.mode === "cutin" ? `${s.name} plays after the current clip` : `Now playing ${s.name}`);
            renderStationChips();
            refresh();
          }).catch((e) => toast(e.message, true));
        },
      }, s.name)),
    el("a", { href: "#/sources/station", class: "add-link" }, ch.stations.length ? "+ Add from sources" : "No stations yet. Add one under Sources"),
  );
}

// Today's programme: what already ran, what is on, what is coming.
function renderRundown() {
  const ch = channel();
  const now = wallNow(), today = dateOf(now), d = dayOf(now), nowMin = minuteOf(now);
  const rows = [];
  for (const h of status.history) {
    if (!["played", "started"].includes(h.status)) continue;
    const w = wallOfTs(h.ts);
    if (dateOf(w) !== today) continue;
    rows.push({ min: minuteOf(w), what: h.title, note: h.kind === "program" ? "program" : `${KIND_NAME[h.kind] || h.kind} · ${h.source}`, kind: h.kind === "program" ? "prog" : h.kind === "spot" ? "spot" : "clip", past: true });
  }
  const np = status.now;
  // The program on air already has the "now" row.
  const past = rows.filter((r) => !(np.mode === "program" && r.kind === "prog" && r.what === np.source)).slice(0, 6).reverse();
  const next = [];
  for (const p of ch.programs) {
    if (!p.enabled) continue;
    for (const [a, b] of programSpans(p, d)) {
      if (a > nowMin) next.push({ min: a, what: p.name, note: p.trigger === "title" ? `program · window opens, starts with the show (until ${p.end} at the latest)` : `program · until ${p.end}`, kind: p.trigger === "title" ? "title" : "prog" });
    }
  }
  for (const sp of ch.spots) {
    const m = spotMinutes(sp, d).find((x) => x > nowMin);
    if (m != null) next.push({ min: m, what: sp.name, note: "spot", kind: "spot" });
  }
  next.sort((a, b) => a.min - b.min);
  const on = { min: nowMin, what: nowTitle(np), note: "on air now", kind: np.mode === "cutin" ? "clip" : np.mode === "program" ? "prog" : "radio", now: true };
  const all = [...past, on, ...next.slice(0, 6)];
  $("#rundown").replaceChildren(
    ...all.map((r) =>
      el("li", { class: "rd" + (r.now ? " now" : "") + (r.past ? " past" : "") },
        el("span", { class: "rd-time" }, hhmm(r.min)),
        el("span", { class: `ldot ${r.kind}`, "aria-hidden": "true" }),
        el("span", { class: "grow" }, r.what),
        el("span", { class: "muted small" }, r.note))),
    ...(next.length ? [] : [el("li", { class: "empty" }, "Nothing else scheduled today.")]),
  );
}

function renderChannelSettings() {
  const c = channel();
  const name = el("input", { value: c.name, onchange: (e) => { c.name = e.target.value; saveConfig("Renamed"); } });
  const mount = el("input", { value: c.mount, title: "The stream plays at /<name>.mp3", onchange: (e) => { c.mount = e.target.value.trim().toLowerCase(); saveConfig("Stream renamed. The audio engine restarts for a moment."); } });
  // The admin can hand a channel to a user (or take it back).
  const owner = me.admin && userList && userList.length
    ? el("label", { class: "field" }, "Belongs to",
        el("select", { onchange: (e) => { c.owner = e.target.value; saveConfig(`${c.name} now belongs to ${ownerName(c.owner)}`); } },
          el("option", { value: "", selected: !c.owner }, `${me.admin_name} (admin)`),
          ...userList.map((u) => el("option", { value: u.id, selected: c.owner === u.id }, u.name))))
    : null;
  $("#channel-settings").replaceChildren(
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field" }, "Stream name", mount),
    el("p", { class: "muted small" }, `Plays at /${c.mount}.mp3. Renaming the stream restarts the audio engine for a few seconds.`),
    ...(owner ? [owner] : []),
    el("label", { class: "check" },
      el("input", { type: "checkbox", checked: !!c.public, onchange: (e) => { c.public = e.target.checked; saveConfig(c.public ? `${c.name} is public now` : `${c.name} is private now`); } }),
      "Public: everyone with a login can listen and see what's on, but only its owner and the admin can change it"),
    el("label", { class: "check" },
      el("input", { type: "checkbox", checked: c.all_sources, onchange: (e) => {
        c.all_sources = e.target.checked;
        if (!c.all_sources && !c.sources.length) c.sources = [...cfg.video, ...cfg.podcasts].map((s) => s.id);
        saveConfig();
      } }),
      "Cut in every video channel and podcast, including ones added later"),
    el("div", { class: "row gap" },
      el("a", { class: "button", href: `/api/channels/${encodeURIComponent(c.id)}/export`, title: "A zip with this channel's stations, programs, spots, sources, own audio and pictures, for Settings › Import on another rRadio" }, "Export channel"),
      el("button", {
        class: "danger", disabled: me.admin && cfg.channels.length < 2,
        onclick: () => {
          if (!confirm(`Remove the channel ${c.name} with its stations, programs and spots?`)) return;
          cfg.channels = cfg.channels.filter((x) => x !== c);
          location.hash = "#/home";
          saveConfig("Channel removed");
        },
      }, "Remove channel")),
  );
}

$("#form-import").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, out = $("#import-result");
  const send = (replace) => {
    const fd = new FormData();
    fd.append("file", f.file.files[0]);
    fd.append("name", f.name.value.trim());
    if (replace) fd.append("replace", "true");
    return api("/api/import", { method: "POST", body: fd });
  };
  out.textContent = "Importing…";
  try {
    let r;
    try {
      r = await send(false);
    } catch (e) {
      if (!/full backup/.test(e.message) || !confirm(`${e.message} The current config is kept as a copy next to config.yaml. Restore it?`)) throw e;
      r = await send(true);
    }
    out.textContent = r.summary;
    f.reset();
    await loadConfig();
    toast(r.summary);
  } catch (e) {
    out.replaceChildren(el("span", { class: "bad" }, e.message));
  }
});

const skip = () => api(`/api/skip?${chq()}`, { method: "POST" }).then(refresh).catch((e) => toast(e.message, true));
$("#btn-skip").addEventListener("click", skip);
$("#btn-skip-song").addEventListener("click", skip);
$("#form-play-url").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try {
    const it = await api("/api/play-url", { method: "POST", body: { url: f.url.value, channel: channelId } });
    toast(`Queued: ${it.title}`);
    f.reset();
    refresh();
  } catch (e) { toast(e.message, true); }
});

// --- stations, programs and their pickers --------------------------------------------------

async function loadPlaylists(force = false) {
  if (playlists && !force) return playlists;
  try {
    playlists = await api("/api/navidrome/playlists");
  } catch {
    playlists = [];
  }
  return playlists;
}

async function fillPlaylists(select, current = "") {
  const lists = await loadPlaylists();
  select.replaceChildren(
    el("option", { value: "" }, "Random songs from the whole library"),
    ...lists.map((p) => el("option", { value: p.name, selected: p.name === current }, `${p.name} (${p.songs})`)),
  );
  if (!lists.length) select.append(el("option", { value: "", disabled: true }, "No playlists found. Check Settings → Navidrome."));
}

// Stream URL or Navidrome playlist inputs, used by the program form.
function basePicker(container) {
  const kind = el("select", { name: "kind" },
    el("option", { value: "stream" }, "Radio stream"),
    el("option", { value: "navidrome" }, "Navidrome"));
  const url = el("input", { name: "url", type: "url", placeholder: "Stream URL (https://…/stream.mp3)", required: true });
  const pl = el("select", { name: "playlist", class: "hidden" });
  const shuffle = el("label", { class: "check hidden" }, el("input", { type: "checkbox", name: "shuffle", checked: true }), "Shuffle");
  kind.addEventListener("change", () => {
    const nd = kind.value === "navidrome";
    url.classList.toggle("hidden", nd);
    url.required = !nd;
    pl.classList.toggle("hidden", !nd);
    shuffle.classList.toggle("hidden", !nd);
    if (nd) fillPlaylists(pl);
  });
  container.replaceChildren(kind, url, pl, shuffle);
}
const baseValue = (f) => ({
  kind: f.kind.value,
  url: f.kind.value === "stream" ? f.url.value : "",
  playlist: f.kind.value === "navidrome" ? f.playlist.value : "",
  shuffle: f.shuffle.checked,
});
const baseLabel = (b) =>
  b.kind === "navidrome" ? `Navidrome · ${b.playlist || "random songs from the library"}${b.playlist && b.shuffle ? " · shuffled" : ""}` : b.url;
const baseKey = (b) => (b.kind === "navidrome" ? `navidrome:${b.playlist}:${b.shuffle}` : `stream:${b.url}`);

function daysPicker(container, selected) {
  container.replaceChildren(
    ...DAYS.map((d) => el("label", {}, el("input", { type: "checkbox", value: d, checked: selected.includes(d) }), d[0].toUpperCase() + d.slice(1))),
  );
}
const pickedDays = (container) => $$("input:checked", container).map((i) => i.value);

// --- schedule ---------------------------------------------------------------------------

// `refresh`: a status update; only the now line moves, so an open popup stays put.
function renderTimeline(refresh = false) {
  const ch = channel();
  const now = wallNow(), today = dayOf(now), nowMin = minuteOf(now);
  const pct = (m) => `${(m / 14.4).toFixed(3)}%`;
  const line = $("#timeline .trow.today .now");
  const tl = $("#timeline");
  if (refresh && line && tl.dataset.channel === ch.id && tl.dataset.day === String(today)) {
    line.style.left = pct(nowMin);
    return;
  }
  tl.dataset.channel = ch.id;
  tl.dataset.day = String(today);
  const hours = [0, 3, 6, 9, 12, 15, 18, 21, 24];
  const rows = DAYS.map((_, d) => {
    const lane = el("div", { class: "lane" });
    for (const p of ch.programs) {
      for (const [a, b] of programSpans(p, d)) {
        lane.append(el("button", {
          class: "block" + (p.trigger === "title" ? " title" : "") + (p.enabled ? "" : " off"),
          style: `left:${pct(a)};width:${pct(b - a)}`,
          "data-program": p.id, "data-day": d,
          "aria-label": `${p.name}, ${DAY_NAMES[d]} ${p.start}–${p.end}`,
        }, p.name));
      }
    }
    for (const sp of ch.spots) {
      for (const m of spotMinutes(sp, d)) {
        lane.append(el("button", { class: "tick", style: `left:${pct(m)}`, "data-spot": sp.id, "data-day": d, "data-min": m, "aria-label": `${sp.name}, ${DAY_NAMES[d]} ${hhmm(m)}` }));
      }
    }
    if (d === today) lane.append(el("i", { class: "now", style: `left:${pct(nowMin)}` }));
    return el("div", { class: "trow" + (d === today ? " today" : "") }, el("span", { class: "tday" }, DAY_NAMES[d]), lane);
  });
  $("#timeline").replaceChildren(
    el("div", { class: "trow head" }, el("span", { class: "tday" }),
      el("div", { class: "lane scale" }, ...hours.map((h) => el("span", { style: `left:${pct(h * 60)}` }, `${pad2(h)}:00`)))),
    ...rows,
  );
  if (pop.anchor) {
    const a = pop.anchor.dataset;
    const same = $(`#timeline [data-day="${a.day}"]${a.spot ? `[data-spot="${a.spot}"][data-min="${a.min}"]` : `[data-program="${a.program}"]`}`);
    same ? showPop(same, pop.pinned) : closePop();
  }
  const st = ch.stations.find((s) => s.id === ch.current_station);
  $("#legend-station").textContent = st ? st.name : "none";
}

// --- schedule popups: hover (or tap) a spot tick or a program block for details ---

const spotInfo = new Map(); // spot id -> { at, data } from /api/spots/<id>/info
let pop = { anchor: null, pinned: false, timer: null };

function spotInfoLine(s, info) {
  if (!info) return el("div", { class: "small muted" }, s.content === "feed" ? "Checking the feed…" : "");
  if (info.error) return el("div", { class: "small bad" }, `Feed: ${info.error}`);
  const ep = info.episode;
  if (!ep) return null;
  const bits = [ep.published ? fmtAgo(ep.published) : "", ep.duration ? fmtTime(ep.duration) : ""].filter(Boolean).join(" · ");
  const fate = ep.new ? "not played yet" : s.only_new ? "already played, so the next one is skipped unless a new episode comes" : "already played; plays again";
  return el("div", { class: "pop-ep" },
    el("div", { class: "small muted" }, "Newest episode"),
    el("div", {}, ep.title),
    el("div", { class: "small muted" }, [bits, fate].filter(Boolean).join(" · ")));
}

const hostOf = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; } };

function spotPop(s, d, m) {
  const sum = spotSummary(s);
  const st = status?.spots?.[s.id] || {};
  const cached = spotInfo.get(s.id);
  const info = cached?.data;
  if (!cached || Date.now() - cached.at > 5 * 60e3) {
    spotInfo.set(s.id, { at: Date.now(), data: info });
    api(`/api/spots/${s.id}/info`).then((data) => {
      spotInfo.set(s.id, { at: Date.now(), data });
      if (pop.anchor?.dataset.spot === s.id) showPop(pop.anchor, pop.pinned);
    }).catch(() => {});
  }
  return [
    el("div", { class: "pop-head" },
      artImg(info?.art || previewOf(s.image), "pop-art"),
      el("div", {},
        el("div", { class: "pop-kind" }, s.content === "feed" ? "News / feed spot" : "Spot"),
        el("strong", {}, s.name),
        el("div", { class: "small muted" }, `${DAY_NAMES[d]} ${hhmm(m)}${s.enabled ? "" : " · paused"}`))),
    el("div", { class: "small" }, s.content === "feed"
      ? `News feed from ${hostOf(s.feed_url)}${s.only_new ? " · only new episodes" : ""}`
      : sum.what),
    el("div", { class: "small muted" }, sum.when),
    spotInfoLine(s, info),
    el("div", { class: "small muted" },
      [st.next ? `Next ${whenLabel(st.next)}` : "", st.last ? `last ${fmtAgo(st.last)}` : ""].filter(Boolean).join(" · ")),
    st.error ? el("div", { class: "small bad" }, st.error) : null,
    el("div", { class: "row gap" },
      el("button", { class: "small-btn", onclick: () => api(`/api/spots/${s.id}/fire`, { method: "POST" }).then(() => { toast(`Queued: ${s.name}`); refresh(); }).catch((e) => toast(e.message, true)) }, "Play now"),
      el("button", { class: "small-btn", onclick: () => { closePop(); editSpot(s); } }, "Edit")),
  ];
}

function programPop(p, d) {
  return [
    el("div", { class: "pop-head" },
      artImg(previewOf(p.image) || previewOf(channel().stations.find((s) => baseKey(s) === baseKey(p))?.image), "pop-art"),
      el("div", {},
        el("div", { class: "pop-kind" }, p.trigger === "title" ? "Program, started by the stream title" : "Program"),
        el("strong", {}, p.name),
        el("div", { class: "small muted" }, `${DAY_NAMES[d]} ${p.start}–${p.end}${p.enabled ? "" : " · paused"}`))),
    el("div", { class: "small" }, baseLabel(p)),
    p.trigger === "title" ? el("div", { class: "small muted" }, `Plays while the title matches “${p.title_start || p.title_keep || "…"}”, at most until ${p.end}.`) : null,
    el("div", { class: "row gap" }, el("button", { class: "small-btn", onclick: () => { closePop(); focusRow(`li[data-program="${p.id}"]`); } }, "Edit")),
  ];
}

function showPop(anchor, pinned) {
  const ch = channel();
  const d = Number(anchor.dataset.day);
  let body = null;
  if (anchor.dataset.spot) {
    const s = ch.spots.find((x) => x.id === anchor.dataset.spot);
    if (s) body = spotPop(s, d, Number(anchor.dataset.min));
  } else {
    const p = ch.programs.find((x) => x.id === anchor.dataset.program);
    if (p) body = programPop(p, d);
  }
  if (!body) return closePop();
  clearTimeout(pop.timer);
  pop = { anchor, pinned, timer: null };
  const box = $("#tl-pop");
  box.replaceChildren(...body.filter(Boolean));
  box.classList.remove("hidden");
  const r = anchor.getBoundingClientRect();
  const w = box.offsetWidth, h = box.offsetHeight;
  const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
  const top = r.bottom + 8 + h < window.innerHeight ? r.bottom + 8 : Math.max(8, r.top - h - 8);
  box.style.left = `${left}px`;
  box.style.top = `${top}px`;
}

function closePop() {
  clearTimeout(pop.timer);
  pop = { anchor: null, pinned: false, timer: null };
  $("#tl-pop").classList.add("hidden");
}

const popTarget = (e) => e.target.closest?.("#timeline .tick, #timeline .block");
$("#timeline").addEventListener("mouseover", (e) => {
  const a = popTarget(e);
  if (a && !pop.pinned) showPop(a, false);
});
$("#timeline").addEventListener("mouseout", (e) => {
  if (!pop.pinned && popTarget(e)) pop.timer = setTimeout(closePop, 250);
});
$("#timeline").addEventListener("click", (e) => {
  const a = popTarget(e);
  if (!a) return;
  if (pop.pinned && pop.anchor === a) return closePop();
  showPop(a, true);
});
$("#timeline").addEventListener("focusin", (e) => { const a = popTarget(e); if (a && !pop.pinned) showPop(a, false); });
$("#tl-pop").addEventListener("mouseenter", () => clearTimeout(pop.timer));
$("#tl-pop").addEventListener("mouseleave", () => { if (!pop.pinned) pop.timer = setTimeout(closePop, 250); });
document.addEventListener("click", (e) => { if (pop.anchor && !popTarget(e) && !e.target.closest("#tl-pop")) closePop(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pop.anchor) closePop(); });
window.addEventListener("scroll", () => { if (pop.anchor && !pop.pinned) closePop(); }, { passive: true });

function focusRow(sel) {
  const li = $(sel);
  if (!li) return;
  li.scrollIntoView({ behavior: "smooth", block: "center" });
  li.classList.remove("flash");
  void li.offsetWidth;
  li.classList.add("flash");
}

$("#btn-new-program").addEventListener("click", () => {
  $("#form-program").classList.remove("hidden");
  $("#form-program").scrollIntoView({ behavior: "smooth", block: "center" });
});

// Programs show as a summary row; Edit opens the full form in place.
const openPrograms = new Set();

function daysLabel(days) {
  if (days.length === 7) return "every day";
  const cap = (d) => d[0].toUpperCase() + d.slice(1);
  if (days.join() === "mon,tue,wed,thu,fri") return "Mon–Fri";
  if (days.join() === "sat,sun") return "Sat–Sun";
  return days.map(cap).join(" ");
}

function programSummary(p) {
  const when = `${daysLabel(p.days)} · ${p.start}–${p.end}`;
  const how = p.trigger === "title" ? `while the stream title matches “${p.title_start || "?"}”` : "the whole window";
  return `${when} · ${how} · ${p.cutins_interrupt ? "cut-ins may interrupt" : "cut-ins wait"}${p.keep_day ? " · kept 24 h" : ""}`;
}

function renderPrograms() {
  const ch = channel();
  $("#programs").replaceChildren(
    ...(ch.programs.length
      ? ch.programs.map((p) => {
          const open = openPrograms.has(p.id);
          const toggle = () => { open ? openPrograms.delete(p.id) : openPrograms.add(p.id); renderPrograms(); };
          const head = el("div", { class: "row between" },
            el("label", { class: "check" },
              el("input", { type: "checkbox", checked: p.enabled, onchange: (e) => { p.enabled = e.target.checked; saveConfig(p.enabled ? "Enabled" : "Paused"); } }),
              el("strong", {}, p.name)),
            el("span", { class: "row gap" },
              el("button", { class: open ? "small-btn primary" : "small-btn", onclick: toggle }, open ? "Done" : "Edit"),
              el("button", { class: "small-btn danger", onclick: () => { if (!confirm(`Remove ${p.name}?`)) return; openPrograms.delete(p.id); ch.programs = ch.programs.filter((x) => x !== p); saveConfig("Program removed"); } }, "Remove")),
          );
          if (!open) {
            return el("li", { "data-program": p.id },
              head,
              el("div", { class: "small muted title" }, baseLabel(p)),
              el("div", { class: "small muted" }, programSummary(p), el("span", { "data-f": "pmeta" })),
            );
          }
          const days = el("div", { class: "days" });
          daysPicker(days, p.days);
          days.addEventListener("change", () => { p.days = pickedDays(days); saveConfig(); });
          const time = (k) => el("input", { type: "time", value: p[k], "aria-label": k === "start" ? "Starts" : "Ends", onchange: (e) => { p[k] = e.target.value; saveConfig(); } });
          // Shown on the web page and in the apps while the program is on (streams send no pictures).
          const pic = imageField(p.image, {
            placeholder: "Picture URL (shown while it plays)",
            find: p.kind === "stream" ? () => findLogo(p.url, p.name) : null,
          });
          const savePic = el("button", { class: "small-btn primary", type: "button", onclick: () => { p.image = pic.input.value.trim(); saveConfig("Picture saved"); } }, "Save picture");
          return el("li", { "data-program": p.id, class: "editing" },
            head,
            el("div", { class: "small muted title" }, baseLabel(p)),
            el("div", { class: "row gap" }, el("label", { class: "inline" }, "From", time("start")), el("label", { class: "inline" }, "to", time("end"))),
            days,
            pic.box,
            el("div", { class: "row gap" }, savePic),
            el("label", { class: "check small" },
              el("input", { type: "checkbox", checked: !!p.cutins_interrupt, onchange: (e) => { p.cutins_interrupt = e.target.checked; saveConfig(); } }),
              "Let new video uploads and podcast episodes interrupt this program (otherwise they wait until it ends)"),
            el("label", { class: "check small" },
              el("input", { type: "checkbox", checked: !!p.keep_day, onchange: (e) => { p.keep_day = e.target.checked; saveConfig(); } }),
              "Keep it for 24 hours, so the apps can listen back (recorded in RAM if downloads go there: about 60 MB per hour)"),
            titleTrigger(p),
          );
        })
      : [el("li", { class: "empty" }, "No programs yet. Add one with + Program.")]),
  );
  renderProgramMeta();
}

// Start and end a program by the stream title the station sends.
function titleTrigger(p) {
  const box = el("div", { class: "title-trigger" });
  const mode = el("select", { onchange: (e) => {
    p.trigger = e.target.value;
    if (p.trigger === "title" && !p.title_start) renderPrograms(); // saved once a start pattern is set
    else saveConfig();
  } },
    el("option", { value: "time", selected: p.trigger !== "title" }, "Play the whole window"),
    el("option", { value: "title", selected: p.trigger === "title" }, "Only while the stream title says the show is on"));
  box.append(el("label", { class: "inline small" }, "When:", mode));
  if (p.trigger !== "title") return box;

  const field = (k, label, attrs = {}) => el("label", {}, label,
    el("input", { value: p[k] ?? "", ...attrs, onchange: (e) => {
      p[k] = attrs.type === "number" ? Number(e.target.value) : e.target.value.trim();
      if (p.title_start) saveConfig();
    } }));
  const result = el("div", { class: "small" });
  const check = async (btn) => {
    btn.disabled = true;
    result.textContent = "Listening for a title…";
    try {
      const url = p.title_url || (p.kind === "stream" ? p.url : "");
      if (!url) throw new Error("Set the stream to watch first");
      const r = await api("/api/programs/title-check", { method: "POST", body: { url, title_start: p.title_start, title_keep: p.title_keep, title_end: p.title_end } });
      const hit = r.title_end ? "matches the end pattern" : r.title_start ? "matches the start pattern (show on)" : r.title_keep ? "matches the keep pattern" : "matches no pattern";
      result.replaceChildren("Now: ", el("strong", {}, r.title || "(empty title)"), ` · ${hit}`);
    } catch (e) {
      result.replaceChildren(el("span", { class: "bad" }, e.message));
    } finally { btn.disabled = false; }
  };
  box.append(
    el("div", { class: "filters" },
      field("title_start", "Show is on when the title matches", { placeholder: "e.g. Jetzt: Hafenradio Live", required: true }),
      field("title_keep", "…and stays on for titles like", { placeholder: "e.g. Nachrichten|Werbung" }),
      field("title_end", "Ends at once when the title matches", { placeholder: "optional" }),
      field("end_after_minutes", "Ends after this many minutes of other titles", { type: "number", min: 0, step: 0.5 }),
      field("title_url", "Stream to watch", { type: "url", placeholder: p.kind === "stream" ? "the program's own stream" : "https://…" }),
    ),
    el("div", { class: "row gap small" },
      el("button", { class: "small-btn", type: "button", onclick: (e) => check(e.target) }, "Check title now"),
      el("span", { class: "muted", "data-f": "tmeta" })),
    result,
    ...(p.title_start ? [] : [el("div", { class: "small bad" }, "Enter a start pattern to save.")]),
    el("details", { class: "small" }, el("summary", { class: "muted" }, "Titles seen in the window"), el("ul", { class: "list compact", "data-f": "tlog" })),
  );
  return box;
}

function renderProgramMeta() {
  if (!cfg || !status || !status.programs) return;
  const hm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  for (const [id, st] of Object.entries(status.programs)) {
    const li = $(`li[data-program="${id}"]`);
    if (!li) continue;
    const meta = $('[data-f="tmeta"]', li);
    const parts = [];
    if (st.on_air) parts.push(el("span", { class: "ok" }, "On air"));
    else if (st.show_on) parts.push("Show on, waiting for a clip to finish");
    else if (st.window_open) parts.push("Window open, waiting for the show");
    else parts.push("Window closed, not watching");
    if (st.title != null) parts.push(` · now: ${st.title || "(empty)"}`);
    if (st.error) parts.push(el("span", { class: "bad" }, ` · ${st.error}`));
    if (meta) meta.replaceChildren(...parts);
    const pm = $('[data-f="pmeta"]', li);
    if (pm) pm.replaceChildren(
      st.on_air ? el("span", { class: "ok" }, " · on air") : st.window_open ? " · window open" : "",
      st.error ? el("span", { class: "bad" }, ` · ${st.error}`) : "");
    const log = $('[data-f="tlog"]', li);
    if (log) log.replaceChildren(...(st.log.length
      ? st.log.map((t) => el("li", {}, el("span", { class: "muted" }, hm(t.ts) + "  "), t.title || "(empty)"))
      : [el("li", { class: "empty" }, "Nothing yet. Titles show up here while the window is open.")]));
  }
}

$("#form-program").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const days = pickedDays($("[data-days]", f));
  if (!days.length) return toast("Pick at least one day", true);
  const base = baseValue(f);
  // Like a station: look for the stream's logo, so the program has a picture from the start.
  const image = base.kind === "stream" && base.url ? await findLogo(base.url, f.name.value).then((x) => x.image, () => "") : "";
  channel().programs.push({ name: f.name.value, ...base, ...(image ? { image } : {}), start: f.start.value, end: f.end.value, days, enabled: true });
  f.name.value = ""; f.url.value = "";
  f.classList.add("hidden");
  saveConfig("Program added");
});

// --- spots ----------------------------------------------------------------------------

let editingSpot = null;
let spotPic = null;
function spotPicture(value) {
  spotPic = imageField(value, { placeholder: "Picture URL (optional)" });
  $("#spot-image").replaceChildren(spotPic.box);
}

function spotSummary(s) {
  const what = s.content === "feed"
    ? `Newest episode of ${s.feed_url}${s.only_new ? " (only new ones)" : ""}`
    : `${s.files.length ? s.files.join(", ") : "no files picked"}${s.files.length > 1 || s.files.some((f) => f.endsWith("/")) ? (s.order === "random" ? " · random" : " · in turn") : ""}`;
  let when;
  if (s.trigger === "times") when = `at ${s.times.join(", ") || "—"}`;
  else if (s.trigger === "songs") when = `after every ${s.every_songs} songs`;
  else if (s.every_minutes === 60) when = s.offset_minutes ? `every hour at :${pad2(s.offset_minutes)}` : "every hour on the hour";
  else when = `every ${s.every_minutes} min${s.offset_minutes ? ` (+${s.offset_minutes})` : ""}`;
  const days = s.days.length === 7 ? "" : ` · ${s.days.map((d) => d[0].toUpperCase() + d.slice(1)).join(" ")}`;
  const win = s.active_from !== s.active_to ? ` · ${s.active_from}–${s.active_to}` : "";
  const extra = [s.between_songs && "between songs", !s.during_programs && "not during programs"].filter(Boolean).join(" · ");
  return { what, when: when + days + win + (extra ? ` · ${extra}` : "") };
}

function renderSpots() {
  const ch = channel();
  $("#spots").replaceChildren(
    ...(ch.spots.length
      ? ch.spots.map((s) => {
          const sum = spotSummary(s);
          return el("li", { "data-spot": s.id },
            el("div", { class: "row between" },
              el("label", { class: "check" },
                el("input", { type: "checkbox", checked: s.enabled, onchange: (e) => { s.enabled = e.target.checked; saveConfig(s.enabled ? "Enabled" : "Paused"); } }),
                el("strong", {}, s.name)),
              el("span", { class: "row gap" },
                el("button", { class: "small-btn", onclick: () => api(`/api/spots/${s.id}/fire`, { method: "POST" }).then(() => { toast(`Queued: ${s.name}`); refresh(); }).catch((e) => toast(e.message, true)) }, "Play now"),
                el("button", { class: "small-btn", onclick: () => editSpot(s) }, "Edit"),
                el("button", { class: "small-btn danger", onclick: () => { if (!confirm(`Remove ${s.name}?`)) return; ch.spots = ch.spots.filter((x) => x !== s); saveConfig("Spot removed"); } }, "Remove")),
            ),
            el("div", { class: "small muted title" }, sum.what),
            el("div", { class: "small muted" }, sum.when, el("span", { "data-f": "meta" })),
          );
        })
      : [el("li", { class: "empty" }, "No spots yet. Add hourly news or your own jingles with + Spot.")]),
  );
  renderSpotMeta();
}

function renderSpotMeta() {
  if (!cfg || !status || !status.spots) return;
  for (const [id, st] of Object.entries(status.spots)) {
    const meta = $(`li[data-spot="${id}"] [data-f="meta"]`);
    if (!meta) continue;
    meta.replaceChildren(
      st.next ? ` · next ${whenLabel(st.next)}` : "",
      st.last ? ` · last ${fmtAgo(st.last)}` : "",
      st.error ? el("span", { class: "bad" }, ` · ${st.error}`) : "",
    );
  }
}

function renderFilePicker(selected = []) {
  const box = $("#spot-files");
  const sel = new Set(selected);
  const files = mediaLib.files.filter((f) => !f.folder.startsWith("played"));
  if (!files.length) {
    box.replaceChildren(el("div", { class: "small muted" }, "No files yet. Upload some under Sources › Own media."));
    return;
  }
  const rows = [];
  const byFolder = {};
  for (const f of files) (byFolder[f.folder] ||= []).push(f);
  for (const folder of Object.keys(byFolder).sort()) {
    if (folder) rows.push(el("label", { class: "check folder" }, el("input", { type: "checkbox", value: folder + "/", checked: sel.has(folder + "/") }), `${folder}/ (whole folder)`));
    for (const f of byFolder[folder]) rows.push(el("label", { class: "check" + (folder ? " sub" : "") }, el("input", { type: "checkbox", value: f.path, checked: sel.has(f.path) }), f.name));
  }
  box.replaceChildren(...rows);
}

function spotFormMode() {
  const f = $("#form-spot");
  const content = f.content.value;
  $$("[data-for]", f).forEach((d) => d.classList.toggle("hidden", d.dataset.for !== content));
  f.feed_url.required = content === "feed";
}
$$('#form-spot input[name="content"]').forEach((r) => r.addEventListener("change", spotFormMode));

function resetSpotForm() {
  const f = $("#form-spot");
  editingSpot = null;
  f.reset();
  spotPicture("");
  daysPicker($("[data-days]", f), DAYS);
  renderFilePicker();
  spotFormMode();
  $("#feed-result").textContent = "";
  $("#spot-form-title").textContent = "New spot";
  $("#spot-submit").textContent = "Add spot";
  $("#spot-card").classList.add("hidden");
}

function showSpotCard() {
  $("#spot-card").classList.remove("hidden");
  $("#spot-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function editSpot(s) {
  const f = $("#form-spot");
  editingSpot = s;
  f.name.value = s.name;
  f.content.value = s.content;
  f.order.value = s.order;
  f.feed_url.value = s.feed_url;
  f.only_new.checked = s.only_new;
  f.trigger.value = s.trigger;
  f.every_minutes.value = s.every_minutes;
  f.offset_minutes.value = s.offset_minutes;
  f.times.value = s.times.join(", ");
  f.every_songs.value = s.every_songs;
  f.active_from.value = s.active_from;
  f.active_to.value = s.active_to;
  f.between_songs.checked = s.between_songs;
  f.during_programs.checked = s.during_programs;
  daysPicker($("[data-days]", f), s.days);
  spotPicture(s.image);
  renderFilePicker(s.files);
  spotFormMode();
  $("#feed-result").textContent = "";
  $("#spot-form-title").textContent = `Edit ${s.name}`;
  $("#spot-submit").textContent = "Save spot";
  showSpotCard();
}
$("#btn-check-feed").addEventListener("click", async (ev) => {
  const out = $("#feed-result"), url = $("#form-spot").feed_url.value.trim();
  if (!url) return;
  ev.target.disabled = true;
  out.textContent = "Checking…";
  try {
    const r = await api("/api/feeds/check", { method: "POST", body: { query: url } });
    out.replaceChildren(el("span", { class: "ok" }, "Works. "), `Newest: ${r.title}${r.published ? ` · ${fmtAgo(r.published)}` : ""} (${r.episodes} episodes)`);
  } catch (e) {
    out.replaceChildren(el("span", { class: "bad" }, e.message));
  } finally { ev.target.disabled = false; }
});
$("#spot-cancel").addEventListener("click", resetSpotForm);
$("#btn-new-spot").addEventListener("click", () => { resetSpotForm(); showSpotCard(); });

$("#form-spot").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const days = pickedDays($("[data-days]", f));
  if (!days.length) return toast("Pick at least one day", true);
  const files = $$("#spot-files input:checked").map((i) => i.value);
  if (f.content.value === "media" && !files.length) return toast("Pick at least one file or folder", true);
  const spot = {
    ...(editingSpot || {}),
    name: f.name.value,
    enabled: editingSpot ? editingSpot.enabled : true,
    content: f.content.value,
    files,
    order: f.order.value,
    feed_url: f.feed_url.value,
    only_new: f.only_new.checked,
    trigger: f.trigger.value,
    every_minutes: Number(f.every_minutes.value || 60),
    offset_minutes: Number(f.offset_minutes.value || 0),
    times: f.times.value.split(/[,\s]+/).filter(Boolean),
    every_songs: Number(f.every_songs.value || 4),
    days,
    active_from: f.active_from.value || "00:00",
    active_to: f.active_to.value || "00:00",
    between_songs: f.between_songs.checked,
    during_programs: f.during_programs.checked,
    image: spotPic ? spotPic.input.value.trim() : "",
  };
  if (spot.trigger === "times" && !spot.times.length) return toast("Enter at least one time", true);
  const ch = channel();
  if (editingSpot) ch.spots = ch.spots.map((s) => (s === editingSpot ? spot : s));
  else ch.spots.push(spot);
  const wasEdit = !!editingSpot;
  await saveConfig(wasEdit ? "Spot saved" : "Spot added");
  resetSpotForm();
  refresh();
});

// --- sources ------------------------------------------------------------------------
//
// One library. Stations belong to channels (each keeps its own copy, matched by
// stream URL or playlist); video channels and podcasts are shared and each
// channel picks which of them cut in.

function sourceRows() {
  const rows = [];
  const stations = new Map();
  for (const c of mine()) {
    for (const s of c.stations) {
      const k = baseKey(s);
      if (!stations.has(k)) stations.set(k, { key: k, type: s.kind === "navidrome" ? "navidrome" : "station", name: s.name, base: s, copies: [] });
      stations.get(k).copies.push({ c, s });
    }
  }
  rows.push(...stations.values());
  const allIds = [...cfg.video, ...cfg.podcasts].map((s) => s.id);
  for (const [type, list] of [["video", cfg.video], ["podcast", cfg.podcasts]]) {
    for (const src of list) rows.push({ key: src.id, type, name: src.name, src, allIds });
  }
  return rows;
}

const TYPE_NAME = { station: "Radio station", navidrome: "Navidrome", video: "Video", podcast: "Podcast", media: "Own media" };
const FILTERS = [["all", "All"], ["station", "Radio stations"], ["navidrome", "Navidrome"], ["video", "Video"], ["podcast", "Podcasts"], ["media", "Own media"]];

function usesSource(c, row) {
  if (row.type === "station" || row.type === "navidrome") return row.copies.some((x) => x.c === c);
  return c.all_sources || c.sources.includes(row.src.id);
}

function setUses(c, row, on) {
  if (row.type === "station" || row.type === "navidrome") {
    if (on) {
      const { id, ...copy } = row.base;
      c.stations.push(copy);
    } else {
      c.stations = c.stations.filter((s) => baseKey(s) !== row.key);
    }
    return saveConfig(on ? `${row.name} added to ${c.name}` : `${row.name} removed from ${c.name}`);
  }
  if (c.all_sources) {
    c.all_sources = false;
    c.sources = row.allIds.filter((x) => x !== row.src.id);
  } else {
    c.sources = on ? [...new Set([...c.sources, row.src.id])] : c.sources.filter((x) => x !== row.src.id);
  }
  return saveConfig(on ? `${c.name} now cuts in ${row.name}` : `${c.name} no longer cuts in ${row.name}`);
}

function sourceLatest(row) {
  if (row.type === "station" || row.type === "navidrome") {
    const on = row.copies.filter(({ c, s }) => c.current_station === s.id).map(({ c }) => c.name);
    const progs = mine().flatMap((c) => c.programs.filter((p) => baseKey(p) === row.key).map((p) => `${p.name} on ${c.name}`));
    const parts = [on.length ? `Station on ${on.join(", ")}` : "", progs.length ? `Program: ${progs.join(", ")}` : ""].filter(Boolean);
    return { text: parts.length ? parts.join(" · ") : "Not picked on any channel", state: "ok" };
  }
  if (!row.src.enabled) return { text: "Paused", state: "off" };
  const st = status?.sources?.[row.src.id];
  if (!st) return { text: "Not checked yet", state: "off" };
  if (st.error) return { text: st.error, state: "bad" };
  const latest = st.latest ? `${st.latest.title}${st.latest.published ? ` · ${fmtAgo(st.latest.published)}` : ""}` : "Nothing found";
  return { text: latest, state: "ok", checked: st.last_check ? `checked ${fmtAgo(st.last_check)}` : "" };
}

function renderSources() {
  const rows = sourceRows();
  const counts = { all: rows.length, media: mediaLib.files.length };
  for (const r of rows) counts[r.type] = (counts[r.type] || 0) + 1;
  $("#source-filters").replaceChildren(
    ...FILTERS.map(([k, label]) => el("a", { href: `#/sources/${k}`, class: "chip" + (sourceFilter === k ? " active" : "") }, `${label} ${counts[k] || 0}`)),
  );
  const media = sourceFilter === "media";
  $("#source-table-box").classList.toggle("hidden", media);
  $("#sources-note").classList.toggle("hidden", media);
  $("#media-card").classList.toggle("hidden", !(media || sourceFilter === "all"));
  $("#source-head").replaceChildren(
    el("tr", {},
      el("th", {}, "Source"), el("th", {}, "Type"), el("th", {}, "Latest"),
      ...mine().map((c) => el("th", { class: "center" }, c.name)),
      el("th", {}, el("span", { class: "sr" }, "Actions"))),
  );
  renderSourcesTable();
}

// `refresh`: a status update, which must not throw away an open editor's typing.
function renderSourcesTable(refresh = false) {
  if (!cfg || view !== "sources") return;
  const rows = sourceRows().filter((r) => sourceFilter === "all" || r.type === sourceFilter);
  const cols = mine().length + 4;
  const trs = [];
  for (const r of rows) {
    const latest = sourceLatest(r);
    const detail = r.type === "video" ? r.src.channel_id : r.type === "podcast" ? r.src.url : baseLabel(r.base);
    trs.push(el("tr", { "data-source": r.key },
      el("td", {},
        el("div", { class: "src-name" },
          el("span", { class: `sdot ${latest.state}`, "aria-hidden": "true" }),
          artImg(previewOf(r.base?.image), "src-art"),
          el("div", {}, el("strong", {}, r.name),
            me.admin && r.src?.owner ? el("span", { class: "owner-tag" }, `${ownerName(r.src.owner)}'s`) : null,
            el("div", { class: "muted small ellipsis" }, detail),
            r.base?.image && status?.image_errors?.[r.base.image] ? el("div", { class: "small bad" }, `Logo didn't load: ${status.image_errors[r.base.image]}`) : null))),
      el("td", { class: "muted" }, TYPE_NAME[r.type]),
      el("td", { class: "muted small latest" }, latest.state === "bad" ? el("span", { class: "bad" }, latest.text) : latest.text, latest.checked ? el("div", {}, latest.checked) : null),
      ...mine().map((c) => el("td", { class: "center" },
        el("input", { type: "checkbox", class: "big-check", checked: usesSource(c, r), "aria-label": `Use ${r.name} on ${c.name}`, onchange: (e) => setUses(c, r, e.target.checked) }))),
      el("td", { class: "right" }, el("button", { class: "small-btn", "aria-expanded": openSource === r.key ? "true" : "false", onclick: () => { openSource = openSource === r.key ? null : r.key; renderSourcesTable(); } }, openSource === r.key ? "Close" : "Edit")),
    ));
    if (openSource === r.key) trs.push(el("tr", { class: "editor-row" }, el("td", { colspan: cols }, sourceEditor(r))));
  }
  const box = $("#source-rows");
  // Keep an open editor's half-typed values while the status refreshes.
  if (refresh && openSource && box.querySelector(".editor-row")) {
    for (const r of rows) {
      const td = $(`tr[data-source="${CSS.escape(r.key)}"] .latest`, box);
      const l = sourceLatest(r);
      if (td) td.replaceChildren(l.state === "bad" ? el("span", { class: "bad" }, l.text) : l.text, l.checked ? el("div", {}, l.checked) : "");
    }
    return;
  }
  box.replaceChildren(...(trs.length ? trs : [el("tr", {}, el("td", { colspan: cols, class: "empty" }, "Nothing here yet. Add one with + Add source."))]));
}

function sourceEditor(r) {
  if (r.type === "station" || r.type === "navidrome") {
    const name = el("input", { value: r.name, "aria-label": "Name" });
    const url = el("input", { value: r.base.url, type: "url", "aria-label": "Stream URL", class: "grow" });
    const pl = el("select", { "aria-label": "Playlist" });
    const shuffle = el("input", { type: "checkbox", checked: r.base.shuffle });
    const pic = imageField(r.base.image, {
      label: "Logo",
      placeholder: r.type === "station" ? "Logo URL (streams send titles, not pictures)" : "Picture URL (songs show their own cover)",
      find: r.type === "station" ? () => findLogo(url.value, name.value) : null,
    });
    const image = pic.input;
    if (r.type === "navidrome") fillPlaylists(pl, r.base.playlist);
    return el("div", { class: "stack" },
      el("div", { class: "row gap" }, name, ...(r.type === "station" ? [url] : [pl, el("label", { class: "check" }, shuffle, "Shuffle")])),
      pic.box,
      el("div", { class: "row gap" },
        el("button", { class: "small-btn primary", onclick: () => {
          for (const { s } of r.copies) {
            s.name = name.value;
            s.image = image.value.trim();
            if (r.type === "station") s.url = url.value;
            else { s.playlist = pl.value; s.shuffle = shuffle.checked; }
          }
          openSource = null;
          saveConfig("Station saved");
        } }, "Save"),
        el("button", { class: "small-btn danger", onclick: () => {
          if (!confirm(`Remove ${r.name} from every channel?`)) return;
          for (const c of mine()) c.stations = c.stations.filter((s) => baseKey(s) !== r.key);
          openSource = null;
          saveConfig("Station removed");
        } }, "Remove everywhere")),
      el("p", { class: "muted small" }, "Changes apply to this station on every channel that has it."));
  }
  const src = r.src;
  const kind = r.type;
  const owned = ownsSource(src);
  const num = (k, label, step) => el("label", {}, label, el("input", { type: "number", min: 0, step, value: src[k], "data-k": k }));
  const txt = (k, label, ph) => el("label", {}, label, el("input", { value: src[k], placeholder: ph, "data-k": k }));
  const box = el("div", { class: "stack" },
    owned ? null : el("p", { class: "muted small" }, `Added by ${ownerName(src.owner)}, so only they can change its filters or remove it. Untick your channels above to stop cutting it in.`),
    el("label", { class: "check" + (owned ? "" : " hidden") }, el("input", { type: "checkbox", checked: src.enabled, onchange: (e) => { src.enabled = e.target.checked; saveConfig(src.enabled ? "Enabled" : "Paused"); } }), "Check this source for new items"),
    el("div", { class: "filters" + (owned ? "" : " hidden") },
      num("min_minutes", "Min minutes", 0.5),
      num("max_minutes", "Max minutes", 1),
      txt("include", "Only titles matching", "e.g. news|update"),
      txt("exclude", "Skip titles matching", "e.g. live|trailer")),
    el("div", { class: "row gap" },
      el("button", { class: "small-btn primary" + (owned ? "" : " hidden"), onclick: () => {
        for (const i of $$("[data-k]", box)) src[i.dataset.k] = i.type === "number" ? Number(i.value || 0) : i.value;
        saveConfig("Filters saved");
      } }, "Save filters"),
      el("button", { class: "small-btn", onclick: async () => {
        try {
          const it = await api(`/api/sources/${kind}/${src.id}/play-latest?${chq()}`, { method: "POST" });
          toast(`Queued on ${channel().name}: ${it.title}`);
          refresh();
        } catch (e) { toast(e.message, true); }
      } }, `Play latest on ${channel().name}`),
      el("button", { class: "small-btn danger" + (owned ? "" : " hidden"), onclick: () => {
        if (!confirm(`Remove ${src.name}?`)) return;
        if (kind === "video") cfg.video = cfg.video.filter((x) => x !== src);
        else cfg.podcasts = cfg.podcasts.filter((x) => x !== src);
        openSource = null;
        saveConfig("Removed");
      } }, "Remove")),
  );
  return box;
}

// The add card: one form per kind of source.
let addKind = "station";
function showAddKind(k) {
  addKind = k;
  $$("#add-kind button").forEach((b) => b.classList.toggle("active", b.dataset.kind === k));
  $$("#add-source form").forEach((f) => f.classList.toggle("hidden", !f.dataset.add.split(" ").includes(k)));
  const f = $("#form-station");
  const nd = k === "navidrome";
  f.url.classList.toggle("hidden", nd);
  f.url.required = !nd;
  f.playlist.classList.toggle("hidden", !nd);
  $("[data-shuffle]", f).classList.toggle("hidden", !nd);
  $$("[data-hint]", f).forEach((p) => p.classList.toggle("hidden", p.dataset.hint !== k));
  if (nd) fillPlaylists(f.playlist);
  $(".chips", f).replaceChildren(
    el("span", { class: "muted small" }, "Add to:"),
    ...mine().map((c) => el("label", {}, el("input", { type: "checkbox", value: c.id, checked: c.id === channel()?.id }), c.name)),
  );
}
$$("#add-kind button").forEach((b) => b.addEventListener("click", () => showAddKind(b.dataset.kind)));
$("#btn-add-source").addEventListener("click", () => {
  $("#add-source").classList.remove("hidden");
  const k = ["station", "navidrome", "video", "podcast", "media"].includes(sourceFilter) ? sourceFilter : addKind;
  showAddKind(k);
});

// Radio streams carry titles but no pictures, so logos come from radio-browser.info.
const findLogo = (url, name = "") => api("/api/station/logo", { method: "POST", body: { query: url, name } });

// Where to preview a configured picture: uploads directly, other URLs through rRadio.
const previewOf = (url) => (!url ? null : url.startsWith("/api/images/") ? url : status?.images?.[url] || null);

// A picture field: URL, upload, optional "Find logo", and a preview with what went wrong.
function imageField(value, { label = "Picture", placeholder = "Picture URL (optional)", find = null } = {}) {
  const input = el("input", { value: value || "", type: "text", inputmode: "url", "aria-label": `${label} URL`, class: "grow", placeholder });
  const note = el("div", { class: "small muted" });
  const preview = el("img", { class: "pic-preview hidden", alt: "" });
  const show = () => {
    const v = input.value.trim();
    const src = previewOf(v);
    const failed = (why) => {
      preview.classList.add("hidden");
      note.classList.add("bad");
      note.textContent = `rRadio couldn't load this picture: ${why}. Check the address or upload the file instead.`;
    };
    preview.onerror = () => fetch(preview.src).then((r) => r.json()).then((d) => failed(d.detail || "unknown error"), () => failed("unknown error"));
    preview.onload = () => { preview.classList.remove("hidden"); };
    note.classList.remove("bad");
    const err = status?.image_errors?.[v];
    if (err) failed(err);
    else if (src) { preview.src = src; note.textContent = ""; }
    else { preview.classList.add("hidden"); preview.removeAttribute("src"); note.textContent = v ? "Preview after saving." : ""; }
  };
  input.addEventListener("input", show);
  const file = el("input", { type: "file", accept: "image/*", class: "hidden", onchange: async () => {
    if (!file.files[0]) return;
    const fd = new FormData();
    fd.append("file", file.files[0]);
    note.textContent = "Uploading…";
    try {
      input.value = (await api("/api/images", { method: "POST", body: fd })).url;
      show();
      note.textContent = "Uploaded. Save to keep it.";
    } catch (e) { note.textContent = e.message; note.classList.add("bad"); }
    file.value = "";
  } });
  const findBtn = find && el("button", { class: "small-btn", type: "button", onclick: async () => {
    findBtn.disabled = true;
    note.classList.remove("bad");
    note.textContent = "Looking for a logo…";
    try {
      const r = await find();
      input.value = r.image;
      show();
      note.textContent = `Found on ${r.found_on}. Save to keep it.`;
    } catch (e) { note.textContent = e.message; note.classList.add("bad"); }
    findBtn.disabled = false;
  } }, "Find logo");
  const box = el("div", { class: "pic-field" }, preview,
    el("div", { class: "grow stack" },
      el("div", { class: "row gap" }, input, findBtn, el("button", { class: "small-btn", type: "button", onclick: () => file.click() }, "Upload…"), file),
      note));
  show();
  return { box, input };
}

$("#form-station").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const nd = addKind === "navidrome";
  const targets = $$(".chips input:checked", f).map((i) => i.value);
  if (!targets.length) return toast("Tick at least one channel", true);
  const btn = $("button.primary", f);
  btn.disabled = true;
  let image = "";
  if (!nd) image = await findLogo(f.url.value, f.name.value).then((x) => x.image, () => "");
  for (const c of cfg.channels) {
    if (targets.includes(c.id)) c.stations.push({ name: f.name.value, kind: nd ? "navidrome" : "stream", url: nd ? "" : f.url.value, playlist: nd ? f.playlist.value : "", shuffle: f.shuffle.checked, image });
  }
  f.name.value = "";
  f.url.value = "";
  btn.disabled = false;
  saveConfig(image ? "Station added, with its logo" : "Station added");
});

$("#form-video").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const btn = $("button", f);
  btn.disabled = true;
  try {
    const ch = await api("/api/video/resolve", { method: "POST", body: { query: f.query.value } });
    if (cfg.video.some((c) => c.channel_id === ch.channel_id)) throw new Error(`${ch.name} is already in the list`);
    cfg.video.push({ name: ch.name, channel_id: ch.channel_id, enabled: true });
    f.reset();
    await saveConfig(`Added ${ch.name}. Tick the channels it should cut into.`);
  } catch (e) { toast(e.message, true); }
  btn.disabled = false;
});

$("#form-podcast").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const btn = $("button", f);
  btn.disabled = true;
  try {
    const { name } = await api("/api/podcast/resolve", { method: "POST", body: { query: f.url.value } });
    cfg.podcasts.push({ name: name || f.url.value, url: f.url.value, enabled: true });
    f.reset();
    await saveConfig(`Added ${name || "podcast"}. Tick the channels it should cut into.`);
  } catch (e) { toast(`Couldn't read that feed: ${e.message}`, true); }
  btn.disabled = false;
});

// --- own media ----------------------------------------------------------------------

async function loadMedia() {
  try {
    mediaLib = await api("/api/media");
  } catch (e) {
    toast(e.message, true);
  }
  renderMedia();
  if (cfg && view === "sources") renderSources();
  if (!editingSpot) renderFilePicker();
}

function renderMedia() {
  $("#folder-list").replaceChildren(...mediaLib.folders.map((f) => el("option", { value: f })));
  const rows = [];
  let folder = null;
  const chName = cfg ? channel().name : "this channel";
  for (const f of mediaLib.files) {
    if (f.folder !== folder) {
      folder = f.folder;
      if (folder) rows.push(el("li", { class: "folder-head" }, `${folder}/`));
    }
    rows.push(
      el("li", { class: "item-row" },
        el("div", { class: "grow" }, el("div", { class: "title" }, f.name), el("div", { class: "small muted" }, `${fmtSize(f.size)} · added ${fmtAgo(f.mtime)}`)),
        el("button", { class: "small-btn", onclick: () => api("/api/media/play", { method: "POST", body: { path: f.path, channel: channelId } }).then(() => { toast(`Queued on ${chName}: ${f.name}`); refresh(); }).catch((e) => toast(e.message, true)) }, `Play on ${chName}`),
        el("button", { class: "small-btn danger", onclick: () => { if (!confirm(`Delete ${f.name}?`)) return; api(`/api/media/${encodeURIComponent(f.path).replace(/%2F/g, "/")}`, { method: "DELETE" }).then(loadMedia).catch((e) => toast(e.message, true)); } }, "Delete"),
      ),
    );
  }
  $("#media").replaceChildren(...(rows.length ? rows : [el("li", { class: "empty" }, "No files yet. Upload jingles, station IDs or ads with + Add source › Own media.")]));
}

$("#form-upload").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const btn = $("button", f);
  btn.disabled = true;
  let n = 0;
  for (const file of f.file.files) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("folder", f.folder.value);
    btn.textContent = `Uploading ${++n}/${f.file.files.length}…`;
    try {
      await api("/api/media", { method: "POST", body: fd });
    } catch (e) {
      toast(`${file.name}: ${e.message}`, true);
    }
  }
  btn.disabled = false;
  btn.textContent = "Upload";
  f.file.value = "";
  toast(`Uploaded ${plural(n, "file")}`);
  loadMedia();
});

// --- settings -----------------------------------------------------------------------------

function renderSettings() {
  const f = $("#form-settings");
  for (const c of $$('input[name="streams"]', f)) c.checked = cfg.settings.streams.includes(c.value);
  for (const [k, v] of Object.entries(cfg.settings)) {
    const i = f.elements[k];
    if (!i || k === "streams") continue;
    if (i.type === "checkbox") i.checked = v;
    else i.value = v;
  }
}

$("#form-settings").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target;
  cfg.settings = {
    ...cfg.settings,
    station_name: f.station_name.value,
    timezone: f.timezone.value,
    invidious_url: f.invidious_url.value,
    poll_minutes: Number(f.poll_minutes.value),
    max_age_hours: Number(f.max_age_hours.value),
    ytdlp_fallback: f.ytdlp_fallback.checked,
    video_audio: f.video_audio.value,
    cache_in_ram: f.cache_in_ram.checked,
    replay_keep: f.replay_keep.checked,
    streams: $$('input[name="streams"]:checked', f).map((c) => c.value),
    navidrome_url: f.navidrome_url.value,
    navidrome_user: f.navidrome_user.value,
    navidrome_password: f.navidrome_password.value,
    navidrome_bitrate: Number(f.navidrome_bitrate.value),
  };
  playlists = null;
  saveConfig("Settings saved").then(refresh);
});

$("#btn-test-inv").addEventListener("click", async () => {
  const out = $("#inv-result");
  out.textContent = "Testing…";
  out.className = "small";
  const r = await api("/api/invidious/test", { method: "POST", body: { url: $("#form-settings").invidious_url.value } }).catch((e) => ({ ok: false, error: e.message }));
  out.textContent = r.ok ? `Connected · Invidious ${r.version}` : r.error;
  out.className = "small " + (r.ok ? "ok" : "bad");
});

$("#btn-test-nd").addEventListener("click", async () => {
  const f = $("#form-settings");
  const out = $("#nd-result");
  out.textContent = "Testing…";
  out.className = "small";
  const body = { url: f.navidrome_url.value, user: f.navidrome_user.value, password: f.navidrome_password.value };
  const r = await api("/api/navidrome/test", { method: "POST", body }).catch((e) => ({ ok: false, error: e.message }));
  out.textContent = r.ok ? `Connected · server ${r.version} · ${r.playlists} playlist${r.playlists === 1 ? "" : "s"}` : r.error;
  out.className = "small " + (r.ok ? "ok" : "bad");
});

// --- voice messages ---------------------------------------------------------------------------

let recorder = null;
let voiceBlob = null;
let voiceTimer = null;

function voiceReset() {
  voiceBlob = null;
  const a = $("#voice-audio");
  if (a.src) URL.revokeObjectURL(a.src);
  a.removeAttribute("src");
  $("#voice-preview").hidden = true;
  $("#voice-time").hidden = true;
  $("#voice-title").value = "";
}

function showVoice(blob) {
  voiceBlob = blob;
  $("#voice-audio").src = URL.createObjectURL(blob);
  $("#voice-preview").hidden = false;
}

async function sendVoice(target) {
  if (!voiceBlob) return;
  const type = (voiceBlob.type || "audio/webm").split(";")[0];
  const ext = { "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav" }[type] || "webm";
  const fd = new FormData();
  fd.append("file", new File([voiceBlob], `voice.${ext}`, { type }));
  fd.append("channel", target);
  fd.append("title", $("#voice-title").value);
  for (const b of $$("#voice-preview button")) b.disabled = true;
  try {
    const r = await api("/api/voice", { method: "POST", body: fd });
    toast(r.channels.length > 1 ? `Playing on ${plural(r.channels.length, "channel")}` : "Playing next");
    voiceReset();
    refresh();
  } catch (e) {
    toast(e.message, true);
  } finally {
    for (const b of $$("#voice-preview button")) b.disabled = false;
  }
}

$("#btn-voice-rec").addEventListener("click", async () => {
  const btn = $("#btn-voice-rec");
  if (recorder) { recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    toast("This browser can only record over HTTPS (or on localhost). Use the file field below instead.", true);
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    toast(`No microphone: ${e.message}`, true);
    return;
  }
  voiceReset();
  const chunks = [];
  const started = Date.now();
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(voiceTimer);
    $("#voice-time").textContent = $("#voice-time").textContent.replace("Recording", "Length");
    btn.textContent = "● Record";
    btn.classList.remove("recording");
    const type = recorder.mimeType;
    recorder = null;
    showVoice(new Blob(chunks, { type }));
  };
  recorder.start();
  btn.textContent = "■ Stop";
  btn.classList.add("recording");
  const t = $("#voice-time");
  t.hidden = false;
  const show = () => {
    const s = Math.round((Date.now() - started) / 1000);
    t.textContent = `Recording ${fmtTime(s)}`;
    if (s >= 300 && recorder) recorder.stop(); // five minutes is plenty for an announcement
  };
  show();
  voiceTimer = setInterval(show, 500);
});

$("#voice-file").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  if (f) { voiceReset(); showVoice(f); }
  ev.target.value = "";
});
$("#btn-voice-send").addEventListener("click", () => sendVoice(channelId));
$("#btn-voice-all").addEventListener("click", () => sendVoice("all"));
$("#btn-voice-discard").addEventListener("click", voiceReset);
if (!navigator.mediaDevices?.getUserMedia) {
  $("#voice-hint").textContent = "Recording in the browser needs HTTPS (or localhost). You can still send a recording from your phone's voice recorder as a file.";
}

// --- app devices ----------------------------------------------------------------------------

let pairing = null; // { code, expires } while a QR code is shown
let pairTimer = null;

function channelNames(ids) {
  if (!ids.length) return "All channels";
  return ids.map((id) => cfg.channels.find((c) => c.id === id)?.name || id).join(", ");
}

function renderPairChannels() {
  const who = $("#form-pair").user;
  if (me.admin) {
    const was = who.value;
    who.replaceChildren(el("option", { value: "" }, "You (admin)"), ...(userList || []).map((u) => el("option", { value: u.id }, u.name)));
    who.value = (userList || []).some((u) => u.id === was) ? was : "";
    $("[data-pair-role]").hidden = !!who.value;
  }
  const box = $("[data-pair-channels]");
  const ticked = new Set($$("input:checked", box).map((i) => i.value));
  box.replaceChildren(...cfg.channels.map((c) =>
    el("label", { class: "check" }, el("input", { type: "checkbox", value: c.id, checked: ticked.has(c.id) }), c.name)));
}

async function loadDevices() {
  const data = await api("/api/devices").catch((e) => { toast(e.message, true); return null; });
  if (!data) return;
  $("#devices-warning").hidden = data.password_set;
  const list = $("#devices-list");
  if (!data.devices.length) {
    list.replaceChildren(el("p", { class: "muted small" }, "No apps paired yet."));
  } else {
    list.replaceChildren(...data.devices.map((d) => {
      const role = el("select", { "aria-label": `What ${d.name} can do`, onchange: (e) =>
        api(`/api/devices/${d.id}`, { method: "PATCH", body: { role: e.target.value } })
          .then(() => toast("Saved")).catch((err) => { toast(err.message, true); loadDevices(); }) },
        el("option", { value: "member" }, "Listen and control"),
        el("option", { value: "admin" }, "Everything"));
      role.value = d.role;
      const whose = me.admin && d.user_id ? `${ownerName(d.user_id)}'s · ` : "";
      return el("div", { class: "device-row" },
        el("div", { class: "grow" },
          el("strong", {}, d.name),
          el("div", { class: "small muted" }, `${whose}${channelNames(d.channels)} · last used ${d.last_seen ? fmtAgo(d.last_seen) : "never"}`)),
        me.admin && !d.user_id ? role : null,
        el("button", { type: "button", class: "small-btn", onclick: () => {
          if (!confirm(`Remove ${d.name}? The app stops working until it is paired again.`)) return;
          api(`/api/devices/${d.id}`, { method: "DELETE" }).then(() => { toast("Removed"); loadDevices(); }).catch((e) => toast(e.message, true));
        } }, "Remove"));
    }));
  }
  // An app just used the code shown here: hide it.
  if (pairing && !data.pending.some((p) => p.code === pairing.code)) {
    hidePairing();
    toast("App paired");
  }
}

function hidePairing() {
  pairing = null;
  clearInterval(pairTimer);
  $("#pair-box").hidden = true;
}

function tickPairing() {
  if (!pairing) return;
  const left = Math.round(pairing.expires - Date.now() / 1000);
  if (left <= 0) { hidePairing(); toast("The pairing code expired", true); return; }
  $("#pair-expires").textContent = `valid for ${Math.floor(left / 60)}:${pad2(left % 60)}`;
  if (left % 3 === 0) loadDevices();
}

$("#form-pair").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const body = {
    name: f.name.value, role: f.user.value ? "member" : f.role.value, user: f.user.value, server: location.origin,
    channels: $$("[data-pair-channels] input:checked").map((i) => i.value),
  };
  try {
    const r = await api("/api/devices/pair", { method: "POST", body });
    if (pairing) api(`/api/devices/pair/${pairing.code}`, { method: "DELETE" }).catch(() => {});
    pairing = { code: r.code, expires: r.expires };
    $("#pair-qr").innerHTML = r.qr_svg; // generated by the server, no user markup
    $("#pair-code").textContent = r.code.replace(/(.{5})/, "$1 ");
    $("#pair-server").textContent = r.server;
    $("#pair-box").hidden = false;
    clearInterval(pairTimer);
    pairTimer = setInterval(tickPairing, 1000);
    tickPairing();
  } catch (e) {
    toast(e.message, true);
  }
});

$("#form-pair").user.addEventListener("change", (e) => { $("[data-pair-role]").hidden = !!e.target.value; });

$("#btn-pair-cancel").addEventListener("click", () => {
  if (pairing) api(`/api/devices/pair/${pairing.code}`, { method: "DELETE" }).catch(() => {});
  hidePairing();
});

// --- users and your account -------------------------------------------------------------

async function loadUsers() {
  const data = await api("/api/users").catch((e) => { toast(e.message, true); return null; });
  if (!data) return;
  userList = data.users;
  $("#users-warning").hidden = data.password_set;
  $("#users-list").replaceChildren(
    ...(userList.length
      ? userList.map((u) => {
          const role = el("select", { "aria-label": `Role of ${u.name}`, onchange: (e) =>
            api(`/api/users/${u.id}`, { method: "PATCH", body: { role: e.target.value } })
              .then(() => { toast("Saved"); loadUsers(); }).catch((err) => { toast(err.message, true); loadUsers(); }) },
            el("option", { value: "user" }, "User"),
            el("option", { value: "admin" }, "Admin"));
          role.value = u.role;
          const chans = u.channels.length ? u.channels.map((c) => c.name + (c.public ? " (public)" : "")).join(", ") : "no channels yet";
          return el("div", { class: "user-row" },
            el("div", { class: "grow" },
              el("strong", {}, u.name),
              el("div", { class: "small muted" }, `${chans} · ${plural(u.devices, "app")} · files in ${u.folder}`)),
            role,
            el("button", { type: "button", class: "small-btn", onclick: () => {
              const pw = prompt(`New password for ${u.name} (8+ characters). They are logged out everywhere.`);
              if (!pw) return;
              api(`/api/users/${u.id}`, { method: "PATCH", body: { password: pw } }).then(() => toast("Password changed")).catch((e) => toast(e.message, true));
            } }, "New password"),
            el("button", { type: "button", class: "small-btn danger", onclick: () => {
              if (!confirm(`Remove ${u.name}? Their apps stop working. Their channels become yours (private), so nothing stops playing.`)) return;
              api(`/api/users/${u.id}`, { method: "DELETE" }).then(() => { toast("Removed"); loadUsers(); loadConfig(); }).catch((e) => toast(e.message, true));
            } }, "Remove"));
        })
      : [el("p", { class: "muted small" }, "No users yet.")]),
  );
  if (cfg) renderConfig();
}

$("#form-user").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try {
    const u = await api("/api/users", { method: "POST", body: { name: f.name.value.trim().toLowerCase(), password: f.password.value, role: f.role.value } });
    toast(`Added ${u.name}. They log in on this page with their name and password.`);
    f.reset();
    loadUsers();
  } catch (e) { toast(e.message, true); }
});

$("#form-password").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try {
    await api("/api/me/password", { method: "POST", body: { old: f.old.value, new: f.new.value } });
    toast("Password changed. Other browsers are logged out.");
    f.reset();
  } catch (e) { toast(e.message, true); }
});

// --- config ------------------------------------------------------------------------------

function renderConfig() {
  const ch = channel();
  channelId = ch ? ch.id : null;
  const ro = !!ch && !canEdit(ch) && (view === "channel" || view === "schedule");
  document.body.classList.toggle("readonly", ro);
  $$("[data-readonly-note]").forEach((e) => (e.textContent = ro
    ? `${ch.name} is ${ownerName(ch.owner)}'s public channel. You can listen and see what's on, but only ${ownerName(ch.owner)} can change it.`
    : ""));
  if (!ch && (view === "channel" || view === "schedule")) { location.hash = "#/home"; return; }
  $$("[data-channel-name]").forEach((e) => (e.textContent = ch ? ch.name : ""));
  $$("[data-week]").forEach((a) => (a.href = `#/schedule/${encodeURIComponent(channelId)}`));
  if (view === "channel") {
    renderChannelChips($("#channel-chips"), "channel");
    renderStationChips();
    renderChannelSettings();
  }
  if (view === "schedule") {
    renderChannelChips($("#schedule-chips"), "schedule");
    renderPrograms();
    renderSpots();
    renderTimeline();
  }
  if (view === "sources") { renderSources(); renderMedia(); }
  if (view === "settings") { if (me.admin) renderSettings(); renderPairChannels(); }
}

async function loadConfig() {
  cfg = await api("/api/config");
  renderConfig();
}

// --- start -------------------------------------------------------------------------

daysPicker($("#form-program [data-days]"), DAYS.slice(0, 5));
basePicker($("#form-program [data-base]"));
resetSpotForm();

api("/api/me")
  .then((m) => { me = m; })
  .catch(() => {})
  .then(() => {
    $("#btn-logout").hidden = !me.login;
    document.body.classList.toggle("is-user", !me.admin);
    $$("[data-user-name]").forEach((e) => (e.textContent = me.user ? me.user.name : ""));
    if (me.admin && me.login) loadUsers();
    return loadConfig();
  })
  .then(() => { route(); connectLive(); if (!live) refresh(); });
$("#btn-logout").addEventListener("click", () =>
  api("/api/logout", { method: "POST" }).finally(() => location.replace("/login")));
setInterval(tick, 3000);
