/* Academia Shield dashboard. No framework and no external requests: it runs fully offline. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const pct = (v, d = 0) => (v == null ? "n/a" : `${(v * 100).toFixed(d)}%`);
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "");
const secs = (v) => (v == null ? "n/a" : v < 10 ? `${v.toFixed(1)} s` : `${v.toFixed(0)} s`);

const STATE_TEXT = {
  healthy: "Healthy", impacted: "Impacted", degraded: "Degraded", at_risk: "At risk", replacing: "Replacing",
  draining: "Draining", starting: "Starting", failing: "Failing", down: "Down", quarantined: "Paused",
};
const STATE_ICON = {
  healthy: "●", impacted: "◆", degraded: "▲", at_risk: "▲", replacing: "↻", draining: "↻", starting: "↻", failing: "✕", down: "✕", quarantined: "■",
};
const OUTCOME_TEXT = {
  prevented: "Prevented", recovered: "Recovered", self_resolved: "Cleared itself", escalated: "Escalated",
  resolved_by_operator: "Fixed by operator", predicted: "Predicted", active: "Failing", mitigating: "Recovering", degraded: "Degraded",
};
const stateTag = (s) => `<span class="st st-${esc(s)}"><span aria-hidden="true">${STATE_ICON[s] || "●"}</span>${esc(STATE_TEXT[s] || s)}</span>`;
const outcomeTag = (i) => {
  const key = i.status === "resolved" ? i.outcome : i.status === "escalated" ? "escalated" : i.status;
  return `<span class="st st-${esc(key)}">${esc(OUTCOME_TEXT[key] || key)}</span>`;
};

const app = {
  state: null, route: "live", traffic: new Map(), notes: [], unread: 0, openService: null, selected: null,
  highlight: null, layoutKey: "", layout: null, hist: {}, catalog: null, model: null, bench: null,
  incidentId: null, lastIncidentSig: "",
};

/* ---------------------------------------------------------------- network */
async function api(url, opts = {}) {
  const init = { method: opts.method || (opts.body ? "POST" : "GET"), headers: {} };
  if (opts.body) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(url, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed with status ${r.status}`);
  return data;
}
function act(url, body, okText) {
  return api(url, { method: "POST", body: body || {} })
    .then((d) => { if (okText) toast("info", okText); return d; })
    .catch((e) => { toast("critical", "That did not work", e.message); throw e; });
}
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { $("#conn").hidden = true; };
  ws.onclose = () => { $("#conn").hidden = false; setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "state") onState(m.data);
    else if (m.type === "notification") onNote(m.data);
  };
}
function onNote(n) {
  app.notes.unshift(n);
  app.notes = app.notes.slice(0, 200);
  if (app.route !== "alerts") { app.unread += 1; }
  updateBadges();
  toast(n.level, n.title, n.body);
  if (app.route === "alerts") renderAlerts();
}
function toast(level, title, body) {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.innerHTML = `<b>${esc(title)}</b>${body ? `<span>${esc(body)}</span>` : ""}`;
  const box = $("#toasts");
  box.prepend(el);
  while (box.children.length > 3) box.lastChild.remove();
  setTimeout(() => el.remove(), level === "critical" ? 9000 : 6000);
}
function updateBadges() {
  const s = app.state;
  const a = $("#navAlerts"); a.hidden = !app.unread; a.textContent = app.unread;
  const inc = $("#navIncidents"); const n = s ? s.open_incidents.length : 0; inc.hidden = !n; inc.textContent = n;
}

function onState(s) {
  app.state = s;
  (s.traffic_recent || []).forEach((b) => app.traffic.set(b.ts, b));
  for (const v of s.instances) {
    const h = (app.hist[v.id] = app.hist[v.id] || []);
    h.push(v.risk_ema || 0);
    if (h.length > 300) h.shift();
  }
  const sel = $("#modeSelect");
  if (document.activeElement !== sel && sel.value !== s.mode) sel.value = s.mode;
  updateBadges();
  renderLive();
}

/* ---------------------------------------------------------------- routing */
function go() {
  const r = (location.hash.replace(/^#\/?/, "") || "live").split("/");
  app.route = ["live", "forecast", "incidents", "analytics", "model", "lab", "alerts"].includes(r[0]) ? r[0] : "live";
  app.incidentId = r[0] === "incidents" && r[1] ? Number(r[1]) : app.incidentId;
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === app.route) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (app.route === "alerts") { app.unread = 0; updateBadges(); }
  app.layoutKey = "";
  const fn = { live: mountLive, forecast: mountForecast, incidents: mountIncidents, analytics: mountAnalytics, model: mountModel, lab: mountLab, alerts: mountAlerts }[app.route];
  fn();
  window.scrollTo(0, 0);
}

/* ---------------------------------------------------------------- live map */
function mountLive() {
  $("#view").innerHTML = `
    <section class="briefing" id="briefing"><h1 id="briefText">Connecting to the platform</h1>
      <div class="gauges" id="gauges"></div></section>
    <section class="live">
      <div>
        <div class="map-wrap"><svg id="map" viewBox="0 0 1000 520" role="img" aria-label="Service dependency map"></svg></div>
        <div class="legend">
          <span><svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" stroke="currentColor" stroke-width="2"/></svg>Required call</span>
          <span><svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" stroke="currentColor" stroke-width="2" stroke-dasharray="4 4"/></svg>Optional call</span>
          <span><svg width="18" height="14"><ellipse cx="9" cy="7" rx="8" ry="6" fill="none" stroke="currentColor"/><ellipse cx="9" cy="7" rx="4" ry="3" fill="none" stroke="currentColor"/></svg>Rings grow with failure risk</span>
          <span>Select a service for details</span>
        </div>
      </div>
      <div class="rail"><div><h2>Next ${esc(app.state ? app.state.model.horizon_s : 30)} seconds</h2>
        <p class="small muted">Chance each instance fails, highest first</p></div>
        <div id="forecastList"></div></div>
    </section>
    <div id="demoBox"></div>
    <section class="strip">
      <div class="panel"><h2>What users experienced</h2><p class="sub">Requests per second from synthetic users over the last 5 minutes</p>
        <div class="leg"><span><i style="background:var(--s2)"></i>Succeeded</span><span><i style="background:var(--crit)"></i>Failed</span></div>
        <div id="trafficChart"></div></div>
      <div class="panel"><h2>Latest alerts</h2><ul class="feed" id="feed"></ul></div>
    </section>`;
  loadTraffic();
  renderLive();
}
async function loadTraffic() {
  try {
    const rows = await api("/api/traffic?seconds=300");
    rows.forEach((b) => app.traffic.set(b.ts, b));
    renderTraffic();
  } catch (e) { /* the socket keeps filling this in */ }
}

function layoutMap(s) {
  // Columns come from the discovered dependency graph, not from a hand-drawn diagram.
  const names = s.services.map((x) => x.name);
  const key = names.join() + "|" + s.edges.map((e) => e.src + ">" + e.dst).join();
  if (key === app.layoutKey && app.layout) return app.layout;
  app.layoutKey = key;
  const depth = Object.fromEntries(names.map((n) => [n, 0]));
  for (let i = 0; i < names.length; i++) {
    s.edges.forEach((e) => { if (depth[e.src] + 1 > depth[e.dst]) depth[e.dst] = depth[e.src] + 1; });
  }
  const maxD = Math.max(...Object.values(depth), 1);
  const cols = Array.from({ length: maxD + 1 }, () => []);
  names.forEach((n) => cols[depth[n]].push(n));
  const pos = {};
  const W = 1000, H = 520, nodeW = 158, nodeH = 66;
  cols.forEach((col, d) => {
    if (d > 0) {
      const bary = (n) => {
        const ps = s.edges.filter((e) => e.dst === n).map((e) => pos[e.src]).filter(Boolean);
        return ps.length ? ps.reduce((a, p) => a + p.y, 0) / ps.length : H / 2;
      };
      col.sort((a, b) => bary(a) - bary(b));
    }
    col.forEach((n, i) => {
      pos[n] = {
        x: 28 + (W - nodeW - 56) * (maxD ? d / maxD : 0),
        y: (H * (i + 1)) / (col.length + 1) - nodeH / 2,
      };
    });
  });
  app.layout = { pos, nodeW, nodeH, cols, depthOf: depth };
  return app.layout;
}

function isobars(s, svc, x, y, w, h) {
  const r = svc.risk || 0;
  if (r < 0.12) return "";
  const n = r > 0.66 ? 3 : r > 0.33 ? 2 : 1;
  let out = "";
  for (let i = 1; i <= n; i++) {
    const g = i * (6 + r * 15);
    out += `<rect class="iso" x="${x - g}" y="${y - g}" width="${w + 2 * g}" height="${h + 2 * g}" rx="${14 + g}" stroke-width="${(1.8 - i * 0.3).toFixed(1)}" opacity="${(0.9 - i * 0.2).toFixed(2)}"/>`;
  }
  return out;
}

function edgePath(layout, e, x1, y1, x2, y2) {
  // A long edge threads the gaps between nodes in the columns it crosses, so it never
  // looks as if it starts from a service it merely passes behind.
  const { pos, nodeW, nodeH, cols, depthOf } = layout;
  const pts = [[x1, y1, "c"]];
  const from = depthOf[e.src], to = depthOf[e.dst];
  for (let c = from + 1; c < to; c++) {
    const col = cols[c].map((n) => pos[n]).sort((a, b) => a.y - b.y);
    const want = y1 + ((y2 - y1) * (c - from)) / (to - from);
    const gaps = [col[0].y - 16, ...col.slice(0, -1).map((p, i) => (p.y + nodeH + col[i + 1].y) / 2), col[col.length - 1].y + nodeH + 16];
    const y = gaps.reduce((best, g) => (Math.abs(g - want) < Math.abs(best - want) ? g : best), gaps[0]);
    pts.push([col[0].x - 6, y, "c"], [col[0].x + nodeW + 6, y, "l"]);
  }
  pts.push([x2, y2, "c"]);
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 1; i < pts.length; i++) {
    const [ax, ay] = pts[i - 1], [bx, by, how] = pts[i];
    if (how === "l") { d += ` L${bx},${by}`; continue; }
    const m = (bx - ax) / 2;
    d += ` C${ax + m},${ay} ${bx - m},${by} ${bx},${by}`;
  }
  return d;
}

function renderMap(s) {
  const svg = $("#map");
  if (!svg) return;
  const layout = layoutMap(s);
  const { pos, nodeW, nodeH } = layout;
  const hi = app.hover ? new Set([app.hover, ...(s.services.find((x) => x.name === app.hover)?.blast_radius || [])]) : app.highlight ? new Set(app.highlight) : null;
  let edges = "";
  for (const e of s.edges) {
    const a = pos[e.src], b = pos[e.dst];
    if (!a || !b) continue;
    const x1 = a.x + nodeW, y1 = a.y + nodeH / 2, x2 = b.x, y2 = b.y + nodeH / 2;
    const width = (1 + Math.log1p(e.rate) * 0.75).toFixed(1);
    const bad = e.error_rate >= 0.1;
    const cls = ["edge", e.required ? "" : "optional", bad ? "bad" : "", hi ? (hi.has(e.src) && hi.has(e.dst) ? "lit" : "dim") : ""].join(" ");
    edges += `<path class="${cls}" d="${edgePath(layout, e, x1, y1, x2, y2)}" stroke-width="${width}"><title>${esc(e.src)} calls ${esc(e.dst)}: ${e.rate.toFixed(1)} per second, ${(e.error_rate * 100).toFixed(0)}% failing, ${e.avg_ms.toFixed(0)} ms average</title></path>`;
  }
  let rings = "", nodes = "";
  for (const svc of s.services) {
    const p = pos[svc.name];
    rings += isobars(s, svc, p.x, p.y, nodeW, nodeH);
    const isRoot = s.roots.includes(svc.name) && svc.state !== "healthy";
    const insts = s.instances.filter((v) => v.service === svc.name);
    const pips = insts.map((v, i) => {
      const c = { healthy: "var(--ok)", impacted: "var(--watch)", degraded: "var(--warn)", at_risk: "var(--warn)", draining: "var(--warn)", starting: "var(--ink3)" }[v.state] || "var(--crit)";
      return `<circle class="pip" cx="${p.x + nodeW - 14 - i * 12}" cy="${p.y + 16}" r="5" fill="${c}"><title>${esc(v.id)}: ${esc(STATE_TEXT[v.state])}</title></circle>`;
    }).join("");
    const riskTxt = svc.risk >= 0.12 ? `${Math.round(svc.risk * 100)}% risk` : `${(svc.rps || 0).toFixed(1)} req/s`;
    const dim = hi && !hi.has(svc.name) ? " dim" : "";
    nodes += `<g class="node${isRoot ? " root" : ""}${dim}" data-svc="${esc(svc.name)}" data-state="${esc(svc.state)}" tabindex="0" role="button" aria-label="${esc(svc.label)}: ${esc(STATE_TEXT[svc.state])}, ${riskTxt}">
      <rect class="box" x="${p.x}" y="${p.y}" width="${nodeW}" height="${nodeH}" rx="10"/>
      <text class="name" x="${p.x + 12}" y="${p.y + 22}">${esc(svc.label)}</text>
      <text class="statetext" x="${p.x + 12}" y="${p.y + 41}">${STATE_ICON[svc.state] || ""} ${esc(STATE_TEXT[svc.state] || svc.state)}</text>
      <text class="sub" x="${p.x + 12}" y="${p.y + 57}">${esc(riskTxt)}</text>
      ${pips}
      ${isRoot ? `<text class="rootflag" x="${p.x}" y="${p.y - 7}">Root cause</text>` : ""}
    </g>`;
  }
  const focused = document.activeElement?.closest?.(".node")?.dataset.svc;
  svg.innerHTML = `<g>${edges}</g><g>${rings}</g><g>${nodes}</g>`;
  if (focused) svg.querySelector(`[data-svc="${focused}"]`)?.focus();
}

function renderLive() {
  const s = app.state;
  if (!s) return;
  renderDrawer();
  if (app.route === "forecast") renderForecast();
  if (app.route === "lab") renderLabLive();
  if (app.route !== "live") return;
  const b = $("#briefing");
  if (!b) return;
  b.dataset.level = s.briefing.level;
  $("#briefText").textContent = s.briefing.text;
  const a = s.availability;
  $("#gauges").innerHTML = `
    <div class="gauge"><b>${a.minute.pct == null ? "n/a" : a.minute.pct.toFixed(1) + "%"}</b><span>Success, last minute</span></div>
    <div class="gauge"><b>${s.actions.proactive}</b><span>Replaced early</span></div>
    <div class="gauge"><b>${s.actions.reactive}</b><span>Restarted after failure</span></div>`;
  renderMap(s);
  renderForecastList(s);
  renderDemo(s);
  renderTraffic();
  renderFeed();
}

function riskClass(r) { return r >= 0.6 ? "h" : r >= 0.3 ? "w" : ""; }
function renderForecastList(s) {
  const el = $("#forecastList");
  if (!el) return;
  const rows = [...s.instances].sort((a, b) => (b.risk_ema || 0) - (a.risk_ema || 0)).slice(0, 7);
  el.innerHTML = rows.map((v) => {
    const r = v.risk_ema || 0;
    const when = v.ttf_s && r >= 0.3 ? `, about ${Math.round(v.ttf_s)} s` : "";
    const why = r >= 0.3 && v.factors[0] ? `<span class="fc-why">${esc(v.factors[0].text)}</span>` : "";
    return `<button class="fc" type="button" data-open="${esc(v.service)}">
      <span class="fc-top"><b>${esc(v.id)}</b><span>${Math.round(r * 100)}%${when}</span></span>
      <span class="bar"><i class="${riskClass(r)}" style="width:${Math.max(1, r * 100)}%"></i></span>${why}</button>`;
  }).join("");
}

function renderDemo(s) {
  const box = $("#demoBox");
  if (!box) return;
  const d = s.scenario;
  if (d.state === "idle") {
    if (box.dataset.k !== "idle") {
      box.dataset.k = "idle";
      box.innerHTML = `<section class="demo"><div class="demo-head"><div><h2>See it handle real failures</h2>
        <p class="muted">Runs four real failures one after another and explains what happens. Takes about four minutes.</p></div>
        <button class="primary" type="button" data-demo="start">Start guided demo</button></div></section>`;
    }
    return;
  }
  const running = d.state === "running";
  const key = JSON.stringify([d.state, d.current, d.steps.map((x) => [x.status, x.result])]);
  if (box.dataset.k === key) return;
  box.dataset.k = key;
  const cur = d.steps[d.current];
  const heading = running ? (cur ? cur.title : "Starting") : d.state === "finished" ? "Guided demo finished" : "Guided demo stopped";
  box.innerHTML = `<section class="demo"><div class="demo-head"><div><h2>${esc(heading)}</h2>
      ${running && cur ? `<p>${esc(cur.intro)}</p>` : ""}</div>
      ${running ? `<button type="button" data-demo="stop">Stop demo</button>` : `<button class="primary" type="button" data-demo="start">Run it again</button>`}</div>
    <ol class="steps">${d.steps.map((x) => `<li class="${esc(x.status)}"><b>${esc(x.title)}</b>
      ${x.result ? `<p class="res">${esc(x.result)}</p>` : x.status === "running" ? `<p>In progress</p>` : ""}</li>`).join("")}</ol></section>`;
}

/* ---------------------------------------------------------------- charts */
function renderTraffic() {
  const el = $("#trafficChart");
  if (!el) return;
  const now = Math.floor(Date.now() / 1000) - 2;
  const span = 300;
  const W = 900, H = 150, L = 34, B = 20, T = 6;
  const rows = [];
  let peak = 10;
  for (let t = now - span + 1; t <= now; t++) {
    const b = app.traffic.get(t);
    if (b) { rows.push([t, b]); peak = Math.max(peak, (b.ok || 0) + (b.fail || 0)); }
  }
  peak = Math.ceil(peak / 10) * 10;
  const bw = (W - L) / span;
  const y = (v) => T + (H - T - B) * (1 - v / peak);
  let bars = "";
  rows.forEach(([t, b]) => {
    const x = L + (t - (now - span + 1)) * bw;
    const ok = b.ok || 0, fail = b.fail || 0;
    const w = Math.max(bw - 0.5, 0.8).toFixed(2);
    if (ok) bars += `<rect x="${x.toFixed(1)}" y="${y(ok).toFixed(1)}" width="${w}" height="${(y(0) - y(ok)).toFixed(1)}" fill="var(--s2)"/>`;
    if (fail) bars += `<rect x="${x.toFixed(1)}" y="${y(ok + fail).toFixed(1)}" width="${w}" height="${(y(ok) - y(ok + fail)).toFixed(1)}" fill="var(--crit)"/>`;
  });
  const ticks = [0, peak / 2, peak].map((v) => `<g class="grid"><line x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/></g><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`).join("");
  const xt = [300, 200, 100, 0].map((a) => {
    const x = L + (span - a) * bw - (a === 0 ? bw : 0);
    return `<text x="${x}" y="${H - 4}" text-anchor="${a === 0 ? "end" : a === 300 ? "start" : "middle"}">${a === 0 ? "now" : `${a / 60 >= 1 ? (a / 60).toFixed(a % 60 ? 1 : 0) + " min" : a + " s"} ago`}</text>`;
  }).join("");
  el.innerHTML = rows.length
    ? `<svg class="chart axis" viewBox="0 0 ${W} ${H}" role="img" aria-label="Successful and failed requests per second" data-traffic>${ticks}${xt}${bars}<rect data-hit x="${L}" y="${T}" width="${W - L}" height="${H - T - B}" fill="transparent"/></svg>`
    : `<p class="empty">Traffic will appear here in a few seconds.</p>`;
  const hit = el.querySelector("[data-hit]");
  if (hit) {
    hit.addEventListener("mousemove", (ev) => {
      const r = hit.getBoundingClientRect();
      const t = now - span + 1 + Math.round(((ev.clientX - r.left) / r.width) * (span - 1));
      const b = app.traffic.get(t);
      showTip(ev, b ? `${fmtTime(t)}: ${b.ok} succeeded, ${b.fail} failed${b.p95_ms ? `, p95 ${Math.round(b.p95_ms)} ms` : ""}` : `${fmtTime(t)}: no data`);
    });
    hit.addEventListener("mouseleave", hideTip);
  }
}
function showTip(ev, text) {
  const t = $("#tip");
  t.textContent = text;
  t.hidden = false;
  t.style.left = `${Math.min(ev.clientX + 14, innerWidth - 270)}px`;
  t.style.top = `${ev.clientY + 14}px`;
}
function hideTip() { $("#tip").hidden = true; }

function renderFeed() {
  const el = $("#feed");
  if (!el) return;
  if (!app.notes.length) { el.innerHTML = `<li style="display:block" class="muted">No alerts so far. Alerts appear here the moment something goes wrong.</li>`; return; }
  el.innerHTML = app.notes.slice(0, 6).map((n) => `<li><time>${fmtTime(n.ts)}</time><div><span class="lvl lvl-${esc(n.level)}">${esc(n.title)}</span></div></li>`).join("");
}

function spark(values, color, max) {
  const vals = values.map((v) => (v == null ? null : v));
  const present = vals.filter((v) => v != null);
  if (present.length < 2) return `<svg viewBox="0 0 100 28"></svg>`;
  const hi = max ?? Math.max(...present, 1e-6);
  const step = 100 / Math.max(1, vals.length - 1);
  let d = "", pen = false;
  vals.forEach((v, i) => {
    if (v == null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${(i * step).toFixed(1)},${(26 - (Math.min(v, hi) / hi) * 24).toFixed(1)}`;
    pen = true;
  });
  return `<svg viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

function lineChart(series, opts) {
  // series: [{name, color, values: [[x, y]...]}]; opts: {ymax, yfmt, threshold, height}
  const W = 760, H = opts.height || 220, L = 44, R = 12, T = 10, B = 24;
  const xs = series.flatMap((s) => s.values.map((v) => v[0]));
  if (xs.length < 2) return `<p class="empty">${esc(opts.empty || "Not enough data yet.")}</p>`;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const ymax = opts.ymax ?? Math.max(...series.flatMap((s) => s.values.map((v) => v[1] ?? 0)), 1e-6) * 1.1;
  const ymin = opts.ymin ?? 0;
  const X = (v) => L + ((v - x0) / Math.max(x1 - x0, 1e-6)) * (W - L - R);
  const Y = (v) => T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin));
  const fmt = opts.yfmt || ((v) => v.toFixed(0));
  const ticks = [0, 0.5, 1].map((f) => ymin + (ymax - ymin) * f);
  let g = ticks.map((t) => `<g class="grid"><line x1="${L}" x2="${W - R}" y1="${Y(t)}" y2="${Y(t)}"/></g><text x="${L - 6}" y="${Y(t) + 4}" text-anchor="end">${esc(fmt(t))}</text>`).join("");
  if (opts.xfmt) {
    g += [x0, (x0 + x1) / 2, x1].map((v, i) => `<text x="${X(v)}" y="${H - 6}" text-anchor="${["start", "middle", "end"][i]}">${esc(opts.xfmt(v))}</text>`).join("");
  }
  if (opts.threshold != null) {
    g += `<line x1="${L}" x2="${W - R}" y1="${Y(opts.threshold)}" y2="${Y(opts.threshold)}" stroke="var(--ink2)" stroke-dasharray="5 4"/><text x="${W - R}" y="${Y(opts.threshold) - 5}" text-anchor="end">${esc(opts.thresholdLabel || "")}</text>`;
  }
  const paths = series.map((s) => {
    let d = "", pen = false;
    s.values.forEach(([x, y]) => {
      if (y == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(x).toFixed(1)},${Y(y).toFixed(1)}`;
      pen = true;
    });
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }).join("");
  const legend = series.length > 1 ? `<div class="leg">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
  return `${legend}<svg class="chart axis" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || "Line chart")}">${g}${paths}</svg>`;
}

