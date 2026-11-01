import { createMap, esc, ICON_URLS, KIND_GROUP, KIND_LABEL, LEVEL_COLORS } from "./map.js";
import { createPipeline } from "./pipeline.js";
import { createVoicePlayer } from "./voice.js";
import { createTimeline, fmtTime } from "./chart.js";

const $ = (sel) => document.querySelector(sel);
const LEVEL_RANK = { low: 0, moderate: 1, high: 2, severe: 3 };
const TAGS = {
  reasoning: "think", decision: "decide", tool_result: "tool", retry: "retry", warning: "warn", guardrail: "check",
  step_error: "error", failed: "error", done: "done", approved: "approve", run_start: "start",
  voice_start: "voice", voice_chunk: "voice", voice_ready: "voice", step_start: "step",
};

let currentRun = null, result = null, stream = null, alertLang = "en", buildingFilter = "all", buildingLimit = 80, playTimer = null;

// --- components ----------------------------------------------------------------------------------

const progressBar = $("#progress");
const pipeline = createPipeline($("#pipeline"), {
  onProgress: (p) => { progressBar.style.width = `${Math.round(p * 100)}%`; },
});
const voice = createVoicePlayer({
  root: $("#voice"), audio: $("#voice-audio"), play: $("#voice-play"), status: $("#voice-status"),
  canvas: $("#voice-wave"), segments: $("#voice-segments"), download: $("#voice-download"),
  retry: $("#voice-retry"), autoplay: $("#voice-autoplay"),
});
const flood = createMap("map", { onPick: (lat, lon) => routeFrom({ lat, lon }, "Selected point") });
const timeline = createTimeline($("#tl-chart"), $("#tl-tip"), $("#tl-readout"), { onScrub: (h) => flood.setHour(h) });

document.querySelectorAll("[data-icon]").forEach((img) => { img.src = ICON_URLS[img.dataset.icon]; });

// --- header --------------------------------------------------------------------------------------

function tick() {
  $("#clock").textContent = new Date().toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }) + " IST";
}
tick();
setInterval(tick, 30000);

async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("#app-name").textContent = h.app_name;
    const llm = h.llm_providers.map((p) => p.split(":").pop().split("/").pop());
    const chips = [
      [llm.length ? `LLM · ${llm.join(" → ")}` : "LLM · template only", llm.length > 0],
      ["Routing · Valhalla", true],
      [h.tamil_translation ? "Tamil · Bodhan Translate" : "Tamil · off", h.tamil_translation],
      [h.tamil_speech ? "Voice · Bodhan Speak" : "Voice · off", h.tamil_speech],
    ];
    if (h.telegram) chips.push([`Telegram · ${h.telegram_channel ? "@" + h.telegram_channel.split("/").pop() : "ready"}`, true]);
    if (h.telegram_channel) setupTelegram(h.telegram_channel);
    passcodeRequired = Boolean(h.approval_passcode);
    $("#capabilities").replaceChildren(...chips.map(([t, on]) => chip(t, on ? "on" : "off")));
    const [min, max] = h.replay_range;
    Object.assign($("#replay-date"), { min, max });
  } catch (err) {
    $("#capabilities").replaceChildren(chip(`Backend unreachable: ${err.message}`, "off"));
  }
}

// --- form ----------------------------------------------------------------------------------------

document.querySelectorAll('input[name="mode"]').forEach((r) =>
  r.addEventListener("change", () => { $("#replay-field").hidden = document.querySelector('input[name="mode"]:checked').value !== "replay"; }),
);

document.querySelectorAll(".scenario").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelector(`input[name="mode"][value="${btn.dataset.mode}"]`).checked = true;
    $("#replay-field").hidden = btn.dataset.mode !== "replay";
    if (btn.dataset.date) $("#replay-date").value = btn.dataset.date;
    $("#run-form").requestSubmit();
  }),
);

