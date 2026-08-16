"""Popcorn Check — FastAPI LAN app for Rotten Tomatoes scores + YouTube TV.

Routes:
  GET  /             Rotten Tomatoes lookup page
  GET  /yt           YouTube TV movie browser page
  GET  /api/lookup   ?q=<title> -> RT search candidates
  GET  /api/movie    ?slug=<slug> -> full RT scorecard
  GET  /api/yttv     ?query=&limit=&offset= -> YTTV catalog + ratings

Both HTML pages are embedded here and are mobile-first for household phone
use on the LAN. Design system: warm "cinema" dark palette, one amber accent,
tomato-red + butter-yellow score meters, system type, >=44px hit targets.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

import rt
import ytv as ytv_module

app = FastAPI(title="Popcorn Check")

# ---------------------------------------------------------------------------
# Shared design shell
# ---------------------------------------------------------------------------

_CSS = """
:root {
  color-scheme: dark;
  --bg: #0e0c0a;
  --bg-grad: radial-gradient(1200px 500px at 50% -120px, #1d1813 0%, #0e0c0a 60%);
  --surface: #191613;
  --surface-2: #221d18;
  --border: #2b251f;
  --border-strong: #3a322a;
  --text: #f0ebe1;
  --text-muted: #a49b8f;
  --text-faint: #6f675c;
  --accent: #e3b04b;
  --accent-ink: #1a1206;
  --tomato: #e5484d;
  --butter: #e3b04b;
  --fresh: #3fae5c;
  --rotten: #e5484d;
  --radius: 14px;
  --radius-sm: 10px;
  --shadow: 0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px -12px rgba(0,0,0,.6);
  --font: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-display: "Iowan Old Style", Georgia, "Times New Roman", serif;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  font-family: var(--font);
  background: var(--bg);
  background-image: var(--bg-grad);
  background-attachment: fixed;
  color: var(--text);
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

.wrap { max-width: 820px; margin: 0 auto; padding: 0 18px 72px; }

/* Top bar */
.topbar {
  position: sticky; top: 0; z-index: 10;
  backdrop-filter: blur(12px);
  background: color-mix(in srgb, var(--bg) 82%, transparent);
  border-bottom: 1px solid var(--border);
}
.topbar-inner {
  max-width: 820px; margin: 0 auto; padding: 10px 18px;
  display: flex; align-items: center; gap: 14px;
}
.brand {
  display: flex; align-items: center; gap: 9px;
  font-family: var(--font-display);
  font-weight: 600; font-size: 1.1rem; letter-spacing: -.01em;
  white-space: nowrap;
}
.brand .mark {
  width: 26px; height: 26px; border-radius: 8px;
  background: linear-gradient(135deg, var(--tomato), var(--butter));
  display: grid; place-items: center; font-size: 13px;
  box-shadow: 0 2px 6px rgba(0,0,0,.4);
}
nav { display: flex; gap: 4px; margin-left: auto; }
nav a {
  text-decoration: none; color: var(--text-muted);
  font-size: .92rem; font-weight: 600;
  padding: 8px 12px; border-radius: 999px; min-height: 40px;
  display: inline-flex; align-items: center;
  transition: color .15s, background .15s;
}
nav a:hover { color: var(--text); background: var(--surface-2); }
nav a.active { color: var(--accent-ink); background: var(--accent); }

h1 { font-family: var(--font-display); font-size: 1.75rem; font-weight: 600; letter-spacing: -.01em; margin: 22px 0 4px; }
.lede { color: var(--text-muted); margin: 0 0 20px; font-size: 1.05rem; }

/* Controls */
input[type="search"] {
  width: 100%; font-size: 1.05rem; color: var(--text);
  background: var(--surface-2); border: 1px solid var(--border-strong);
  border-radius: var(--radius); padding: 13px 16px;
  min-height: 48px;
}
input[type="search"]::placeholder { color: var(--text-faint); }
input[type="search"]:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 1px; border-color: transparent;
}

.btn {
  font-family: var(--font); font-size: 1rem; font-weight: 600;
  background: var(--accent); color: var(--accent-ink);
  border: 0; border-radius: var(--radius); padding: 13px 20px; min-height: 48px;
  cursor: pointer; transition: filter .15s, transform .05s;
}
.btn:hover { filter: brightness(1.07); }
.btn:active { transform: translateY(1px); }
.btn:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
.btn.ghost { background: var(--surface-2); color: var(--text); border: 1px solid var(--border-strong); }

.searchrow { display: flex; gap: 10px; }
.searchrow input { flex: 1; }

/* Cards */
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 18px; box-shadow: var(--shadow);
}
.muted { color: var(--text-muted); }
.small { font-size: .86rem; }