function hbars(rows, opts = {}) {
  // rows: [{label, parts:[{v,color,name}]}]
  const max = Math.max(1, ...rows.map((r) => r.parts.reduce((a, p) => a + p.v, 0)));
  const body = rows.map((r) => {
    const total = r.parts.reduce((a, p) => a + p.v, 0);
    const segs = r.parts.filter((p) => p.v > 0).map((p) => `<span title="${esc(p.name)}: ${p.v}" style="display:inline-block;height:14px;width:calc(${(p.v / max) * 100}% - 2px);margin-right:2px;background:${p.color};border-radius:2px"></span>`).join("");
    return `<div style="display:grid;grid-template-columns:${opts.labelW || "120px"} minmax(0,1fr) 34px;gap:8px;align-items:center;margin:6px 0"><span class="small">${esc(r.label)}</span><span style="display:flex">${segs}</span><span class="small" style="text-align:right">${total}</span></div>`;
  }).join("");
  return body;
}

/* ---------------------------------------------------------------- drawer */
function openDrawer(service) {
  app.openService = service;
  $("#drawer").hidden = false;
  app.drawerSig = "";
  renderDrawer();
  refreshDrawerHistory();
}
function closeDrawer() { app.openService = null; $("#drawer").hidden = true; app.hover = null; }
async function refreshDrawerHistory() {
  if (!app.openService || !app.state) return;
  const insts = app.state.instances.filter((v) => v.service === app.openService);
  app.drawerHist = app.drawerHist || {};
  await Promise.all(insts.map(async (v) => {
    try { app.drawerHist[v.id] = await api(`/api/history/${encodeURIComponent(v.id)}?seconds=120`); } catch (e) { /* retried below */ }
  }));
  renderDrawer();
}
setInterval(refreshDrawerHistory, 2500);

