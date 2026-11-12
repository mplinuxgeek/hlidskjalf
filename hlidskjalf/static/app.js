/* hlidskjalf frontend - vanilla JS + uPlot (vendored). */
"use strict";

const $ = (id) => document.getElementById(id);
const css = getComputedStyle(document.documentElement);
const V = (n) => css.getPropertyValue(n).trim();
// colours come from the active theme (theme.js); re-read on every theme change
const COL = {};
function readCol() {
  Object.assign(COL, {
    s: [V("--s1"), V("--s2"), V("--s3"), V("--s4"), V("--s5"), V("--s6")],
    cpu: V("--a-cpu"), mem: V("--a-mem"), temp: V("--a-temp"), ups: V("--a-ups"),
    ct: V("--a-ct"), vm: V("--a-vm"), disk: V("--a-disk"), net: V("--a-net"), gpu: V("--a-gpu"),
    muted: V("--muted"), line: V("--text") + "0f", text2: V("--text-2"),
    ok: V("--ok"), warn: V("--warn"), bad: V("--bad"),
  });
}
readCol();

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode / disabled */ } },
};
const TABS = ["cpu", "mem", "gpu", "temps", "net", "disk", "ct", "vm"];
const S = {
  info: null, snap: null, range: store.get("hl-range") || "live", tab: "cpu",
  live: [], seq: 0, hist: {}, histAt: {}, ghist: {}, ghistAt: {}, lastOk: 0, charts: {}, chartTab: {},
};
if (!["live", "1h", "24h", "7d"].includes(S.range)) S.range = "live";
// charts per tab; only the visible tab's charts are redrawn
const TAB_CHARTS = { cpu: ["cpu", "load", "pw"], mem: ["mem"], gpu: ["geng", "gfreq", "gpw", "gcl"], temps: ["temp"], net: ["net"], disk: ["io"], ct: [], vm: [] };
const MAXLIVE = 300;