/* Score meters */
.scores { display: flex; gap: 10px; margin: 14px 0 4px; flex-wrap: wrap; }
.meter {
  flex: 1 1 120px; min-width: 120px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 12px 14px;
}
.meter .k { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--text-faint); font-weight: 600; }
.meter .v { font-size: 1.9rem; font-weight: 800; letter-spacing: -.02em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.meter .v .pct { font-size: .9rem; font-weight: 600; color: var(--text-faint); }
.meter.tomato .v { color: var(--tomato); }
.meter.butter .v { color: var(--butter); }
.meter .sub { font-size: .76rem; color: var(--text-faint); margin-top: 2px; }

.pos { color: var(--fresh); }
.neg { color: var(--rotten); }

/* Movie poster + header */
.hero { display: flex; gap: 16px; }
.poster {
  flex: 0 0 auto; width: 84px; height: 126px; object-fit: cover;
  border-radius: var(--radius-sm); border: 1px solid var(--border);
  background: var(--surface-2); box-shadow: var(--shadow);
}
.hero .info { min-width: 0; }
.hero h2 { margin: 0 0 2px; font-size: 1.25rem; font-weight: 700; letter-spacing: -.01em; line-height: 1.25; }
.genres { color: var(--text-muted); font-size: .88rem; margin-top: 4px; }

/* Synopsis */
details { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 10px; }
summary {
  min-height: 44px; display: flex; align-items: center; gap: 8px;
  font-weight: 600; font-size: .98rem; cursor: pointer; color: var(--text);
  list-style: none; user-select: none;
}
summary::-webkit-details-marker { display: none; }
summary::before {
  content: "▸"; color: var(--accent); font-size: .9rem;
  transition: transform .15s; flex: 0 0 auto;
}
details[open] summary::before { transform: rotate(90deg); }
details p { margin: 10px 0 0; color: var(--text-muted); font-size: .95rem; }

/* Explore grid */
.grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
@media (min-width: 620px) { .grid { grid-template-columns: 1fr 1fr; } }

/* Toolbar: genre filter + sort */
.toolbar { display: flex; flex-direction: column; gap: 12px; margin: 16px 0 0; }
.toolbar > * { width: 100%; }
.eyebrow { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); font-weight: 700; margin: 0 0 6px; }
@media (min-width: 620px) {
  .toolbar { flex-direction: row; align-items: flex-start; }
  .toolbar > * { width: auto; }
  .sortgroup { flex: 0 0 auto; }
}
.dropdown { position: relative; flex: 1; }
.dd-toggle { width: 100%; justify-content: space-between; display: flex; align-items: center; gap: 8px; }
.dd-right { display: flex; align-items: center; gap: 8px; }
.caret { color: var(--text-muted); font-size: .88rem; line-height: 1; }
.dd-toggle .count {
  background: var(--surface-2); border: 1px solid var(--border-strong);
  border-radius: 999px; padding: 0 9px; font-size: .88rem; font-weight: 700;
  color: var(--accent); min-height: 20px; display: inline-flex; align-items: center;
}
.dd-panel {
  position: absolute; z-index: 20; top: calc(100% + 6px); left: 0; right: 0;
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden;
}
.dd-list { max-height: 320px; overflow-y: auto; padding: 6px; }
.dd-item {
  display: flex; align-items: center; gap: 10px; padding: 10px 12px;
  border-radius: var(--radius-sm); cursor: pointer; min-height: 44px;
}
.dd-item:hover { background: var(--surface-2); }
.dd-item input[type="checkbox"] { width: 18px; height: 18px; accent-color: var(--accent); flex: 0 0 auto; margin: 0; }
.dd-item .gname { flex: 1; font-size: .95rem; }
.dd-item .gcount { color: var(--text-muted); font-size: .9rem; font-variant-numeric: tabular-nums; }
.dd-foot { display: flex; gap: 8px; padding: 8px; border-top: 1px solid var(--border); }
.dd-foot .btn { flex: 1; padding: 9px 12px; min-height: 40px; font-size: .9rem; }