function renderDrawer() {
  const name = app.openService, s = app.state;
  const el = $("#drawer");
  if (!name || !s || el.hidden) return;
  const svc = s.services.find((x) => x.name === name);
  if (!svc) return;
  const insts = s.instances.filter((v) => v.service === name);
  const hist = app.drawerHist || {};
  const instHtml = insts.map((v) => {
    const h = hist[v.id] || [];
    const metric = (label, val, key, color, max) => `<div><span>${esc(label)}: ${esc(val)}</span>${spark(h.map((r) => r[key]), color, max)}</div>`;
    const reasons = v.factors.length ? `<ul class="reasons">${v.factors.map((f) => `<li>${esc(f.text)}</li>`).join("")}</ul>` : "";
    const predText = (v.risk_ema || 0) >= 0.3 ? `<p><b>${Math.round(v.risk_ema * 100)}% chance of failure${v.ttf_s ? `, in about ${Math.round(v.ttf_s)} s` : ""}.</b> ${v.kind_label ? `Most likely ${esc(v.kind_label.toLowerCase())}.` : ""}</p>${reasons}` : "";
    return `<div class="inst"><div class="inst-head"><b>${esc(v.id)}</b>${stateTag(v.state)}</div>
      <p class="small muted">Up ${Math.round(v.uptime_s)} s. Traffic weight ${v.weight == null ? "n/a" : Math.round(v.weight * 100) + "%"}. Circuit breaker ${esc(v.breaker || "n/a")}.</p>
      ${predText}
      <div class="mini">
        ${metric("Failure risk", pct(v.risk_ema), "risk_ema", "var(--warn)", 1)}
        ${metric("Memory", pct(v.mem_pct), "mem_pct", "var(--s1)", 1)}
        ${metric("CPU", pct(v.cpu), "cpu", "var(--s1)", 1)}
        ${metric("Own p95", v.self_p95_ms == null ? "n/a" : Math.round(v.self_p95_ms) + " ms", "self_p95_ms", "var(--s1)")}
        ${metric("Own errors", pct(v.err_self), "err_self", "var(--crit)", 1)}
        ${metric("Dependency errors", pct(v.err_dep), "err_dep", "var(--watch)", 1)}
        ${metric("Worker slots", pct(v.pool_pct), "pool_pct", "var(--s1)", 1)}
        ${metric("Loop stall", v.lag_ms == null ? "n/a" : Math.round(v.lag_ms) + " ms", "lag_ms", "var(--s1)")}
      </div></div>`;
  }).join("");
  const blast = svc.blast_radius.length ? `If this stops, these stop too: ${svc.blast_radius.map(esc).join(", ")}.` : "Nothing else stops if this stops.";
  const deps = svc.depends_on.length ? `Calls ${svc.depends_on.map(esc).join(", ")}.` : "Calls no other service.";
  const action = svc.quarantined
    ? `<button class="primary" type="button" data-act="rollback" data-svc="${esc(name)}">Roll back release</button>`
    : `<button type="button" data-act="restart" data-svc="${esc(name)}">Restart without downtime</button>`;
  el.innerHTML = `<div class="drawer-head"><div><h2>${esc(svc.label)}</h2>${stateTag(svc.state)}</div>
      <button class="ghost" type="button" data-close-drawer>Close</button></div>
    ${svc.quarantined ? `<div class="callout warn">${esc(svc.quarantined)}</div>` : ""}
    <p class="small">${deps} ${blast}</p>
    <div class="form-row" style="margin-top:0">${action}
      <button type="button" data-lab="${esc(name)}">Test a failure here</button>
      <button type="button" data-ask="Why is ${esc(name)} in this state?">Explain</button></div>
    ${instHtml}`;
}

