"""Popcorn Check — FastAPI LAN app for Rotten Tomatoes + streaming catalogs.

Routes:
  GET  /             Rotten Tomatoes lookup page
  GET  /yt           Multi-provider movie browser page
  GET  /api/lookup   ?q=<title> -> RT search candidates
  GET  /api/movie    ?slug=<slug> -> full RT scorecard
  GET  /api/yttv     Active provider catalog + ratings

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
nav a, nav button {
  text-decoration: none; color: var(--text-muted);
  font-family: var(--font);
  font-size: .92rem; font-weight: 600;
  padding: 8px 12px; border-radius: 999px; min-height: 40px;
  display: inline-flex; align-items: center;
  border: 0; cursor: pointer;
  background: transparent;
  transition: color .15s, background .15s;
}
nav a:hover, nav button:hover { color: var(--text); background: var(--surface-2); }
nav a.active { color: var(--accent-ink); background: var(--accent); }
nav button[hidden] { display: none; }

h1 { font-family: var(--font-display); font-size: 1.75rem; font-weight: 600; letter-spacing: -.01em; margin: 22px 0 4px; }

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
  content: "+"; color: var(--accent); font-size: 1.15rem; font-weight: 800;
  width: 22px; height: 22px; border: 1px solid var(--border-strong);
  border-radius: 50%; display: grid; place-items: center;
  line-height: 1; flex: 0 0 auto;
}
details[open] summary::before { content: "−"; }
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
.tile .top { display: flex; align-items: flex-start; gap: 14px; }
.tile .poster {
  width: 40%; height: auto; aspect-ratio: 2 / 3; object-fit: cover; flex: 0 0 auto;
  border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface-2);
}
@media (min-width: 620px) { .tile .poster { width: 32%; } }
.poster-placeholder {
  display: grid; place-items: center; padding: 8px; text-align: center;
  color: var(--text-muted); font-size: .85rem; line-height: 1.2;
}
.tile .meta { min-width: 0; flex: 1; }
.tile h3 { margin: 0 0 2px; font-size: 1.125rem; font-weight: 700; letter-spacing: -.01em; line-height: 1.3; }
.tile .year { color: var(--text-muted); font-weight: 500; font-size: .9rem; letter-spacing: .01em; }
.providers { display: flex; flex-wrap: wrap; gap: 6px; }
.pchip {
  display: inline-flex; align-items: center; gap: 5px; min-height: 25px;
  border: 1px solid var(--border-strong); border-radius: 999px;
  padding: 3px 9px 3px 5px; font-size: .76rem; font-weight: 750;
  line-height: 1; letter-spacing: .01em; white-space: nowrap;
  background: var(--surface-2); color: var(--text);
}
.pchip .pmark {
  display: block; flex: none; width: 17px; height: 17px; border-radius: 50%;
}
.pmark-defs { display: none; }
.tile .genres { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.gchip {
  font-size: .8rem; font-weight: 600; color: var(--text-muted);
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; padding: 2px 6px; letter-spacing: .01em;
}

/* In-card score meters */
.scores2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 110px), 1fr));
  gap: 8px;
  margin-top: 12px;
}
.smeter {
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--surface-2); padding: 7px 8px;
  display: flex; align-items: center; justify-content: center; gap: 6px;
}
.smeter .icon { font-size: 1rem; line-height: 1; }
.smeter .v { font-size: 1.3rem; font-weight: 800; letter-spacing: -.02em; line-height: 1; font-variant-numeric: tabular-nums; }
.smeter .v .pct { font-size: .9rem; font-weight: 600; color: var(--text-muted); }
.smeter.critic { border-top: 2px solid var(--tomato); }
.smeter.critic .v { color: var(--tomato); }
.smeter.popcorn { border-top: 2px solid var(--butter); }
.smeter.popcorn .v { color: var(--butter); }
.smeter.score-pair { gap: 8px; }
.score-pair .score { display: flex; align-items: center; gap: 4px; min-width: 0; }
.score-pair .score + .score { border-left: 1px solid var(--border-strong); padding-left: 8px; }
.score-pair .popcorn-score .v { color: var(--butter); }
.smeter.imdb { border-top: 2px solid #f5c518; }
.smeter.imdb .icon { color: #1a1206; background: #f5c518; border-radius: 3px; padding: 1px 3px; font-size: .65rem; font-weight: 900; }
.smeter.imdb .v { color: #f5c518; }

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
    <a href="/yt" id="nav-yt">Movies</a>
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
const topNav = document.querySelector('.topbar nav');
topNav.innerHTML = '<button type="button" id="back-to-top" hidden>↑ Back to top</button>';
const backToTop = document.getElementById('back-to-top');
const updateBackToTop = () => { backToTop.hidden = window.scrollY < 240; };
backToTop.addEventListener('click', () => {
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ? 'auto' : 'smooth';
  window.scrollTo({top: 0, behavior});
});
window.addEventListener('scroll', updateBackToTop, {passive: true});
updateBackToTop();
const genreCatalog = __GENRE_CATALOG__;
const providerCatalog = {
  netflix: {label: 'Netflix', symbol: 'pm-netflix'},
  youtube_tv: {label: 'YouTube TV', symbol: 'pm-youtube-tv'},
  amazon_prime: {label: 'Prime', symbol: 'pm-amazon-prime'},
  peacock: {label: 'Peacock', symbol: 'pm-peacock'},
  paramount_plus: {label: 'Paramount+', symbol: 'pm-paramount-plus'}
};
const NO_GENRE = '__no_genre__';
app.innerHTML = `
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
      <div class="seg" id="sort-seg">
        <button data-sort="popular" class="active">Popular</button>
        <button data-sort="critic">Critics</button>
        <button data-sort="popcorn">Popcorn</button>
        <button data-sort="imdb">IMDb</button>
      </div>
    </div>
  </div>
  <div class="resultbar" id="resultbar" aria-live="polite">Loading movies…</div>
  <div class="grid" id="grid"><p class="empty" role="status">Loading streaming movies…</p></div>
  <button class="btn ghost" id="more" style="width:100%;margin-top:16px;display:none">Show more</button>`;

const qEl = document.getElementById('q');
const genreToggle = document.getElementById('genre-toggle');
const genrePanel = document.getElementById('genre-panel');
const genreListEl = document.getElementById('genre-list');
const genreCountEl = document.getElementById('genre-count');
const sortSeg = document.getElementById('sort-seg');
const grid = document.getElementById('grid');
const resultbar = document.getElementById('resultbar');
const more = document.getElementById('more');
const PAGE = 200;

const GENRE_STORAGE_KEY = 'yttv.selectedGenres';

function loadSelectedGenres() {
  try {
    const saved = JSON.parse(localStorage.getItem(GENRE_STORAGE_KEY) || '[]');
    const valid = new Set([...genreCatalog.map(g => g.key), NO_GENRE]);
    return new Set(saved.filter(key => valid.has(key)));
  } catch (error) {
    return new Set();
  }
}

function saveSelectedGenres() {
  try {
    localStorage.setItem(GENRE_STORAGE_KEY, JSON.stringify([...selected]));
  } catch (error) {
    // Ignore storage failures (private browsing, quota, disabled storage).
  }
}

let all = [];
let genres = [];          // [{key, label, count}], canonical order
let selected = loadSelectedGenres();
let sortMode = 'popular';
let visible = PAGE;

async function init() {
  resultbar.textContent = 'Loading movies…';
  grid.innerHTML = '<p class="empty" role="status">Loading streaming movies…</p>';
  try {
    const r = await fetch('/api/yttv');
    if (!r.ok) throw new Error(`Request failed: ${r.status}`);
    all = await r.json();
    const counts = new Map(genreCatalog.map(g => [g.key, 0]));
    let noGenreCount = 0;
    for (const x of all) {
      const keys = x.genre_keys || [];
      if (!keys.length) noGenreCount += 1;
      for (const key of keys) counts.set(key, (counts.get(key) || 0) + 1);
    }
    genres = genreCatalog.map(g => ({...g, count: counts.get(g.key) || 0}));
    genres.push({key: NO_GENRE, label: 'No genre listed', count: noGenreCount});
    renderGenreList();
    updateGenreUI();
    render();
  } catch (error) {
    resultbar.textContent = '';
    grid.innerHTML = '<div class="empty" role="alert">Movies couldn’t be loaded.<br><button class="btn ghost" id="retry" type="button" style="margin-top:12px">Try again</button></div>';
    document.getElementById('retry').addEventListener('click', init);
  }
}

function renderGenreList() {
  genreListEl.innerHTML = genres.map(g => `
    <label class="dd-item">
      <input type="checkbox" value="${esc(g.key)}" ${selected.has(g.key) ? 'checked' : ''}>
      <span class="gname">${esc(g.label)}</span>
      <span class="gcount">${g.count}</span>
    </label>`).join('');
  genreListEl.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
      saveSelectedGenres();
      updateGenreUI();
      refresh();
    });
  });
}

function updateGenreUI() {
  const n = selected.size;
  genreCountEl.textContent = n ? `${n} selected` : 'All';
}

function matches(x) {
  const q = qEl.value.trim().toLowerCase();
  if (q && !(x.title || '').toLowerCase().includes(q)) return false;
  const keys = x.genre_keys || [];
  if (selected.size && !keys.some(key => selected.has(key)) &&
      !(selected.has(NO_GENRE) && !keys.length)) return false;
  return true;
}

function sorted(items) {
  const arr = items.slice();
  if (sortMode === 'critic') {
    arr.sort((a, b) =>
      ((pct(b.tomatometer) ?? -1) - (pct(a.tomatometer) ?? -1)) ||
      ((pct(b.popcornmeter) ?? -1) - (pct(a.popcornmeter) ?? -1)) ||
      ((pct(b.imdb_score) ?? -1) - (pct(a.imdb_score) ?? -1))
    );
  } else if (sortMode === 'popcorn') {
    arr.sort((a, b) =>
      ((pct(b.popcornmeter) ?? -1) - (pct(a.popcornmeter) ?? -1)) ||
      ((pct(b.tomatometer) ?? -1) - (pct(a.tomatometer) ?? -1)) ||
      ((pct(b.imdb_score) ?? -1) - (pct(a.imdb_score) ?? -1))
    );
  } else if (sortMode === 'imdb') {
    arr.sort((a, b) =>
      ((pct(b.imdb_score) ?? -1) - (pct(a.imdb_score) ?? -1)) ||
      ((pct(b.tomatometer) ?? -1) - (pct(a.tomatometer) ?? -1)) ||
      ((pct(b.popcornmeter) ?? -1) - (pct(a.popcornmeter) ?? -1))
    );
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
  const label = sortMode === 'critic' ? 'critics score' : sortMode === 'popcorn' ? 'popcorn score' : sortMode === 'imdb' ? 'IMDb rating' : 'popularity';
  resultbar.textContent = `Showing ${shown.length.toLocaleString()} of ${items.length.toLocaleString()} · sorted by ${label}`;
}

function tile(x) {
  const t = pct(x.tomatometer), p = pct(x.popcornmeter), imdb = pct(x.imdb_score);
  const providerChips = (x.providers || []).map(key => {
    const provider = providerCatalog[key];
    if (!provider) return '';
    return `<span class="pchip">
      <svg class="pmark" aria-hidden="true" focusable="false"><use href="#${provider.symbol}"/></svg>${esc(provider.label)}
    </span>`;
  }).join('');
  const genreChips = (x.genres || []).map(g => `<span class="gchip">${esc(g)}</span>`).join('');
  const smeter = (label, icon, val, cls, unit = '%') => `
    <div class="smeter ${cls}" aria-label="${label}: ${val == null ? 'not rated' : unit === '%' ? `${val} percent` : `${val} out of 10`}">
      <span class="icon" aria-hidden="true">${icon}</span>
      <div class="v">${val == null ? '—' : val}<span class="pct">${val == null ? '' : unit}</span></div>
    </div>`;
  const rtMeter = `
    <div class="smeter critic score-pair" aria-label="Critics score: ${t == null ? 'not rated' : `${t} percent`}; audience score: ${p == null ? 'not rated' : `${p} percent`}">
      <div class="score">
        <span class="icon" aria-hidden="true">🍅</span>
        <div class="v">${t == null ? '—' : t}<span class="pct">${t == null ? '' : '%'}</span></div>
      </div>
      <div class="score popcorn-score">
        <span class="icon" aria-hidden="true">🍿</span>
        <div class="v">${p == null ? '—' : p}<span class="pct">${p == null ? '' : '%'}</span></div>
      </div>
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
          <div class="scores2">
            ${rtMeter}
            ${smeter('IMDb rating', 'IMDb', imdb, 'imdb', '/10')}
          </div>
        </div>
      </div>
      ${providerChips ? `<div class="providers" aria-label="Available on">${providerChips}</div>` : ''}
      ${x.synopsis ? `<details><summary>Movie summary</summary><p>${esc(x.synopsis)}</p></details>` : ''}
    </div>`;
}

qEl.addEventListener('input', refresh);
genreToggle.addEventListener('click', () => { genrePanel.hidden = !genrePanel.hidden; });
document.getElementById('genre-clear').addEventListener('click', () => {
  selected.clear(); saveSelectedGenres(); updateGenreUI(); renderGenreList(); refresh();
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


# Provider marks, drawn once as a sprite the tile chips reference by id. Each
# is the service's own icon-only logo, reduced to what still reads at 17px.
_PROVIDER_SPRITE = """<svg class="pmark-defs" aria-hidden="true" focusable="false"><defs>
  <symbol id="pm-netflix" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#000"/> <path d="M7 4h3.4l6.6 16H13.6z" fill="#B1060F"/> <rect x="7" y="4" width="3.4" height="16" fill="#E50914"/> <rect x="13.6" y="4" width="3.4" height="16" fill="#E50914"/></symbol>
  <symbol id="pm-youtube-tv" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#FF0000"/> <path d="M9.6 7.8 17 12l-7.4 4.2z" fill="#fff"/></symbol>
  <symbol id="pm-amazon-prime" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#00A8E1"/> <path d="M4.6 13.6c4.6 3.4 9.8 3.4 14.2.6" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/> <path d="M17.2 11.4 20.4 12l-1.1 3.1z" fill="#fff"/></symbol>
  <symbol id="pm-peacock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#000"/><path d="M12 18.6 10.4 8.2Q12 5.6 13.6 8.2Z" fill="#FCB711" transform="rotate(-62.5 12 18.6)"/><path d="M12 18.6 10.4 8.2Q12 5.6 13.6 8.2Z" fill="#F37021" transform="rotate(-37.5 12 18.6)"/><path d="M12 18.6 10.4 8.2Q12 5.6 13.6 8.2Z" fill="#CC004C" transform="rotate(-12.5 12 18.6)"/><path d="M12 18.6 10.4 8.2Q12 5.6 13.6 8.2Z" fill="#6460AA" transform="rotate(12.5 12 18.6)"/><path d="M12 18.6 10.4 8.2Q12 5.6 13.6 8.2Z" fill="#0089D0" transform="rotate(37.5 12 18.6)"/><path d="M12 18.6 10.4 8.2Q12 5.6 13.6 8.2Z" fill="#0DB14B" transform="rotate(62.5 12 18.6)"/></symbol>
  <symbol id="pm-paramount-plus" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#0064FF"/><path d="M12 6.6Q13.3 12.4 16.9 18.9L7.1 18.9Q10.7 12.4 12 6.6Z" fill="#fff"/><circle cx="3.17" cy="15.39" r="0.72" fill="#fff"/><circle cx="4.21" cy="13.34" r="0.72" fill="#fff"/><circle cx="5.71" cy="11.61" r="0.72" fill="#fff"/><circle cx="7.59" cy="10.30" r="0.72" fill="#fff"/><circle cx="9.73" cy="9.48" r="0.72" fill="#fff"/><circle cx="12.00" cy="9.20" r="0.72" fill="#fff"/><circle cx="14.27" cy="9.48" r="0.72" fill="#fff"/><circle cx="16.41" cy="10.30" r="0.72" fill="#fff"/><circle cx="18.29" cy="11.61" r="0.72" fill="#fff"/><circle cx="19.79" cy="13.34" r="0.72" fill="#fff"/><circle cx="20.83" cy="15.39" r="0.72" fill="#fff"/></symbol>
