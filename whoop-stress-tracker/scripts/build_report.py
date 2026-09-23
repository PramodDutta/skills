#!/usr/bin/env python3
"""Turn analysis.json into a self-contained HTML report plus a short Markdown summary.

  python3 build_report.py out/analysis.json
      -> out/stress_report.html and out/stress_summary.md
  python3 build_report.py out/analysis.json --html report.html --md summary.md

The HTML has no external dependencies: open it in any browser, light or dark.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from common import WEEKDAYS, WEEKDAYS_LONG, read_json

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WHOOP Stress Map</title>
<style>
:root {
  color-scheme: light;
  --bg: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,.10);
  --accent: #2a78d6; --deemph: #c9c8c1; --pos: #e34948; --neg: #2a78d6; --good: #006300;
  --empty: #f0efec; --banner: #fff4dc; --banner-ink: #6b4a00;
  --h0: #cde2fb; --h1: #b7d3f6; --h2: #9ec5f4; --h3: #6da7ec; --h4: #3987e5; --h5: #256abf; --h6: #184f95; --h7: #0d366b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,.10);
    --accent: #3987e5; --deemph: #4a4a46; --pos: #e66767; --neg: #3987e5; --good: #0ca30c;
    --empty: #232322; --banner: #3a2f14; --banner-ink: #f5d68a;
    --h0: #104281; --h1: #184f95; --h2: #1c5cab; --h3: #256abf; --h4: #3987e5; --h5: #5598e7; --h6: #86b6ef; --h7: #b7d3f6;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,.10);
  --accent: #3987e5; --deemph: #4a4a46; --pos: #e66767; --neg: #3987e5; --good: #0ca30c;
  --empty: #232322; --banner: #3a2f14; --banner-ink: #f5d68a;
  --h0: #104281; --h1: #184f95; --h2: #1c5cab; --h3: #256abf; --h4: #3987e5; --h5: #5598e7; --h6: #86b6ef; --h7: #b7d3f6;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 24px 16px 56px; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -.01em; }
h2 { font-size: 17px; margin: 0 0 4px; }
.sub { color: var(--ink2); margin: 0; font-size: 13px; }
.banner { background: var(--banner); color: var(--banner-ink); border-radius: 8px; padding: 8px 12px; margin: 12px 0 0; font-size: 13px; }
.hero { font-size: 26px; line-height: 1.25; font-weight: 600; margin: 20px 0 16px; letter-spacing: -.015em; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin: 14px 0; }
.card > p.lede { color: var(--ink2); margin: 0 0 12px; font-size: 13px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; }
.kpi .label { color: var(--ink2); font-size: 12px; }
.kpi .value { font-size: 20px; font-weight: 600; margin-top: 2px; }
.kpi .note { color: var(--muted); font-size: 12px; }
.heat { display: grid; gap: 2px; margin-top: 8px; }
.heat .rowlab, .heat .collab { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; display: flex; align-items: center; }
.heat .collab { justify-content: center; }
.heat .cell { height: 26px; border-radius: 3px; background: var(--empty); outline: none; }
.heat .cell:hover, .heat .cell:focus-visible { box-shadow: 0 0 0 2px var(--ink); }
.legend { display: flex; align-items: center; gap: 8px; color: var(--ink2); font-size: 12px; margin-top: 10px; flex-wrap: wrap; }
.ramp { display: flex; gap: 2px; }
.ramp span { width: 18px; height: 10px; border-radius: 2px; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: -1px; margin-right: 4px; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--muted); font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; }
svg .val { fill: var(--ink2); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--ink2); font-weight: 600; border-bottom: 1px solid var(--grid); padding: 6px 8px 6px 0; }
td { border-bottom: 1px solid var(--grid); padding: 7px 8px 7px 0; vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.scroll { overflow-x: auto; }
.scroll table.wide { min-width: 520px; }
.win { padding: 10px 0; border-bottom: 1px solid var(--grid); }
.win:last-child { border-bottom: 0; }
.win-head { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; }
.win-head b { font-size: 15px; }
.pill { font-size: 12px; color: var(--ink2); border: 1px solid var(--border); border-radius: 999px; padding: 1px 8px; }
.win-meta, .win-cal { color: var(--ink2); font-size: 13px; margin-top: 2px; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--ink2); font-size: 13px; }
.recs { counter-reset: rec; display: grid; gap: 12px; }
.rec { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.rec h3 { margin: 0 0 4px; font-size: 16px; display: flex; gap: 10px; align-items: baseline; }
.rec h3::before { counter-increment: rec; content: counter(rec); font-size: 12px; font-weight: 600; color: var(--surface); background: var(--ink); border-radius: 50%; min-width: 20px; height: 20px; display: inline-grid; place-items: center; }
.rec .why { color: var(--ink2); margin: 0 0 8px; font-size: 14px; }
.rec ul { margin: 0 0 8px; padding-left: 20px; }
.rec li { margin: 4px 0; }
.rec .measure { color: var(--muted); font-size: 12px; margin: 0; }
.muted { color: var(--muted); }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 640px) { .two { grid-template-columns: 1fr; } .hero { font-size: 22px; } }
#tip { position: fixed; pointer-events: none; z-index: 10; background: var(--surface); color: var(--ink); border: 1px solid var(--border);
  box-shadow: 0 4px 16px rgba(0,0,0,.12); border-radius: 8px; padding: 6px 10px; font-size: 12px; max-width: 260px; display: none; }
#tip b { font-size: 14px; display: block; }
footer { color: var(--muted); font-size: 12px; margin-top: 24px; }
</style>
</head>
<body>
<main id="app"></main>
<div id="tip" role="status"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  "use strict";
  const A = JSON.parse(document.getElementById("data").textContent);
  const app = document.getElementById("app");
  const tip = document.getElementById("tip");
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const SVGNS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "text") n.textContent = v;
      else if (k === "cls") n.className = v;
      else if (k === "style") n.setAttribute("style", v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined) n.append(k.nodeType ? k : document.createTextNode(String(k)));
    return n;
  }
  function svg(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "text") n.textContent = v; else n.setAttribute(k, v);
    }
    return n;
  }
  const fmt = (x, d = 1) => (x === null || x === undefined ? "–" : Number(x).toFixed(d));
  const sgn = (x, d = 1) => (x === null || x === undefined ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(d));
  const pct = (x) => (x === null || x === undefined ? "–" : Math.round(x * 100) + "%");
  const hh = (h) => String(h).padStart(2, "0") + ":00";

  function hover(node, lines) {
    node.setAttribute("tabindex", "0");
    const show = (x, y) => {
      tip.replaceChildren(el("b", { text: lines[0] }), ...lines.slice(1).map((l) => el("div", { text: l })));
      tip.style.display = "block";
      const r = tip.getBoundingClientRect();
      tip.style.left = Math.min(window.innerWidth - r.width - 8, Math.max(8, x + 12)) + "px";
      tip.style.top = Math.max(8, y - r.height - 12) + "px";
    };
    node.addEventListener("pointermove", (e) => show(e.clientX, e.clientY));
    node.addEventListener("pointerleave", () => (tip.style.display = "none"));
    node.addEventListener("focus", () => { const b = node.getBoundingClientRect(); show(b.left + b.width / 2, b.top); });
    node.addEventListener("blur", () => (tip.style.display = "none"));
  }
  function table(headers, rows, numeric) {
    return el("div", { cls: "scroll" }, el("table", { cls: headers.length >= 4 ? "wide" : null },
      el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { cls: numeric && numeric[i] ? "num" : null, text: h })))),
      el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => el("td", { cls: numeric && numeric[i] ? "num" : null, text: c })))))));
  }
  function card(title, lede, ...kids) {
    return el("section", { cls: "card" }, el("h2", { text: title }), lede ? el("p", { cls: "lede", text: lede }) : null, ...kids);
  }
  const CW = Math.max(290, Math.min(860, app.clientWidth - 66));
  const heatColor = (v) => (v === null || v === undefined ? "var(--empty)" : `var(--h${Math.min(7, Math.max(0, Math.floor((v / 3) * 8)))})`);

  // ---------- header ----------
  const src = A.sources || {};
  const w = src.whoop, hr = src.heart_rate;
  const range = w ? `${w.from} – ${w.to}` : hr ? `${hr.from} – ${hr.to}` : "";
  app.append(el("h1", { text: "When you're stressed" }));
  app.append(el("p", { cls: "sub", text: [range, A.timezone].filter(Boolean).join(" · ") }));
  if (A.demo) app.append(el("div", { cls: "banner", text: "Demo: synthetic data with planted patterns, not a real person's data." }));
  app.append(el("p", { cls: "hero", text: A.headline }));

  // ---------- KPI row ----------
  const T = A.timing || {}, D = A.daily || {};
  const kpis = [];
  if (T.windows && T.windows.length) {
    const top = T.windows[0];
    kpis.push(["Peak window", `${top.start}–${top.end}`, `${top.label}${top.recurrence != null ? ", " + pct(top.recurrence) + " of them" : ""}`]);
  }
  if (T.high_minutes_per_day != null) kpis.push(["High-stress time", `${Math.round(T.high_minutes_per_day)} min/day`, "awake minutes at 2–3 of 3"]);
  if (D.heavy_load_days && D.heavy_load_days.length) {
    const h = D.heavy_load_days[0];
    kpis.push(["Heaviest day", ({Mon:"Monday",Tue:"Tuesday",Wed:"Wednesday",Thu:"Thursday",Fri:"Friday",Sat:"Saturday",Sun:"Sunday"})[h.load_day], `HRV ${sgn(h.hrv_vs_base_pct, 0)}% next morning`]);
  }
  if (D.n_scored) kpis.push(["High-stress mornings", `${(D.high_days || []).length} of ${D.n_scored}`, "HRV/RHR well off your baseline"]);
  if (kpis.length) app.append(el("div", { cls: "kpis" }, kpis.map(([l, v, n]) => el("div", { cls: "kpi" }, el("div", { cls: "label", text: l }), el("div", { cls: "value", text: v }), el("div", { cls: "note", text: n })))));

  // ---------- heatmap ----------
  if (T.heatmap) {
    let lo = 24, hi = -1;
    T.heatmap.forEach((row) => row.forEach((c, h) => { if (c.mean !== null) { lo = Math.min(lo, h); hi = Math.max(hi, h); } }));
    if (hi >= lo) {
      const hours = []; for (let h = lo; h <= hi; h++) hours.push(h);
      const grid = el("div", { cls: "heat", style: `grid-template-columns: 34px repeat(${hours.length}, minmax(0, 1fr))`, role: "img", "aria-label": "Stress by weekday and hour" });
      grid.append(el("div"));
      hours.forEach((h) => grid.append(el("div", { cls: "collab", text: h % 3 === 0 ? String(h).padStart(2, "0") : "" })));
      T.heatmap.forEach((row, d) => {
        grid.append(el("div", { cls: "rowlab", text: DAYS[d] }));
        hours.forEach((h) => {
          const c = row[h];
          const cell = el("div", { cls: "cell", style: `background:${heatColor(c.mean)}` });
          hover(cell, c.mean === null
            ? [`${DAYS[d]} ${hh(h)}`, c.minutes ? `only ${c.minutes} min of data` : "no data (asleep or not worn)"]
            : [`${fmt(c.mean)} / 3`, `${DAYS[d]} ${hh(h)}–${hh((h + 1) % 24)}`, `${fmt(c.high_pct, 0)}% of minutes high · ${c.minutes} min of data`]);
          grid.append(cell);
        });
      });
      const legend = el("div", { cls: "legend" }, el("span", { text: "calm 0" }),
        el("span", { cls: "ramp" }, [0, 1, 2, 3, 4, 5, 6, 7].map((i) => el("span", { style: `background:var(--h${i})` }))),
        el("span", { text: "3 high" }), el("span", { cls: "muted", text: "· gray = asleep, exercising or no data" }));
      const rows = [];
      T.heatmap.forEach((row, d) => row.forEach((c, h) => { if (c.mean !== null) rows.push([DAYS[d], `${hh(h)}–${hh((h + 1) % 24)}`, fmt(c.mean, 2), fmt(c.high_pct, 0) + "%", String(c.minutes)]); }));
      app.append(card("Stress by hour and weekday",
        A.intraday_method ? `Score 0–3 per awake minute: ${A.intraday_method}. Hover or tab through the cells for values.` : null,
        grid, legend,
        el("details", {}, el("summary", { text: "Table view" }), table(["Day", "Hour", "Mean score", "High", "Minutes"], rows, [0, 0, 1, 1, 1]))));
    }
  }

  // ---------- hour profile + windows ----------
  if (T.hour_profile) {
    const wins = T.windows || [];
    const inWin = (h) => wins.slice(0, 2).some((x) => h * 60 < x.end_min && (h + 1) * 60 > x.start_min);
    const W = CW, H = 190, L = 28, B = 22, top = 10, bw = Math.min(24, (W - L) / 24 - 4);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Average stress by hour of day" });
    const y = (v) => top + (H - B - top) * (1 - v / 3);
    [0, 1, 2, 3].forEach((t) => {
      s.append(svg("line", { x1: L, x2: W, y1: y(t), y2: y(t), stroke: t === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }));
      s.append(svg("text", { x: L - 8, y: y(t) + 4, "text-anchor": "end", text: String(t) }));
    });
    T.hour_profile.forEach((p) => {
      const cx = L + ((W - L) / 24) * (p.hour + 0.5);
      if (p.hour % 3 === 0) s.append(svg("text", { x: cx, y: H - 6, "text-anchor": "middle", text: String(p.hour).padStart(2, "0") }));
      if (p.mean === null) return;
      const h = Math.max(1, y(0) - y(p.mean)), r = Math.min(4, h / 2);
      const x0 = cx - bw / 2, y0 = y(p.mean);
      const path = svg("path", { d: `M${x0},${y(0)} V${y0 + r} Q${x0},${y0} ${x0 + r},${y0} H${x0 + bw - r} Q${x0 + bw},${y0} ${x0 + bw},${y0 + r} V${y(0)} Z`, fill: inWin(p.hour) ? "var(--accent)" : "var(--deemph)" });
      const hit = svg("rect", { x: cx - (W - L) / 48, y: top, width: (W - L) / 24, height: H - B - top, fill: "transparent" });
      const g = svg("g", {}); g.append(path, hit);
      hover(g, [`${fmt(p.mean, 2)} / 3`, `${hh(p.hour)}–${hh((p.hour + 1) % 24)}, all days`, `${fmt(p.high_pct, 0)}% of minutes high`]);
      s.append(g);
    });
    const kids = [s, el("div", { cls: "legend" }, el("span", {}, el("span", { cls: "swatch", style: "background:var(--accent)" }), "your top stress windows"), el("span", {}, el("span", { cls: "swatch", style: "background:var(--deemph)" }), "other hours"))];
    if (wins.length) {
      kids.push(el("h2", { style: "margin-top:16px", text: "Your stress windows" }));
      kids.push(el("div", {}, wins.map((x) => {
        const cal = (x.calendar || []).filter((c) => c.count >= 2).map((c) => `${c.title} (${c.count}×)`).join(", ");
        return el("div", { cls: "win" },
          el("div", { cls: "win-head" }, el("b", { text: `${x.label} ${x.start}–${x.end}` }),
            x.days_covered ? el("span", { cls: "pill", text: `${x.days_hit} of ${x.days_covered} days` }) : null),
          el("div", { cls: "win-meta", text: `${fmt(x.mean_score)}/3 vs ${fmt(x.type_mean)} usual` + (x.excess_bpm != null ? ` · ${sgn(x.excess_bpm, 0)} bpm over your awake baseline` : "") }),
          cal ? el("div", { cls: "win-cal", text: `Usually on your calendar: ${cal}` }) : null);
      })));
    }
    if ((T.calm_windows || []).length) {
      kids.push(el("p", { cls: "lede", style: "margin-top:10px", text: "Calmest stretches (good for deep work or hard conversations): " + T.calm_windows.map((x) => `${x.label} ${x.start}–${x.end} (${fmt(x.mean_score)})`).join(" · ") }));
    }
    app.append(card("Average stress by hour", "All days combined. The blue bars are the hours inside your top stress windows.", ...kids));
  }

  // ---------- calendar ----------
  const C = A.calendar || {};
  if ((C.stressful || []).length || (C.calming || []).length) {
    const kids = [];
    if ((C.stressful || []).length) kids.push(table(["Event", "Times", "Score", "vs that day", "10+ high min"],
      C.stressful.map((e) => [e.title, String(e.n), fmt(e.mean_score), sgn(e.mean_lift), pct(e.high_share)]), [0, 1, 1, 1, 1]));
    if ((C.calming || []).length) {
      kids.push(el("p", { cls: "lede", style: "margin-top:12px", text: "Calmer than your day: " + C.calming.map((e) => `${e.title} (${sgn(e.mean_lift)})`).join(", ") }));
    }
    const lede = C.meeting_load_rho != null ? `Across the workday, stress ${C.meeting_load_rho >= 0.3 ? "tracks" : C.meeting_load_rho <= -0.3 ? "runs opposite to" : "barely follows"} how many meetings you have (rank correlation ${sgn(C.meeting_load_rho, 2)}).` : null;
    app.append(card("What's on your calendar when it spikes", lede, ...kids));
  }

  // ---------- daily: weekday carry-over + trend ----------
  if (D.weekday_pattern && D.n_scored) {
    const P = D.weekday_pattern;
    const W = CW, rowH = 26, H = rowH * 7 + 8, L = Math.min(90, Math.round(CW * 0.2));
    const maxAbs = Math.max(0.5, ...P.map((p) => Math.abs(p.mean_index || 0)));
    const sx = (v) => ((W - L - 60) / 2) * (v / maxAbs);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Next-morning stress index by the day before" });
    const cx = L + (W - L - 60) / 2;
    s.append(svg("line", { x1: cx, x2: cx, y1: 0, y2: H, stroke: "var(--axis)", "stroke-width": 1 }));
    P.forEach((p, i) => {
      const yy = 4 + i * rowH;
      s.append(svg("text", { x: 0, y: yy + 16, text: `after ${p.load_day}` }));
      if (p.mean_index === null) return;
      const v = p.mean_index, len = Math.abs(sx(v)), x0 = v >= 0 ? cx : cx - len, r = Math.min(4, len / 2);
      const d = v >= 0
        ? `M${x0},${yy + 5} H${x0 + len - r} Q${x0 + len},${yy + 5} ${x0 + len},${yy + 5 + r} V${yy + 19 - r} Q${x0 + len},${yy + 19} ${x0 + len - r},${yy + 19} H${x0} Z`
        : `M${x0 + len},${yy + 5} H${x0 + r} Q${x0},${yy + 5} ${x0},${yy + 5 + r} V${yy + 19 - r} Q${x0},${yy + 19} ${x0 + r},${yy + 19} H${x0 + len} Z`;
      const g = svg("g", {});
      g.append(svg("path", { d, fill: v >= 0 ? "var(--pos)" : "var(--neg)" }));
      g.append(svg("rect", { x: L, y: yy, width: W - L, height: rowH, fill: "transparent" }));
      g.append(svg("text", { class: "val", x: v >= 0 ? cx + len + 6 : cx - len - 6, y: yy + 16, "text-anchor": v >= 0 ? "start" : "end", text: sgn(v) }));
      hover(g, [`${sgn(v, 2)} stress index`, `mornings after ${p.load_day} (${p.n} days)`, `HRV ${sgn(p.hrv_vs_base_pct, 0)}% vs your baseline`, `${pct(p.high_share)} of them high-stress`]);
      s.append(g);
    });
    const kids = [s, el("div", { cls: "legend" }, el("span", {}, el("span", { cls: "swatch", style: "background:var(--pos)" }), "more stressed than usual"), el("span", {}, el("span", { cls: "swatch", style: "background:var(--neg)" }), "calmer than usual"))];

    const days = (D.days || []).filter((r) => r.index !== null).slice(CW < 500 ? -60 : -120);
    if (days.length >= 7) {
      const W2 = CW, H2 = 150, top = 8, B = 20, n = days.length, step = (W2 - 28) / n, bw = Math.max(2, Math.min(12, step - 2));
      const m = Math.max(1.5, ...days.map((r) => Math.abs(r.index)));
      const y0 = top + (H2 - top - B) / 2, sc = (H2 - top - B) / 2 / m;
      const s2 = svg("svg", { viewBox: `0 0 ${W2} ${H2}`, role: "img", "aria-label": "Daily stress index" });
      s2.append(svg("line", { x1: 28, x2: W2, y1: y0, y2: y0, stroke: "var(--axis)", "stroke-width": 1 }));
      [-1, 1].forEach((t) => { if (Math.abs(t) <= m) { s2.append(svg("line", { x1: 28, x2: W2, y1: y0 - t * sc, y2: y0 - t * sc, stroke: "var(--grid)", "stroke-width": 1 })); s2.append(svg("text", { x: 22, y: y0 - t * sc + 4, "text-anchor": "end", text: t > 0 ? "+1" : "−1" })); } });
      days.forEach((r, i) => {
        const x = 28 + i * step + (step - bw) / 2, h = Math.max(1, Math.abs(r.index) * sc);
        const g = svg("g", {});
        g.append(svg("rect", { x, y: r.index >= 0 ? y0 - h : y0, width: bw, height: h, rx: Math.min(2, bw / 2), fill: r.index >= 0 ? "var(--pos)" : "var(--neg)" }));
        g.append(svg("rect", { x: 28 + i * step, y: top, width: step, height: H2 - top - B, fill: "transparent" }));
        hover(g, [`${sgn(r.index, 2)} stress index`, `${r.weekday} ${r.date}`, `HRV ${fmt(r.hrv, 0)} ms (${sgn(r.hrv_vs_base_pct, 0)}%) · RHR ${fmt(r.rhr, 0)}`, r.recovery != null ? `recovery ${fmt(r.recovery, 0)}%` : ""]);
        s2.append(g);
        if (i === 0 || i === n - 1 || (n > 20 && i % Math.ceil(n / 6) === 0 && i < n - 4)) s2.append(svg("text", { x: x + bw / 2, y: H2 - 4, "text-anchor": i === n - 1 ? "end" : "middle", text: r.date.slice(5) }));
      });
      kids.push(el("h2", { style: "margin-top:16px", text: "Morning by morning" }), s2);
      kids.push(el("details", {}, el("summary", { text: "Table view" }), table(["Date", "Stress index", "HRV", "vs baseline", "RHR", "Recovery"],
        days.slice().reverse().map((r) => [`${r.weekday} ${r.date}`, sgn(r.index, 2), fmt(r.hrv, 0), sgn(r.hrv_vs_base_pct, 0) + "%", fmt(r.rhr, 0), r.recovery != null ? fmt(r.recovery, 0) + "%" : "–"]), [0, 1, 1, 1, 1, 1])));
    }
    app.append(card("Which days carry over into the next morning",
      "WHOOP measures you in your sleep, so each morning's HRV and resting heart rate reflect the day before. Index = how far they sit from your own 28-day baseline (0 = normal, +1 = clearly strained).",
      ...kids));
  }

  // ---------- nights ----------
  const N = A.sleep_hr || {}, S = D.sleep || {};
  if (N.nights || S.median_bedtime) {
    const kids = [];
    const facts = [];
    if (S.median_bedtime) facts.push(["Typical bedtime", S.median_bedtime + (S.bedtime_spread_min != null ? ` ± ${S.bedtime_spread_min} min` : "")]);
    if (S.weekday_bedtime && S.weekend_bedtime) facts.push(["Weekdays / weekends", `${S.weekday_bedtime} / ${S.weekend_bedtime}`]);
    if (S.mean_performance != null) facts.push(["Sleep performance", `${S.mean_performance}% avg · ${S.short_nights} nights under 70%`]);
    if (S.bedtime_vs_stress_rho != null) facts.push(["Later bedtime → stress", `rank correlation ${sgn(S.bedtime_vs_stress_rho, 2)}`]);
    if (N.nights) facts.push(["Heart rate bottoms out late", `${pct(N.late_nadir_share)} of ${N.nights} nights`]);
    kids.push(table(["", ""], facts));
    if ((N.by_evening || []).length) {
      kids.push(el("p", { cls: "lede", style: "margin-top:12px", text: "Share of nights whose heart-rate low came in the last third of sleep, by the evening before:" }));
      kids.push(table(["Evening", "Nights", "Late low", "First hour vs night low"], N.by_evening.map((e) => [e.evening, String(e.n), pct(e.late_share), `${fmt(e.first_hour_excess)} bpm`]), [0, 1, 1, 1]));
    }
    app.append(card("Evenings and nights", "A heart rate that stays up after you fall asleep is the evening still working on you: stress, a late meal, alcohol or a late workout.", ...kids));
  }

  // ---------- journal ----------
  if ((A.journal || []).length) {
    app.append(card("Habits from your WHOOP Journal", "Same-night effect: mornings after answering yes vs no. |t| ≥ 2 is unlikely to be chance; these are still associations, not proof.",
      table(["Question", "Yes / no", "Stress index", "HRV", "t"], A.journal.map((j) => [j.question, `${j.n_yes} / ${j.n_no}`, sgn(j.index_diff, 2), sgn(j.hrv_diff_pct, 0) + "%", fmt(j.t)]), [0, 1, 1, 1, 1])));
  }

  // ---------- episodes ----------
  if ((T.episodes_recent || []).length) {
    const eps = T.episodes_recent.slice(-10).reverse();
    app.append(card(`Recent high-stress stretches (${T.episodes_count} in total)`, "10+ minutes at 2–3 of 3.",
      table(["When", "Minutes", "Peak", "Heart rate", "Calendar"], eps.map((e) => [`${e.weekday} ${e.start.slice(0, 10)} ${e.start.slice(11, 16)}–${e.end.slice(11, 16)}`, String(e.minutes), fmt(e.peak_score), e.excess_bpm != null ? sgn(e.excess_bpm, 0) + " bpm" : "–", (e.events || []).join(", ") || "–"]), [0, 1, 1, 1, 0])));
  }

  // ---------- plan ----------
  const R = A.recommendations || [];
  if (R.length) {
    const sec = el("section", {}, el("h2", { style: "margin:24px 0 10px", text: "Your plan" }));
    const list = el("div", { cls: "recs" });
    R.forEach((r) => list.append(el("article", { cls: "rec" }, el("h3", {}, el("span", { text: r.title })), el("p", { cls: "why", text: r.why }),
      el("ul", {}, (r.try || []).map((t) => el("li", { text: t }))), el("p", { cls: "measure", text: "Measure: " + r.measure }))));
    sec.append(list);
    sec.append(card("Run it as an experiment", null, el("p", { style: "margin:0", text: "Pick one change (start with #1), do it every day for 14 days, keep wearing WHOOP, then re-run the analysis with --since-change set to your start date. One change at a time is how you learn what actually works for you." })));
    app.append(sec);
  }

  // ---------- before / after ----------
  const X = A.change;
  if (X && (X.daily || X.intraday)) {
    const rows = [];
    if (X.daily) {
      rows.push(["HRV (ms)", fmt(X.daily.hrv_before), fmt(X.daily.hrv_after), sgn(X.daily.hrv_change_pct) + "%"]);
      rows.push(["Resting HR", fmt(X.daily.rhr_before), fmt(X.daily.rhr_after), sgn(X.daily.rhr_after - X.daily.rhr_before)]);
      if (X.daily.recovery_before != null) rows.push(["Recovery %", fmt(X.daily.recovery_before, 0), fmt(X.daily.recovery_after, 0), sgn(X.daily.recovery_after - X.daily.recovery_before, 0)]);
    }
    if (X.intraday) {
      rows.push(["Awake stress score", fmt(X.intraday.mean_before, 2), fmt(X.intraday.mean_after, 2), sgn(X.intraday.mean_after - X.intraday.mean_before, 2)]);
      rows.push(["High-stress min/day", fmt(X.intraday.high_min_per_day_before, 0), fmt(X.intraday.high_min_per_day_after, 0), sgn(X.intraday.high_min_per_day_after - X.intraday.high_min_per_day_before, 0)]);
      (X.intraday.windows || []).forEach((wv) => rows.push([wv.window, fmt(wv.before, 2), fmt(wv.after, 2), sgn(wv.after - wv.before, 2)]));
    }
    app.append(card(`Before vs after ${X.since}`, "Intraday scores for both periods use your pre-change baseline, so improvements aren't hidden by a moving baseline.", table(["", "Before", "After", "Change"], rows, [0, 1, 1, 1])));
  }

  // ---------- data & method ----------
  const srcRows = [];
  if (w) srcRows.push(["WHOOP", `${w.kind === "whoop_api" ? "Developer API" : w.kind === "whoop_export" ? "app export" : w.kind}: ${w.days} days, ${w.workouts} workouts${w.sleep_hr_nights ? ", " + w.sleep_hr_nights + " nights of sleep heart rate" : ""}${w.journal_answers ? ", " + w.journal_answers + " journal answers" : ""}`]);
  if (hr) srcRows.push(["Minute heart rate", `${hr.minutes.toLocaleString()} minutes, ${hr.from} – ${hr.to}; removed: ${Object.entries(hr.excluded_minutes || {}).map(([k, v]) => `${v.toLocaleString()} ${k}`).join(", ") || "none"}`]);
  if (src.stress_log) srcRows.push(["Stress log", `${src.stress_log.minutes.toLocaleString()} minutes`]);
  if (src.calendar) srcRows.push(["Calendar", `${src.calendar.events} events`]);
  const notes = (A.notes || []).map((n) => el("li", { text: n }));
  app.append(card("Data and method", null, table(["Source", "What was used"], srcRows),
    notes.length ? el("ul", { style: "margin:10px 0 0; padding-left:20px; color:var(--ink2); font-size:13px" }, notes) : null));
  app.append(el("footer", { text: `Generated ${A.generated_at} by whoop-stress-tracker. Wellness insight from your own wearable data, not medical advice; see a doctor about persistent stress, chest pain or palpitations.` }));
})();
</script>
</body>
</html>
"""