/* ---------------------------------------------------------------- forecast page */
function mountForecast() {
  $("#view").innerHTML = `<div class="page-head"><h1>Failure forecast</h1>
    <p>The model checks every instance once a second and estimates its chance of failing within the next ${app.state ? app.state.model.horizon_s : 30} seconds.</p></div>
    <div class="split"><div class="panel scroll" id="fcTable"></div><div class="stack"><div class="panel" id="fcDetail"></div></div></div>`;
  renderForecast();
}
function renderForecast() {
  const s = app.state;
  const t = $("#fcTable");
  if (!s || !t) return;
  const rows = [...s.instances].sort((a, b) => (b.risk_ema || 0) - (a.risk_ema || 0));
  if (!app.selected || !s.instances.find((v) => v.id === app.selected)) app.selected = rows[0]?.id;
  t.innerHTML = `<table><thead><tr><th>Instance</th><th>State</th><th class="n">Risk</th><th style="width:110px">Recent risk</th><th class="n">Memory</th><th class="n">Own p95</th></tr></thead><tbody>
    ${rows.map((v) => `<tr class="pick ${v.id === app.selected ? "sel" : ""}" data-select="${esc(v.id)}" tabindex="0"><td><b>${esc(v.id)}</b></td><td>${stateTag(v.state)}</td>
      <td class="n">${Math.round((v.risk_ema || 0) * 100)}%</td><td style="height:30px"><span style="display:block;width:100px;height:26px">${spark(app.hist[v.id] || [], "var(--warn)", 1).replace("<svg ", '<svg style="width:100px;height:26px" ')}</span></td>
      <td class="n">${pct(v.mem_pct)}</td><td class="n">${v.self_p95_ms == null ? "n/a" : Math.round(v.self_p95_ms) + " ms"}</td></tr>`).join("")}
    </tbody></table>`;
  const v = s.instances.find((x) => x.id === app.selected);
  const d = $("#fcDetail");
  if (!v || !d) return;
  const h = app.hist[v.id] || [];
  const th = s.model.threshold;
  d.innerHTML = `<h2>${esc(v.id)}</h2><p class="sub">Failure risk since this page opened. It acts when risk stays over the line for 3 seconds.</p>
    ${lineChart([{ name: "Risk", color: "var(--warn)", values: h.map((r, i) => [i - h.length + 1, r]) }], {
      ymax: 1, yfmt: (x) => `${Math.round(x * 100)}%`, threshold: th, thresholdLabel: `Act at ${Math.round(th * 100)}%`,
      xfmt: (x) => (x >= -0.5 ? "now" : `${Math.abs(Math.round(x))} s ago`), label: `Failure risk for ${v.id}`, empty: "Collecting readings. Check back in a few seconds.",
    })}
    <div class="facts">
      <div><b>${Math.round((v.risk_ema || 0) * 100)}%</b><span>Failure risk</span></div>
      <div><b>${v.ttf_s && (v.risk_ema || 0) >= 0.3 ? `${Math.round(v.ttf_s)} s` : "None"}</b><span>Estimated time left</span></div>
      <div><b>${esc(v.kind_label && (v.risk_ema || 0) >= 0.3 ? v.kind_label : "None")}</b><span>Likely failure type</span></div>
      <div><b>${v.anomalous ? "Unusual" : "Normal"}</b><span>Compared with healthy behaviour</span></div>
    </div>
    ${v.factors.length ? `<h3>Why the model thinks so</h3><ul class="reasons" style="margin-top:6px">${v.factors.map((f) => `<li>${esc(f.text)} <span class="muted small">(adds ${Math.round(f.impact * 100)} points of risk)</span></li>`).join("")}</ul>` : `<p class="muted">No warning signs on this instance right now.</p>`}`;
}