$("#run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const body = {
    mode,
    horizon_hours: Number(document.querySelector('input[name="horizon"]:checked').value),
    query: $("#query").value.trim() || null,
    replay_date: mode === "replay" ? $("#replay-date").value : null,
  };
  $("#form-error").hidden = true;
  setRunning(true);
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
    startRun(run_id);
  } catch (err) {
    $("#form-error").textContent = err.message;
    $("#form-error").hidden = false;
    setRunning(false);
  }
});

function setRunning(on) {
  $("#run-button").disabled = on;
  $("#run-label").textContent = on ? "Agent working…" : "Deploy agent";
  $("#radar").hidden = !on;
  progressBar.style.opacity = on ? "1" : "0";
}

function setStatus(status) {
  const pill = $("#run-status");
  pill.className = `status-pill ${status}`;
  pill.textContent = status.replaceAll("_", " ");
}

// --- run lifecycle -------------------------------------------------------------------------------

function startRun(runId) {
  currentRun = runId;
  result = null;
  stream?.close();
  stopPlay();
  $("#hero").hidden = true;
  $("#trace").replaceChildren();
  $("#trace-count").textContent = "";
  $("#results").hidden = true;
  $("#dock").hidden = true;
  $("#legend").hidden = true;
  $("#sources").replaceChildren();
  progressBar.style.width = "0";
  pipeline.reset();
  voice.reset();
  flood.flyHome();
  setStatus("running");

  stream = new EventSource(`/api/runs/${runId}/events`);
  stream.onmessage = (msg) => onEvent(JSON.parse(msg.data));
  stream.addEventListener("end", (msg) => {
    stream.close();
    pipeline.handle({ type: "end" });
    const { status } = JSON.parse(msg.data);
    if (status === "failed") { setRunning(false); setStatus("failed"); pipeline.fail(); }
  });
  stream.onerror = () => { if (stream.readyState !== EventSource.CLOSED) $("#run-status").textContent = "reconnecting…"; };
}

async function onEvent(ev) {
  appendTrace(ev);
  pipeline.handle(ev);
  if (ev.type === "done") {
    setRunning(false);
    setStatus(ev.data?.status || "done");
    const run = await api(`/api/runs/${currentRun}`);
    renderResult(run.result);
  } else if (ev.type === "voice_start") {
    voice.show("Synthesising Tamil speech…");
  } else if (ev.type === "voice_chunk") {
    voice.addPart(ev.data.index, ev.data.total, ev.data.url);
  } else if (ev.type === "voice_ready") {
    voice.finished(ev.data.url);
  } else if (ev.type === "warning" && /voice alert failed/i.test(ev.message)) {
    voice.failed("Voice synthesis failed. The text alert is unaffected.");
  }
}

function appendTrace(ev) {
  if (ev.type === "step_end") return;
  const li = document.createElement("li");
  li.className = `t-${ev.type}`;
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = TAGS[ev.type] || ev.type;
  li.append(tag, ev.message);
  const list = $("#trace");
  list.append(li);
  list.scrollTop = list.scrollHeight;
  $("#trace-count").textContent = `· ${list.children.length} events`;
}

// --- results -------------------------------------------------------------------------------------

function renderResult(res) {
  result = res;
  flood.setResult(res);
  $("#results").hidden = false;
  $("#dock").hidden = false;
  $("#legend").hidden = false;
  renderStats(res);
  renderTimeline(res);
  renderAlert(res);
  renderBuildings(res);
  renderRoutes(res);
  renderSources(res.sources);
  selectTab("alert");
  // Grow the 3D columns hour by hour once, so the rain build-up is visible.
  setTimeout(() => startPlay(true), 900);
}

function renderStats(res) {
  const cells = res.cells.features.map((f) => f.properties).filter((p) => p.in_focus);
  const peak = Math.max(...cells.map((p) => p.rain_24h_max_mm));
  const count = (lvl) => cells.filter((p) => p.level === lvl).length;
  const items = [
    ["Peak 24 h", peak, " mm", ""],
    ["Severe cells", count("severe"), "", "severe"],
    ["High cells", count("high"), "", "high"],
    ["Buildings", (res.facilities_at_risk || []).length, "", ""],
    ["Routes", (res.routes || []).length, "", ""],
  ];
  $("#stats").replaceChildren(...items.map(([label, value, unit, cls]) => {
    const d = document.createElement("div");
    d.className = `stat ${cls}`;
    d.innerHTML = `<b data-to="${value}" data-unit="${unit}">0${unit}</b><span>${label}</span>`;
    return d;
  }));
  document.querySelectorAll("#stats b").forEach(countUp);
}