</defs></svg>"""


@app.get("/yt", response_class=HTMLResponse)
def yt_page() -> HTMLResponse:
    genre_catalog = json.dumps([
        {"key": key, "label": label}
        for key, label in ytv_module.CANONICAL_GENRES
    ])
    yt_js = _YT_PAGE_JS.replace("__GENRE_CATALOG__", genre_catalog)
    return HTMLResponse(_SHELL.replace("{css}", _CSS).replace(
        '<div class="wrap" id="app"></div>',
        _PROVIDER_SPRITE + '\n<div class="wrap" id="app"></div>').replace(
        "</script>\n</body>", yt_js + "\n</script>\n</body>"))


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
    """Return the active multi-provider catalog for client-side filter/sort.

    The /yt page loads active provider snapshots once and does genre
    filtering, search, and score sorting in the browser for instant response.
    """
    # This request must remain read-only so it can serve the last committed
    # snapshot while a catalog or enrichment sync owns SQLite's writer lock.
    con = ytv_module._read_db()
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT c.title, c.year, "
            # Prefer the critic score refreshed with each JustWatch snapshot
            # while legacy RT matches are being revalidated.
            "COALESCE(c.jw_tomatometer, r.tomatometer) AS tomatometer, "
            "r.popcornmeter, r.audience_score_type, c.imdb_score, "
            "c.jw_genres, r.genres AS rt_genres, c.tmdb_genres, "
            "COALESCE(c.jw_poster, r.poster) AS poster, "
            "COALESCE(r.synopsis, c.jw_synopsis) AS synopsis, "
            "(SELECT MIN(cp.popularity) FROM catalog_providers cp "
            " WHERE cp.jw_id = c.jw_id AND cp.active = 1) AS popularity, "
            "(SELECT GROUP_CONCAT(cp.provider_key) FROM catalog_providers cp "
            " WHERE cp.jw_id = c.jw_id AND cp.active = 1) AS provider_keys "
            "FROM catalog c "
            "LEFT JOIN ratings r ON r.jw_id = c.jw_id "
            "WHERE EXISTS (SELECT 1 FROM catalog_providers cp "
            "              WHERE cp.jw_id = c.jw_id AND cp.active = 1) "
            "ORDER BY COALESCE(popularity, 999999) ASC"
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["providers"] = sorted(
                (d.pop("provider_keys") or "").split(","),
                key=lambda key: list(ytv_module.PROVIDERS).index(key),
            )
            def genres_from(column: str) -> list[str]:
                try:
                    value = json.loads(d.pop(column) or "[]")
                    return value if isinstance(value, list) else []
                except (TypeError, ValueError):
                    return []

            d["genre_keys"], d["genres"], d["genre_source"] = \
                ytv_module.preferred_genres(
                genres_from("jw_genres"), genres_from("rt_genres"),
                genres_from("tmdb_genres")
            )
            out.append(d)
    finally:
        con.close()
    return JSONResponse(out)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8790)