/* ---------------------------------------------------------------- incidents */
async function mountIncidents() {
  $("#view").innerHTML = `<div class="page-head"><h1>Incidents</h1><p>Every failure, near miss and recovery, with what the platform knew and when.</p></div>
    <div class="split"><div class="panel scroll" id="incList"></div><div class="panel" id="incDetail"></div></div>`;
  await refreshIncidents();
}
async function refreshIncidents() {
  if (app.route !== "incidents") return;
  let rows;
  try { rows = await api("/api/incidents?limit=100"); } catch (e) { return; }
  app.incidents = rows;
  const list = $("#incList");
  if (!list) return;
  if (!rows.length) {
    list.innerHTML = `<div class="empty">No incidents yet.<br><button class="primary" type="button" data-demo="start">Start guided demo</button></div>`;
    $("#incDetail").innerHTML = `<p class="empty">An incident's full story will appear here.</p>`;
    return;
  }
  if (!app.incidentId || !rows.find((r) => r.id === app.incidentId)) app.incidentId = rows[0].id;
  list.innerHTML = `<table><thead><tr><th>#</th><th>Service</th><th>Type</th><th>Outcome</th><th class="n">Recovery</th></tr></thead><tbody>
    ${rows.map((i) => `<tr class="pick ${i.id === app.incidentId ? "sel" : ""}" data-incident="${i.id}" tabindex="0"><td>${i.id}</td><td><b>${esc(i.service)}</b><div class="small muted">${fmtTime(i.opened_at)}</div></td><td class="small">${esc(i.kind_label || "Unclassified")}</td><td>${outcomeTag(i)}</td><td class="n">${i.ttr_s != null ? secs(i.ttr_s) : i.outcome === "prevented" ? "No outage" : "n/a"}</td></tr>`).join("")}
    </tbody></table>`;
  renderIncident(rows.find((r) => r.id === app.incidentId));
}
setInterval(refreshIncidents, 3000);
function renderIncident(i) {
  const d = $("#incDetail");
  if (!d || !i) return;
  const failure = i.failed_requests != null ? `${i.failed_requests} of ${i.total_requests}` : "Still counting";
  const warn = i.outcome === "prevented" ? (i.predicted_ttf_s ? `About ${Math.round(i.predicted_ttf_s)} s` : "Yes") : i.lead_s != null ? secs(i.lead_s) : "None";
  d.innerHTML = `<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap"><div><h2>Incident ${i.id}: ${esc(i.service)}</h2><p class="muted">${esc(i.kind_label || "Unclassified")}</p></div>
      <a href="/api/incidents/${i.id}/report" download="incident-${i.id}.md"><button type="button">Download report</button></a></div>
    <div class="facts">
      <div><b>${esc(OUTCOME_TEXT[i.status === "resolved" ? i.outcome : i.status] || i.status)}</b><span>Outcome</span></div>
      <div><b>${esc(warn)}</b><span>Warning before failure</span></div>
      <div><b>${i.ttr_s != null ? secs(i.ttr_s) : "No outage"}</b><span>Time to recover</span></div>
      <div><b>${esc(failure)}</b><span>User requests failed</span></div>
    </div>
    ${i.affected.length ? `<p><b>Spread to:</b> ${i.affected.map(esc).join(", ")}</p>` : ""}
    <ol class="tl">${i.timeline.map((e) => `<li data-k="${esc(e.kind)}"><time>${fmtTime(e.ts)}</time>${esc(e.text)}</li>`).join("")}</ol>
    ${i.factors.length ? `<h3 style="margin-top:16px">Why the model raised the alarm</h3><ul class="reasons" style="margin-top:6px">${i.factors.map((f) => `<li>${esc(f.text)}</li>`).join("")}</ul>` : ""}
    ${i.recommendation ? `<div class="callout"><b>Suggested fix.</b> ${esc(i.recommendation)}</div>` : ""}`;
}