/* ---------------- formatting ---------------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function fmtB(b, d = 1) {
  if (b == null || isNaN(b)) return "-";
  const u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0; b = +b;
  while (Math.abs(b) >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i === 0 ? 0 : b >= 100 ? 0 : d)} ${u[i]}`;
}
const fmtSize = (b) => fmtB(b, 0);  // drive / volume sizes, IEC like everything else
function fmtRate(bps) {  // bytes/s, IEC (disk I/O); network uses fmtBits
  if (bps == null || isNaN(bps)) return "-";
  const u = ["B/s", "KiB/s", "MiB/s", "GiB/s"]; let i = 0; bps = +bps;
  while (bps >= 1024 && i < u.length - 1) { bps /= 1024; i++; }
  return `${bps.toFixed(bps >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtBits(bps) {  // bytes/s -> bit/s
  if (bps == null) return "-";
  let b = bps * 8; const u = ["b/s", "kb/s", "Mb/s", "Gb/s"]; let i = 0;
  while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
  return `${b.toFixed(b >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtDur(sec, short = false) {
  if (sec == null || sec < 0) return "-";
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return short ? `${d}d ${h}h` : `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${sec}s`;
}
const pctA = (v) => v == null || isNaN(v) ? "-" : `${(+v).toFixed(Math.abs(v) < 10 && v != 0 ? 1 : 0)}%`;  // 1 decimal below 10
const pct = (v, d = 0) => v == null ? "-" : `${(+v).toFixed(d)}%`;
const levelCls = (p, w = 80, b = 90) => p >= b ? "bad" : p >= w ? "warn" : "";
const tempCls = (t, crit) => { const hot = crit ? crit - 10 : 85, warm = crit ? crit - 25 : 70; return t >= hot ? "hot" : t >= warm ? "warm" : ""; };

/* ---------------- sparkline (inline SVG) ---------------- */
// monotone cubic (Fritsch-Carlson) through pts: smooth, never overshoots the data
function monoPath(pts) {
  const n = pts.length;
  if (n === 1) return `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  const d = [], m = new Array(n);
  for (let i = 0; i < n - 1; i++) d.push((pts[i + 1][1] - pts[i][1]) / (pts[i + 1][0] - pts[i][0] || 1e-9));
  m[0] = d[0]; m[n - 1] = d[n - 2];
  for (let i = 1; i < n - 1; i++) m[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (d[i] === 0) { m[i] = m[i + 1] = 0; continue; }
    const a = m[i] / d[i], b = m[i + 1] / d[i], t = a * a + b * b;
    if (t > 9) { const k = 3 / Math.sqrt(t); m[i] = k * a * d[i]; m[i + 1] = k * b * d[i]; }
  }
  let p = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1], dx = (x1 - x0) / 3;
    p += `C${(x0 + dx).toFixed(1)},${(y0 + m[i] * dx).toFixed(1)} ${(x1 - dx).toFixed(1)},${(y1 - m[i + 1] * dx).toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return p;
}
function sparkPath(vals, w, h, max, pad = 2, min = 0) {
  const n = vals.length;
  if (n < 2) return { line: "", area: "" };
  const m = (max ?? Math.max(1e-9, ...vals.filter((v) => v != null))) - min || 1e-9;
  const segs = [[]];
  let first = null, last = null;
  vals.forEach((v, i) => {
    if (v == null) { if (segs[segs.length - 1].length) segs.push([]); return; }
    const x = (i / (n - 1)) * w;
    const y = h - pad - Math.max(0, Math.min(1, (v - min) / m)) * (h - pad * 2);
    segs[segs.length - 1].push([x, y]);
    if (first == null) first = x; last = x;
  });
  const line = segs.filter((g) => g.length).map(monoPath).join("");
  const area = line ? `${line}L${last.toFixed(1)},${h}L${first.toFixed(1)},${h}Z` : "";
  return { line, area };
}
let gradId = 0;
function sparkSVG(vals, color, max, w = 100, h = 26, min = 0) {
  const { line, area } = sparkPath(vals, w, h, max, 2, min);
  const id = "g" + (gradId++ % 100000);
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><path d="${area}" fill="url(#${id})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}
function setSpark(svg, vals, color, max) {
  svg.setAttribute("viewBox", "0 0 100 46");
  svg.innerHTML = sparkSVG(vals, color, max, 100, 46).replace(/^<svg[^>]*>|<\/svg>$/g, "");
}

/* ---------------- shared components ---------------- */
// Stat cell: the one summary-number style used by every tab's strip (and UPS).
// `v` is a formatted string like "4.5 GiB", "15%", "41 °C"; its unit is split off.
function uw(v) {
  const m = /^(-?[\d.,]+)\s?(.*)$/.exec(String(v ?? "-"));
  return m ? `${m[1]}${m[2] ? `<span class="u">${esc(m[2])}</span>` : ""}` : esc(v);
}
function stat(label, v, sub = "", cls = "") {
  return `<div class="stat${cls.includes("lead") ? " lead" : ""}"><span>${esc(label)}</span><b class="${cls.replace("lead", "")}">${uw(v)}</b>${sub ? `<small>${esc(sub)}</small>` : "<small>&nbsp;</small>"}</div>`;
}
function strip(id, cells) { const el = $(id); if (el) el.innerHTML = cells.join(""); }

// Table: one grid component for every list. spec = [{h, w, m, c}] where w is the
// desktop column width, m the phone width (omit = column hidden on phones), c "num"
// for right-aligned tabular numbers or "spk" for the sparkline column.
function tbl(spec, rows, empty) {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const cls = spec.map((x) => `${x.c || ""}${x.m ? "" : " opt"}`);
  const cols = spec.map((x) => x.w).join(" ");
  const colsM = spec.filter((x) => x.m).map((x) => x.m).join(" ");
  const th = `<div class="tr th">${spec.map((x, i) => `<div class="${cls[i]}">${esc(x.h)}</div>`).join("")}</div>`;
  return `<div class="tbl" style="--cols:${cols};--cols-m:${colsM}">${th}${rows.map((r) =>
    r.grp ? `<div class="tr grp">${esc(r.grp)}</div>` :
      `<div class="tr ${r.cls || ""}"${r.title ? ` title="${esc(r.title)}"` : ""}>${r.cells.map((h, i) => `<div class="${cls[i]}">${h}</div>`).join("")}</div>`).join("")}</div>`;
}
const SPK = "var(--spark-w)";
const nm = (dot, name, sub, extra = "") => `<div class="nm">${dot != null ? `<span class="dot ${dot}"></span>` : ""}<div><b>${esc(name)}${extra}</b><small>${esc(sub) || "&nbsp;"}</small></div></div>`;
const meter = (p, cls = "", color) => `<div class="meter"><i class="${cls}" style="width:${Math.max(0, Math.min(100, p || 0)).toFixed(1)}%${color ? `;background:${color}` : ""}"></i></div>`;
const peak = (a) => { const v = a.filter((x) => x != null); return v.length ? Math.max(...v) : null; };

// Series colours: first series is the tab accent, the rest come from the validated
// categorical palette, skipping hues too close to the accent.
function hue(hex) {
  const n = parseInt(hex.replace("#", ""), 16), r = (n >> 16) / 255, g = (n >> 8 & 255) / 255, b = (n & 255) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  if (!d) return 0;
  const h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}
function pal(acc) {
  const ha = hue(acc);
  const rest = COL.s.filter((c) => { const d = Math.abs(hue(c) - ha); return Math.min(d, 360 - d) > 28; });
  return [acc, ...rest, ...COL.s.filter((c) => !rest.includes(c))];
}

/* ---------------- charts (uPlot) ---------------- */
function grad(color) {
  return (u) => {
    const top = u.bbox?.top, h = u.bbox?.height;
    if (!isFinite(top) || !isFinite(h) || h <= 0) return color + "33";
    const g = u.ctx.createLinearGradient(0, top, 0, top + h);
    g.addColorStop(0, color + "55"); g.addColorStop(1, color + "00");
    return g;
  };
}
// axis labels: compact, unit attached; legend values: full precision + unit
function axisFmt(kind) {
  return (u, vals) => vals.map((v) => {
    if (v == null) return "";
    if (kind === "pct") return v + "%";
    if (kind === "temp") return v + "°";
    if (kind === "bits") return fmtBits(v);
    if (kind === "rate") return fmtRate(v);
    if (kind === "w") return v + " W";
    if (kind === "mhz") return v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + " GHz" : v + " MHz";
    return v;
  });
}
function valFmt(kind) {
  return (u, v) => {
    if (v == null) return "-";
    if (kind === "pct") return pctA(v);
    if (kind === "temp") return Math.round(v) + " °C";
    if (kind === "bits") return fmtBits(v);
    if (kind === "rate") return fmtRate(v);
    if (kind === "w") return v.toFixed(1) + " W";
    if (kind === "mhz") return Math.round(v) + " MHz";
    return (+v).toFixed(2);
  };
}
function chartH(el) { const cl = el.classList; return parseInt(V(cl.contains("tall") ? "--chart-tall" : cl.contains("mid") ? "--chart-mid" : "--chart-h")) || 180; }
function lastIdx(u) {
  const d = u.data; if (!d || !d[0]) return null;
  for (let i = d[0].length - 1; i >= 0; i--) if (d.slice(1).some((s) => s[i] != null)) return i;
  return null;
}
// monotone spline: smooth curves without overshooting real values
const SMOOTH = uPlot.paths?.spline ? uPlot.paths.spline() : undefined;
function makeChart(key, el, series, opts = {}) {
  const kind = opts.kind || "num";
  const h = chartH(el);
  el.style.height = "auto";
  const cfg = {
    width: Math.max(200, el.clientWidth), height: h,
    padding: [8, 16, 0, 0],
    cursor: { points: { size: 7, width: 2 }, drag: { x: false, y: false }, y: false },
    legend: { live: true, show: true },
    scales: {
      x: { time: true },
      y: { range: opts.range || ((u, mn, mx) => [0, Math.max(opts.minMax || 1, mx * 1.12)]) },
    },
    axes: [
      { stroke: COL.muted, grid: { show: false }, ticks: { show: false }, font: "11px InterVar, system-ui", size: 24, space: 110,
        values: (u, splits) => splits.map((t) => {
          const d = new Date(t * 1000);
          const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
          if (S.range === "7d") return d.getHours() === 0 && d.getMinutes() === 0 ? d.toLocaleDateString([], { weekday: "short", day: "numeric" }) : d.toLocaleDateString([], { weekday: "short" }) + " " + hm;
          if (S.range === "live") return hm + ":" + String(d.getSeconds()).padStart(2, "0");
          return hm;
        }) },
      { stroke: COL.muted, grid: { stroke: COL.line, width: 1 }, ticks: { show: false }, font: "11px InterVar, system-ui",
        size: 64, values: axisFmt(kind), space: 36 },
    ],
    series: [{ value: (u, v) => v == null ? "-" : new Date(v * 1000).toLocaleString([], { weekday: S.range === "7d" ? "short" : undefined, hour: "2-digit", minute: "2-digit", second: S.range === "live" ? "2-digit" : undefined }) },
      ...series.map((s, i) => ({
        label: s.label, stroke: s.color, width: i === 0 ? 2 : 1.5, fill: i === 0 && s.fill !== false ? grad(s.color) : undefined,
        points: { show: false }, spanGaps: false, value: valFmt(kind), dash: s.dash, paths: SMOOTH,
      }))],
    // without a cursor the legend shows the latest values instead of "-"
    hooks: { setCursor: [(u) => { if (u.cursor.idx == null) { const i = lastIdx(u); if (i != null) u.setLegend({ idx: i }); } }] },
  };
  const u = new uPlot(cfg, [[], ...series.map(() => [])], el);
  u._kind = kind; u._h = h;
  // resize on the next frame, not inside the observer callback (avoids "ResizeObserver loop" errors)
  let rq = 0;
  u._ro = new ResizeObserver(() => {
    if (rq) return;
    rq = requestAnimationFrame(() => {
      rq = 0;
      const w = el.clientWidth;
      if (w && Math.abs(w - u.width) > 2) u.setSize({ width: w, height: u._h });
    });
  });
  u._ro.observe(el);
  u._el = el;
  S.charts[key] = u;
  return u;
}
function setChart(key, data) {
  const u = S.charts[key];
  if (!u) return;
  u.setData(data);
  if (u.cursor.idx == null) { const i = lastIdx(u); if (i != null) u.setLegend({ idx: i }); }
}

/* ---------------- data access by range ---------------- */
function liveCol(fn) { return S.live.map(fn); }
function chartData(kind) {
  const r = S.range;
  if (r === "live") {
    const t = liveCol((s) => s.t);
    switch (kind) {
      case "cpu": return [t, liveCol((s) => s.cpu), liveCol((s) => s.cpu_user), liveCol((s) => s.cpu_sys), liveCol((s) => s.cpu_iow)];
      case "pw": return [t, ...pwKeys().map((k) => liveCol((s) => s.pw?.[k] ?? null))];
      case "geng": return [t, ...gpuEngines().map((e) => liveCol((s) => s.gpu?.e?.[e.key] ?? null))];
      case "gfreq": return [t, liveCol((s) => s.gpu?.f ?? null), liveCol((s) => s.gpu?.rf ?? null)];
      case "gpw": return [t, liveCol((s) => s.pw?.gpu ?? null), liveCol((s) => s.pw?.pkg ?? null)];
      case "gcl": return [t, ...S.gclKeys.map((k) => liveCol((s) => clientBusy(s.gpu?.c?.[k])))];
      case "load": return [t, liveCol((s) => s.load[0]), liveCol((s) => s.load[1]), liveCol((s) => s.load[2])];
      case "mem": return [t, liveCol((s) => s.mem_pct), liveCol((s) => s.mem.swap_total ? 100 * s.mem.swap_used / s.mem.swap_total : null)];
      case "temp": return [t, ...tempSeries().map((x) => liveCol((s) => s.temps[x.id] ?? null))];
      case "net": { const i = netIface(); return [t, liveCol((s) => s.net[i]?.[0] ?? null), liveCol((s) => s.net[i]?.[1] ?? null)]; }
      case "io": return [t, liveCol((s) => s.disk_total[0]), liveCol((s) => s.disk_total[1])];
    }
  }
  if (["pw", "geng", "gfreq", "gpw", "gcl"].includes(kind)) {
    const g = S.ghist[r];
    if (!g || !g.minutes) return null;
    const t = g.t, nul = () => t.map(() => null);
    switch (kind) {
      case "pw": return [t, ...pwKeys().map((k) => g.pw[k] || nul())];
      case "geng": return [t, ...gpuEngines().map((e) => g.e[e.key] || nul())];
      case "gfreq": return [t, g.f, nul()];
      case "gpw": return [t, g.pw.gpu || nul(), g.pw.pkg || nul()];
      case "gcl": return [t, ...S.gclKeys.map((k) => g.c[k] || nul())];
    }
  }
  const h = S.hist[r];
  if (!h) return null;
  const t = h.t;
  switch (kind) {
    case "cpu": return [t, h.cpu, h.cpu_user, h.cpu_sys, h.cpu_iow];
    case "load": return [t, h.load1, h.load5, h.load15];
    case "mem": return [t, h.mem_pct, t.map(() => null)];
    case "temp": return [t, ...tempSeries().map((x) => h.temps[x.id] || t.map(() => null))];
    case "net": { const i = netIface(); const n = h.net[i] || { rx: t.map(() => null), tx: t.map(() => null) }; return [t, n.rx, n.tx]; }
    case "io": return [t, h.disk_r, h.disk_w];
  }
}
function pwKeys() { return ["pkg", "core", "gpu"].filter((k) => S.info?.power?.[k]); }
function gpuEngines() { return S.info?.gpu?.engines || []; }
function clientBusy(c) { if (!c) return null; const v = Object.values(c.e || {}); return v.length ? Math.max(...v) : 0; }
function tempSeries() {
  const ss = (S.info?.sensors || []).filter((s) => s.primary && s.group !== "core");
  return ss.slice(0, 6);
}
function netIface() {
  const nics = S.snap?.storage?.nics || [];
  const d = nics.find((n) => n.default) || nics.find((n) => n.state === "up");
  if (d) return d.name;
  if (S.info?.default_iface) return S.info.default_iface;
  // before the first snapshot: busiest interface in the latest sample
  const net = Object.entries(S.live.at(-1)?.net || {});
  net.sort((a, b) => b[1][0] + b[1][1] - (a[1][0] + a[1][1]));
  return net[0]?.[0];
}

/* ---------------- setup ---------------- */
function setupCharts() {
  const P = (acc) => pal(acc);
  const c = P(COL.cpu);
  makeChart("cpu", $("ch-cpu"), [{ label: "Busy", color: c[0] }, { label: "User", color: c[1] }, { label: "System", color: c[2] }, { label: "I/O wait", color: c[3] }], { kind: "pct", range: [0, 100] });
  makeChart("load", $("ch-load"), [{ label: "1 min", color: c[0] }, { label: "5 min", color: c[1] }, { label: "15 min", color: c[2] }], { kind: "num" });
  const pwLbl = S.info.power || {};
  makeChart("pw", $("ch-pw"), pwKeys().map((k, i) => ({ label: pwLbl[k], color: c[i] })), { kind: "w", minMax: 5 });
  const m = P(COL.mem);
  makeChart("mem", $("ch-mem"), [{ label: "Used", color: m[0] }, { label: "Swap", color: m[1] }], { kind: "pct", range: [0, 100] });
  const g = S.info.gpu || {};
  if (g.available) {
    const gp = P(COL.gpu);
    makeChart("geng", $("ch-geng"), gpuEngines().map((e, i) => ({ label: e.label, color: gp[i % gp.length] })), { kind: "pct", range: [0, 100] });
    makeChart("gfreq", $("ch-gfreq"), [{ label: "Actual", color: gp[0] }, { label: "Requested", color: gp[1], dash: [4, 4] }], {
      kind: "mhz", range: (u, mn, mx) => [0, Math.max(g.freq_max || 0, mx) * 1.05 || 100] });
    makeChart("gpw", $("ch-gpw"), [{ label: "iGPU", color: gp[0] }, { label: "Package", color: gp[1] }], { kind: "w", minMax: 5 });
  }
  const t = P(COL.temp);
  makeChart("temp", $("ch-temp"), tempSeries().map((x, i) => ({ label: x.group === "nvme" ? x.label.split(" ")[0] : x.label, color: t[i], fill: false })), {
    kind: "temp", range: (u, mn, mx) => [Math.max(0, Math.floor((mn - 5) / 10) * 10), Math.ceil((mx + 5) / 10) * 10],
  });
  const n = P(COL.net);
  makeChart("net", $("ch-net"), [{ label: "In", color: n[0] }, { label: "Out", color: n[1] }], { kind: "bits", minMax: 1000 });
  const d = P(COL.disk);
  makeChart("io", $("ch-io"), [{ label: "Read", color: d[0] }, { label: "Write", color: d[1] }], { kind: "rate", minMax: 1000 });
}
// GPU client chart: series follow the busiest clients, rebuilt when the set changes
S.gclKeys = [];
function syncClientChart() {
  if (!S.info.gpu?.available) return;
  let keys;
  if (S.range === "live") {
    const tot = {};
    for (const x of S.live) for (const [k, c] of Object.entries(x.gpu?.c || {})) tot[k] = (tot[k] || 0) + clientBusy(c);
    keys = Object.entries(tot).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k]) => k);
  } else {
    keys = Object.keys(S.ghist[S.range]?.c || {});
  }
  if (S.charts.gcl && keys.join("|") === S.gclKeys.join("|")) return;
  S.gclKeys = keys;
  S.charts.gcl?._ro.disconnect();
  S.charts.gcl?.destroy();
  $("ch-gcl").innerHTML = "";
  const gp = pal(COL.gpu);
  makeChart("gcl", $("ch-gcl"), keys.map((k, i) => ({ label: k, color: gp[i % gp.length], fill: false })), { kind: "pct", range: (u, mn, mx) => [0, Math.min(100, Math.max(10, mx * 1.15))] });
}
// theme change: uPlot bakes colours in, so rebuild every chart with the new ones
let themeTimer = 0;
function retheme() {
  readCol();
  for (const [k, u] of Object.entries(S.charts)) {
    u._ro.disconnect(); u.destroy();
    u._el.innerHTML = ""; u._el.style.height = "";
    delete S.charts[k];
  }
  S.gclKeys = [];
  setupCharts(); redrawCharts(); renderTab();
}
window.addEventListener("hl-theme", () => {
  if (!S.info) return;   // not initialised yet: init() reads the colours itself
  clearTimeout(themeTimer); themeTimer = setTimeout(retheme, 80);
});
function redrawCharts() {
  if (S.tab === "gpu") syncClientChart();
  for (const k of TAB_CHARTS[S.tab] || []) {
    if (!S.charts[k]) continue;
    const d = chartData(k);
    const el = S.charts[k]._el;
    if (!d) {
      setChart(k, [[], ...S.charts[k].series.slice(1).map(() => [])]);
      const gk = ["pw", "geng", "gfreq", "gpw", "gcl"].includes(k);
      showNoData(el, S.range === "live" ? "Collecting..." : gk && S.ghist[S.range] ? "No GPU or power history yet (kept in-process as 1-minute averages)" : "Loading history...");
      continue;
    }
    showNoData(el, null);
    setChart(k, d);
  }
}
function showNoData(el, msg) {
  if (!el) return;
  let n = el.querySelector(".nodata");
  if (!msg) { n?.remove(); return; }
  if (!n) { n = document.createElement("div"); n.className = "nodata"; el.appendChild(n); }
  n.textContent = msg;
}