/* Sort segmented control */
.seg {
  display: flex; background: var(--surface-2); border: 1px solid var(--border-strong);
  border-radius: var(--radius); padding: 4px; gap: 4px; width: 100%;
}
.seg button {
  flex: 1; font-family: var(--font); font-size: .88rem; font-weight: 600;
  color: var(--text-muted); background: transparent; border: 0;
  border-radius: calc(var(--radius) - 4px); padding: 9px 12px; min-height: 40px;
  cursor: pointer; transition: color .15s, background .15s; white-space: nowrap;
}
.seg button:hover { color: var(--text); }
.seg button.active { background: var(--accent); color: var(--accent-ink); }

/* Selected genre chips */
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.chips:empty { display: none; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--surface-2); border: 1px solid var(--border-strong);
  border-radius: 999px; padding: 4px 8px 4px 12px; font-size: .92rem; font-weight: 600;
}
.chip button {
  border: 0; background: transparent; color: var(--text-faint); cursor: pointer;
  font-size: 1rem; line-height: 1; padding: 2px 5px; border-radius: 999px;
}
.chip button:hover { color: var(--text); background: var(--border); }

/* Results bar */
.resultbar { display: flex; align-items: center; justify-content: space-between; color: var(--text-muted); font-size: .92rem; margin: 12px 0 10px; }

/* Movie cards */
.tile {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 10px;
  transition: border-color .15s, transform .1s;
}
.tile:hover { border-color: var(--border-strong); }
.tile .top { display: flex; gap: 12px; }
.tile .poster {
  width: 66px; height: 99px; object-fit: cover; flex: 0 0 auto;
  border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface-2);
}
.poster-placeholder {
  display: grid; place-items: center; padding: 8px; text-align: center;
  color: var(--text-muted); font-size: .85rem; line-height: 1.2;
}
.tile .meta { min-width: 0; flex: 1; }
.tile h3 { margin: 0 0 2px; font-size: 1.125rem; font-weight: 700; letter-spacing: -.01em; line-height: 1.3; }
.tile .year { color: var(--text-muted); font-weight: 500; font-size: .9rem; letter-spacing: .01em; }
.tile .genres { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.gchip {
  font-size: .8rem; font-weight: 600; color: var(--text-muted);
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; padding: 2px 6px; letter-spacing: .01em;
}

/* In-card score meters */
.scores2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.smeter {
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--surface-2); padding: 9px 11px;
}
.smeter .k {
  display: flex; align-items: center; gap: 5px; font-size: .78rem;
  text-transform: uppercase; letter-spacing: .06em; color: var(--text-muted); font-weight: 700;
}
.smeter .v { font-size: 1.7rem; font-weight: 800; letter-spacing: -.02em; line-height: 1.15; font-variant-numeric: tabular-nums; }
.smeter .v .pct { font-size: .9rem; font-weight: 600; color: var(--text-muted); }
.smeter .state { font-size: .82rem; font-weight: 700; margin-top: 1px; min-height: 1em; }
.smeter.critic { border-top: 2px solid var(--tomato); }
.smeter.critic .v { color: var(--tomato); }
.smeter.popcorn { border-top: 2px solid var(--butter); }
.smeter.popcorn .v { color: var(--butter); }
.smeter .state.fresh { color: var(--fresh); }
.smeter .state.rotten { color: var(--rotten); }
.smeter .state.verified { color: var(--butter); }

.tile details { margin-top: 0; }
.tile details p { font-size: .98rem; }