/* ---------------------------------------------------------------- analytics */
async function mountAnalytics() {
  $("#view").innerHTML = `<div class="page-head" style="display:flex;justify-content:space-between;gap:12px;align-items:end;flex-wrap:wrap"><div><h1>Failure analytics</h1><p>Measured on this machine from every request and incident recorded so far.</p></div></div>
    <div id="anaBody"></div>`;
  await refreshAnalytics();
}
async function refreshAnalytics() {
  if (app.route !== "analytics") return;
  let a, b;
  try { [a, b] = await Promise.all([api("/api/analytics"), api("/api/benchmark")]); } catch (e) { return; }
  const el = $("#anaBody");
  if (!el) return;
  const s = a.summary;
  const kpi = (v, l) => `<div class="kpi"><b>${esc(v)}</b><span>${esc(l)}</span></div>`;
  const avail = (a.minutes || []).map((m) => [m.ts, m.ok + m.fail ? (100 * m.ok) / (m.ok + m.fail) : null]);
  el.innerHTML = `<div class="kpis">
      ${kpi(s.availability_pct == null ? "n/a" : s.availability_pct.toFixed(2) + "%", "Requests succeeded")}
      ${kpi(s.incidents, "Incidents")}
      ${kpi(s.prevented, "Failures prevented")}
      ${kpi(s.recovered, "Recovered after failure")}
      ${kpi(s.mttr_s == null ? "n/a" : s.mttr_s.toFixed(1) + " s", "Mean time to recover")}
      ${kpi(s.failed_requests.toLocaleString(), "Failed user requests")}
    </div>
    ${benchHtml(b)}
    <div class="two" style="margin-top:18px">
      <div class="panel"><h2>Availability by minute</h2><p class="sub">Share of user requests that succeeded, last 3 hours</p>
        ${lineChart([{ name: "Availability", color: "var(--s1)", values: avail }], { ymin: Math.min(90, ...avail.map((x) => x[1] ?? 100)), ymax: 100, yfmt: (v) => `${v.toFixed(0)}%`, xfmt: (t) => fmtTime(t).slice(0, 5), label: "Availability by minute", empty: "This fills in after the first full minute of traffic.", height: 200 })}</div>
      <div class="panel"><h2>Incidents by service</h2><p class="sub">Where failures start, and how they ended</p>
        ${a.by_service.length ? `<div class="leg"><span><i style="background:var(--s2)"></i>Prevented</span><span><i style="background:var(--s1)"></i>Recovered</span><span><i style="background:var(--s3)"></i>Other</span></div>` + hbars(a.by_service.map((r) => ({ label: r.service, parts: [{ v: r.prevented, color: "var(--s2)", name: "Prevented" }, { v: r.recovered, color: "var(--s1)", name: "Recovered" }, { v: r.other, color: "var(--s3)", name: "Other" }] }))) : emptyDemo()}</div>
      <div class="panel"><h2>Incidents by failure type</h2>
        ${a.by_kind.length ? hbars(a.by_kind.map((r) => ({ label: r.kind, parts: [{ v: r.incidents, color: "var(--s1)", name: "Incidents" }] })), { labelW: "190px" }) : emptyDemo()}</div>
      <div class="panel"><h2>Were the alarms right?</h2><p class="sub">In this environment we know which failures were injected, so every alarm can be checked.</p>
        <div class="facts"><div><b>${a.alarm_truth.alarms}</b><span>Alarms raised</span></div><div><b>${a.alarm_truth.with_injected_fault}</b><span>A failure really was building</span></div><div><b>${a.alarm_truth.without}</b><span>No known cause</span></div></div></div>
    </div>`;
}
const emptyDemo = () => `<div class="empty">Nothing recorded yet.<br><button class="primary" type="button" data-demo="start">Start guided demo</button></div>`;
setInterval(() => { if (document.activeElement?.tagName !== "SELECT") refreshAnalytics(); }, 8000);

function benchHtml(b) {
  if (!b || !b.modes) return "";
  const m = b.modes;
  const rows = [["observe", "No protection"], ["reactive", "Recover after failure"], ["autonomous", "Predict and recover"]].filter(([k]) => m[k]);
  return `<div class="panel"><h2>Does prediction make a difference?</h2>
    <p class="sub">Measured by giving each setup the same ${b.faults.length} failures, ${b.repeats} time${b.repeats > 1 ? "s" : ""} each, at ${b.rps} requests a second. Recorded ${esc((b.finished_at || "").slice(0, 10))}.</p>
    <div class="scroll"><table><thead><tr><th>Setup</th><th class="n">Requests succeeded</th><th class="n">Failed requests</th><th class="n">Seconds of user-visible errors</th><th class="n">Failures that reached the point of breaking</th></tr></thead>
    <tbody>${rows.map(([k, label]) => `<tr><td><b>${esc(label)}</b></td><td class="n">${m[k].availability_pct.toFixed(2)}%</td><td class="n">${m[k].failed.toLocaleString()}</td><td class="n">${m[k].bad_seconds}</td><td class="n">${m[k].instance_failures} of ${m[k].trials}</td></tr>`).join("")}</tbody></table></div></div>`;
}

/* ---------------------------------------------------------------- model */
async function mountModel() {
  $("#view").innerHTML = `<div class="page-head"><h1>The prediction model</h1><p>How it was trained, and how it did on failures it had never seen.</p></div><div id="modelBody"></div>`;
  let c;
  try { c = await api("/api/model"); } catch (e) { c = null; }
  const el = $("#modelBody");
  if (!el) return;
  if (!c || !c.evaluation) { el.innerHTML = `<div class="panel empty">No model report found. Train one with <code>python -m foresight.ml.train</code>.</div>`; return; }
  const ev = c.evaluation, e = ev.event;
  const kpi = (v, l) => `<div class="kpi"><b>${esc(v)}</b><span>${esc(l)}</span></div>`;
  const LABEL = { memory_leak: "Memory leak", cpu_ramp: "CPU saturation", pool_leak: "Connection pool exhaustion", latency_ramp: "Latency degradation", error_ramp: "Rising error rate" };
  el.innerHTML = `<div class="kpis">
      ${kpi(`${e.caught} of ${e.failures}`, "Unseen failures predicted")}
      ${kpi(e.lead_median_s == null ? "n/a" : `${Math.round(e.lead_median_s)} s`, "Median warning")}
      ${kpi(`${e.false_alarms} of ${e.survivors}`, "False alarms on runs with no failure")}
      ${kpi(ev.window_auc.toFixed(3), "AUC per second")}
      ${kpi(ev.ttf_mae_s == null ? "n/a" : `${ev.ttf_mae_s.toFixed(1)} s`, "Time-to-failure error")}
      ${kpi(pct(ev.type_accuracy), "Failure type accuracy")}
    </div>
    <div class="two">
      <div class="panel"><h2>By failure type</h2><p class="sub">Failures in ${c.n_test_episodes} experiments kept out of training</p>
        <table><thead><tr><th>Failure</th><th class="n">Predicted</th><th class="n">Median warning</th></tr></thead><tbody>
        ${Object.entries(e.per_kind).map(([k, v]) => `<tr><td>${esc(LABEL[k] || k)}</td><td class="n">${v.caught} of ${v.failures}</td><td class="n">${v.lead_median_s == null ? "n/a" : Math.round(v.lead_median_s) + " s"}</td></tr>`).join("")}
        <tr><td>Sudden crash or hang</td><td class="n">${ev.sudden.warned} of ${ev.sudden.failures}</td><td class="n">Not predictable</td></tr>
        </tbody></table>
        ${ev.cross_validation ? `<p class="small muted" style="margin-top:10px">Treat a perfect score with care: the test set is small. Cross-validation across the training experiments caught ${pct(ev.cross_validation.recall)} of failures, with false alarms on ${pct(ev.cross_validation.false_alarm_episode_rate)} of runs without a failure. That is the safer estimate.</p>` : ""}
        <div class="callout">Sudden failures give no warning in the data, so no model can predict them. The platform handles those with fast detection and restart.</div></div>
      ${ev.unseen_type ? `<div class="panel"><h2>A failure it was never taught</h2><p class="sub">Each row retrains the model with that failure type removed, then tests on it. This is the closest estimate of a brand-new problem.</p>
        <table><thead><tr><th>Failure removed from training</th><th class="n">Still predicted</th><th class="n">Median warning</th></tr></thead><tbody>
        ${Object.entries(ev.unseen_type).map(([k, v]) => `<tr><td>${esc(LABEL[k] || k)}</td><td class="n">${v.caught} of ${v.failures}</td><td class="n">${v.lead_median_s == null ? "n/a" : Math.round(v.lead_median_s) + " s"}</td></tr>`).join("")}
        </tbody></table>
        <div class="callout">The model needs examples of a failure type before it can predict it reliably. New failure types need new training data, and fast restart covers anything it misses.</div></div>` : ""}
      <div class="panel"><h2>Choosing the alarm level</h2><p class="sub">Picked at ${Math.round(c.threshold * 100)}% using only training data: the most failures caught while keeping false alarms under 10%.</p>
        ${lineChart([
          { name: "Failures caught", color: "var(--s1)", values: c.sweep.map((r) => [r.threshold, r.recall]) },
          { name: "False alarm rate", color: "var(--s3)", values: c.sweep.map((r) => [r.threshold, r.false_alarm_episode_rate]) },
        ], { ymax: 1, yfmt: (v) => `${Math.round(v * 100)}%`, xfmt: (v) => `Alarm at ${Math.round(v * 100)}%`, label: "Failures caught and false alarms at each alarm level", height: 200 })}</div>
      <div class="panel"><h2>How it was trained</h2>
        <p>The data comes from a chaos campaign on this exact system: real processes, real traffic and ${c.n_episodes} injected experiments, including harmless spikes the model has to learn to ignore.</p>
        <div class="facts"><div><b>${c.n_samples.toLocaleString()}</b><span>Telemetry samples</span></div><div><b>${c.n_train_episodes}</b><span>Training experiments</span></div><div><b>${c.n_test_episodes}</b><span>Held out for testing</span></div><div><b>${esc(c.horizon_s)} s</b><span>Prediction window</span></div></div>
        <p class="small muted">Four models work together: gradient boosting for failure risk, a regressor for time left, a classifier for failure type, and an isolation forest that flags unusual behaviour. Experiments are split whole, so nothing from a test run leaks into training.</p></div>
      <div class="panel"><h2>What the model looks at most</h2><p class="sub">Drop in AUC when one signal is scrambled</p>
        ${hbars(c.top_features.slice(0, 8).map((f) => ({ label: featureName(f.feature), parts: [{ v: Math.max(0, Math.round(f.auc_drop * 10000)) / 10, color: "var(--s1)", name: "AUC drop x 1000" }] })), { labelW: "190px" })}</div>
    </div>`;
}
function featureName(f) {
  const SIG = { mem: "Memory", cpu: "CPU", pool: "Worker slots", lag: "Loop stall", err_self: "Own errors", err_dep: "Dependency errors", self_p95: "Own p95", p95: "Total p95", inflight: "Requests in progress", rps: "Traffic", fds: "Open handles" };
  const ST = { last: "now", mean10: "10 s average", max10: "10 s peak", slope10: "10 s trend", slope30: "30 s trend" };
  const ETA = { mem_eta: "Memory: time to limit", pool_eta: "Worker slots: time to limit", err_eta: "Own errors: time to limit", lat_eta: "Own p95: time to limit", probe_miss10: "Missed health checks", warm: "Time since start" };
  if (ETA[f]) return ETA[f];
  const m = f.match(/^(.*)_(last|mean10|max10|slope10|slope30)$/);
  return m ? `${SIG[m[1]] || m[1]}, ${ST[m[2]]}` : f;
}