function renderHosts() {
  const nav = $("hosts");
  const me = S.info.hostname.split(".")[0];
  nav.innerHTML = (S.info.hosts || []).map((h) => {
    const cur = h.name === me || location.origin === h.url;
    let ok = false;
    try { ok = ["http:", "https:"].includes(new URL(h.url, location.href).protocol); } catch { /* bad URL */ }
    return ok ? `<a href="${esc(h.url)}" class="${cur ? "cur" : ""}">${esc(h.name)}</a>` : `<span class="${cur ? "cur" : ""}">${esc(h.name)}</span>`;
  }).join("");
}

function renderIdent() {
  const i = S.info;
  document.title = `${i.hostname.split(".")[0]} · hlidskjalf`;
  $("hostname").textContent = i.hostname.split(".")[0];
  $("os-badge").textContent = i.os;
  $("cpu-model").textContent = `${i.cpu.model} · ${i.cpu.cores}C/${i.cpu.threads}T`;
  renderFacts();
  // core tiles
  const groups = {};
  const tempByCore = {};
  for (const s of i.sensors) if (s.group === "core") { const m = /Core (\d+)/.exec(s.label); if (m) tempByCore[m[1]] = s; }
  for (const c of i.cpu.cpus) {
    const g = c.type || "";
    (groups[g] ||= {});
    (groups[g][c.pkg + ":" + c.core] ||= { core: c.core, cpus: [], max: c.max_mhz }).cpus.push(c.cpu);
  }
  const names = { P: "Performance cores", E: "Efficiency cores", "": "Cores" };
  $("cores").innerHTML = Object.keys(groups).sort().reverse().map((g) => {
    const cores = Object.values(groups[g]);
    const thr = cores.reduce((a, c) => a + c.cpus.length, 0);
    return `<div class="core-group"><h4>${names[g]} <em>${cores.length} cores · ${thr} threads · <span id="freq-${esc(g || "x")}">-</span></em></h4><div class="core-grid">` +
      cores.map((c) => {
        const ts = tempByCore[c.core];
        return `<div class="core" title="core ${c.core} · cpu ${c.cpus.join(", ")}"><div class="bars">${c.cpus.map((n) => `<div class="bar"><i id="cb-${n}"></i></div>`).join("")}</div><span class="t" ${ts ? `id="ct-${esc(ts.id)}"` : ""}>${ts ? "-" : "&nbsp;"}</span></div>`;
      }).join("") + `</div></div>`;
  }).join("");
}