function countUp(b) {
  const to = Number(b.dataset.to), unit = b.dataset.unit, t0 = performance.now(), dur = 1200;
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    b.textContent = `${Math.round(to * e)}${unit}`;
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderTimeline(res) {
  const byCell = res.rain_hourly?.by_cell || {};
  const series = Object.values(byCell);
  if (!series.length) return;
  const wettest = series.reduce((a, b) => (b.reduce((x, y) => x + y, 0) > a.reduce((x, y) => x + y, 0) ? b : a));
  const mean = wettest.map((_, i) => series.reduce((s, cell) => s + (cell[i] || 0), 0) / series.length);
  timeline.render({ times: res.rain_hourly.times, values: wettest, mean });
}

function startPlay(fromStart = false) {
  stopPlay();
  if (!timeline.length) return;
  if (fromStart || timeline.head >= timeline.length - 1) timeline.setHead(0);
  $("#tl-play").classList.add("playing");
  const per = Math.max(25, 4500 / timeline.length);
  playTimer = setInterval(() => {
    if (timeline.head >= timeline.length - 1) { stopPlay(); return; }
    timeline.setHead(timeline.head + 1);
  }, per);
}

function stopPlay() {
  clearInterval(playTimer);
  playTimer = null;
  $("#tl-play").classList.remove("playing");
}

$("#tl-play").addEventListener("click", () => (playTimer ? stopPlay() : startPlay()));
$("#tl-chart").addEventListener("pointerdown", stopPlay);

// alert

function renderAlert(res) {
  const alert = res.alert;
  $("#no-alert").hidden = Boolean(alert);
  $("#alert-card").hidden = !alert;
  if (!alert) {
    $("#all-clear-text").textContent = res.facts?.summary || "No part of Chennai reaches high flood risk in this window.";
    return;
  }
  const levels = res.cells.features.map((f) => f.properties).filter((p) => p.in_focus).map((p) => p.level);
  const worst = levels.reduce((a, b) => (LEVEL_RANK[b] > LEVEL_RANK[a] ? b : a), "low");
  const sev = $("#severity");
  sev.style.setProperty("--sev", LEVEL_COLORS[worst]);
  sev.textContent = `${worst} flood risk · ${fmtTime(res.window.start, { day: "numeric", month: "short" })} – ${fmtTime(res.window.end, { day: "numeric", month: "short" })}`;

  const hasTa = Boolean(alert.body_ta);
  document.querySelector('.lang[data-lang="ta"]').disabled = !hasTa;
  setLang("en");

  const check = alert.guardrail || {};
  const tr = alert.translation || {};
  const meta = [
    [`Written by ${alert.generated_by}`, ""],
    [check.passed ? `Fact check passed${check.attempts ? ` · ${check.attempts} draft${check.attempts > 1 ? "s" : ""}` : ""}` : "Fact check failed", check.passed ? "ok" : "bad"],
    [hasTa ? "Tamil numbers verified" : tr.provider ? "Tamil rejected · English only" : "Tamil not configured", hasTa ? "ok" : "bad"],
  ];
  $("#alert-meta").replaceChildren(...meta.map(([t, c]) => chip(t, c)));

  const approve = $("#approve-button");
  approve.disabled = false;
  $("#approve-label").textContent = "Hold to approve";
  $("#approve-stamp").hidden = true;
  $("#tg-posts").hidden = true;
  passcodeInput.hidden = !passcodeRequired;
}

function setLang(lang) {
  alertLang = lang;
  document.querySelectorAll(".lang").forEach((b) => b.classList.toggle("active", b.dataset.lang === lang));
  const a = result?.alert;
  if (!a) return;
  const card = $("#alert-card");
  card.lang = lang;
  $("#alert-headline").textContent = a[`headline_${lang}`] || "";
  $("#alert-body").textContent = a[`body_${lang}`] || "";
  $("#alert-actions").replaceChildren(...(a[`actions_${lang}`] || []).map((t) => Object.assign(document.createElement("li"), { textContent: t })));
}
document.querySelectorAll(".lang").forEach((b) => b.addEventListener("click", () => setLang(b.dataset.lang)));

$("#voice-retry").addEventListener("click", async () => {
  voice.show("Trying again…");
  $("#voice-retry").hidden = true;
  try {
    const { url } = await api(`/api/runs/${currentRun}/speech`, { method: "POST" });
    voice.setTotal(1);
    voice.addPart(0, 1, url);
    voice.finished(url);
  } catch (err) {
    voice.failed(err.message);
  }
});

// approve: press and hold, so a stray click never releases a public alert

let holdTimer = null, passcodeRequired = false;
const approveBtn = $("#approve-button");
const passcodeInput = $("#approve-passcode");

function passcodeMissing() {
  if (!passcodeRequired || passcodeInput.value) return false;
  passcodeInput.focus();
  toast("Enter the officer passcode to approve");
  return true;
}

approveBtn.addEventListener("pointerdown", () => {
  if (approveBtn.disabled || passcodeMissing()) return;
  approveBtn.classList.add("holding");
  $("#approve-label").textContent = "Keep holding…";
  holdTimer = setTimeout(approve, 1100);
});
["pointerup", "pointerleave", "pointercancel"].forEach((ev) =>
  approveBtn.addEventListener(ev, () => {
    if (!holdTimer) return;
    clearTimeout(holdTimer);
    holdTimer = null;
    approveBtn.classList.remove("holding");
    if (!approveBtn.disabled) $("#approve-label").textContent = "Hold to approve";
  }),
);
approveBtn.addEventListener("click", (e) => { if (e.detail === 0) approve(); }); // keyboard

async function approve() {
  holdTimer = null;
  if (passcodeMissing()) return;
  approveBtn.disabled = true;
  approveBtn.classList.remove("holding");
  $("#approve-label").textContent = "Approving…";
  try {
    const body = JSON.stringify({ passcode: passcodeRequired ? passcodeInput.value : null });
    const res = await api(`/api/runs/${currentRun}/approve`, { method: "POST", body });
    passcodeInput.hidden = true;
    const when = new Date(res.approved_at * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });
    $("#approve-label").textContent = "Approved";
    const stamp = $("#approve-stamp");
    stamp.textContent = `Released ${when} IST`;
    stamp.hidden = false;
    appendTrace({ type: "approved", message: "Officer approved the alert for release" });
    renderTelegramPosts(res.telegram);
    setStatus("approved");
  } catch (err) {
    approveBtn.disabled = false;
    $("#approve-label").textContent = "Hold to approve";
    if (/passcode/i.test(err.message)) {
      passcodeInput.value = "";
      passcodeInput.focus();
    }
    toast(err.message);
  }
}