def build_html(analysis: dict) -> str:
    data = json.dumps(analysis, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", data)


def build_markdown(a: dict) -> str:
    out = ["# When you're stressed", "", f"**{a.get('headline', '')}**", ""]
    if a.get("demo"):
        out += ["> Demo: synthetic data with planted patterns.", ""]
    t = a.get("timing") or {}
    if t.get("windows"):
        out.append("## Stress windows")
        for w in t["windows"]:
            cal = ", ".join(c["title"] for c in w.get("calendar", []) if c["count"] >= 2)
            how = f"{w['days_hit']} of {w['days_covered']} days" if w.get("days_covered") else ""
            bpm = f", {w['excess_bpm']:+.0f} bpm" if w.get("excess_bpm") is not None else ""
            out.append(
                f"- **{w['label']} {w['start']}-{w['end']}**: {w['mean_score']:.1f}/3 vs {w['type_mean']:.1f} usual"
                + (f" ({how}{bpm})" if how else "")
                + (f"; usually on your calendar: {cal}" if cal else "")
            )
        out.append("")
    d = a.get("daily") or {}
    if d.get("heavy_load_days"):
        out.append("## Days that carry over")
        for h in d["heavy_load_days"]:
            name = WEEKDAYS_LONG[WEEKDAYS.index(h["load_day"])]
            out.append(f"- After **{name}s**: HRV {h['hrv_vs_base_pct']:+.0f}% vs baseline next morning (index {h['mean_index']:+.1f}, n={h['n']})")
        out.append("")
    c = a.get("calendar") or {}
    if c.get("stressful"):
        out.append("## Meetings that spike it")
        for e in c["stressful"][:4]:
            out.append(f"- {e['title']}: {e['mean_lift']:+.1f} above that day's average across {e['n']} occurrences")
        out.append("")
    if a.get("recommendations"):
        out.append("## Plan")
        for i, r in enumerate(a["recommendations"], 1):
            first = (r.get("try") or [""])[0]
            out.append(f"{i}. **{r['title']}**: {r['why']} Try: {first}")
        out.append("")
    ch = a.get("change") or {}
    if ch.get("daily") or ch.get("intraday"):
        out.append(f"## Since {ch['since']}")
        if ch.get("daily"):
            dd = ch["daily"]
            out.append(f"- HRV {dd['hrv_before']} -> {dd['hrv_after']} ms ({dd['hrv_change_pct']:+.1f}%), resting HR {dd['rhr_before']} -> {dd['rhr_after']}")
        if ch.get("intraday"):
            ii = ch["intraday"]
            out.append(f"- High-stress minutes/day {ii['high_min_per_day_before']} -> {ii['high_min_per_day_after']}")
            for wv in ii.get("windows", []):
                out.append(f"- {wv['window']}: {wv['before']} -> {wv['after']}")
        out.append("")
    notes = a.get("notes") or []
    if notes:
        out.append("## Caveats")
        out += [f"- {n}" for n in notes]
        out.append("")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("analysis", help="analysis.json from stress_analysis.py")
    ap.add_argument("--html", help="output HTML (default: next to the analysis)")
    ap.add_argument("--md", help="output Markdown summary (default: next to the analysis)")
    args = ap.parse_args(argv)
    a = read_json(args.analysis)
    base = Path(args.analysis).parent
    html_path = Path(args.html) if args.html else base / "stress_report.html"
    md_path = Path(args.md) if args.md else base / "stress_summary.md"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(build_html(a), encoding="utf-8")
    md_path.write_text(build_markdown(a), encoding="utf-8")
    if not a.get("demo"):  # real health data: owner-only, like the analysis JSON
        for p in (html_path, md_path):
            os.chmod(p, 0o600)
    print(f"wrote {html_path}\nwrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