function renderFacts() {
  const i = S.info;
  const up = S.snap?.uptime ?? i.uptime;
  $("facts").innerHTML = [
    ["Machine", i.machine || "-"],
    ["CPU", i.cpu?.model ? `${i.cpu.model} · ${i.cpu.cores}C/${i.cpu.threads}T` : "-"],
    // installed size as on the DIMM label; older servers without it fall back to the kernel-usable total
    ["RAM", i.ram_gib ? `${i.ram_gib} GB` : fmtB(S.live.at(-1)?.mem?.total)],
    ["Kernel", i.kernel], ["Uptime", fmtDur(up)],
    ["Booted", new Date((Date.now() / 1000 - up) * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
}

/* ---------------- live tick ---------------- */
function onLive() {
  const s = S.live.at(-1);
  if (!s) return;
  if (!S.factsRam) { S.factsRam = true; renderFacts(); }   // RAM total arrives with the first sample
  renderTiles(s);
  renderTab();
  if (S.range === "live") redrawCharts();
}

function renderTiles(s) {
  const i = S.info, last = S.live.slice(-90);
  $("t-cpu").innerHTML = uw(Math.round(s.cpu) + "%");
  $("t-cpu-sub").textContent = `load ${s.load.map((x) => x.toFixed(2)).join(" · ")}`;
  setSpark($("s-cpu"), last.map((x) => x.cpu), COL.cpu, 100);
  $("t-mem").innerHTML = uw(fmtB(s.mem.used));
  $("t-mem-sub").textContent = `${pct(s.mem_pct)} of ${fmtB(s.mem.total, 0)}`;
  setSpark($("s-mem"), last.map((x) => x.mem_pct), COL.mem, 100);
  const pk = i.sensors.find((x) => x.group === "cpu");
  if (pk && s.temps[pk.id] != null) {
    $("t-temp").innerHTML = uw(Math.round(s.temps[pk.id]) + " °C");
    const hottest = Math.max(...i.sensors.filter((x) => x.group === "core").map((x) => s.temps[x.id] ?? 0));
    $("t-temp-sub").textContent = hottest > 0 ? `hottest core ${Math.round(hottest)} °C` : pk.label;
    setSpark($("s-temp"), last.map((x) => x.temps[pk.id]), COL.temp, Math.max(...last.map((x) => x.temps[pk.id] || 0)) * 1.15);
  } else { $("t-temp").textContent = "n/a"; }
  const ni = netIface();
  const nr = s.net[ni] || [0, 0];
  $("t-net").innerHTML = uw(fmtBits(nr[0]));
  $("t-net-sub").textContent = `in · ${fmtBits(nr[1])} out${ni ? " · " + ni : ""}`;
  setSpark($("s-net"), last.map((x) => (x.net[ni]?.[0] ?? 0)), COL.net);
  // GPU tile: hidden on hosts without a GPU
  const gi = i.gpu || {};
  const tile = $("tile-gpu");
  if (tile.hidden === !!gi.available) { tile.hidden = !gi.available; layoutTiles(); }
  if (gi.available) {
    const g = s.gpu || {};
    const busy = gpuBusy(s);
    $("t-gpu-name").textContent = (gi.name || "").replace(/ Graphics$/, "");
    $("t-gpu").innerHTML = uw(Math.round(busy) + "%");
    const top = Object.entries(g.c || {}).sort((a, b) => clientBusy(b[1]) - clientBusy(a[1]))[0];
    $("t-gpu-sub").textContent = [g.f != null ? `${g.f} MHz` : "", s.pw?.gpu != null ? s.pw.gpu.toFixed(1) + " W" : "", top && clientBusy(top[1]) > 0 ? top[0] : ""].filter(Boolean).join(" · ");
    setSpark($("s-gpu"), last.map((x) => gpuBusy(x)), COL.gpu, 100);
  }
}
function layoutTiles() {
  const vis = [...document.querySelectorAll("#tiles .tile")].filter((t) => !t.hidden).length;
  $("tiles").style.setProperty("--n", vis);
  $("tiles").classList.toggle("odd", vis % 2 === 1);
}
// overall GPU busy = busiest engine (what intel_gpu_top calls the GPU's load)
function gpuBusy(s) { const e = Object.values(s?.gpu?.e || {}); return e.length ? Math.max(...e) : 0; }

function renderTab() {
  const s = S.live.at(-1);
  if (!s) return;
  ({ cpu: renderCPU, mem: renderMem, gpu: renderGPU, temps: renderTemps, net: renderNet, disk: renderStorage, ct: renderContainers, vm: renderVMs })[S.tab]?.(s);
}

/* ---------------- CPU ---------------- */
function renderCPU(s) {
  const i = S.info;
  const fr = s.freq.filter((x) => x);
  const avg = fr.length ? fr.reduce((a, b) => a + b, 0) / fr.length : null;
  strip("st-cpu", [
    stat("Busy", pctA(s.cpu), `${i.cpu.threads} threads`, "lead " + levelCls(s.cpu)),
    stat("User", pctA(s.cpu_user)), stat("System", pctA(s.cpu_sys)), stat("I/O wait", pctA(s.cpu_iow)),
    stat("Load", s.load[0].toFixed(2), `5m ${s.load[1].toFixed(2)} · 15m ${s.load[2].toFixed(2)}`),
    stat("Clock", avg ? (avg / 1000).toFixed(2) + " GHz" : "-", fr.length ? `peak ${(Math.max(...fr) / 1000).toFixed(2)} GHz` : ""),
    stat("Processes", s.procs ?? "-", s.ctxt != null ? `${s.ctxt.toLocaleString()} ctx/s` : ""),
    ...(s.pw?.pkg != null ? [stat("Package power", s.pw.pkg.toFixed(1) + " W")] : []),
  ]);
  i.cpu.cpus.forEach((c, idx) => {
    const el = $("cb-" + c.cpu); if (!el) return;
    const v = s.cpus[idx] ?? 0;
    el.style.height = v + "%"; el.classList.toggle("hot", v > 90);
  });
  for (const g of ["P", "E", "x"]) {
    const el = $("freq-" + g); if (!el) continue;
    const fs = i.cpu.cpus.map((c, idx) => (c.type || "x") === g ? s.freq[idx] : null).filter((x) => x);
    el.textContent = fs.length ? `${(Math.max(...fs) / 1000).toFixed(2)} GHz peak` : "";
  }
  for (const t of i.sensors.filter((x) => x.group === "core")) {
    const el = $("ct-" + t.id); if (!el) continue;
    const v = s.temps[t.id];
    el.textContent = v == null ? "-" : Math.round(v) + "°";
    el.className = "t " + (v != null ? tempCls(v, t.crit) : "");
  }
  $("load-sub").textContent = `${pctA(100 * s.load[0] / i.cpu.threads)} of ${i.cpu.threads} threads`;
}

/* ---------------- memory ---------------- */
function renderMem(s) {
  const m = s.mem;
  const appUsed = Math.max(0, m.used - m.shared);
  const cache = Math.min(m.cached, m.total - m.used - m.free);
  const mp = pal(COL.mem);
  const ct = ctMemRows(s);
  const ctTotal = ct.reduce((a, [, v]) => a + v[1], 0);
  strip("st-mem", [
    stat("Used", fmtB(m.used), `${pctA(s.mem_pct)} of ${fmtB(m.total, 0)}`, "lead " + levelCls(s.mem_pct)),
    stat("Available", fmtB(m.avail)), stat("Apps", fmtB(appUsed)), stat("Cache", fmtB(cache)),
    stat("Shared", fmtB(m.shared)), stat("Buffers", fmtB(m.buffers)),
    stat("Swap", m.swap_total ? fmtB(m.swap_used) : "none", m.swap_total ? `of ${fmtB(m.swap_total, 0)}` : ""),
    stat("Containers", fmtB(ctTotal), `${ct.length} running`),
  ]);
  $("mem-total").textContent = `${fmtB(m.total, 0)} total`;
  const parts = [["Apps", appUsed, mp[0]], ["Shared", m.shared, mp[2]], ["Cache", cache, mp[1]], ["Free", m.free, "rgba(255,255,255,.08)"]];
  $("mem-bar").innerHTML = parts.map(([, v, c]) => `<i style="width:${(100 * v / m.total).toFixed(2)}%;background:${c}"></i>`).join("");
  $("mem-legend").innerHTML = parts.map(([n, v, c]) => `<span style="--c:${c}">${n}<b>${fmtB(v)}</b></span>`).join("");
  // containers by memory
  $("ctmem-sub").textContent = ct.length ? `${pctA(100 * ctTotal / m.total)} of RAM` : "";
  const max = ct[0]?.[1][1] || 1;
  $("ctmem").innerHTML = tbl([{ h: "Container", w: "minmax(0,1fr)", m: "minmax(0,1fr)" }, { h: "Share", w: "minmax(60px,1fr)", m: "80px" }, { h: "Memory", w: "76px", m: "72px", c: "num" }],
    ct.map(([, v, name]) => ({ cells: [`<b>${esc(name)}</b>`, meter(100 * v[1] / max), fmtB(v[1])] })), "No running containers.");
}
function ctMemRows(s) {
  if (!S.snap) return [];
  const names = Object.fromEntries((S.snap.containers.containers || []).map((c) => [c.id, c.name]));
  return Object.entries(s.ct || {}).filter(([id, v]) => names[id] && v[1] != null).map(([id, v]) => [id, v, names[id]]).sort((a, b) => b[1][1] - a[1][1]);
}

/* ---------------- GPU ---------------- */
const CLS_LABEL = { rcs: "Render", ccs: "Compute", vcs: "Video", vecs: "VideoEnh", bcs: "Blitter" };
function renderGPU(s) {
  const gi = S.info.gpu || {};
  if (!gi.available) {
    strip("st-gpu", [stat("GPU", "none", gi.error || "no supported GPU found")]);
    $("gpu-ov").innerHTML = `<div class="empty">No GPU detected${gi.error ? ": " + esc(gi.error) : ""}.</div>`;
    $("gpu-clients").innerHTML = "";
    for (const id of ["ch-geng", "ch-gfreq", "ch-gpw", "ch-gcl"]) showNoData($(id), "No GPU");
    return;
  }
  const g = s.gpu || {};
  const e = g.e || {};
  const engines = gpuEngines();
  const gp = pal(COL.gpu);
  const busy = gpuBusy(s);
  const busiest = engines.reduce((a, x) => (e[x.key] ?? -1) > (e[a?.key] ?? -1) ? x : a, null);
  const byCls = (cls) => peak(engines.filter((x) => x.cls === cls).map((x) => e[x.key]));
  const owners = Object.entries(g.c || {});
  strip("st-gpu", [
    stat("Busy", pctA(busy), busiest ? busiest.label : "", "lead " + levelCls(busy)),
    ...["rcs", "vcs", "vecs", "ccs"].filter((c) => engines.some((x) => x.cls === c)).map((c) => stat(CLS_LABEL[c], pctA(byCls(c) ?? 0))),
    stat("Frequency", g.f != null ? g.f + " MHz" : "-", gi.freq_max ? `of ${gi.freq_max} MHz` : ""),
    stat("RC6 idle", g.rc6 != null ? pctA(g.rc6) : "-", "power-gated"),
    stat("iGPU power", s.pw?.gpu != null ? s.pw.gpu.toFixed(1) + " W" : "-", s.pw?.pkg != null ? `package ${s.pw.pkg.toFixed(1)} W` : ""),
    stat("Clients", owners.reduce((a, [, c]) => a + c.n, 0), `${owners.length} owners`),
  ]);
  $("gpu-sub").textContent = `${gi.name} · ${gi.driver} · ${gi.pci.replace(/^0000:/, "")}`;
  $("gpu-src").textContent = gi.pmu ? "i915 PMU" : "DRM fdinfo (sum of clients)";
  $("gpu-freq-sub").textContent = gi.freq_max ? `max ${gi.freq_max} MHz${gi.freq_limit && gi.freq_limit !== gi.freq_max ? ` · limit ${gi.freq_limit}` : ""}` : "";
  $("gpu-ov").innerHTML = `<div class="mrows">${engines.map((x, i) => { const v = e[x.key]; return `<div class="mrow"><span><i class="sw" style="background:${gp[i % gp.length]}"></i>${esc(x.label)}</span>${meter(v, "", gp[i % gp.length])}<b>${v != null ? pctA(v) : "-"}</b></div>`; }).join("")}
    ${gi.freq_max ? `<div class="mrow sep"><span>Frequency</span>${meter(100 * (g.f || 0) / gi.freq_max)}<b>${g.f ?? "-"} MHz</b></div>` : ""}
    ${g.rc6 != null ? `<div class="mrow"><span>RC6 idle</span>${meter(g.rc6, "", COL.muted)}<b>${pctA(g.rc6)}</b></div>` : ""}</div>`;
  // clients by engine class
  const classes = [...new Set(engines.map((x) => x.cls || x.key.replace(/\d+$/, "")))];
  const hist = S.live.slice(-60);
  const rows = owners.sort((a, b) => clientBusy(b[1]) - clientBusy(a[1]));
  $("gpu-cl-sub").textContent = "per container / process, from DRM fdinfo";
  $("gpu-clients").innerHTML = tbl([
    { h: "Owner", w: "minmax(160px,1.4fr)", m: "minmax(0,1fr)" },
    ...classes.map((c) => ({ h: CLS_LABEL[c] || c, w: "88px", m: "56px", c: "num" })),
    { h: "Busiest, 2 min", w: SPK, c: "spk" },
  ], rows.map(([name, c]) => {
    const h = hist.map((x) => clientBusy(x.gpu?.c?.[name]));
    return { cells: [nm(null, name, `${c.k === "container" ? "container" : c.k} · ${c.n} client${c.n === 1 ? "" : "s"}`),
      ...classes.map((cl) => c.e[cl] != null ? pctA(c.e[cl]) : "-"),
      sparkSVG(h, COL.gpu, Math.max(5, ...h.filter((v) => v != null)))] };
  }), "No processes have the GPU open.");
}

/* ---------------- temps & UPS ---------------- */
const GROUP_LBL = { cpu: "CPU package", nvme: "Hottest NVMe", dimm: "Hottest DIMM", ram: "Hottest DIMM", board: "Board", acpi: "ACPI", pch: "Chipset", gpu: "GPU", wifi: "Wi-Fi" };
function renderTemps(s) {
  const i = S.info;
  // 0 °C readings are unwired/unused motherboard inputs, not real sensors
  const sens = i.sensors.filter((x) => s.temps[x.id] != null && s.temps[x.id] > 0);
  const groups = {};
  for (const x of sens) if (x.group !== "core") (groups[x.group] ||= []).push(x);
  const hotCore = peak(sens.filter((x) => x.group === "core").map((x) => s.temps[x.id]));
  const gcell = (grp) => { const l = groups[grp]; const top = l.reduce((a, x) => s.temps[x.id] > s.temps[a.id] ? x : a); const v = s.temps[top.id];
    return stat(GROUP_LBL[grp] || grp[0].toUpperCase() + grp.slice(1), Math.round(v) + " °C", l.length > 1 ? top.label : grp === "cpu" ? "" : top.label, (grp === "cpu" ? "lead " : "") + tempCls(v, top.crit)); };
  const u = S.snap?.ups;
  const fans = (i.fans || []).filter((f) => s.fans[f.id] != null && s.fans[f.id] > 0);  // 0 rpm = empty header
  strip("st-temps", [
    ...(groups.cpu ? [gcell("cpu")] : []),
    ...(hotCore != null ? [stat("Hottest core", Math.round(hotCore) + " °C", "", tempCls(hotCore))] : []),
    ...Object.keys(groups).filter((g) => g !== "cpu").map(gcell),
    stat("Fans", fans.length ? Math.max(...fans.map((f) => s.fans[f.id])) + " rpm" : "none", fans.length ? `${fans.length} reported` : "not reported"),
    ...(u?.available ? [stat("UPS battery", Math.round(u.charge ?? 0) + "%", u.status_text), stat("UPS runtime", fmtDur(u.runtime, true), u.watts != null ? `${u.watts} W load` : "")] : []),
  ]);
  const pk = i.sensors.find((x) => x.group === "cpu");
  $("temp-sub").textContent = pk && s.temps[pk.id] != null ? `CPU ${Math.round(s.temps[pk.id])} °C` : "";
  const chartIds = tempSeries().map((x) => x.id);
  const tp = pal(COL.temp);
  // secondary sensors of one device ("nvme1 ... (sensor 2)") fold into their device's row
  const all = sens.filter((x) => x.group !== "core");
  const extra = {};
  const list = all.filter((x) => { const m = / \(sensor \d+\)$/.exec(x.label); const base = m && all.find((y) => y.label === x.label.slice(0, m.index));
    if (base) { (extra[base.id] ||= []).push(x); return false; } return true; });
  $("sens-sub").textContent = `${all.length} sensors${fans.length ? ` · ${fans.length} fans` : ""}`;
  const rows = list.map((x) => {
    const v = s.temps[x.id], ci = chartIds.indexOf(x.id);
    const more = (extra[x.id] || []).map((y) => Math.round(s.temps[y.id]) + "°").join(" · ");
    return { title: x.lm, cells: [`<span class="dim">${ci >= 0 ? `<i class="sw" style="background:${tp[ci]}"></i>` : `<i class="sw"></i>`}${esc(x.label)}${more ? ` <small>· ${more}</small>` : ""}</span>`, meter(100 * v / (x.crit || 100), tempCls(v, x.crit) === "hot" ? "bad" : tempCls(v, x.crit) === "warm" ? "warn" : ""), `<b>${Math.round(v)} °C</b>`] };
  });
  if (fans.length) rows.push({ grp: "Fans" }, ...fans.map((f) => ({ cells: [`<span class="dim">${esc(f.label)}</span>`, "", `<b>${s.fans[f.id]} rpm</b>`] })));
  // innerHTML swap makes a new .tbl, so keep the scroll position, and skip the
  // swap entirely while the user is dragging its scrollbar (it would drop the grab)
  const sEl = $("sensors"), old = sEl.querySelector(".tbl");
  if (!S.sensDrag) {
    const top = old ? old.scrollTop : 0;
    sEl.innerHTML = tbl([{ h: "Sensor", w: "minmax(0,1fr)", m: "minmax(0,1fr)" }, { h: "Level", w: "72px", m: "72px" }, { h: "Now", w: "64px", m: "64px", c: "num" }], rows, "No temperature sensors.");
    const nt = sEl.querySelector(".tbl"); if (nt) nt.scrollTop = top;
  }
  if (!sEl.dataset.dragWired) {
    sEl.dataset.dragWired = "1";
    sEl.addEventListener("pointerdown", () => { S.sensDrag = true; });
    addEventListener("pointerup", () => { S.sensDrag = false; });
    addEventListener("pointercancel", () => { S.sensDrag = false; });
  }
  renderUPS();
}
function renderUPS() {
  const u = S.snap?.ups;
  const el = $("ups-body");
  if (!u) return;
  if (!u.available) {
    $("ups-model").textContent = "";
    el.innerHTML = `<div class="empty">No UPS data: ${esc(u.error)}</div>`; return;
  }
  const cls = u.low_battery ? "st-bad" : u.on_battery ? "st-warn" : "st-ok";
  $("ups-model").textContent = `${u.model} · ${u.name}@${u.server}`;
  const C = 2 * Math.PI * 50, c = (u.charge ?? 0) / 100;
  const color = u.low_battery ? COL.bad : u.on_battery ? COL.warn : COL.ups;
  if (!el.querySelector(".ring")) {
    el.innerHTML = `<div class="ring"><svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="50" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="10"/><circle id="ups-arc" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round" style="transition:stroke-dashoffset .8s"/></svg><div class="lbl"><div><b id="ups-charge"></b><span>battery</span></div></div></div>
      <div><div id="ups-pill"></div><div class="ups-stats" id="ups-stats" style="margin-top:var(--sp3)"></div></div>
      <div class="ups-chart"><div id="ups-spark"></div><div class="cap" id="ups-cap"></div></div>`;
  }
  const arc = $("ups-arc");
  arc.setAttribute("stroke", color); arc.setAttribute("stroke-dasharray", C); arc.setAttribute("stroke-dashoffset", C * (1 - c));
  $("ups-charge").textContent = Math.round(u.charge ?? 0) + "%";
  $("ups-pill").innerHTML = `<span class="status-pill ${cls}">${esc(u.status_text || u.status)}</span>`;
  $("ups-stats").innerHTML = [
    stat("Runtime", fmtDur(u.runtime, true)), stat("Load", (u.load ?? "-") + "%"), stat("Power", (u.watts ?? "-") + " W", u.nominal_watts ? `of ${u.nominal_watts} W` : ""),
    stat("Input", (u.input_voltage ?? "-") + " V"), stat("Output", (u.output_voltage ?? "-") + " V"), stat("Battery", (u.battery_voltage ?? "-") + " V"),
  ].join("");
  const h = u.hist || [];
  $("ups-spark").innerHTML = sparkSVG(h.map((x) => x[2]), COL.ups, Math.max(20, ...h.map((x) => x[2] || 0)) * 1.2, 300, 72);
  $("ups-cap").innerHTML = `<span>Load, last ${h.length ? fmtDur(Date.now() / 1000 - h[0][0], true) : "hour"}</span><span>now ${u.load ?? "-"}%</span>`;
}
function renderUPSTile() {
  const u = S.snap.ups;
  if (!u.available) { $("t-ups").textContent = "n/a"; $("t-ups-sub").textContent = u.error || ""; return; }
  $("t-ups").innerHTML = uw(Math.round(u.charge ?? 0) + "%");
  $("t-ups-sub").textContent = `${u.status_text} · ${u.watts != null ? u.watts + " W" : pct(u.load)} · ${fmtDur(u.runtime, true)}`;
  $("t-ups-bar").style.width = (u.charge ?? 0) + "%";
}

/* ---------------- network ---------------- */
const linkSpeed = (mbit) => mbit ? (mbit >= 1000 ? mbit / 1000 + " Gb/s" : mbit + " Mb/s") : "-";
function renderNet(s) {
  const ni = netIface();
  const nr = s.net[ni] || [0, 0];
  const nics = S.snap?.storage?.nics || [];
  const nic = nics.find((n) => n.name === ni);
  const win = S.live.slice(-300);
  const up = nics.filter((n) => n.state === "up");
  strip("st-net", [
    stat("In", fmtBits(nr[0]), ni || "", "lead"), stat("Out", fmtBits(nr[1])),
    stat("Peak in", fmtBits(peak(win.map((x) => x.net[ni]?.[0])) ?? 0), "last 10 min"), stat("Peak out", fmtBits(peak(win.map((x) => x.net[ni]?.[1])) ?? 0), "last 10 min"),
    stat("Link", linkSpeed(nic?.speed ?? nics.find((n) => nic?.members?.includes(n.name))?.speed), nic?.kind === "bridge" ? "bridge member" : ""),
    stat("Interfaces up", `${up.length}/${nics.length}`, `${nics.filter((n) => n.kind === "bridge").length} bridges`),
  ]);
  $("net-iface").textContent = ni ? `${ni} · default route` : "";
  if (!S.snap) return;
  const hist = S.live.slice(-60);
  const list = nics.filter((n) => n.state === "up" || n.kind === "physical");
  $("nic-sub").textContent = `${list.length} shown · bits per second`;
  $("nic-list").innerHTML = tbl([
    { h: "Interface", w: "minmax(180px,1.3fr)", m: "minmax(0,1fr)" },
    { h: "Addresses", w: "minmax(0,1.6fr)" },
    { h: "Link", w: "80px", c: "num" },
    { h: "In", w: "96px", m: "80px", c: "num" }, { h: "Out", w: "96px", m: "80px", c: "num" },
    { h: "Last 2 min", w: SPK, c: "spk" },
  ], list.map((n) => {
    const r = s.net[n.name] || [0, 0];
    const h = hist.map((x) => x.net[n.name] ? x.net[n.name][0] + x.net[n.name][1] : null);
    const desc = [n.kind === "bridge" ? `bridge${n.members.length ? " · " + n.members.join(", ") : ""}${n.taps ? ` · ${n.taps} taps` : ""}` : n.driver || n.kind,
      n.master ? `in ${n.master}` : "", n.mtu && n.mtu !== 1500 ? "mtu " + n.mtu : ""].filter(Boolean).join(" · ");
    return { cls: n.state === "up" ? "" : "off", cells: [nm(n.state === "up" ? "run" : "", n.name, desc, n.default ? ` <span class="chip">default</span>` : ""),
      `<span class="dim">${n.ips.length ? n.ips.map(esc).join(", ") : `<span class="mono">${esc(n.mac)}</span>`}</span>`,
      linkSpeed(n.speed), `<b>${fmtBits(r[0])}</b>`, fmtBits(r[1]), sparkSVG(h, COL.net)] };
  }), "No interfaces.");
}

/* ---------------- storage ---------------- */
function renderStorage(s) {
  if (!S.snap) return;
  const st = S.snap.storage || {};
  const fs = st.filesystems || [];
  const disks = st.disks || [];
  const smart = S.snap.smart || {};
  let total = 0, used = 0, ntotal = 0, nused = 0;
  fs.forEach((f) => { if (f.net) { ntotal += f.size || 0; nused += f.used || 0; } else { total += f.size || 0; used += f.used || 0; } });
  const healthy = disks.filter((d) => smart[d.name]?.passed === true).length;
  const failing = disks.filter((d) => smart[d.name]?.passed === false).length;
  const dtemps = disks.map((d) => smart[d.name]?.temp).filter((x) => x != null);
  const busy = peak(disks.map((d) => s.disk[d.name]?.[2]));
  strip("st-disk", [
    stat("Read", fmtRate(s.disk_total[0]), "all drives", "lead"), stat("Write", fmtRate(s.disk_total[1])),
    stat("Busiest drive", busy != null ? busy + "%" : "-", "utilisation"),
    stat("Local used", fmtB(used), `${pctA(total ? 100 * used / total : 0)} of ${fmtB(total, 0)}`),
    ...(ntotal ? [stat("Network used", fmtB(nused), `${pctA(100 * nused / ntotal)} of ${fmtB(ntotal, 0)}`, levelCls(100 * nused / ntotal, 90, 97))] : []),
    stat("Drives", disks.length, failing ? `${failing} failing` : `${healthy} SMART healthy`, failing ? "bad" : ""),
    ...(dtemps.length ? [stat("Hottest drive", Math.max(...dtemps) + " °C")] : []),
  ]);
  $("io-sub").textContent = `${disks.length} drives`;
  // filesystems
  const groups = [["Local", fs.filter((f) => !f.net && f.fstype !== "btrfs" && !f.vg)], ["LVM", fs.filter((f) => f.vg)], ["btrfs", fs.filter((f) => f.fstype === "btrfs")], ["Network", fs.filter((f) => f.net)]];
  $("fs-sub").textContent = `${fs.length} mounted`;
  const rows = [];
  for (const [name, l] of groups.filter(([, l]) => l.length)) {
    rows.push({ grp: name + (name === "LVM" ? " · VG " + [...new Set(l.map((f) => f.vg))].join(", ") : "") });
    for (const f of l) {
      const p = f.size ? 100 * f.used / f.size : 0;
      const what = f.lv ? `${f.lv} · ${f.fstype}` : f.subvol ? `${f.subvol} · ${f.fstype}` : f.net ? `${f.source}` : `${f.kname || f.source.replace("/dev/", "")} · ${f.fstype}`;
      rows.push({ title: f.source, cells: [nm(null, f.mount, `${what}${f.ro ? " · ro" : ""}${f.stale ? " · not responding" : ""}`),
        f.size ? meter(p, levelCls(p, 80, 92)) : "", f.size ? `<b>${pctA(p)}</b>` : "-", f.size ? fmtB(f.used) : "-", f.size ? fmtB(f.size) : esc(f.error || "-")] });
    }
  }
  $("fs-list").innerHTML = tbl([{ h: "Mount", w: "minmax(0,1.6fr)", m: "minmax(0,1fr)" }, { h: "Usage", w: "minmax(80px,1.2fr)", m: "64px" }, { h: "Use", w: "64px", m: "48px", c: "num" },
    { h: "Used", w: "90px", c: "num" }, { h: "Size", w: "90px", m: "72px", c: "num" }], rows, "No filesystems.");
  // drives
  const hist = S.live.slice(-60);
  $("disk-list").innerHTML = tbl([
    { h: "Drive", w: "minmax(200px,1.6fr)", m: "minmax(0,1fr)" }, { h: "Device", w: "112px" }, { h: "Size", w: "84px", c: "num" },
    { h: "Temp", w: "64px", m: "52px", c: "num" }, { h: "Wear", w: "64px", m: "52px", c: "num" }, { h: "Power-on", w: "84px", c: "num" },
    { h: "Written", w: "90px", c: "num" }, { h: "Read", w: "90px", c: "num" }, { h: "Write", w: "90px", c: "num" }, { h: "Busy", w: "56px", c: "num" },
    { h: "I/O, 2 min", w: SPK, c: "spk" }, { h: "Health", w: "104px", m: "88px", c: "num" },
  ], disks.map((d) => {
    const sm = smart[d.name] || {};
    const io = s.disk[d.name];
    let chip = `<span class="chip">SMART n/a</span>`;
    if (sm.standby) chip = `<span class="chip">standby</span>`;
    else if (sm.passed === true) chip = `<span class="chip h-ok">healthy</span>`;
    else if (sm.passed === false) chip = `<span class="chip h-bad">FAILING</span>`;
    else if (sm.error) chip = `<span class="chip" title="${esc(sm.error)}">SMART n/a</span>`;
    const extra = [sm.media_errors ? `${sm.media_errors} media errors` : "", sm.pending ? `${sm.pending} pending sectors` : "", sm.critical_warning ? `critical warning ${sm.critical_warning}` : ""].filter(Boolean);
    const h = hist.map((x) => x.disk[d.name] ? x.disk[d.name][0] + x.disk[d.name][1] : null);
    return { cells: [nm(null, d.model || d.name, `${d.name} · ${d.tran.toUpperCase()}${d.rotational ? " · HDD" : ""}${sm.firmware ? " · fw " + sm.firmware : ""}${extra.length ? " · " + extra.join(", ") : ""}`),
      esc("/dev/" + d.name), fmtB(d.size, d.size >= 2 ** 40 ? 1 : 0), sm.temp != null ? `<b>${sm.temp} °C</b>` : "-",
      sm.type === "hdd" ? (sm.reallocated ?? "-") : sm.wear_pct != null ? sm.wear_pct + "%" : "-",
      sm.hours != null ? (sm.hours > 360 * 24 ? (sm.hours / 8760).toFixed(1) + " y" : sm.hours > 48 ? (sm.hours / 24).toFixed(0) + " d" : sm.hours + " h") : "-",
      sm.written ? fmtB(sm.written) : "-", io ? fmtRate(io[0]) : "-", io ? fmtRate(io[1]) : "-", io ? io[2] + "%" : "-",
      sparkSVG(h, COL.disk), chip] };
  }), "No physical drives found.");
  // btrfs / md side cards (empty => I/O chart takes the full row)
  const side = (st.btrfs || []).map((b) => {
    const a = b.alloc;
    const row = (n, x, prof) => `<div class="mrow"><span>${n}${prof ? ` <small class="dim">${esc(prof)}</small>` : ""}</span>${meter(x.total ? 100 * x.used / x.total : 0)}<b>${fmtB(x.used)}</b></div>`;
    return `<article class="card" data-acc="disk"><header><h3>btrfs · ${esc(b.label || b.uuid.slice(0, 8))}</h3><span class="sub">${esc(b.mounts.join(", "))}</span></header>
      <div class="mrows">${row("Allocated", { used: b.allocated, total: b.device_size })}${row("Data", a.data, a.data.profiles.join("/"))}${row("Metadata", a.metadata, a.metadata.profiles.join("/"))}${row("System", a.system, a.system.profiles.join("/"))}</div>
      <div class="ports" style="margin-top:var(--sp3)">${b.devices.map((d) => `<span class="chip">${esc(d.name)} · ${fmtB(d.size, 0)}</span>`).join("")}<span class="chip ${b.errors ? "h-bad" : "h-ok"}">${b.errors ? b.errors + " device errors" : "0 device errors"}</span></div></article>`;
  }).concat((st.md || []).map((m) => {
    const ok = m.state === "clean" || m.state === "active";
    const fsl = (st.filesystems || []).filter((f) => f.kname === m.name);
    const disks = Object.fromEntries((st.disks || []).map((d) => [d.name, d]));
    // sync_completed is "done / total" in sectors while a resync/check/recovery runs
    const sc = /^(\d+) \/ (\d+)$/.exec(m.sync_completed || "");
    const busy = m.sync_action && m.sync_action !== "idle";
    const pct = sc ? 100 * +sc[1] / +sc[2] : null;
    const eta = sc && m.sync_speed ? (+sc[2] - +sc[1]) * 512 / m.sync_speed : null;
    const kv = (k, v, cls = "") => `<div class="mrow kv"><span>${k}</span><b class="${cls}">${v}</b></div>`;
    const mm = m.mismatch_cnt;
    return `<article class="card" data-acc="disk"><header><h3>md · ${esc(m.name)}</h3><span class="sub">${esc(m.level)} · ${fmtB(m.size, 0)}</span></header>
      <div class="ports"><span class="status-pill ${m.degraded ? "st-bad" : ok ? "st-ok" : "st-warn"}">${m.degraded ? `degraded (${m.degraded} missing)` : esc(m.state)}</span>
      <span class="chip">${m.members.filter((x) => /in_sync/.test(x.state)).length}/${m.raid_disks} in sync</span></div>
      <div class="mrows" style="margin-top:var(--sp3)">
      ${busy ? `<div class="mrow"><span>${esc(m.sync_action)}</span>${meter(pct ?? 0, "", COL.warn)}<b>${pct != null ? pct.toFixed(1) + "%" : "-"}</b></div>
        ${kv("Speed", m.sync_speed ? fmtRate(m.sync_speed) + (eta ? ` · ${fmtDur(eta, true)} left` : "") : "-")}` : ""}
      ${fsl.map((f) => `<div class="mrow"><span title="${esc(f.mount)}">${esc(f.mount)} <small class="dim">${esc(f.fstype)}</small></span>${meter(f.size ? 100 * f.used / f.size : 0)}<b>${f.size ? Math.round(100 * f.used / f.size) + "%" : "-"}</b></div>`).join("")}
      ${kv("Last check", m.last_sync_action && m.last_sync_action !== "none" ? esc(m.last_sync_action) : "never")}
      ${mm != null ? kv("Mismatches", mm.toLocaleString(), mm > 0 ? "warn" : "") : ""}
      ${kv("Metadata", `${esc(m.metadata || "-")}${m.consistency ? " · " + esc(m.consistency) : ""}${m.bitmap_chunk ? ` · ${fmtB(m.bitmap_chunk, 0)} chunk` : ""}`)}
      ${m.chunk_size ? kv("Stripe chunk", fmtB(m.chunk_size, 0)) : ""}
      ${m.uuid ? `<div class="mrow kv"><span>UUID</span><b class="mono dim" title="${esc(m.uuid)}">${esc(m.uuid.slice(0, 13))}…</b></div>` : ""}
      <div class="mrow sep kv"><span>Members</span><b></b></div>
      ${m.members.map((x) => { const d = disks[x.dev] || {}, sm = smart[x.dev] || {};
        const bad = /faulty/.test(x.state), spare = x.slot == null;
        return `<div class="mrow kv"><span title="${esc(d.model || "")}${d.serial ? " · " + esc(d.serial) : ""}"><b class="${bad ? "bad" : ""}">${esc(x.dev)}</b> <small class="dim">${spare ? "spare" : "slot " + x.slot} · ${esc(d.model || "")}</small></span>
          <b class="${bad ? "bad" : x.errors ? "warn" : ""}">${bad ? "faulty" : spare ? "spare" : esc(x.state.replace(/_/g, " "))}${x.errors ? ` · ${x.errors} err` : ""}${sm.temp != null ? ` · ${sm.temp}°` : ""}</b></div>`; }).join("")}
      </div></article>`;
  })).join("");
  if ($("disk-side").innerHTML !== side) $("disk-side").innerHTML = side;
}

/* ---------------- containers ---------------- */
function linkHost() { return S.info.link_host || location.hostname; }
function portChip(p) {
  const label = `${p.port}${p.proto === "udp" ? "/udp" : ""}`;
  if (p.scheme !== "http" && p.scheme !== "https") return `<span class="chip" title="${esc(p.ip)}:${esc(p.port)}/${esc(p.proto)}${p.cport ? " → " + esc(p.cport) : ""}">${esc(label)}</span>`;
  const path = p.port === 32400 ? "/web" : "";
  const host = p.ip && p.ip !== "0.0.0.0" && p.ip !== "::" && p.ip !== "v6" ? p.ip : linkHost();
  return `<a class="chip" href="${p.scheme}://${esc(host)}:${p.port}${path}" target="_blank" rel="noopener" title="${p.scheme}://${esc(host)}:${p.port}${path}">${label}</a>`;
}
function renderCounts() {
  const c = S.snap.containers, v = S.snap.vms;
  $("ct-count").textContent = c.available ? `${c.containers.filter((x) => x.state === "running").length}/${c.containers.length}` : "";
  $("vm-count").textContent = v.available && v.vms.length ? `${v.vms.filter((x) => x.state === "running").length}/${v.vms.length}` : "";
}
function renderContainers(s) {
  if (!S.snap) return;
  const c = S.snap.containers;
  const list = $("ct-list");
  if (!c.available) { strip("st-ct", [stat("Podman", "n/a", c.error)]); list.innerHTML = `<div class="empty">Podman unavailable: ${esc(c.error)}</div>`; return; }
  const cts = c.containers;
  const run = cts.filter((x) => x.state === "running");
  const cur = s.ct || {};
  const cpuT = run.reduce((a, x) => a + (cur[x.id]?.[0] || 0), 0), memT = run.reduce((a, x) => a + (cur[x.id]?.[1] || 0), 0);
  const unhealthy = cts.filter((x) => x.health === "unhealthy").length;
  strip("st-ct", [
    stat("Running", run.length, `of ${cts.length} defined`, "lead"), stat("Stopped", cts.length - run.length, "", cts.some((x) => x.state !== "running" && x.exit_code) ? "warn" : ""),
    stat("Healthy", cts.filter((x) => x.health === "healthy").length, unhealthy ? `${unhealthy} unhealthy` : `${cts.filter((x) => x.health).length} with healthchecks`, unhealthy ? "bad" : ""),
    stat("CPU", pctA(cpuT), "of one core, summed"), stat("Memory", fmtB(memT), `${pctA(100 * memT / (s.mem.total || 1))} of RAM`),
    stat("Ports", cts.reduce((a, x) => a + x.ports.length, 0), "published / listening"),
  ]);
  $("ct-sub").textContent = `rootful podman · ${c.containers.filter((x) => x.host_net).length} on host network`;
  const now = Date.now() / 1000;
  const hist = S.live.slice(-60);
  list.innerHTML = tbl([
    { h: "Name", w: "minmax(150px,1.2fr)", m: "minmax(0,1fr)" }, { h: "Image", w: "minmax(0,1.5fr)" }, { h: "Status", w: "minmax(120px,.9fr)", m: "92px" },
    { h: "CPU", w: "64px", m: "52px", c: "num" }, { h: "CPU, 2 min", w: SPK, c: "spk" },
    { h: "Memory", w: "80px", m: "68px", c: "num" }, { h: "Memory, 2 min", w: SPK, c: "spk" }, { h: "Ports", w: "minmax(0,1.2fr)" },
  ], cts.map((x) => {
    const r = x.state === "running";
    const dot = !r ? (x.exit_code ? "bad" : "") : x.health === "unhealthy" ? "bad" : x.health === "starting" ? "warn" : "run";
    const cv = cur[x.id];
    const cpuH = hist.map((q) => q.ct[x.id]?.[0] ?? null);
    const memH = hist.map((q) => q.ct[x.id]?.[1] ?? null);
    const memV = memH.filter((v) => v != null);
    const memMax = Math.max(...memV, 1), memMin = Math.min(...memV, memMax);
    const memPad = Math.max((memMax - memMin) * 0.25, memMax * 0.02);
    const hchip = x.health ? ` <span class="chip ${x.health === "healthy" ? "h-ok" : x.health === "unhealthy" ? "h-bad" : "h-warn"}" title="${esc(x.health_log)}">${esc(x.health)}</span>` : "";
    const status = r ? `<b>Up ${fmtDur(now - x.started, true)}</b>${hchip}` : `<b>${esc(x.state)}${x.exit_code ? ` (${x.exit_code})` : ""}</b>`;
    const sub = r ? (x.restarts ? `${x.restarts} restarts` : "") : (x.exited ? `${fmtDur(now - x.exited, true)} ago` : "");
    const ver = x.version && x.version !== x.tag ? ` · v${esc(x.version.replace(/^v/, ""))}` : "";
    return { cls: r ? "" : "off", cells: [
      nm(dot, x.name, `${x.unit || x.short_id}${x.host_net ? " · host net" : ""}${x.pod ? " · pod " + x.pod : ""}`),
      `<div class="stack2"><span class="dim" title="${esc(x.image)}${x.tag ? ":" + esc(x.tag) : ""}">${esc(x.image)}${x.tag ? `<span class="tag">:${esc(x.tag)}</span>` : ""}</span><small>${x.digest ? `<span class="mono">${esc(x.digest)}</span>` : ""}${ver}</small></div>`,
      `<div class="stack2"><span>${status}</span>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`,
      r && cv ? `<b>${pctA(cv[0])}</b>` : "-", r ? sparkSVG(cpuH, COL.ct, Math.max(5, ...cpuH.filter((v) => v != null))) : "",
      r && cv ? `<b>${fmtB(cv[1])}</b>` : "-", r ? sparkSVG(memH, pal(COL.ct)[1], memMax + memPad, 100, 26, Math.max(0, memMin - memPad)) : "",
      `<div class="ports">${x.ports.map(portChip).join("") || "-"}</div>`] };
  }), "No containers.");
}

/* ---------------- VMs ---------------- */
S.vmHist = Object.create(null); S.vmMemHist = Object.create(null);
function recordVMs() {
  for (const x of S.snap?.vms?.vms || []) {
    const r = x.state === "running";
    const h = (S.vmHist[x.name] ||= []);
    h.push(r ? x.cpu_vcpu_pct ?? null : null);
    if (h.length > 24) h.shift();  // 2 min at the 5 s snapshot interval
    const m = (S.vmMemHist[x.name] ||= []);
    m.push(r ? x.mem_used ?? null : null);
    if (m.length > 24) m.shift();
  }
}
function renderVMs() {
  if (!S.snap) return;
  const v = S.snap.vms;
  const el = $("vm-list");
  if (!v.available) { strip("st-vm", [stat("libvirt", "n/a", v.error)]); el.innerHTML = `<div class="empty">libvirt unavailable: ${esc(v.error)}</div>`; return; }
  const vms = v.vms;
  const run = vms.filter((x) => x.state === "running");
  const sum = (l, f) => l.reduce((a, x) => a + (f(x) || 0), 0);
  strip("st-vm", [
    stat("Running", run.length, `of ${vms.length} defined`, "lead"), stat("vCPUs", sum(run, (x) => x.vcpus), `of ${S.info.cpu.threads} host threads`),
    stat("CPU", pctA(run.length ? sum(run, (x) => x.cpu_vcpu_pct) / run.length : 0), "avg per vCPU"),
    stat("Memory", fmtB(sum(run, (x) => x.mem), 0), `${fmtB(sum(run, (x) => x.mem_used))} used by guests`),
    stat("Disk on host", fmtB(sum(vms, (x) => x.disks.reduce((a, d) => a + (d.allocated || 0), 0))), "allocated"),
    stat("Autostart", vms.filter((x) => x.autostart).length, "start on boot"),
  ]);
  $("vm-sub").textContent = `libvirt · ${esc(S.info.hostname.split(".")[0])} · read-only`;
  el.innerHTML = tbl([
    { h: "Name", w: "minmax(150px,1.2fr)", m: "minmax(0,1fr)" }, { h: "State", w: "110px" }, { h: "vCPU", w: "56px", c: "num" },
    { h: "CPU", w: "64px", m: "52px", c: "num" }, { h: "CPU, 2 min", w: SPK, c: "spk" }, { h: "Memory", w: "140px", m: "92px", c: "num" },
    { h: "Memory, 2 min", w: SPK, c: "spk" }, { h: "Snapshots", w: "84px", c: "num" }, { h: "Disk", w: "84px", c: "num" }, { h: "On host", w: "84px", c: "num" }, { h: "Net", w: "90px", c: "num" }, { h: "Address", w: "minmax(0,1.4fr)" },
  ], vms.map((x) => {
    const r = x.state === "running";
    const cls = r ? "st-ok" : x.state === "paused" ? "st-warn" : x.state === "crashed" ? "st-bad" : "st-idle";
    const disk = x.disks.reduce((a, d) => a + (d.capacity || 0), 0);
    const alloc = x.disks.reduce((a, d) => a + (d.allocated || 0), 0);
    const h = S.vmHist[x.name] || [];
    // guest memory in use, scaled like the containers tab (zoomed to the recent range)
    const memH = S.vmMemHist[x.name] || [];
    const memV = memH.filter((v) => v != null);
    const memMax = Math.max(...memV, 1), memMin = Math.min(...memV, memMax);
    const memPad = Math.max((memMax - memMin) * 0.25, memMax * 0.02);
    return { cls: r ? "" : "off", cells: [
      nm(r ? "run" : "", x.name, [x.os, x.firmware, x.autostart ? "autostart" : ""].filter(Boolean).join(" · ")),
      `<span class="status-pill ${cls}">${esc(x.state)}</span>`, x.vcpus,
      r && x.cpu_vcpu_pct != null ? `<b>${pctA(x.cpu_vcpu_pct)}</b>` : "-", r ? sparkSVG(h, COL.vm, Math.max(5, ...h.filter((q) => q != null))) : "",
      x.mem_used != null ? `<b>${fmtB(x.mem_used)}</b> / ${fmtB(x.mem, 0)}` : fmtB(x.mem, 0),
      r && memV.length ? sparkSVG(memH, pal(COL.vm)[1], memMax + memPad, 100, 26, Math.max(0, memMin - memPad)) : "",
      x.snapshots == null ? "-" : x.snapshots.length ? `<b title="${esc(x.snapshots.join(", "))}">${x.snapshots.length}</b>` : `<span class="dim">0</span>`,
      disk ? fmtB(disk, 0) : "-", alloc ? fmtB(alloc) : "-", r && x.net_rx != null ? fmtBits(x.net_rx + x.net_tx) : "-",
      `<span class="dim">${x.nics.map((n) => `${n.ip ? esc(n.ip) + " · " : ""}<span class="mono">${esc(n.mac)}</span> · ${esc(n.source || n.type)}`).join("<br>") || "no interfaces"}</span>`] };
  }), "No libvirt domains defined on this host.");
}

function renderFoot() {
  const s = S.snap.self, w = S.snap.workers;
  const errs = Object.entries(w).filter(([, x]) => x.error).map(([k, x]) => `${k}: ${x.error}`);
  $("foot").innerHTML = `<span>hlidskjalf <b>${esc(S.info.version)}</b></span>
    <span>self <b>${s.cpu_pct != null ? s.cpu_pct.toFixed(2) + "% CPU" : "measuring..."}</b> · <b>${fmtB(s.rss, 0)}</b> RSS</span>
    <span>history <b>${S.snap.history_ready ? "sysstat" : "loading"}</b></span>
    <span>sampling every <b>${S.info.sample_interval}s</b></span>
    ${errs.length ? `<span style="color:var(--bad)">${esc(errs.join(" · "))}</span>` : ""}`;
}

/* ---------------- polling ---------------- */
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
async function pollLive() {
  try {
    const d = await getJSON(`/api/live?since=${S.seq}`);
    if (d.seq < S.seq) S.live = [];  // server restarted
    if (d.samples.length) {
      S.live.push(...d.samples);
      if (S.live.length > MAXLIVE) S.live.splice(0, S.live.length - MAXLIVE);
      S.seq = d.seq;
      onLive();
      const p = $("pulse"); p.classList.remove("beat", "stale"); void p.offsetWidth; p.classList.add("beat");
    }
    S.lastOk = Date.now();
  } catch (e) {
    $("pulse").classList.add("stale");
  }
  setTimeout(pollLive, (S.info?.sample_interval || 2) * 1000);
}
async function pollSnap() {
  try {
    S.snap = await getJSON("/api/snapshot");
    recordVMs(); renderCounts(); renderUPSTile(); renderFacts(); renderFoot(); renderTab();
  } catch (e) { console.warn(e); }
  setTimeout(pollSnap, 5000);
}
async function loadHistory(force) {
  const r = S.range;
  if (r === "live") return;
  const fresh = (at) => at && Date.now() - at < 55000;
  if (!force && fresh(S.histAt[r]) && fresh(S.ghistAt[r])) { redrawCharts(); return; }
  let retry = false;
  await Promise.all([
    (async () => {
      try {
        const r2 = await fetch(`/api/history?range=${r}`, { cache: "no-store" });
        if (r2.status === 503) { retry = true; return; }
        S.hist[r] = await r2.json(); S.histAt[r] = Date.now();
      } catch (e) { console.warn(e); }
    })(),
    (async () => {
      try {
        const r2 = await fetch(`/api/gpu/history?range=${r}`, { cache: "no-store" });
        if (r2.status === 503) { retry = true; return; }
        S.ghist[r] = await r2.json(); S.ghistAt[r] = Date.now();
      } catch (e) { console.warn(e); }
    })(),
  ]);
  if (retry) setTimeout(() => loadHistory(true), 5000);
  if (S.range === r) redrawCharts();
}
function setRange(r) {
  S.range = r; store.set("hl-range", r);
  document.querySelectorAll("#range button").forEach((b) => b.classList.toggle("on", b.dataset.r === r));
  redrawCharts();
  loadHistory();
}

function setTab(t, fromHash) {
  if (!TABS.includes(t)) t = "cpu";
  S.tab = t;
  store.set("hl-tab", t);
  if (!fromHash && location.hash !== "#" + t) history.replaceState(null, "", "#" + t);
  document.querySelectorAll("#tabs button").forEach((b) => {
    const on = b.dataset.tab === t;
    b.classList.toggle("on", on); b.setAttribute("aria-selected", on);
    if (on) requestAnimationFrame(() => b.scrollIntoView({ block: "nearest", inline: "center" }));
  });
  document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== t; });
  $("range").classList.toggle("dim", t === "ct" || t === "vm");
  renderTab();
  // charts in a previously hidden panel need their real width before data
  requestAnimationFrame(() => {
    for (const k of TAB_CHARTS[t] || []) {
      const u = S.charts[k]; if (!u) continue;
      const w = u._el.clientWidth; if (w && Math.abs(w - u.width) > 2) u.setSize({ width: w, height: u._h });
    }
    redrawCharts();
  });
}

/* ---------------- export (CSV / JSON of the selected range) ---------------- */
function download(name, mime, text) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const slug = (x) => String(x).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
// history is columnar ({t: [...], cpu: [...], net: {eth0: {rx: [...]}}}): collect every series as [name, values]
function seriesCols(obj, n, name, out = []) {
  for (const [k, v] of Object.entries(obj || {})) {
    if (k === "t") continue;
    if (Array.isArray(v)) { if (v.length === n) out.push([name(k), v]); }
    else if (v && typeof v === "object") seriesCols(v, n, (x) => name(k) + "_" + slug(x), out);
  }
  return out;
}
const HIST_NAMES = {
  cpu: "cpu_pct", cpu_user: "cpu_user_pct", cpu_sys: "cpu_system_pct", cpu_iow: "cpu_iowait_pct", mem_pct: "mem_used_pct", mem_used: "mem_used_bytes",
  disk_r: "disk_read_bytes_per_s", disk_w: "disk_write_bytes_per_s", net: "net_bytes_per_s", temps: "temp_c", fans: "fan_rpm",
};
const GPU_NAMES = { e: "gpu_engine_pct", f: "gpu_freq_mhz", rc6: "gpu_rc6_pct", pw: "power_w", c: "gpu_client_pct" };
function historyTable(h, g) {
  const sens = Object.fromEntries((S.info.sensors || []).map((x) => [slug(x.id), slug(x.label) + "_" + slug(x.id)]));
  const sets = [];
  if (h?.t) sets.push([h.t, seriesCols(h, h.t.length, (k) => HIST_NAMES[k] || slug(k))
    .map(([n, v]) => [n.replace(/^temp_c_(.+)$/, (m, id) => "temp_c_" + (sens[id] || id))
      .replace(/^net_bytes_per_s_(.+)_(rx|tx)$/, "net_$1_$2_bytes_per_s"), v])]);
  if (g?.t && g.minutes) sets.push([g.t, seriesCols(g, g.t.length, (k) => GPU_NAMES[k] || "gpu_" + slug(k))]);
  return joinSets(sets);
}
// live samples are row objects: flatten each one, arrays become name_0, name_1, ...
function liveTable(samples) {
  const flat = (o, pre, out) => {
    for (const [k, v] of Object.entries(o)) {
      if (k === "t" && !pre) continue;
      const n = pre + slug(k);
      if (v != null && typeof v === "object") flat(v, n + "_", out); else out[n] = v;
    }
    return out;
  };
  const rows = samples.map((x) => flat(x, "", {}));
  const names = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  return { names, t: samples.map((x) => x.t), rows: rows.map((r) => names.map((n) => r[n])) };
}
// series sets on different time axes (host history, GPU history) merge on the timestamp
function joinSets(sets) {
  const names = sets.flatMap(([, cols]) => cols.map((c) => c[0]));
  const byT = new Map();
  let off = 0;
  for (const [t, cols] of sets) {
    t.forEach((ts, i) => {
      let row = byT.get(ts);
      if (!row) byT.set(ts, row = new Array(names.length).fill(null));
      cols.forEach((c, j) => { row[off + j] = c[1][i]; });
    });
    off += cols.length;
  }
  const t = [...byT.keys()].sort((a, b) => a - b);
  return { names, t, rows: t.map((ts) => byT.get(ts)) };
}
function toCSV({ names, t, rows }) {
  const cell = (v) => v == null ? "" : typeof v === "number" ? String(+v.toFixed(3)) : /[",\n]/.test(v) ? `"${String(v).replace(/"/g, '""')}"` : String(v);
  return [["time_utc", "epoch_s", ...names].join(","),
    ...rows.map((r, i) => [new Date(t[i] * 1000).toISOString(), +t[i].toFixed(3), ...r.map(cell)].join(","))].join("\n") + "\n";
}
async function exportData(fmt) {
  const r = S.range, host = S.info.hostname.split(".")[0];
  const d = new Date(), p2 = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}`;
  const name = `hlidskjalf-${slug(host)}-${r}-${stamp}.${fmt}`;
  let h = null, g = null;
  if (r !== "live") {
    const get = async (u) => { const x = await fetch(u, { cache: "no-store" }); if (!x.ok) throw new Error(x.status === 503 ? "history is still loading, try again shortly" : `${u}: HTTP ${x.status}`); return x.json(); };
    [h, g] = await Promise.all([get(`/api/history?range=${r}`), get(`/api/gpu/history?range=${r}`).catch(() => null)]);
  }
  if (fmt === "csv") {
    const tb = r === "live" ? liveTable(S.live) : historyTable(h, g);
    if (!tb.t.length) throw new Error("no data for this range yet");
    return download(name, "text/csv;charset=utf-8", toCSV(tb));
  }
  download(name, "application/json", JSON.stringify({
    generated: d.toISOString(), host: S.info.hostname, range: r, version: S.info.version,
    info: S.info, snapshot: S.snap,
    ...(r === "live" ? { samples: S.live } : { history: h, gpu_history: g }),
  }, null, 1));
}
function setupExport() {
  const btn = $("export-btn"), menu = $("export-menu");
  const show = (on) => {
    menu.hidden = !on; btn.setAttribute("aria-expanded", on);
    if (on) $("export-range").textContent = `${S.info.hostname.split(".")[0]} · ${S.range === "live" ? `live buffer (${S.live.length} samples)` : "last " + S.range}`;
  };
  btn.addEventListener("click", (e) => { e.stopPropagation(); show(menu.hidden); });
  document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target)) show(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") show(false); });
  menu.querySelectorAll("button").forEach((b) => b.addEventListener("click", async () => {
    try { await exportData(b.dataset.fmt); show(false); }
    catch (e) { $("export-range").textContent = "Export failed: " + e.message; console.warn(e); }
  }));
}

/* ---------------- hardware report (click the machine tile) ---------------- */
// One model for both the on-screen view and the .txt export: sections of rows,
// with a header row for tables and none for key/value lists.
function hwSections(hw) {
  const i = S.info, st = S.snap?.storage || {}, smart = S.snap?.smart || {}, b = hw.board, c = hw.cpu;
  const kv = (rows) => rows.filter((r) => r[1]);
  const out = [];
  out.push({ title: "System", rows: kv([
    ["System", [b.system, b.system_version && `(version ${b.system_version})`].filter(Boolean).join(" ")],
    ["Motherboard", [b.board, b.board_version && `(rev ${b.board_version})`].filter(Boolean).join(" ")],
    ["BIOS", [b.bios_vendor, b.bios_version, b.bios_date && `(${b.bios_date})`].filter(Boolean).join(" ")],
    ["Chassis", b.chassis], ["Operating system", i.os], ["Kernel", i.kernel],
  ]) });
  const types = {};
  for (const x of i.cpu.cpus || []) if (x.type) (types[x.type] ??= new Set()).add(x.core);
  const mix = Object.entries(types).map(([t, set]) => `${set.size} ${t}`).join(" + ");
  const maxMhz = Math.max(i.cpu.max_mhz || 0, ...(i.cpu.cpus || []).map((x) => x.max_mhz || 0));   // fastest core on hybrid CPUs
  const cache = (c.caches || []).map((x) => x.replace(/(\d+)K$/, (m, k) => k >= 1024 ? `${+(k / 1024).toFixed(1)} MiB` : `${k} KiB`)).join(" · ");
  out.push({ title: "Processor", rows: kv([
    ["Model", i.cpu.model], ["Sockets", c.sockets > 1 ? String(c.sockets) : ""],
    ["Cores / threads", `${i.cpu.cores} cores${mix ? ` (${mix})` : ""} / ${i.cpu.threads} threads`],
    ["Max frequency", maxMhz ? `${(maxMhz / 1000).toFixed(2)} GHz` : ""],
    ["Cache (first core)", cache], ["Microcode", c.microcode], ["Architecture", c.arch],
  ]) });
  const gib = (mib) => mib >= 1024 ? `${+(mib / 1024).toFixed(1)} GiB` : `${mib} MiB`;
  if (hw.memory) {
    const used = hw.memory.filter((m) => m.size_mib);
    out.push({ title: "Memory", sub: `${i.ram_gib ? i.ram_gib + " GB installed · " : ""}${used.length} of ${hw.memory.length} slots populated`,
      head: ["Slot", "Size", "Type", "Speed", "Module"],
      rows: hw.memory.map((m) => m.size_mib ? [m.locator || m.bank, gib(m.size_mib), [m.type, m.form].filter(Boolean).join(" "),
        m.configured_speed || m.speed ? `${m.configured_speed || m.speed} MT/s` : "", [m.manufacturer, m.part].filter(Boolean).join(" ") || "unknown"]
        : [m.locator || m.bank, "empty", "", "", ""]) });
  } else {
    out.push({ title: "Memory", rows: kv([["Installed", i.ram_gib ? `${i.ram_gib} GB` : ""], ["Modules", "not available (needs the privileged helper)"]]) });
  }
  const where = (d) => d.onboard ? "integrated" : d.slot ? `slot ${d.slot}` : "PCIe";
  const gpus = hw.pci.filter((d) => d.class_code.startsWith("03"));
  if (gpus.length) out.push({ title: "Graphics", head: ["Device", "Location", "Driver"], rows: gpus.map((d) => [d.name, where(d), d.driver]) });
  const disks = st.disks || [];
  if (disks.length) out.push({ title: "Storage", sub: `${disks.length} drive${disks.length === 1 ? "" : "s"}`,
    head: ["Device", "Model", "Size", "Connection", "Type", "Firmware"],
    rows: disks.map((d) => {
      const sm = smart[d.name] || {};
      const tran = { nvme: "NVMe", sata: "SATA", usb: "USB", sas: "SAS", mmc: "MMC" }[d.tran] || (d.tran || "").toUpperCase();
      const type = (sm.type || (d.rotational ? "hdd" : "ssd")) === "hdd" ? `HDD${sm.rpm ? ` ${sm.rpm} rpm` : ""}` : "SSD";
      return ["/dev/" + d.name, sm.model || d.model || "", fmtB(d.size, d.size >= 2 ** 40 ? 1 : 0), tran + (d.removable ? " (removable)" : ""), type, sm.firmware || ""];
    }) });
  // expansion: everything behind a PCIe root port, one row per physical device (functions merged)
  const cards = new Map();
  for (const d of hw.pci.filter((x) => !x.onboard)) {
    const key = d.addr.slice(0, -2);
    if (cards.has(key)) cards.get(key).n++; else cards.set(key, { ...d, n: 1 });
  }
  if (cards.size) out.push({ title: "Expansion cards", sub: "PCIe devices behind a root port: add-in cards, M.2 modules, soldered controllers",
    head: ["Slot", "Address", "Type", "Device", "Link", "Driver"],
    rows: [...cards.values()].map((d) => [d.slot || "-", d.addr.replace(/^0000:/, ""), d.class, d.name + (d.n > 1 ? ` (${d.n} functions)` : ""), d.link, d.driver]) });
  if (hw.net.length) out.push({ title: "Network adapters", head: ["Interface", "Adapter", "Kind", "Link", "Driver"],
    rows: hw.net.map((n) => [n.name, n.model || `${n.bus.toUpperCase()} adapter`, n.wireless ? "Wi-Fi" : "Ethernet", n.speed ? linkSpeed(n.speed) : "no link", n.driver]) });
  if (hw.usb.length) out.push({ title: "USB devices", head: ["Port", "Device", "ID", "Speed"],
    rows: hw.usb.map((u) => [u.port, u.name, u.id, u.speed ? `${+u.speed >= 1000 ? +u.speed / 1000 + " Gb/s" : u.speed + " Mb/s"}` : ""]) });
  const u = S.snap?.ups;
  if (u?.available) out.push({ title: "UPS", rows: kv([["Model", u.model], ["Rated power", u.nominal_watts ? `${Math.round(u.nominal_watts)} W` : ""], ["Monitored via", `${u.name}@${u.server}`]]) });
  return out;
}
function hwText(secs) {
  const host = S.info.hostname, now = new Date();
  const lines = ["Hardware report: " + host, "Generated " + now.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) + " by hlidskjalf " + S.info.version, ""];
  for (const s of secs) {
    lines.push(s.title.toUpperCase() + (s.sub ? "  (" + s.sub + ")" : ""), "-".repeat(s.title.length));
    const rows = (s.head ? [s.head, ...s.rows] : s.rows).map((r) => r.map((x) => String(x ?? "")));
    const w = rows[0].map((_, j) => Math.max(...rows.map((r) => r[j].length)));
    rows.forEach((r, n) => {
      lines.push("  " + (s.head ? r.map((x, j) => x.padEnd(w[j])).join("  ") : r[0].padEnd(w[0]) + " : " + r[1]).trimEnd());
      if (s.head && n === 0) lines.push("  " + w.map((x) => "-".repeat(x)).join("  "));
    });
    lines.push("");
  }
  return lines.join("\n");
}
function hwHTML(secs) {
  return secs.map((s) => `<section class="${s.head ? "" : "kv"}"><header><h3>${esc(s.title)}</h3>${s.sub ? `<span class="sub">${esc(s.sub)}</span>` : ""}</header>
    <div class="hw-scroll"><table class="${s.head ? "" : "kv"}">${s.head ? `<thead><tr>${s.head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>` : ""}
    <tbody>${s.rows.map((r) => `<tr>${r.map((x, j) => j === 0 && !s.head ? `<th>${esc(x)}</th>` : `<td>${esc(x)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></section>`).join("");
}
function setupHardware() {
  const box = $("hw"), body = $("hw-body"), card = document.querySelector(".ident");
  let secs = null;
  const show = (on) => { box.hidden = !on; document.body.classList.toggle("modal", on); };
  const open = async () => {
    secs = null; $("hw-title").textContent = "Hardware · " + S.info.hostname.split(".")[0];
    body.innerHTML = `<div class="empty">Reading hardware...</div>`; show(true);
    try {
      const r = await fetch("/api/hardware", { cache: "no-store" });
      if (!r.ok) throw new Error(r.status === 404 ? "this server is too old for the hardware report; update and restart it" : "HTTP " + r.status);
      secs = hwSections(await r.json());
      body.innerHTML = hwHTML(secs);
    } catch (e) { body.innerHTML = `<div class="empty">Hardware report failed: ${esc(e.message)}</div>`; console.warn(e); }
  };
  card.setAttribute("role", "button"); card.tabIndex = 0; card.title = "Hardware report";
  card.addEventListener("click", open);
  card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  $("hw-close").addEventListener("click", () => show(false));
  box.addEventListener("click", (e) => { if (e.target === box) show(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !box.hidden) show(false); });
  $("hw-export").addEventListener("click", () => {
    if (!secs) return;
    const d = new Date(), p2 = (n) => String(n).padStart(2, "0");
    download(`hlidskjalf-${slug(S.info.hostname.split(".")[0])}-hardware-${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}.txt`, "text/plain;charset=utf-8", hwText(secs));
  });
}

async function init() {
  S.info = await getJSON("/api/info");
  renderHosts(); renderIdent(); setupCharts(); setupExport(); setupHardware();
  document.querySelectorAll("#range button").forEach((b) => b.addEventListener("click", () => setRange(b.dataset.r)));
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  document.querySelectorAll("[data-goto]").forEach((el) => el.addEventListener("click", () => {
    setTab(el.dataset.goto); $("tabbar").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  window.addEventListener("hashchange", () => setTab(location.hash.slice(1), true));
  setTab(location.hash.slice(1) || store.get("hl-tab") || "cpu", !!location.hash);
  setRange(S.range);
  pollLive(); pollSnap();
  setInterval(() => { if (S.range !== "live") loadHistory(); }, 60000);
  setInterval(async () => { try { S.info = { ...S.info, ...(await getJSON("/api/info")) }; } catch (e) { /* ignore */ } }, 300000);
}
init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<div style="padding:20px;color:#f87171">Failed to load: ${esc(e.message)}</div>`); console.error(e); });