// telegram: public channel link, QR code, and links to the posts an approval created

const PLANE = document.querySelector("#tg-join svg")?.outerHTML || "";

function setupTelegram(url) {
  const handle = `@${url.split("/").pop()}`;
  const join = $("#tg-join"), pop = $("#tg-pop");
  join.hidden = false;
  $("#tg-link").href = url;
  $("#tg-handle").textContent = `Open ${handle}`;
  $("#hero-tg").href = url;
  $("#hero-tg").hidden = false;
  $("#hero-tg-handle").textContent = url.replace(/^https?:\/\//, "");
  if (window.qrcode) {
    const qr = window.qrcode(0, "M");
    qr.addData(url);
    qr.make();
    $("#tg-qr").innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  }
  const toggle = (open) => {
    pop.hidden = !open;
    join.setAttribute("aria-expanded", String(open));
  };
  join.addEventListener("click", (e) => { e.stopPropagation(); toggle(pop.hidden); });
  document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) toggle(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggle(false); });
}

function renderTelegramPosts(delivery) {
  const box = $("#tg-posts");
  box.replaceChildren();
  if (!delivery?.configured) { box.hidden = true; return; }
  box.hidden = false;
  if (delivery.error) {
    box.append(Object.assign(document.createElement("span"), { className: "bad", textContent: `Not posted to Telegram: ${delivery.error}` }));
    return;
  }
  box.append("Posted to Telegram:");
  delivery.posts.forEach((url, i) => {
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.innerHTML = PLANE;
    a.append(i === 0 ? "English post" : "Tamil post");
    box.append(a);
  });
}