.empty { color: var(--text-muted); font-size: 1rem; text-align: center; padding: 48px 0; }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
"""

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Popcorn Check</title>
<style>{css}</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <div class="brand"><span class="mark">🍿</span><span>Popcorn Check</span></div>
  <nav>
    <a href="/" id="nav-home">RT Lookup</a>
    <a href="/yt" id="nav-yt">YouTube TV</a>
  </nav>
</div></div>
<div class="wrap" id="app"></div>
<script>
const app = document.getElementById('app');
const navHome = document.getElementById('nav-home');
const navYt = document.getElementById('nav-yt');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = n => (n == null || n === '') ? null : Number(n);
const meter = (label, val, cls, sub) => `
  <div class="meter ${cls}">
    <div class="k">${label}</div>
    <div class="v">${val == null ? '—' : val}<span class="pct">${val == null ? '' : '%'}</span></div>
    ${sub ? `<div class="sub">${sub}</div>` : ''}
  </div>`;
</script>
</body>
</html>
"""

_RT_PAGE_JS = r"""
navHome.classList.add('active');
app.innerHTML = `
  <h1>Rotten Tomatoes lookup</h1>
  <p class="lede">Search a movie to see its Tomatometer and Popcornmeter scores.</p>
  <form class="searchrow" id="lookup" onsubmit="return doLookup(event)">
    <input type="search" id="q" placeholder="Movie title…" autocomplete="off" autofocus>
    <button class="btn" type="submit">Look up</button>
  </form>
  <div id="results" style="margin-top:18px"></div>`;

const results = document.getElementById('results');

async function doLookup(e) {
  e.preventDefault();
  const q = document.getElementById('q').value.trim();
  if (!q) return false;
  results.innerHTML = '<p class="muted">Searching…</p>';
  try {
    const r = await fetch('/api/lookup?q=' + encodeURIComponent(q));
    const data = await r.json();
    if (!data || !data.length) { results.innerHTML = '<p class="empty">No matches.</p>'; return false; }
    const pick = data[0];
    const m = await fetch('/api/movie?slug=' + encodeURIComponent(pick.slug));
    const movie = await m.json();
    results.innerHTML = renderMovie(movie);
  } catch (e) {
    results.innerHTML = '<p class="empty">Lookup failed. Try again.</p>';
  }
  return false;
}

function renderMovie(m) {
  if (!m || !m.title) return '<p class="empty">No scorecard found.</p>';
  const t = pct(m.tomatometer), p = pct(m.popcornmeter);
  const sentiment = m.tomatometer_sentiment || '';
  const fresh = t == null ? '' : (t >= 60 ? 'Fresh' : 'Rotten');
  const genres = (m.genres || []).join(' · ');
  const meta = [m.year, m.runtime].filter(Boolean).join(' · ');
  return `
    <div class="card">
      <div class="hero">
        ${m.poster ? `<img class="poster" src="${esc(m.poster)}" alt="">` : ''}
        <div class="info">
          <h2>${esc(m.title)}</h2>
          ${meta ? `<div class="muted small">${esc(meta)}</div>` : ''}
          ${genres ? `<div class="genres">${esc(genres)}</div>` : ''}
        </div>
      </div>
      <div class="scores">
        ${meter('Tomatometer', t, 'tomato', fresh ? `${esc(fresh)} · ${esc(sentiment)}` : '')}
        ${meter('Popcornmeter', p, 'butter', m.audience_sentiment || '')}
      </div>
      ${m.synopsis ? `<details><summary>Movie summary</summary><p>${esc(m.synopsis)}</p></details>` : ''}
    </div>`;
}
"""