/* ---------------------------------------------------------------- failure lab */
const LAB_INFO = {
  memory_leak: "Memory grows until the instance hits its limit and is killed. Predictable.",
  cpu_ramp: "Work blocks the event loop for longer and longer. Predictable.",
  pool_leak: "Worker slots are taken and never returned. Predictable.",
  latency_ramp: "Each request gets a little slower. Predictable.",
  error_ramp: "A growing share of requests fail. Predictable.",
  crash: "The process dies at once. No warning possible.",
  hang: "The process stays alive but stops answering. No warning possible.",
  bad_release: "New instances die during start-up, so restarting can't help. Should escalate to a person.",
  cache_growth: "Memory rises, levels off, then frees itself. Should not raise an alarm.",
  cpu_burst: "A short CPU spike that passes. Should not raise an alarm.",
  latency_blip: "Latency jumps briefly, then recovers. Should not raise an alarm.",
  error_blip: "A few errors for a short while. Should not raise an alarm.",
};
async function mountLab() {
  if (!app.catalog) app.catalog = await api("/api/chaos/catalog");
  const c = app.catalog;
  const group = (title, kinds, checked) => `<div class="lab-group">${esc(title)}</div>${kinds.map((k, i) => `<label class="opt"><input type="radio" name="fault" value="${esc(k)}" ${checked && i === 0 ? "checked" : ""}><span><b>${esc(c.labels[k])}</b><span>${esc(LAB_INFO[k] || "")}</span></span></label>`).join("")}`;
  const services = app.state ? app.state.services : [];
  $("#view").innerHTML = `<div class="page-head"><h1>Failure lab</h1><p>Break the system on purpose and watch it respond. These are real failures in real processes.</p></div>
    <div class="split">
      <div class="panel"><h2>Inject a failure</h2>
        <div class="form-row"><label>Service<select id="labTarget">${services.map((s) => `<option value="${esc(s.name)}" ${app.labTarget === s.name ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select></label>
        <button class="danger" type="button" id="labGo">Inject failure</button></div>
        <div class="lab-pick">${group("Builds up over time", c.progressive, true)}${group("Happens suddenly", c.sudden)}${group("Harmless look-alikes", c.benign)}</div>
      </div>
      <div class="stack">
        <div class="panel"><h2>Compare protection settings</h2><p class="sub">Switch settings, then inject the same failure and compare.</p>
          <p><b>Predict and recover:</b> predicts failures, replaces instances early, and steers traffic with intelligent load balancing.</p>
          <p style="margin-top:6px"><b>Recover after failure:</b> restarts instances only once they fail, with plain round-robin traffic.</p>
          <p style="margin-top:6px"><b>No protection:</b> nothing restarts, and failed instances stay in rotation.</p></div>
        <div class="panel"><h2>Traffic</h2><p class="sub">Synthetic users, in requests per second</p>
          <div class="form-row" style="margin-top:0"><input id="labRps" type="range" min="0" max="60" step="1" value="${app.state ? Math.round(app.state.rps_target) : 18}" aria-label="Requests per second" style="flex:1"><b id="labRpsVal">${app.state ? Math.round(app.state.rps_target) : 18}</b></div></div>
        <div class="panel"><h2>Right now</h2><div id="labLive"></div></div>
        <div class="panel"><h2>Guided demo</h2><p class="sub">Four real failures, narrated. About four minutes.</p><button class="primary" type="button" data-demo="start">Start guided demo</button></div>
      </div>
    </div>`;
  $("#labTarget").addEventListener("change", (e) => { app.labTarget = e.target.value; });
  $("#labGo").addEventListener("click", () => {
    const kind = document.querySelector("input[name=fault]:checked")?.value;
    const target = $("#labTarget").value;
    act("/api/chaos", { target, kind }, `Injected ${c.labels[kind].toLowerCase()} into ${target}`).catch(() => {});
  });
  $("#labRps").addEventListener("input", (e) => { $("#labRpsVal").textContent = e.target.value; });
  $("#labRps").addEventListener("change", (e) => { act("/api/load", { rps: Number(e.target.value) }).catch(() => {}); });
  renderLabLive();
}
function renderLabLive() {
  const el = $("#labLive"), s = app.state;
  if (!el || !s) return;
  const rows = s.instances.filter((v) => v.state !== "healthy" || (v.risk_ema || 0) >= 0.2 || v.faults.length);
  el.innerHTML = `<p style="margin-bottom:8px">${esc(s.briefing.text)}</p>` + (rows.length ? `<table><tbody>${rows.map((v) => `<tr><td><b>${esc(v.id)}</b></td><td>${stateTag(v.state)}</td><td class="n">${Math.round((v.risk_ema || 0) * 100)}% risk</td></tr>`).join("")}</tbody></table>` : "");
}

/* ---------------------------------------------------------------- alerts */
async function mountAlerts() {
  $("#view").innerHTML = `<div class="page-head"><h1>Alerts</h1><p>Everything the platform told people about, as it happened.</p></div>
    <div class="split"><div class="panel"><h2>Send alerts elsewhere</h2>
        <p class="sub">Paste a Slack or Discord webhook address, or any address that accepts JSON. The dashboard always gets alerts.</p>
        <div class="form-row"><label style="flex:1">Webhook address<input id="hookUrl" type="url" placeholder="https://" style="width:100%"></label>
        <button class="primary" type="button" id="hookSave">Save</button><button type="button" id="hookTest">Send test alert</button></div>
        <p class="small muted" id="hookNote" style="margin-top:10px"></p></div>
      <div class="panel"><h2>Alert history</h2><div id="alertList"></div></div></div>`;
  try {
    const st = await api("/api/settings");
    $("#hookUrl").value = st.webhook_url || "";
    $("#hookNote").textContent = st.email.configured ? `Email alerts go to ${st.email.to}.` : "Email is off. Set the FORESIGHT_SMTP_ settings to turn it on; the README explains how.";
    const rows = await api("/api/notifications?limit=100");
    app.notes = rows;
  } catch (e) { /* the list stays empty and the socket will fill it */ }
  $("#hookSave").addEventListener("click", () => {
    api("/api/settings", { method: "PUT", body: { webhook_url: $("#hookUrl").value } }).then(() => toast("info", "Saved")).catch((e) => toast("critical", "Not saved", e.message));
  });
  $("#hookTest").addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: { webhook_url: $("#hookUrl").value } });
      const n = await api("/api/settings/test", { method: "POST", body: {} });
      const res = n.delivery && n.delivery.webhook;
      $("#hookNote").textContent = res ? `Webhook result: ${res}.` : "Test alert shown here. No webhook address is set.";
    } catch (e) { toast("critical", "Test failed", e.message); }
  });
  renderAlerts();
}
function renderAlerts() {
  const el = $("#alertList");
  if (!el) return;
  el.innerHTML = app.notes.length ? `<ul class="feed" style="margin-top:10px">${app.notes.map((n) => `<li><time>${fmtTime(n.ts)}</time><div><span class="lvl lvl-${esc(n.level)}">${esc(n.title)}</span><br><span class="small muted">${esc(n.body)}</span>${n.delivery && n.delivery.webhook ? `<br><span class="small muted">Webhook: ${esc(n.delivery.webhook)}</span>` : ""}</div></li>`).join("")}</ul>` : `<p class="empty">No alerts yet.</p>`;
}

/* ---------------------------------------------------------------- assistant */
function openAssistant(q) {
  $("#assistant").hidden = false;
  renderChips();
  if (q) sendQuestion(q); else $("#asInput").focus();
}
function renderChips() {
  const s = app.state;
  const qs = [];
  if (s && s.briefing.focus) qs.push(`Why is ${s.briefing.focus} in this state?`);
  qs.push("What is at risk?", "What happens if inventory fails?", "What happened last?", "How accurate is the model?", "What should I fix first?");
  $("#asChips").innerHTML = qs.slice(0, 5).map((q) => `<button class="chip" type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("");
}
async function sendQuestion(q) {
  const log = $("#asLog");
  log.insertAdjacentHTML("beforeend", `<div class="msg-q">${esc(q)}</div>`);
  try {
    const a = await api("/api/assistant", { body: { question: q } });
    const acts = a.actions.map((x) => `<button type="button" data-action='${esc(JSON.stringify(x))}'>${esc(x.label)}</button>`).join("");
    log.insertAdjacentHTML("beforeend", `<div class="msg-a">${esc(a.text)}${acts ? `<div class="acts">${acts}</div>` : ""}</div>`);
    if (a.highlight && a.highlight.length) {
      app.highlight = a.highlight;
      clearTimeout(app.hlTimer);
      app.hlTimer = setTimeout(() => { app.highlight = null; }, 8000);
      if (app.state) renderMap(app.state);
    }
  } catch (e) {
    log.insertAdjacentHTML("beforeend", `<div class="msg-a">${esc(e.message)}</div>`);
  }
  log.scrollTop = log.scrollHeight;
  renderChips();
}

/* ---------------------------------------------------------------- global events */
document.addEventListener("click", (ev) => {
  const t = ev.target.closest("[data-open],[data-svc],[data-demo],[data-q],[data-action],[data-close-drawer],[data-act],[data-lab],[data-ask],[data-select],[data-incident]");
  if (!t) return;
  if (t.matches("[data-close-drawer]")) return closeDrawer();
  if (t.dataset.demo === "start") { act("/api/scenario/start", {}, "Guided demo started").then(() => { if (app.route !== "live") location.hash = "#/live"; }).catch(() => {}); return; }
  if (t.dataset.demo === "stop") { act("/api/scenario/stop").catch(() => {}); return; }
  if (t.dataset.q) { sendQuestion(t.dataset.q); return; }
  if (t.dataset.action) { const a = JSON.parse(t.dataset.action); act(a.url, a.body, `Done: ${a.label}`).then(() => { if (a.url.includes("scenario")) location.hash = "#/live"; }).catch(() => {}); return; }
  if (t.dataset.act === "restart") { act(`/api/services/${t.dataset.svc}/restart`, {}, `Restarting ${t.dataset.svc}`).catch(() => {}); return; }
  if (t.dataset.act === "rollback") { act(`/api/services/${t.dataset.svc}/rollback`, {}, `Rolled back ${t.dataset.svc}`).catch(() => {}); return; }
  if (t.dataset.lab) { app.labTarget = t.dataset.lab; closeDrawer(); location.hash = "#/lab"; return; }
  if (t.dataset.ask) { openAssistant(t.dataset.ask); return; }
  if (t.dataset.select) { app.selected = t.dataset.select; renderForecast(); return; }
  if (t.dataset.incident) { app.incidentId = Number(t.dataset.incident); renderIncident((app.incidents || []).find((r) => r.id === app.incidentId)); document.querySelectorAll("#incList tr").forEach((r) => r.classList.toggle("sel", r.dataset.incident === t.dataset.incident)); return; }
  if (t.dataset.open) { openDrawer(t.dataset.open); return; }
  if (t.dataset.svc) { openDrawer(t.dataset.svc); }
});
document.addEventListener("keydown", (ev) => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches?.(".node,tr.pick")) { ev.preventDefault(); ev.target.dispatchEvent(new MouseEvent("click", { bubbles: true })); }
  if (ev.key === "Escape") { if (!$("#drawer").hidden) closeDrawer(); else $("#assistant").hidden = true; }
  if (ev.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) { ev.preventDefault(); openAssistant(); }
});
document.addEventListener("mouseover", (ev) => {
  const n = ev.target.closest?.(".node");
  const name = n ? n.dataset.svc : null;
  if (name !== (app.hover || null)) { app.hover = name; if (app.state && app.route === "live") renderMap(app.state); }
});
window.addEventListener("hashchange", go);
$("#askBtn").addEventListener("click", () => { const a = $("#assistant"); if (a.hidden) openAssistant(); else a.hidden = true; });
$("#askClose").addEventListener("click", () => { $("#assistant").hidden = true; });
$("#asForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const i = $("#asInput");
  if (i.value.trim()) { sendQuestion(i.value.trim()); i.value = ""; }
});
$("#modeSelect").addEventListener("change", (ev) => {
  act("/api/mode", { mode: ev.target.value }, `Protection set to: ${ev.target.selectedOptions[0].textContent}`).catch(() => {});
});
$("#themeBtn").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("foresight-theme", root.dataset.theme); } catch (e) { /* preference just won't persist */ }
});
try { const t = localStorage.getItem("foresight-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* use system theme */ }

api("/api/notifications?limit=50").then((rows) => { app.notes = rows; if (app.route === "live") renderFeed(); }).catch(() => {});
go();
connect();