// buildings

function renderBuildings(res) {
  const list = res.facilities_at_risk || [];
  $("#buildings-count").textContent = list.length;
  $("#dl-csv").href = `/api/runs/${currentRun}/buildings.csv`;
  $("#dl-geojson").href = `/api/runs/${currentRun}/buildings.geojson`;
  const counts = {};
  for (const f of list) counts[f.kind] = (counts[f.kind] || 0) + 1;
  buildingFilter = "all";
  buildingLimit = 80;
  $("#building-search").value = "";
  const chips = [["all", "All", list.length], ...Object.keys(KIND_LABEL).filter((k) => counts[k]).map((k) => [k, KIND_LABEL[k], counts[k]])];
  $("#kind-chips").replaceChildren(...chips.map(([k, label, n]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `kind-chip${k === "all" ? " active" : ""}`;
    b.dataset.kind = k;
    b.append(label);
    const strong = document.createElement("b");
    strong.textContent = n;
    b.append(strong);
    b.addEventListener("click", () => {
      buildingFilter = k;
      buildingLimit = 80;
      document.querySelectorAll(".kind-chip").forEach((c) => c.classList.toggle("active", c === b));
      drawBuildings();
    });
    return b;
  }));
  drawBuildings();
}

function drawBuildings() {
  const q = $("#building-search").value.trim().toLowerCase();
  const all = (result?.facilities_at_risk || []).filter((f) =>
    (buildingFilter === "all" || f.kind === buildingFilter) &&
    (!q || [f.name, f.locality, f.address].some((s) => (s || "").toLowerCase().includes(q))));
  const shown = all.slice(0, buildingLimit);
  $("#building-list").replaceChildren(...shown.map((f) => {
    const li = document.createElement("li");
    li.className = "building";
    li.tabIndex = 0;
    li.innerHTML = `<img alt="" src="${ICON_URLS[KIND_GROUP[f.kind] || "relief"]}">
      <div><div class="building-name"></div><div class="building-sub"></div></div>
      <span class="level" style="--c:${LEVEL_COLORS[f.level]}">${esc(f.level)}</span>`;
    li.querySelector(".building-name").textContent = f.name;
    li.querySelector(".building-sub").textContent = [KIND_LABEL[f.kind], f.locality, f.rain_24h_max_mm != null ? `${Math.round(f.rain_24h_max_mm)} mm/24 h` : null].filter(Boolean).join(" · ");
    const go = () => { stopPlay(); flood.focusFacility(f); };
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    return li;
  }));
  const more = $("#building-more");
  more.hidden = all.length <= buildingLimit;
  more.textContent = `Show more (${all.length - shown.length} left)`;
}
$("#building-search").addEventListener("input", () => { buildingLimit = 80; drawBuildings(); });
$("#building-more").addEventListener("click", () => { buildingLimit += 120; drawBuildings(); });

// routes

function renderRoutes(res) {
  $("#route-result").replaceChildren();
  const routes = res.routes || [];
  const canRoute = res.outcome === "alert";
  $("#route-form").querySelectorAll("input,button").forEach((x) => { x.disabled = !canRoute; });
  $("#route-pick").disabled = $("#route-locate").disabled = !canRoute;
  if (!canRoute) $("#route-result").textContent = "No high-risk area in this run, so no evacuation is needed.";
  $("#evac-list").replaceChildren(...(routes.length ? routes.map((r) => {
    const li = document.createElement("li");
    li.className = "evac";
    const verdict = r.hazard_overlap_km === 0 ? "clear of high-risk cells" : `${r.hazard_overlap_km} km through risk cells (unavoidable)`;
    li.innerHTML = `<b></b><div class="sub"></div>`;
    li.querySelector("b").textContent = `${r.from.name} → ${r.to.name}`;
    li.querySelector(".sub").textContent = `${r.distance_km} km · ${r.duration_min} min · ${verdict}${r.blocked_points ? ` · ${r.blocked_points} roads blocked` : ""}`;
    li.addEventListener("click", () => { stopPlay(); flood.fitLine(r.coords); });
    return li;
  }) : [Object.assign(document.createElement("li"), { className: "evac", textContent: "No evacuation routes were needed." })]));
}