_YT_PAGE_JS = r"""
navYt.classList.add('active');
app.innerHTML = `
  <h1>YouTube TV movies</h1>
  <p class="lede">Every movie on YouTube TV, with Rotten Tomatoes critics and popcorn scores.</p>
  <input type="search" id="q" placeholder="Filter titles…" autocomplete="off">
  <div class="toolbar">
    <div class="dropdown">
      <button class="btn ghost dd-toggle" id="genre-toggle">
        <span>Genres</span><span class="dd-right"><span class="count" id="genre-count">All</span><span class="caret">▾</span></span>
      </button>
      <div class="dd-panel" id="genre-panel" hidden>
        <div class="dd-list" id="genre-list"></div>
        <div class="dd-foot">
          <button class="btn ghost" id="genre-clear">Clear</button>
          <button class="btn" id="genre-done">Done</button>
        </div>
      </div>
    </div>
    <div class="sortgroup">
      <div class="eyebrow">Sort by</div>
      <div class="seg" id="sort-seg">
        <button data-sort="popular" class="active">Popular</button>
        <button data-sort="critic">Critics</button>
        <button data-sort="popcorn">Popcorn</button>
      </div>
    </div>
  </div>
  <div class="chips" id="chips"></div>
  <div class="resultbar" id="resultbar" aria-live="polite">Loading movies…</div>
  <div class="grid" id="grid"><p class="empty" role="status">Loading YouTube TV movies…</p></div>
  <button class="btn ghost" id="more" style="width:100%;margin-top:16px;display:none">Show more</button>`;

const qEl = document.getElementById('q');
const genreToggle = document.getElementById('genre-toggle');
const genrePanel = document.getElementById('genre-panel');
const genreListEl = document.getElementById('genre-list');
const genreCountEl = document.getElementById('genre-count');
const chipsEl = document.getElementById('chips');
const sortSeg = document.getElementById('sort-seg');
const grid = document.getElementById('grid');
const resultbar = document.getElementById('resultbar');
const more = document.getElementById('more');
const PAGE = 200;

let all = [];
let genres = [];          // [name, count][], sorted alphabetically
let selected = new Set();
let sortMode = 'popular';
let visible = PAGE;

async function init() {
  resultbar.textContent = 'Loading movies…';
  grid.innerHTML = '<p class="empty" role="status">Loading YouTube TV movies…</p>';
  try {
    const r = await fetch('/api/yttv');
    if (!r.ok) throw new Error(`Request failed: ${r.status}`);
    all = await r.json();
    const counts = new Map();
    for (const x of all) for (const g of (x.genres || [])) counts.set(g, (counts.get(g) || 0) + 1);
    genres = [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    renderGenreList();
    render();
  } catch (error) {
    resultbar.textContent = '';
    grid.innerHTML = '<div class="empty" role="alert">Movies couldn’t be loaded.<br><button class="btn ghost" id="retry" type="button" style="margin-top:12px">Try again</button></div>';
    document.getElementById('retry').addEventListener('click', init);
  }
}

function renderGenreList() {
  genreListEl.innerHTML = genres.map(([g, n]) => `
    <label class="dd-item">
      <input type="checkbox" value="${esc(g)}" ${selected.has(g) ? 'checked' : ''}>
      <span class="gname">${esc(g)}</span>
      <span class="gcount">${n}</span>
    </label>`).join('');
  genreListEl.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
      updateGenreUI();
      refresh();
    });
  });
}

function updateGenreUI() {
  const n = selected.size;
  genreCountEl.textContent = n ? String(n) : 'All';
  chipsEl.innerHTML = [...selected].map(g =>
    `<span class="chip">${esc(g)}<button data-g="${esc(g)}" aria-label="Remove ${esc(g)}">×</button></span>`
  ).join('');
  chipsEl.querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => {
      selected.delete(b.dataset.g);
      updateGenreUI();
      renderGenreList();
      refresh();
    });
  });
}

function matches(x) {
  const q = qEl.value.trim().toLowerCase();
  if (q && !(x.title || '').toLowerCase().includes(q)) return false;
  if (selected.size && !(x.genres || []).some(g => selected.has(g))) return false;
  return true;
}

function sorted(items) {
  const arr = items.slice();
  if (sortMode === 'critic') {
    arr.sort((a, b) => (pct(b.tomatometer) ?? -1) - (pct(a.tomatometer) ?? -1));
  } else if (sortMode === 'popcorn') {
    arr.sort((a, b) => (pct(b.popcornmeter) ?? -1) - (pct(a.popcornmeter) ?? -1));
  } else {
    arr.sort((a, b) => (a.popularity ?? 1e9) - (b.popularity ?? 1e9));
  }
  return arr;
}

function refresh() { visible = PAGE; render(); }

function render() {
  const items = sorted(all.filter(matches));
  const shown = items.slice(0, visible);
  grid.innerHTML = shown.length ? shown.map(tile).join('') : '<p class="empty">No titles match.</p>';
  more.style.display = items.length > visible ? '' : 'none';
  const label = sortMode === 'critic' ? 'critics score' : sortMode === 'popcorn' ? 'popcorn score' : 'popularity';
  resultbar.textContent = `Showing ${shown.length.toLocaleString()} of ${items.length.toLocaleString()} · sorted by ${label}`;
}

function tile(x) {
  const t = pct(x.tomatometer), p = pct(x.popcornmeter);
  const fresh = t == null ? '' : (t >= 60 ? 'Fresh' : 'Rotten');
  const verified = (x.audience_score_type || '').toUpperCase() === 'VERIFIED' ? 'Verified' : '';
  const genreChips = (x.genres || []).map(g => `<span class="gchip">${esc(g)}</span>`).join('');
  const smeter = (label, icon, val, cls, state, stateCls) => `
    <div class="smeter ${cls}">
      <div class="k">${icon} ${label}</div>
      <div class="v">${val == null ? '—' : val}<span class="pct">${val == null ? '' : '%'}</span></div>
      <div class="state ${stateCls}">${state}</div>
    </div>`;
  return `
    <div class="tile">
      <div class="top">
        ${x.poster
          ? `<img class="poster" src="${esc(x.poster)}" alt="" loading="lazy">`
          : '<div class="poster poster-placeholder" role="img" aria-label="Poster unavailable">Poster unavailable</div>'}
        <div class="meta">
          <h3>${esc(x.title)}${x.year ? ` <span class="year">(${esc(x.year)})</span>` : ''}</h3>
          ${genreChips ? `<div class="genres">${genreChips}</div>` : ''}
        </div>
      </div>
      <div class="scores2">
        ${smeter('Critics', '🍅', t, 'critic', fresh, fresh === 'Fresh' ? 'fresh' : 'rotten')}
        ${smeter('Popcorn', '🍿', p, 'popcorn', verified, 'verified')}
      </div>
      ${x.synopsis ? `<details><summary>Movie summary</summary><p>${esc(x.synopsis)}</p></details>` : ''}
    </div>`;
}

qEl.addEventListener('input', refresh);
genreToggle.addEventListener('click', () => { genrePanel.hidden = !genrePanel.hidden; });
document.getElementById('genre-clear').addEventListener('click', () => {
  selected.clear(); updateGenreUI(); renderGenreList(); refresh();
});
document.getElementById('genre-done').addEventListener('click', () => { genrePanel.hidden = true; });
document.addEventListener('click', e => {
  if (!genrePanel.hidden && !e.target.closest('.dropdown')) genrePanel.hidden = true;
});
sortSeg.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  sortMode = b.dataset.sort;
  sortSeg.querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
  refresh();
});

more.addEventListener('click', () => { visible += PAGE; render(); });

init();
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_SHELL.replace("{css}", _CSS).replace(
        "</script>\n</body>", _RT_PAGE_JS + "\n</script>\n</body>"))


@app.get("/yt", response_class=HTMLResponse)
def yt_page() -> HTMLResponse:
    return HTMLResponse(_SHELL.replace("{css}", _CSS).replace(
        "</script>\n</body>", _YT_PAGE_JS + "\n</script>\n</body>"))


@app.get("/api/lookup")
def api_lookup(q: str = Query(...)) -> JSONResponse:
    try:
        results = rt.search(q)
    except Exception:
        results = []
    return JSONResponse(results)


@app.get("/api/movie")
def api_movie(slug: str = Query(...)) -> JSONResponse:
    try:
        result = rt.movie(slug)
    except Exception:
        result = None
    return JSONResponse(result)


@app.get("/api/yttv")
def api_yttv() -> JSONResponse:
    """Return the full rated catalog (all titles) for client-side filter/sort.

    The /yt page loads everything once (~2,400 titles) and does genre
    filtering, search, and score sorting in the browser for instant response.
    """
    con = sqlite3.connect(ytv_module.YTTV_DB)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT r.title, r.year, r.tomatometer, r.popcornmeter, "
            "r.audience_score_type, r.genres, r.poster, r.synopsis, "
            "c.popularity "
            "FROM ratings r "
            "LEFT JOIN catalog c ON c.jw_id = r.jw_id "
            "ORDER BY COALESCE(c.popularity, 999999) ASC"
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["genres"] = json.loads(d.get("genres") or "[]")
            except (TypeError, ValueError):
                d["genres"] = []
            out.append(d)
    finally:
        con.close()
    return JSONResponse(out)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8790)