$("#route-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const place = $("#route-place").value.trim();
  if (place) routeFrom({ place }, place);
});
$("#route-pick").addEventListener("click", () => {
  const on = !$("#route-pick").classList.contains("on");
  $("#route-pick").classList.toggle("on", on);
  flood.setPicking(on);
  if (on) toast("Click anywhere on the map to start the route there");
});
$("#route-locate").addEventListener("click", () => {
  if (!navigator.geolocation) return toast("This browser cannot share its location");
  navigator.geolocation.getCurrentPosition(
    (pos) => routeFrom({ lat: pos.coords.latitude, lon: pos.coords.longitude }, "Your location"),
    () => toast("Location permission was not granted"),
    { timeout: 10000 },
  );
});

async function routeFrom(body, label) {
  $("#route-pick").classList.remove("on");
  flood.setPicking(false);
  selectTab("routes");
  stopPlay();
  if (body.lat != null) flood.showStart(body.lat, body.lon);
  const box = $("#route-result");
  box.innerHTML = `<div class="route-loading"><span class="spinner"></span><span>Routing from ${esc(label)}, blocking flooded roads…</span></div>`;
  try {
    const r = await api(`/api/runs/${currentRun}/route`, { method: "POST", body: JSON.stringify(body) });
    flood.showSafeRoute(r);
    const verdict = r.hazard_overlap_km === 0
      ? "Avoids every high-risk cell"
      : `Still crosses ${r.hazard_overlap_km} km of risk cells, the least possible from here`;
    const start = r.from.level ? `You start in a <span class="level" style="--c:${LEVEL_COLORS[r.from.level]}">${esc(r.from.level)}</span> risk cell` : "";
    box.innerHTML = `<div class="route-card">
      <div class="building-sub">${start}</div>
      <h4>${esc(r.to.name)}</h4>
      <div class="building-sub">${esc(KIND_LABEL[r.to.kind] || r.to.kind)}${r.to.locality ? ` · ${esc(r.to.locality)}` : ""}${r.to.address ? ` · ${esc(r.to.address)}` : ""}</div>
      <div class="route-big"><div><b>${r.distance_km} km</b><span>distance</span></div><div><b>${r.duration_min} min</b><span>drive</span></div><div><b>${r.blocked_points}</b><span>roads blocked</span></div></div>
      <div class="building-sub">${verdict} · via ${esc(r.provider)}</div>
      <ol class="route-steps">${r.attempts.map((a) => `<li>${esc(a)}</li>`).join("")}</ol>
    </div>`;
  } catch (err) {
    box.innerHTML = "";
    box.textContent = err.message;
  }
}

function renderSources(sources) {
  $("#sources").replaceChildren(...sources.map((s) => {
    const when = new Date(s.fetched_at * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
    const label = s.stale ? "stale cache" : s.from_cache ? "cached" : "live";
    const c = chip(`${s.source} · ${label} ${when}`, s.stale ? "stale" : "on");
    c.title = `Fetched ${new Date(s.fetched_at * 1000).toLocaleString()}`;
    return c;
  }));
}

// tabs

function selectTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  });
  for (const t of ["alert", "buildings", "routes"]) $(`#tab-${t}`).hidden = t !== name;
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));

// --- helpers -------------------------------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ") : data.detail;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

function chip(text, cls) {
  return Object.assign(document.createElement("span"), { className: `chip ${cls}`, textContent: text });
}

let toastTimer = null;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

loadHealth();
