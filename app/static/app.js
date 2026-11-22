
const IMG = "https://image.tmdb.org/t/p/";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let media = "movie", view = "home", items = [], total = 0, offset = 0, current = null, lastStatus = {};
const arrOpts = {};
const CLICK = `<span class="no-touch">Click</span><span class="touch-only">Tap</span>`;   // "Tap" on touch screens
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

function toast(msg, action) {
  const t = $("toast"); t.innerHTML = `<span>${esc(msg)}</span>`;
  if (action) { const b = document.createElement("button"); b.textContent = action.label; b.onclick = () => { t.style.display = "none"; action.fn(); }; t.appendChild(b); }
  t.style.display = "flex"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", action ? 6000 : 2800);
}
const STALE = `${document.title} was updated, but the server is still running the old version. Restart the container (Compose Down, then Compose Up) to finish.`;
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  // A 404 for an endpoint the page knows about means new files were copied in but the server
  // process wasn't restarted, so the page is newer than the code that's running.
  if (r.status === 404 && body.detail === "Not Found" && path.startsWith("/api/")) throw new Error(STALE);
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
const arrName = () => media === "tv" ? "sonarr" : "radarr";
const arrOn = () => !!lastStatus[arrName()];

// ---- filters
const THIS_YEAR = new Date().getFullYear();
let facets = { genres: [], languages: [], networks: [], year_min: 1920, year_max: THIS_YEAR };
const blankF = () => ({ q: "", genres: {}, mode: "any", ymin: null, ymax: null, rating: 0, votes: 0, runtime: 0, langs: [], nets: [],
  streaming: "", seed: 0, seedTitle: "", match: "", sort: "score" });
let F = blankF();
// TMDB uses a few non-standard codes: "cn" is Cantonese (ISO 639-1 has none), "xx" means no spoken language
const LANG_FIX = { cn: "Cantonese", zh: "Chinese (Mandarin)", xx: "No language", sh: "Serbo-Croatian" };
const langName = (() => { const dn = new Intl.DisplayNames(["en"], { type: "language" }); return x => LANG_FIX[x] || (() => { try { return dn.of(x) || x; } catch { return x; } })(); })();
const fkey = () => `filters_${view === "library" ? "lib" : "picks"}_${media}`;
function saveF() { const { seed, seedTitle, match, ...keep } = F; store.set(fkey(), JSON.stringify(keep)); }   // drill-downs aren't sticky
function loadF() { try { F = { ...blankF(), ...JSON.parse(store.get(fkey()) || "{}") }; } catch { F = blankF(); } }
const inc = () => Object.keys(F.genres).filter(g => F.genres[g] === 1);
const exc = () => Object.keys(F.genres).filter(g => F.genres[g] === -1);
const RUNTIME = () => media === "tv" ? { min: 20, max: 90, step: 5 } : { min: 60, max: 240, step: 10 };

function params() {
  return new URLSearchParams({ media, q: F.q, genres: inc().join(","), exclude_genres: exc().join(","), genre_mode: F.mode,
    year_min: F.ymin || 0, year_max: F.ymax || 9999, min_rating: F.rating, min_votes: F.votes, max_runtime: F.runtime,
    languages: F.langs.join(","), networks: media === "tv" ? F.nets.join(",") : "", streaming: F.streaming,
    seed: F.seed || 0, match: F.match, sort: F.sort });
}
function anyFilter() { return !!(F.q || inc().length || exc().length || F.ymin || F.ymax || F.rating || F.votes || F.runtime || F.langs.length
  || (media === "tv" && F.nets.length) || F.streaming || F.seed || F.match); }
let debounce;
function changed(now) { saveF(); syncControls(); clearTimeout(debounce); debounce = setTimeout(() => load(true), now ? 0 : 200); }

$("q").addEventListener("input", () => { F.q = $("q").value; changed(); });
$("sort").addEventListener("change", () => { F.sort = $("sort").value; changed(true); });
$("streamBtn").onclick = () => {
  if (!lastStatus.my_providers) { toast("Pick your streaming services in Settings first", { label: "Open Settings", fn: () => go("settings") }); return; }
  F.streaming = F.streaming ? "" : "mine"; changed(true);
};

// button labels + active chips
function syncControls() {
  $("q").value = F.q; $("sort").value = F.sort;
  document.querySelector('[data-pop="nets"]').style.display = media === "tv" ? "" : "none";
  $("streamBtn").classList.toggle("active", !!F.streaming);
  $("streamBtn").style.display = view === "library" ? "none" : "";
  document.querySelector('#sort [data-lib]').hidden = view !== "library";
  const set = (k, active, label) => { const b = document.querySelector(`[data-pop="${k}"]`); b.classList.toggle("active", active); b.innerHTML = `${label} <span class="car">▼</span>`; };
  const gi = inc().length + exc().length;
  set("genres", gi > 0, `Genres${gi ? ` <span class="n">${gi}</span>` : ""}`);
  set("years", !!(F.ymin || F.ymax), F.ymin || F.ymax ? yearLabel() : "Years");
  set("rating", !!(F.rating || F.votes), F.rating ? `IMDb ${F.rating}+` : F.votes ? `${F.votes / 1000}k+ votes` : "Rating");
  set("length", !!F.runtime, F.runtime ? `≤ ${fmtRun(F.runtime)}` : (media === "tv" ? "Episode length" : "Length"));
  set("langs", F.langs.length > 0, F.langs.length === 1 ? langName(F.langs[0]) : `Language${F.langs.length ? ` <span class="n">${F.langs.length}</span>` : ""}`);
  set("nets", F.nets.length > 0, F.nets.length === 1 ? esc(F.nets[0]) : `Network${F.nets.length ? ` <span class="n">${F.nets.length}</span>` : ""}`);
  filterFade();

  const chips = [];
  const chip = (label, fn, ex) => chips.push({ label, fn, ex });
  if (F.seed) chip(F.seedTitle || "Because of a title you have", () => { F.seed = 0; F.seedTitle = ""; });
  if (F.match) chip(`Matches: ${F.match}`, () => F.match = "");
  if (F.streaming) chip("On my services", () => F.streaming = "");
  inc().forEach(g => chip(g, () => delete F.genres[g]));
  if (inc().length > 1) chip(F.mode === "all" ? "match all genres" : "match any genre", () => F.mode = F.mode === "all" ? "any" : "all");
  exc().forEach(g => chip("No " + g, () => delete F.genres[g], true));
  if (F.ymin || F.ymax) chip(yearLabel(), () => { F.ymin = F.ymax = null; });
  if (F.rating) chip(`IMDb ${F.rating}+`, () => F.rating = 0);
  if (F.votes) chip(`${F.votes / 1000}k+ votes`, () => F.votes = 0);
  if (F.runtime) chip(`≤ ${fmtRun(F.runtime)}`, () => F.runtime = 0);
  F.langs.forEach(l => chip(langName(l), () => F.langs = F.langs.filter(x => x !== l)));
  if (media === "tv") F.nets.forEach(n => chip(n, () => F.nets = F.nets.filter(x => x !== n)));
  if (F.q) chip(`“${F.q}”`, () => F.q = "");
  $("activeRow").innerHTML = chips.map((c, i) => `<span class="achip ${c.ex ? "ex" : ""}">${esc(c.label)}<button data-i="${i}" title="Remove">×</button></span>`).join("")
    + (chips.length > 1 ? `<button class="link" id="clearAll">Clear all</button>` : "");
  $("activeRow").onclick = e => {
    if (e.target.id === "clearAll") { const sort = F.sort; F = blankF(); F.sort = sort; closePop(); changed(true); return; }
    const b = e.target.closest("button[data-i]"); if (b) { chips[+b.dataset.i].fn(); if (openPop) renderPop(openPop); changed(true); }
  };
}
const yearLabel = () => F.ymin && F.ymax ? `${F.ymin}–${F.ymax}` : F.ymin ? `${F.ymin}+` : `Up to ${F.ymax}`;
const fmtRun = m => m >= 60 && media !== "tv" ? `${Math.floor(m / 60)}h${m % 60 ? " " + (m % 60) + "m" : ""}` : `${m} min`;

// popovers
let openPop = null;
function closePop() { openPop = null; $("pop").classList.remove("on"); }
document.querySelectorAll("[data-pop]").forEach(b => b.onclick = e => {
  e.stopPropagation();
  const k = b.dataset.pop;
  if (openPop === k) { closePop(); return; }
  openPop = k; renderPop(k);
  const fr = $("filters").getBoundingClientRect(), br = b.getBoundingClientRect(), pop = $("pop");
  pop.classList.add("on");
  pop.style.left = Math.max(0, Math.min(br.left - fr.left, fr.width - pop.offsetWidth - 8)) + "px";
});
document.addEventListener("click", e => {   // composedPath survives the popover re-rendering under the click
  if (!openPop) return;
  const path = e.composedPath();
  if (!path.includes($("pop")) && !path.some(el => el.dataset && el.dataset.pop)) closePop();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closePop(); });

// Year presets: the silent era, then one per decade. Only eras your picks actually reach are shown.
const ERAS = (lo, hi) => [[lo, 1929, "Silent era"], [1930, 1939, "’30s"], [1940, 1949, "’40s"], [1950, 1959, "’50s"], [1960, 1969, "’60s"], [1970, 1979, "’70s"],
  [1980, 1989, "’80s"], [1990, 1999, "’90s"], [2000, 2009, "2000s"], [2010, 2019, "2010s"], [2020, hi, "2020s"], [THIS_YEAR - 5, hi, "Last 5 years"]]
  .filter(([x, y]) => y >= lo && x <= hi && x <= y);
function renderPop(k) {
  const pop = $("pop");
  if (k === "genres") {
    pop.style.width = "560px";
    pop.innerHTML = `<h5>Genres</h5><div class="hint">${CLICK} once to include (gold), twice to exclude (red), three times to clear.</div>
      <div class="gchips">${facets.genres.map(g => {
        const st = F.genres[g.name]; return `<button class="gchip ${st === 1 ? "inc" : st === -1 ? "exc" : ""}" data-g="${esc(g.name)}">${esc(g.name)}<small>${g.count}</small></button>`; }).join("")}</div>
      <div class="foot"><div>${inc().length > 1 ? `<span class="hint" style="margin:0 8px 0 0">Included genres:</span>
        <span class="seg"><button data-m="any" class="${F.mode === "any" ? "on" : ""}">Any of them</button><button data-m="all" class="${F.mode === "all" ? "on" : ""}">All of them</button></span>` : ""}</div>
        <button class="link" data-clear>Clear genres</button></div>`;
    pop.onclick = e => {
      const g = e.target.closest("[data-g]"), m = e.target.closest("[data-m]");
      if (g) { const n = g.dataset.g, st = F.genres[n]; if (!st) F.genres[n] = 1; else if (st === 1) F.genres[n] = -1; else delete F.genres[n]; }
      else if (m) F.mode = m.dataset.m;
      else if (e.target.closest("[data-clear]")) F.genres = {};
      else return;
      renderPop("genres"); changed();
    };
  } else if (k === "years") {
    pop.style.width = "380px";
    const lo = Math.min(Math.floor(facets.year_min / 10) * 10, 1960), hi = Math.max(facets.year_max, THIS_YEAR), a = F.ymin || lo, b = F.ymax || hi;
    const pct = v => (100 * (v - lo) / Math.max(1, hi - lo));
    pop.innerHTML = `<h5>Release years</h5><div class="big" id="yLabel">${a} – ${b}</div>
      <div class="range2"><div class="track"></div><div class="fill" id="yFill" style="left:${pct(a)}%;right:${100 - pct(b)}%"></div>
        <input type="range" id="yA" min="${lo}" max="${hi}" value="${a}"><input type="range" id="yB" min="${lo}" max="${hi}" value="${b}"></div>
      <div class="presets">
        ${ERAS(lo, hi).map(([x, y, t]) => `<button data-y="${x},${y}">${t}</button>`).join("")}</div>
      <div class="foot"><span></span><button class="link" data-clear>Any year</button></div>`;
    const upd = () => {
      let x = +$("yA").value, y = +$("yB").value; if (x > y) [x, y] = [y, x];
      F.ymin = x > lo ? x : null; F.ymax = y < hi ? y : null;
      $("yLabel").textContent = `${x} – ${y}`; $("yFill").style.left = pct(x) + "%"; $("yFill").style.right = (100 - pct(y)) + "%"; changed();
    };
    $("yA").oninput = upd; $("yB").oninput = upd;
    pop.onclick = e => {
      const p = e.target.closest("[data-y]");
      if (p) { const [x, y] = p.dataset.y.split(",").map(Number); F.ymin = x > lo ? x : null; F.ymax = y < hi ? y : null; }
      else if (e.target.closest("[data-clear]")) F.ymin = F.ymax = null; else return;
      renderPop("years"); changed(true);
    };
  } else if (k === "rating") {
    pop.style.width = "340px";
    pop.innerHTML = `<h5>Minimum rating</h5><div class="big" id="rLabel">${F.rating ? F.rating.toFixed(1) + "+" : "Any"}</div>
      <input class="slider" type="range" id="rS" min="0" max="9" step="0.5" value="${F.rating}">
      <h5 style="margin-top:16px">Minimum number of votes</h5>
      <div class="seg" id="vSeg">${[0, 1000, 10000, 50000, 100000].map(v => `<button data-v="${v}" class="${F.votes === v ? "on" : ""}">${v ? v / 1000 + "k" : "Any"}</button>`).join("")}</div>
      <div class="hint" style="margin-top:10px">Uses IMDb's rating where available, otherwise TMDB's.</div>`;
    $("rS").oninput = () => { F.rating = +$("rS").value; $("rLabel").textContent = F.rating ? F.rating.toFixed(1) + "+" : "Any"; changed(); };
    pop.onclick = e => { const v = e.target.closest("[data-v]"); if (!v) return; F.votes = +v.dataset.v; renderPop("rating"); changed(true); };
  } else if (k === "length") {
    pop.style.width = "340px";
    const R = RUNTIME(), v = F.runtime || R.max;
    pop.innerHTML = `<h5>${media === "tv" ? "Maximum episode length" : "Maximum runtime"}</h5>
      <div class="big" id="lLabel">${F.runtime ? "≤ " + fmtRun(F.runtime) : "Any length"}</div>
      <input class="slider" type="range" id="lS" min="${R.min}" max="${R.max}" step="${R.step}" value="${v}">
      <div class="presets">${(media === "tv" ? [30, 45, 60] : [90, 105, 120, 150]).map(m => `<button data-l="${m}">≤ ${fmtRun(m)}</button>`).join("")}</div>
      <div class="foot"><span></span><button class="link" data-clear>Any length</button></div>`;
    $("lS").oninput = () => { const x = +$("lS").value; F.runtime = x >= R.max ? 0 : x; $("lLabel").textContent = F.runtime ? "≤ " + fmtRun(F.runtime) : "Any length"; changed(); };
    pop.onclick = e => {
      const l = e.target.closest("[data-l]");
      if (l) F.runtime = +l.dataset.l; else if (e.target.closest("[data-clear]")) F.runtime = 0; else return;
      renderPop("length"); changed(true);
    };
  } else if (k === "langs" || k === "nets") {
    const isL = k === "langs", list = isL ? facets.languages : facets.networks, sel = isL ? F.langs : F.nets;
    pop.style.width = "420px";
    pop.innerHTML = `<h5>${isL ? "Original language" : "Network"}</h5>
      <input type="search" id="lf" placeholder="Filter…" style="width:100%">
      <div class="checks" id="lc"></div>
      <div class="foot"><span class="hint" style="margin:0">${sel.length ? sel.length + " selected" : "None selected = all"}</span><button class="link" data-clear>Clear</button></div>`;
    const draw = () => {
      const f = $("lf").value.toLowerCase();
      $("lc").innerHTML = list.filter(x => (isL ? langName(x.name) : x.name).toLowerCase().includes(f)).map(x =>
        `<label><input type="checkbox" value="${esc(x.name)}" ${sel.includes(x.name) ? "checked" : ""}>${esc(isL ? langName(x.name) : x.name)}<small>${x.count}</small></label>`).join("");
    };
    draw(); $("lf").oninput = draw;
    pop.onchange = e => {
      if (!e.target.matches("input[type=checkbox]")) return;
      const arr = isL ? F.langs : F.nets, v = e.target.value;
      if (e.target.checked) arr.push(v); else arr.splice(arr.indexOf(v), 1);
      pop.querySelector(".foot .hint").textContent = arr.length ? arr.length + " selected" : "None selected = all";
      changed();
    };
    pop.onclick = e => { if (e.target.closest("[data-clear]")) { if (isL) F.langs = []; else F.nets = []; renderPop(k); changed(true); } };
  }
}

function resetFilters() { closePop(); loadF(); syncControls(); }
async function loadFacets() {
  facets = await api("/api/facets?media=" + media);
  syncControls();
}

// ---- grid
let loading = false, loadSeq = 0, moreCursor = "", moreDone = false, moreSeen = new Set();
const phase1 = () => items.filter(x => !x.source);
async function load(reset) {
  if (reset) { offset = 0; items = []; moreCursor = ""; moreDone = false; moreSeen = new Set(); window.scrollTo(0, 0); }
  else if (loading || (phase1().length >= total && (moreDone || view !== "picks"))) return;
  const seq = ++loadSeq; loading = true;
  $("loadingMore").style.display = reset ? "none" : "block";
  try {
    if (reset || phase1().length < total) {
      const p = params(); p.set("offset", offset); p.set("limit", 60);
      const r = await api(`/api/${view === "library" ? "library" : "recs"}?` + p);
      if (seq !== loadSeq) return;            // a newer filter change superseded this one
      total = r.total;
      if (r.all != null) libAll = r.all;
      if (reset || !items.length) { items = r.items; render(); }
      else { items = items.concat(r.items); $("grid").insertAdjacentHTML("beforeend", r.items.map(card).join("")); updateCount(); }
      offset = phase1().length;
    } else {
      // scored picks exhausted: keep going with the endless taste feed
      const known = new Set(items.map(x => x.key));
      let fresh = [];
      for (let tries = 0; tries < 6 && !fresh.length && !moreDone; tries++) {
        const p = params(); p.set("cursor", moreCursor);
        const r = await api("/api/more?" + p);
        if (seq !== loadSeq) return;
        moreCursor = r.cursor; moreDone = r.done;
        fresh = r.items.filter(x => !known.has(x.key) && !moreSeen.has(x.key));
        fresh.forEach(x => moreSeen.add(x.key));
      }
      if (fresh.length) {
        const first = !items.some(x => x.source);
        items = items.concat(fresh);
        if (items.length === fresh.length) render();
        else $("grid").insertAdjacentHTML("beforeend", (first ? divider() : "") + fresh.map(card).join(""));
        updateCount();
      } else if (!items.length) render();
    }
  } catch (e) { if (seq === loadSeq) { toast(e.message); moreDone = true; } }
  finally {
    if (seq === loadSeq) { loading = false; $("loadingMore").style.display = "none"; }
  }
  // keep filling if the sentinel is still on screen (tall windows)
  if (seq === loadSeq && items.length && nearBottom()) load(false);
}
function divider() {
  return `<div class="divider"><div><b>Deeper picks</b><span>${total ? `That's all ${total.toLocaleString()} top picks${anyFilter() ? " for these filters" : ""}. ` : ""}Filmhound keeps scoring more as you scroll.</span></div></div>`;
}
function updateCount() {
  const extra = items.filter(x => x.source).length;
  if (view === "library") { $("count").textContent = total && anyFilter() ? `${total.toLocaleString()} match these filters` : ""; return; }
  $("count").textContent = total || extra ? `${total.toLocaleString()} top picks${extra ? ` + ${extra.toLocaleString()} deeper${moreDone ? "" : " · keeps going as you scroll"}` : ""}` : "";
}
function normSeeds(r) {   // older data stored titles only
  return (r.because || []).map(b => typeof b === "string" ? { title: b, kind: "watched", share: null } : b);
}
const WATCHED = ["rated", "watched", "partial", "liked"];
function years(r) {
  if (r.media !== "tv" || !r.year) return r.year ?? "";
  const ended = ["Ended", "Canceled"].includes(r.status);
  return ended && r.end_year && r.end_year !== r.year ? `${r.year}–${r.end_year}` : ended ? `${r.year}` : `${r.year}–`;
}
const REG = new Map();   // every title on screen, by key, so any card can open the drawer
const ICON = {
  hide: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>`,
  play: `<svg viewBox="0 0 24 24" class="fill"><path d="M8 5.5v13l11-6.5z"/></svg>`,
  star: `<svg viewBox="0 0 24 24" class="fill"><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.8z"/></svg>`,
  eye: `<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`,
  plus: `<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>`,
  send: `<svg viewBox="0 0 24 24"><path d="M4 12l16-8-6 16-2.5-6.5z"/></svg>`,
  left: `<svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>`,
  right: `<svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>`,
  check: `<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`,
  heart: `<svg viewBox="0 0 24 24"><path d="M12 20s-7.5-4.6-9.2-9.3C1.6 7.3 3.9 4 7.3 4c2 0 3.5 1.1 4.7 2.7C13.2 5.1 14.7 4 16.7 4c3.4 0 5.7 3.3 4.5 6.7C19.5 15.4 12 20 12 20z"/></svg>`,
  similar: `<svg viewBox="0 0 24 24"><rect x="3" y="5" width="8" height="12" rx="1.5"/><rect x="13" y="7" width="8" height="12" rx="1.5"/></svg>`,
  ext: `<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>`,
};
const fav = domain => `https://www.google.com/s2/favicons?domain=${domain}&sz=64`;
function myLogos(r, n = 2) {
  const mine = new Set(lastStatus.my_provider_ids || []), p = r.providers || {};
  const seen = new Set(), out = [];
  for (const k of ["flatrate", "free", "ads"]) for (const x of p[k] || []) if (mine.has(String(x.id)) && !seen.has(x.id)) { seen.add(x.id); out.push(x); }
  return out.slice(0, n);
}
function card(r, opt = {}) {
  REG.set(r.key, r);
  if (r.watchlist) WL.add(r.key);
  const poster = r.poster_path ? `style="background-image:url(${IMG}w342${r.poster_path})"` : "";
  const rating = r.imdb_rating ? `IMDb ${r.imdb_rating}` : r.tmdb_rating ? `★ ${(+r.tmdb_rating).toFixed(1)}` : "";
  const seasons = r.media === "tv" && r.seasons ? `${r.seasons} season${r.seasons > 1 ? "s" : ""}` : "";
  const seeds = normSeeds(r), watched = seeds.filter(x => WATCHED.includes(x.kind)), pick = watched.length ? watched : seeds;
  let why = "";
  if (opt.why !== false) {
    if (opt.role) why = `<div class="lbl">${esc(opt.role)}</div>`;
    else if (pick.length) why = `<div class="lbl">${watched.length ? "Because you watched" : "Because you have"}</div><div class="ttl">${pick.slice(0, 2).map(x => esc(x.title)).join(" · ")}</div>`;
    else if (r.source === "taste") why = `<div class="lbl">From your taste</div><div class="ttl">${esc(r.stream)}</div>`;
    else if ((r.matches || []).length) why = `<div class="lbl">Matches your taste</div><div class="ttl">${r.matches.slice(0, 2).map(esc).join(" · ")}</div>`;
  }
  const badge = opt.badge || (r.score != null ? `<span class="match ${r.source === "taste" ? "taste" : ""}">${r.source === "taste" ? "✦ " : ""}${Math.round(r.score * 100)}%</span>` : "");
  const logos = myLogos(r);
  return `<div class="card${opt.dim ? " dim" : ""}" data-key="${r.key}" tabindex="0">
    <div class="poster" ${poster}>${r.poster_path ? "" : `<span>${esc(r.title)}</span>`}
      ${logos.length ? `<div class="plogos">${logos.map(x => `<img src="${IMG}w92${x.logo}" alt="${esc(x.name)}" title="On ${esc(x.name)}">`).join("")}</div>` : ""}
    </div>
    ${badge}
    ${opt.noHide ? "" : `<button class="quick" title="Not interested" data-hide="${r.key}">${ICON.hide}</button>`}
    ${r.owned || r.plex_key || opt.noWatch ? "" : `<button class="quick wl${WL.has(r.key) || r.watchlist ? " on" : ""}" title="Watchlist" data-wl="${r.key}">${ICON.heart}</button>`}
    <div class="t" title="${esc(r.title)}">${esc(r.title)}</div>
    <div class="s">${[years(r), seasons, rating].filter(Boolean).join(" · ")}</div>
    ${why ? `<div class="why">${why}</div>` : ""}</div>`;
}
function skeleton(n = 14) {
  return Array.from({ length: n }, () => `<div class="card sk"><div class="poster"></div><div class="t">&nbsp;</div><div class="s">&nbsp;</div></div>`).join("");
}
function emptyState() {
  const s = lastStatus;
  if (s.running) {
    const m = /(\d+)\/(\d+)/.exec(s.progress || "");
    const pct = m ? Math.round(100 * m[1] / m[2]) : 0;
    return `<div class="empty"><h3>Building your recommendations</h3>
      The first sync takes a few minutes for a big library. This page fills in on its own when it's done.
      <div class="live">${esc(s.step)}${s.progress ? " · " + esc(s.progress) : ""}</div>
      ${m ? `<div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>` : ""}</div>`;
  }
  if (s.configured === false) return `<div class="empty"><h3>Almost there</h3>Connect Plex and TMDB in <a class="link accent" href="#/settings">Settings</a> to build your recommendations.</div>`;
  if (s.error) return `<div class="empty"><h3>Sync failed</h3>${esc(s.error)}<div class="live">Fix the setting and click Sync now.</div></div>`;
  if (anyFilter()) return `<div class="empty"><h3>Nothing matches these filters</h3>Try loosening them, or <button class="link accent" onclick="F=blankF();changed(true)">clear all filters</button>.</div>`;
  if (media === "tv" && s.show_count === 0) return `<div class="empty"><h3>No TV shows found in Plex</h3>Filmhound reads every TV library unless you limit it in Settings.</div>`;
  if (view === "library") return `<div class="empty"><h3>You've watched everything you own</h3>Impressive. Or run a sync if you've added titles since.</div>`;
  return `<div class="empty"><h3>No recommendations yet</h3>Click the sync button up top to read your Plex library.</div>`;
}
function render() {
  const p1 = phase1(), p2 = items.filter(x => x.source);
  $("grid").innerHTML = items.length ? p1.map(r => card(r)).join("") + (p2.length ? (p1.length ? divider() : "") + p2.map(r => card(r)).join("") : "") : emptyState();
  updateCount();
}
// endless scroll: load more whenever the bottom of the grid gets close to the viewport
function nearBottom() { return $("sentinel").getBoundingClientRect().top < innerHeight + 1200; }
let scrollTick = false;
window.addEventListener("scroll", () => {
  if (scrollTick) return; scrollTick = true;
  requestAnimationFrame(() => { scrollTick = false; if (items.length && (view === "picks" || view === "library") && nearBottom()) load(false); });
}, { passive: true });
// Phones: the header is fixed. Scrolling down tucks away the page tabs and the filter bar (the top bar with
// logo, search, sync and settings stays); scrolling up brings them back. The page keeps room for the full header.
const PHONE = matchMedia("(max-width: 560px)");
const HDR = document.querySelector("header");
let hdrFull = 0, lastY = 0;
function measureHeader() {
  if (HDR.classList.contains("tuck")) return;
  hdrFull = HDR.offsetHeight;
  document.body.style.setProperty("--hdr", hdrFull + "px");
}
function tuckHeader(on) {
  if (on === HDR.classList.contains("tuck")) return;
  HDR.classList.toggle("tuck", on);
  if (!on) measureHeader();
}
new ResizeObserver(measureHeader).observe(HDR);
window.addEventListener("scroll", () => {
  const y = window.scrollY, dy = y - lastY;
  if (!PHONE.matches || y <= 0) tuckHeader(false);
  else if (dy > 6 && y > hdrFull && !openPop) tuckHeader(true);
  else if (dy < -6) tuckHeader(false);
  if (Math.abs(dy) > 6 || y <= 0) lastY = y;
}, { passive: true });
PHONE.addEventListener("change", () => tuckHeader(false));
// the header search box is narrow on a phone: a short placeholder that fits
const searchHint = () => { $("gq").placeholder = PHONE.matches ? "Search" : "Search titles and people"; };
PHONE.addEventListener("change", searchHint); searchHint();
// The filter bar scrolls sideways on narrow screens: fade its right edge while more controls are off-screen.
function filterFade() {
  for (const f of [$("filters"), $("subnav")]) f.classList.toggle("fade-right", f.scrollWidth - f.scrollLeft - f.clientWidth > 2);
}
$("filters").addEventListener("scroll", filterFade, { passive: true });
$("subnav").addEventListener("scroll", filterFade, { passive: true });
window.addEventListener("resize", filterFade);

// any card anywhere opens the drawer; the cards around it become prev/next
document.addEventListener("click", e => {
  const w = e.target.closest(".quick.wl");
  if (w) { e.stopPropagation(); e.preventDefault(); toggleWatch(+w.dataset.wl); return; }
  const q = e.target.closest(".quick");
  if (q) { e.stopPropagation(); e.preventDefault(); quickHide(+q.dataset.hide, q.closest(".card")); return; }
  const c = e.target.closest(".card[data-key]");
  if (c && !e.target.closest("a,button")) {
    const scope = c.closest("[data-list]") || c.parentElement;
    navList = [...scope.querySelectorAll(".card[data-key]")].map(x => +x.dataset.key);
    openTitle(+c.dataset.key);
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.classList?.contains("card")) e.target.click();
});
async function quickHide(key, el) {
  const r = REG.get(key); if (!r) return;
  const idx = items.indexOf(r);
  try {
    await api("/api/feedback", { method: "POST", body: JSON.stringify({ tmdb_id: r.tmdb_id, media: r.media, action: "dismissed", title: r.title }) });
  } catch (e) { toast(e.message); return; }
  if (idx >= 0) { items.splice(idx, 1); if (!r.source) total--; updateCount(); }
  document.querySelectorAll(`.card[data-key="${key}"]`).forEach(x => x.remove());
  toast(`Not interested in ${r.title}`, { label: "Undo", fn: async () => {
    await api("/api/feedback/" + key, { method: "DELETE" });
    if (idx >= 0) { items.splice(idx, 0, r); if (!r.source) total++; render(); } else refreshView();
  } });
}

// ---- drawer
let navList = [];
const appFor = r => r.media === "tv" ? "sonarr" : "radarr";
function seedStatus(b) {
  const stars = starText(b.rating);
  const when = b.last_viewed_at ? new Date(b.last_viewed_at * 1000).toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "";
  const eps = b.total_eps ? ` · ${b.watched_eps} of ${b.total_eps} episodes` : "";
  const src = { letterboxd: "on Letterboxd", trakt: "on Trakt", filmhound: "here in Filmhound" }[b.source] || "";
  if (b.kind === "rated") return `<span class="k">You rated it ${stars}</span>${eps}${when ? " · last watched " + when : ""}`;
  if (b.kind === "watched") return `<span class="k">Watched</span>${eps}${when ? " · " + when : ""}`;
  if (b.kind === "partial") return `<span class="k">Partly watched</span>${eps}${when ? " · " + when : ""}`;
  if (b.kind === "seen") return b.rating ? `<span class="k">You rated it ${stars}</span> ${src}` : `<span class="k">Seen</span> ${src}`;
  if (b.kind === "liked") return `<span class="k">You liked it</span> here in Filmhound`;
  return `<span class="k own">In your library</span>`;
}
// Ratings are 1-10 everywhere (Plex's scale): 7 = 3½ stars. Each star button has a left half (half star) and a right half.
function starsWidget(cur, attr) {
  const v = Math.round(cur || 0);
  return `<span class="stars" ${attr}>${[1, 2, 3, 4, 5].map(i => `<button data-star="${i}" class="${v >= i * 2 ? "on" : v === i * 2 - 1 ? "half" : ""}" aria-label="${i} stars">${ICON.star}<span class="sf">${ICON.star}</span></button>`).join("")}</span>`;
}
// 1-10 from where the pointer is on a star: left half = half star. Enter or Space (no pointer) gives a whole star.
const starValue = (b, e) => +b.dataset.star * 2 - (e.detail !== 0 && e.clientX - b.getBoundingClientRect().left < b.offsetWidth / 2 ? 1 : 0);
function starHover(st) {
  const show = v => st.querySelectorAll("button").forEach(x => { const i = +x.dataset.star;
    x.classList.toggle("hov", v >= i * 2); x.classList.toggle("hovhalf", v === i * 2 - 1); });
  st.onmousemove = e => { const b = e.target.closest("[data-star]"); if (b) { show(starValue(b, e)); st.classList.add("hovering"); } };
  st.onmouseleave = () => { show(0); st.classList.remove("hovering"); };
}
const starText = r10 => { const v = Math.round(r10 || 0); return "★".repeat(Math.floor(v / 2)) + (v % 2 ? "½" : ""); };
function plexLink(r) {
  const s = lastStatus;
  if (!r.plex_key || !s.plex_url || !s.plex_machine) return null;
  return `${s.plex_url}/web/index.html#!/server/${s.plex_machine}/details?key=${encodeURIComponent("/library/metadata/" + r.plex_key)}`;
}
const svcIcon = (url, name) => url ? `<img class="svc" src="${esc(url)}/favicon.ico" alt="" onerror="this.remove()">` : "";
function providerRows(p) {
  if (!p) return "";
  const mine = new Set(lastStatus.my_provider_ids || []);
  const rows = [["Stream", [...(p.flatrate || []), ...(p.free || []), ...(p.ads || [])]], ["Rent", p.rent || []], ["Buy", p.buy || []]]
    .filter(([, l]) => l.length);
  if (!rows.length) return `<div class="sec"><h4>Where to watch</h4><p class="muted">Not streaming in ${esc(p.region || "your region")} right now.</p></div>`;
  const seen = new Set();
  return `<div class="sec"><h4>Where to watch</h4>${rows.map(([label, l]) => `<div class="prow"><span class="plabel">${label}</span>${l.filter(x => {
      const k = label + x.id; if (seen.has(k)) return false; seen.add(k); return true; }).map(x =>
      `<a class="prov ${mine.has(String(x.id)) ? "mine" : ""}" ${p.link ? `href="${esc(p.link)}" target="_blank" rel="noopener"` : ""} title="${esc(x.name)}${mine.has(String(x.id)) ? " (yours)" : ""}"><img src="${IMG}w92${x.logo}" alt="${esc(x.name)}"></a>`).join("")}</div>`).join("")}
    <p class="attrib">Availability from JustWatch</p></div>`;
}
function peopleSection(r) {
  const cast = (r.cast || []).slice(0, 14);
  const crew = r.crew || [];
  const groups = [["Directed by", "Director"], ["Created by", "Creator"], ["Written by", ["Screenplay", "Writer"]], ["Cinematography", "Director of Photography"], ["Music", "Original Music Composer"]];
  const crewHtml = groups.map(([label, job]) => {
    const ppl = crew.filter(c => Array.isArray(job) ? job.includes(c.job) : c.job === job);
    const uniq = [...new Map(ppl.map(p => [p.id, p])).values()];
    return uniq.length ? `<div class="crewrow"><span>${label}</span>${uniq.map(p => `<a href="#/person/${p.id}">${esc(p.name)}</a>`).join(", ")}</div>` : "";
  }).join("");
  if (!cast.length && !crewHtml) return "";
  return `<div class="sec"><h4>Cast &amp; crew</h4>
    ${cast.length ? `<div class="castrow">${cast.map(c => `<a class="person" href="#/person/${c.id}" title="See everything with ${esc(c.name)}">
      <span class="ph" ${c.profile ? `style="background-image:url(${IMG}w185${c.profile})"` : ""}>${c.profile ? "" : esc(c.name.split(" ").map(w => w[0]).join("").slice(0, 2))}</span>
      <b>${esc(c.name)}</b><small>${esc(c.character || "")}</small></a>`).join("")}</div>` : ""}
    ${crewHtml}</div>`;
}
async function openTitle(key) {
  let r = REG.get(key) || items.find(x => x.key === key); if (!r) return; current = r;
  $("drawer").classList.add("on"); $("scrim").classList.add("on");
  if (!r.full) {
    renderDrawer(r, true);
    try {
      const d = await api(`/api/title?media=${r.media}&tmdb_id=${r.tmdb_id}`);
      // keep list-level reasons/score when the list had them
      const keep = r.score != null ? { score: r.score, because: r.because, matches: r.matches, seed_count: r.seed_count, components: r.components } : {};
      Object.assign(r, d, Object.fromEntries(Object.entries(keep).filter(([, v]) => v !== undefined)), { full: true });
    } catch (e) { if (current === r) toast(e.message); }
    if (current !== r) return;
  }
  renderDrawer(r);
}
function renderDrawer(r, loading) {
  const seeds = normSeeds(r), tv = r.media === "tv";
  const meta = (tv
    ? [years(r), r.seasons && `${r.seasons} season${r.seasons > 1 ? "s" : ""}`, (r.networks || [])[0], r.status === "Returning Series" ? "Still airing" : r.status === "Ended" || r.status === "Canceled" ? r.status : ""]
    : [r.year, r.runtime && `${Math.floor(r.runtime / 60)}h ${r.runtime % 60}m`]).filter(Boolean).join(" · ");
  const app = appFor(r), appOn = !!lastStatus[app], appUrl = lastStatus[app + "_url"];
  const idx = navList.indexOf(r.key);
  const prev = idx > 0 ? navList[idx - 1] : null, next = idx >= 0 && idx < navList.length - 1 ? navList[idx + 1] : null;
  const links = [
    [`https://www.themoviedb.org/${tv ? "tv" : "movie"}/${r.tmdb_id}`, "themoviedb.org", "TMDB"],
    r.imdb_id && [`https://www.imdb.com/title/${r.imdb_id}/`, "imdb.com", "IMDb"],
    !tv && [`https://letterboxd.com/tmdb/${r.tmdb_id}`, "letterboxd.com", "Letterboxd"],
    [`https://trakt.tv/search/tmdb/${r.tmdb_id}?id_type=${tv ? "show" : "movie"}`, "trakt.tv", "Trakt"],
  ].filter(Boolean);
  const plex = plexLink(r);
  const owned = r.owned || r.plex_key;
  const fbState = r.feedback;
  $("drawer").innerHTML = `
    <div class="d-hero" ${r.backdrop_path ? `style="background-image:url(${IMG}w1280${r.backdrop_path})"` : ""}>
      <div class="d-top">
        <button class="ibtn" ${prev != null ? `onclick="openTitle(${prev})"` : "disabled"} title="Previous (←)">${ICON.left}</button>
        <button class="ibtn" ${next != null ? `onclick="openTitle(${next})"` : "disabled"} title="Next (→)">${ICON.right}</button>
        <span style="flex:1"></span>
        <button class="ibtn" onclick="closeDrawer()" title="Close (Esc)">×</button>
      </div>
      <div class="d-head">
        ${r.poster_path ? `<img class="d-poster" src="${IMG}w342${r.poster_path}" alt="">` : ""}
        <div class="d-titles">
          <h2>${esc(r.title)}</h2>
          <div class="meta">${esc(meta)}</div>
          <div class="d-ratings">
            ${r.score != null ? `<span class="pill mp">${Math.round(r.score * 100)}% match</span>` : ""}
            ${r.imdb_rating ? `<span class="pill"><img src="${fav("imdb.com")}" alt="">${r.imdb_rating}<small>${r.imdb_votes >= 1e6 ? (r.imdb_votes / 1e6).toFixed(1) + "M" : Math.round(r.imdb_votes / 1000) + "k"}</small></span>` : ""}
            ${r.tmdb_rating ? `<span class="pill"><img src="${fav("themoviedb.org")}" alt="">${(+r.tmdb_rating).toFixed(1)}</span>` : ""}
          </div>
          <div class="d-links">${links.map(([u, d, n]) => `<a href="${u}" target="_blank" rel="noopener" title="Open on ${n}"><img src="${fav(d)}" alt="${n}"></a>`).join("")}</div>
        </div>
      </div>
    </div>
    <div class="d-actions">
      ${owned ? `${plex ? `<a class="btn primary" href="${plex}" target="_blank" rel="noopener"><img class="svc" src="${fav("plex.tv")}" alt="">Open in Plex</a>` : `<span class="pill">In your library</span>`}` : `
        ${appOn ? `<button class="btn primary" onclick="toggleArr()">${svcIcon(appUrl)}Add to ${tv ? "Sonarr" : "Radarr"}</button>` : ""}
        ${lastStatus.seerr ? `<button class="btn ${appOn ? "" : "primary"}" onclick="requestIt()">${svcIcon(lastStatus.seerr_url)}Request</button>` : ""}`}
      ${owned ? "" : `<button class="btn ${WL.has(r.key) || r.watchlist ? "on" : ""}" id="dWl" onclick="toggleWatch(${r.key})">${ICON.heart}${WL.has(r.key) || r.watchlist ? "On watchlist" : "Watchlist"}</button>`}
      ${r.trailer ? `<button class="btn" data-trailer="${esc(r.trailer)}">${ICON.play}Trailer</button>` : ""}
      <a class="btn" href="#/like/${r.media}/${r.tmdb_id}" onclick="closeDrawer()">${ICON.similar}More like this</a>
      <button class="btn ${fbState === "seen" || r.my_rating ? "on" : ""}" onclick="toggleSeen()">${owned ? `${ICON.star}Rate` : `${ICON.eye}Seen it`}</button>
      ${owned ? "" : `<button class="btn" onclick="fb('dismissed')" title="Hide it and show fewer like it">${ICON.hide}Not interested</button>`}
    </div>
    <div class="seenbox" id="seenBox">
      <span>${owned ? "Your rating (saved to Plex)" : "How was it?"}</span>
      ${starsWidget(r.my_rating, 'id="seenStars"')}
      ${owned ? "" : `<button class="link" onclick="markSeen(null)">Seen it, no rating</button>`}
    </div>
    <div class="arr" id="arrBox">
      <div class="row">
        <label>Quality profile<select id="aProfile"></select></label>
        <label>Root folder<select id="aRoot"></select></label>
        ${tv ? `<label>Monitor<select id="aMonitor">
          <option value="all">All episodes</option><option value="future">Future episodes</option>
          <option value="missing">Missing episodes</option><option value="firstSeason">First season</option>
          <option value="lastSeason">Latest season</option><option value="pilot">Pilot only</option></select></label>` : ""}
      </div>
      <div class="row">
        <label class="inline"><input type="checkbox" id="aSearch" checked> Start searching right away</label>
        <button class="btn primary" style="margin-left:auto" onclick="addArr()">Add</button>
      </div>
    </div>
    ${(r.genres || []).length ? `<div class="sec"><div class="chips">${r.genres.map(g => `<span>${esc(g)}</span>`).join("")}</div></div>` : ""}
    ${r.overview ? `<div class="sec"><p class="overview">${esc(r.overview)}</p></div>` : ""}
    ${loading ? `<div class="sec"><div class="shimmer" style="height:120px"></div></div>` : ""}
    ${seeds.length ? `<div class="sec"><h4>Why this pick</h4><div class="seeds">${seeds.map(b => `
      <div class="seed">
        <div class="th" ${b.poster_path ? `style="background-image:url(${IMG}w92${b.poster_path})"` : ""}></div>
        <div class="info">
          <div class="nm">${esc(b.title)}${b.year ? ` <span class="muted">(${b.year})</span>` : ""}</div>
          <div class="st">${seedStatus(b)}</div>
          ${b.share != null ? `<div class="meter" role="img" aria-label="How much this pushed the pick" title="How much this pushed the pick"><i style="width:${Math.round(b.share * 100)}%"></i></div>` : ""}
        </div>
      </div>`).join("")}</div>
      ${r.seed_count > seeds.length ? `<div class="more-seeds">+ ${r.seed_count - seeds.length} more of your ${tv ? "shows" : "movies"} point here too</div>` : ""}</div>`
      : r.source === "taste" ? `<div class="sec"><h4>Why this pick</h4><p class="muted">Found through your taste for <b>${esc(r.stream)}</b>.</p></div>` : ""}
    ${(r.matches || []).length ? `<div class="sec"><h4>Matches your taste</h4><div class="chips">${r.matches.map(t =>
      `<a href="#/picks" data-drill-match="${esc(t)}" data-drill-media="${esc(r.media)}">${esc(t)}</a>`).join("")}</div></div>` : ""}
    ${providerRows(r.providers)}
    ${peopleSection(r)}
    ${owned ? `<div class="sec noseed-sec"><label class="inline"><input type="checkbox" ${r.noseed ? "checked" : ""} onchange="setNoseed(${r.key}, this.checked)">
      Don't learn from this</label><div class="help">Keep it in your library, but stop it from shaping your picks. Good for kids' movies or someone else's shows.</div></div>` : ""}
    <div style="height:30px"></div>`;
  $("drawer").scrollTop = 0;
  const st = $("seenStars");
  if (st) {
    st.onclick = e => { const b = e.target.closest("[data-star]"); if (b) markSeen(starValue(b, e)); };
    starHover(st);
  }
}
function toggleSeen() { $("seenBox").classList.toggle("on"); $("arrBox").classList.remove("on"); }
async function markSeen(rating) {
  const r = current;
  try {
    const res = await api("/api/feedback", { method: "POST", body: JSON.stringify({ tmdb_id: r.tmdb_id, media: r.media, action: "seen", rating, title: r.title }) });
    const where = (r.owned || r.plex_key) && rating ? " and saved to Plex" : "";
    r.my_rating = rating; r.feedback = "seen";
    const idx = items.indexOf(r);
    removeCurrent();
    toast(rating ? `Rated ${r.title} ${starText(rating)}${where}` : `Got it, you've seen ${r.title}`,
      r.owned ? null : { label: "Undo", fn: async () => { await api("/api/feedback/" + r.key, { method: "DELETE" }); if (idx >= 0) { items.splice(idx, 0, r); if (!r.source) total++; render(); } } });
  } catch (e) { toast(e.message); }
}
function closeDrawer() { $("drawer").classList.remove("on"); $("scrim").classList.remove("on"); current = null; }
$("scrim").onclick = closeDrawer;
document.addEventListener("keydown", e => {
  if (e.target.matches("input,select,textarea")) return;
  if ($("trailer").classList.contains("on")) { if (e.key === "Escape") closeTrailer(); return; }
  if (!current || !$("drawer").classList.contains("on")) return;
  if (e.key === "Escape") { closeDrawer(); return; }
  const idx = navList.indexOf(current.key);
  if (e.key === "ArrowRight" && idx >= 0 && idx < navList.length - 1) openTitle(navList[idx + 1]);
  if (e.key === "ArrowLeft" && idx > 0) openTitle(navList[idx - 1]);
});
function removeCurrent() {
  // after acting on a title, close the panel and fade the card out, so it's clear what happened
  const r = current; if (!r) return;
  const i = items.indexOf(r);
  if (i >= 0) { items.splice(i, 1); if (!r.source) total--; updateCount(); }
  navList = navList.filter(k => k !== r.key);
  closeDrawer();
  document.querySelectorAll(`.card[data-key="${r.key}"]`).forEach(x => { x.classList.add("leaving"); setTimeout(() => x.remove(), 350); });
}
async function fb(action) {
  const r = current;
  try {
    await api("/api/feedback", { method: "POST", body: JSON.stringify({ tmdb_id: r.tmdb_id, media: r.media, action, title: r.title }) });
    const idx = items.indexOf(r);
    removeCurrent();
    toast(`Not interested in ${r.title}`, { label: "Undo", fn: async () => { await api("/api/feedback/" + r.key, { method: "DELETE" }); if (idx >= 0) { items.splice(idx, 0, r); if (!r.source) total++; render(); } } });
  } catch (e) { toast(e.message); }
}
async function requestIt() {
  const r = current;
  try {
    await api("/api/request", { method: "POST", body: JSON.stringify({ tmdb_id: r.tmdb_id, media: r.media, title: r.title }) });
    toast(`Requested ${r.title}`); removeCurrent();
  } catch (e) { toast(e.message); }
}

// ---- trailer
document.addEventListener("click", e => {
  const tr = e.target.closest("[data-trailer]");
  if (tr) { playTrailer(tr.dataset.trailer); return; }
  const dm = e.target.closest("[data-drill-match]");
  if (dm) { e.preventDefault(); drill({ match: dm.dataset.drillMatch }, dm.dataset.drillMedia); }
});
function playTrailer(key) {
  $("trailerFrame").innerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(key)}?autoplay=1&rel=0" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe>`;
  $("trailer").classList.add("on");
}
function closeTrailer() { $("trailer").classList.remove("on"); $("trailerFrame").innerHTML = ""; }
$("trailerClose").onclick = closeTrailer;
$("trailer").onclick = e => { if (e.target.id === "trailer") closeTrailer(); };

// ---- radarr / sonarr
async function toggleArr() {
  const box = $("arrBox"), app = appFor(current);
  $("seenBox").classList.remove("on");
  if (box.classList.contains("on")) { box.classList.remove("on"); return; }
  try { arrOpts[app] = arrOpts[app] || await api(`/api/arr/${app}/options`); } catch (e) { toast(e.message); return; }
  const o = arrOpts[app];
  const tb = b => b ? ` (${(b / 1e12).toFixed(1)} TB free)` : "";
  $("aProfile").innerHTML = o.profiles.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("aRoot").innerHTML = o.roots.map(f => `<option value="${esc(f.path)}">${esc(f.path)}${tb(f.free)}</option>`).join("");
  const lp = store.get(app + "Profile"), lr = store.get(app + "Root"), ls = store.get(app + "Search"), lm = store.get(app + "Monitor");
  if (lp && o.profiles.some(p => String(p.id) === lp)) $("aProfile").value = lp;
  if (lr && o.roots.some(f => f.path === lr)) $("aRoot").value = lr;
  if (ls !== null) $("aSearch").checked = ls === "1";
  if (lm && $("aMonitor")) $("aMonitor").value = lm;
  box.classList.add("on");
}
async function addArr() {
  const r = current, app = appFor(r);
  const body = { tmdb_id: r.tmdb_id, tvdb_id: r.tvdb_id, title: r.title,
    profile_id: +$("aProfile").value, root: $("aRoot").value, search: $("aSearch").checked,
    monitor: $("aMonitor") ? $("aMonitor").value : "all" };
  store.set(app + "Profile", body.profile_id); store.set(app + "Root", body.root);
  store.set(app + "Search", body.search ? "1" : "0"); if ($("aMonitor")) store.set(app + "Monitor", body.monitor);
  try {
    await api(`/api/arr/${app}/add`, { method: "POST", body: JSON.stringify(body) });
    toast(`Added ${r.title} to ${app === "sonarr" ? "Sonarr" : "Radarr"}`); removeCurrent();
  } catch (e) { toast(e.message); }
}

// ---- history (hidden, seen & rated, requested)
let hiddenRows = [], hiddenFilter = "seen", histShown = 120;
const FB_GROUPS = {
  seen: { label: "Seen it", match: r => ["seen", "liked", "disliked"].includes(r.action),
    note: "Titles you've seen outside Plex: rated here, or imported from Letterboxd or Trakt. They shape your taste and never show up as picks." },
  dismissed: { label: "Not interested", match: r => r.action === "dismissed",
    note: "Hidden from your picks, and similar titles get nudged down. Restore one to bring it back." },
  requested: { label: "Requested", match: r => r.action === "requested", note: "Sent to Seerr from Filmhound." },
  added: { label: "Added", match: r => r.action === "added", note: "Added to Radarr or Sonarr from Filmhound." },
};
const SRC = { letterboxd: ["letterboxd.com", "Letterboxd"], trakt: ["trakt.tv", "Trakt"] };
async function loadHidden() {
  $("hiddenGrid").innerHTML = `<div class="hgrid">${skeleton(10)}</div>`;
  try { hiddenRows = await api("/api/feedback"); } catch (e) { $("hiddenGrid").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  histShown = 120; renderHidden();
}
function renderHidden() {
  const counts = {}; for (const [k, g] of Object.entries(FB_GROUPS)) counts[k] = hiddenRows.filter(g.match).length;
  $("pills").innerHTML = Object.entries(FB_GROUPS).map(([k, g]) =>
    `<button data-a="${k}" class="${k === hiddenFilter ? "on" : ""}">${g.label} <small>${counts[k].toLocaleString()}</small></button>`).join("");
  $("hiddenNote").textContent = FB_GROUPS[hiddenFilter].note;
  const q = $("histQ").value.toLowerCase();
  const rows = hiddenRows.filter(FB_GROUPS[hiddenFilter].match).filter(r => !q || (r.title || "").toLowerCase().includes(q));
  const undoable = ["seen", "dismissed"].includes(hiddenFilter);
  $("hiddenGrid").innerHTML = rows.length ? rows.slice(0, histShown).map(r => {
    const stars = starText(r.rating);
    const src = SRC[r.source];
    return `<div class="hcard">
      <div class="poster" ${r.poster_path ? `style="background-image:url(${IMG}w342${r.poster_path})"` : ""}>${r.poster_path ? "" : `<span>${esc(r.title)}</span>`}
        ${src ? `<img class="srcbadge" src="${fav(src[0])}" title="From ${src[1]}" alt="${src[1]}">` : ""}</div>
      <div class="t" title="${esc(r.title || "")}">${esc(r.title || "#" + r.tmdb_id)}</div>
      <div class="s">${[r.year, r.media === "tv" ? "TV" : "Movie"].filter(Boolean).join(" · ")}${stars ? ` · <span class="gold">${stars}</span>` : ""}</div>
      ${undoable ? `<button class="btn sm" onclick="undo(${r.key})">${hiddenFilter === "dismissed" ? "Restore" : "Remove"}</button>` : ""}
    </div>`;
  }).join("") + (rows.length > histShown ? `<div class="empty" style="padding:20px"><button class="btn" onclick="histShown+=240;renderHidden()">Show more (${(rows.length - histShown).toLocaleString()} left)</button></div>` : "")
    : `<div class="empty">Nothing here yet.</div>`;
}
$("pills").onclick = e => { const b = e.target.closest("button"); if (b) { hiddenFilter = b.dataset.a; histShown = 120; renderHidden(); } };
$("histQ").oninput = () => renderHidden();
async function undo(key) {
  try { await api("/api/feedback/" + key, { method: "DELETE" }); } catch (e) { toast(e.message); return; }
  hiddenRows = hiddenRows.filter(r => r.key !== key); renderHidden();
  toast("Restored. Your picks will update in a minute");
}

// ---- for you (rows)
let homeSeq = 0;
async function loadHome() {
  $("homeRows").innerHTML = `<div class="hrow"><div class="hrow-head"><h3 class="shimmer" style="width:220px;height:22px"></h3></div><div class="hscroll">${skeleton(8)}</div></div>`.repeat(3);
  const my = ++homeSeq; let d;
  try { d = await api("/api/home?media=" + media); } catch (e) { if (my === homeSeq) $("homeRows").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (my !== homeSeq) return;   // Movies/TV was switched again before this came back
  if (!d.rows.length) { $("homeRows").innerHTML = `<div class="grid">${emptyState()}</div>`; return; }
  $("homeRows").innerHTML = d.rows.map(row => `
    <div class="hrow">
      <div class="hrow-head"><h3>${esc(row.title)}</h3>${row.subtitle ? `<span class="muted">${esc(row.subtitle)}</span>` : ""}
        <a class="link see" href="#" data-see='${esc(JSON.stringify(row.see_all || {}))}'>See all ›</a></div>
      <div class="hwrap"><button class="scroll-btn l" aria-label="Scroll left">${ICON.left}</button>
        <div class="hscroll" data-list>${row.items.map(r => card(r, rowOpts(row, r))).join("")}</div>
        <button class="scroll-btn r" aria-label="Scroll right">${ICON.right}</button></div>
    </div>`).join("");
}
$("homeRows").addEventListener("click", e => {
  const b = e.target.closest(".scroll-btn");
  if (b) { const sc = b.parentElement.querySelector(".hscroll"); sc.scrollBy({ left: (b.classList.contains("l") ? -1 : 1) * sc.clientWidth * 0.85, behavior: "smooth" }); return; }
  const a = e.target.closest("[data-see]");
  if (a) { e.preventDefault(); const s = JSON.parse(a.dataset.see); if (s.view) go(s.view); else if (s.tab === "library") go("library"); else drill(s, media); }
});
function drill(s, m) {
  // jump to All Picks with a preset filter (from a row, a taste chip, etc.)
  if (m && m !== media) setMedia(m, false);
  view = "picks"; loadF();
  const base = blankF(); base.sort = s.sort || "score";
  F = { ...base, seed: s.seed || 0, seedTitle: s.seed_label || (s.seed_title ? "Because of " + s.seed_title : ""), match: s.match || "", streaming: s.streaming || "",
        ymin: s.year_min || null, ymax: s.year_max || null };
  closeDrawer();
  skipRoute = location.hash !== "#/picks";
  location.hash = "#/picks"; showView("picks", true);
}

// ---- your library
let libAll = 0;   // everything unwatched you own, before filters: what the collection picks from
// Collection sizes on offer, never more than there is: 25, 50, 100, 250, or all of them when that's fewer.
const collSizes = n => [...[25, 50, 100, 250].filter(x => x < n), ...(n && n <= 250 ? [n] : [])];
function libHead() {
  const n = libAll, sizes = collSizes(n), pick = store.get("collSize");
  const chosen = sizes.includes(+pick) ? +pick : sizes.filter(x => x <= 50).pop() || sizes[0];
  return `<div class="list-head">
    <div><h2>Your Library</h2><p class="muted">${n ? `${n.toLocaleString()} ${media === "tv" ? (n === 1 ? "show" : "shows") : (n === 1 ? "movie" : "movies")} you own but haven't watched, best for you first. The collection takes the top ones and replaces the one made last time.` : ""}</p></div>
    ${n ? `<div class="lib-tools"><label class="inline">Collection size <select id="collN">${sizes.map(x =>
      `<option value="${x}" ${x === chosen ? "selected" : ""}>${x === n ? `All ${x}` : x}</option>`).join("")}</select></label>
    <button class="btn primary" id="pushColl" title="Makes (or replaces) a collection in Plex so these show up on your TV">Make Plex collection</button></div>` : ""}
  </div>`;
}
document.addEventListener("change", e => { if (e.target.id === "collN") store.set("collSize", e.target.value); });
document.addEventListener("click", async e => {
  if (e.target.closest("#pushColl")) {
    const b = e.target.closest("#pushColl"); b.disabled = true;
    try { const r = await api("/api/plex/collection", { method: "POST", body: JSON.stringify({ media, limit: +($("collN")?.value || 50) }) });
      toast(`Plex collection “${r.name}” now has ${r.count} titles`); }
    catch (err) { toast(err.message); }
    b.disabled = false;
  }
});

// ---- rate
let rateQ = [], rateMode = "library", rateDone = 0, rateRemaining = 0;
const rateSkipKey = () => `rateSkip_${media}`;
function rateSkips() { try { return JSON.parse(store.get(rateSkipKey()) || "[]"); } catch { return []; } }
async function loadRate(reset = true) {
  if (reset) { rateQ = []; rateDone = 0; }
  $("rateBody").innerHTML = rateShell(`<div class="shimmer" style="height:420px;border-radius:12px"></div>`);
  let d; try { d = await api(`/api/rate/queue?media=${media}&mode=${rateMode}&skip=${rateSkips().slice(-500).join(",")}`); }
  catch (e) { $("rateBody").innerHTML = rateShell(`<div class="empty">${esc(e.message)}</div>`); return; }
  rateQ = d.items; rateRemaining = d.remaining ?? rateQ.length;
  renderRate();
}
function rateShell(inner) {
  return `<div class="rate">
    <div class="rate-top">
      <div class="seg" id="rateSeg"><button data-mode="library" class="${rateMode === "library" ? "on" : ""}">Watched in Plex, not rated</button>
        <button data-mode="picks" class="${rateMode === "picks" ? "on" : ""}">Seen any of your picks?</button></div>
      <span class="muted">${rateDone ? `${rateDone} rated this session` : ""}</span>
    </div>
    <p class="muted rate-help">${rateMode === "library"
      ? `Ratings are the strongest signal Filmhound has. These go straight to Plex too. ${CLICK} the left half of a star for a half star. <span class="keys-help">Keys: <kbd>1</kbd>–<kbd>5</kbd> to rate (hold <kbd>Shift</kbd> for a half star: <kbd>Shift</kbd>+<kbd>4</kbd> is 3½), <kbd>S</kbd> skip.</span>`
      : `If you've watched any of these somewhere else, rate them and they'll stop showing up as picks. ${CLICK} the left half of a star for a half star. <span class="keys-help">Keys: <kbd>1</kbd>–<kbd>5</kbd> rate (<kbd>Shift</kbd> for a half star), <kbd>N</kbd> haven't seen it, <kbd>K</kbd> seen it, no rating, <kbd>X</kbd> not interested.</span>`}</p>
    ${rateMode === "library" && lastStatus.letterboxd && !lastStatus.letterboxd_export ? `<p class="muted rate-help">Rated these on Letterboxd already? Only your latest diary entries have been read. <a href="#/settings">Upload your Letterboxd export</a> once and everything you've rated there drops off this list.</p>` : ""}
    ${inner}</div>`;
}
function renderRate() {
  const r = rateQ[0];
  if (!r) {
    $("rateBody").innerHTML = rateShell(`<div class="empty"><h3>${rateMode === "library" ? "All caught up" : "That's the lot for now"}</h3>
      ${rateDone ? `You rated ${rateDone} this session. Your picks update automatically in a minute.` : ""}</div>`);
    return;
  }
  const nextPosters = rateQ.slice(1, 5).map(x => `<div class="rq" ${x.poster_path ? `style="background-image:url(${IMG}w185${x.poster_path})"` : ""}></div>`).join("");
  const ppl = (r.directors || []).length ? "Directed by " + r.directors.join(", ") : (r.creators || []).length ? "Created by " + r.creators.join(", ") : "";
  $("rateBody").innerHTML = rateShell(`
    <div class="rate-card">
      <div class="rate-poster" ${r.poster_path ? `style="background-image:url(${IMG}w500${r.poster_path})"` : ""}></div>
      <div class="rate-info">
        <h2>${esc(r.title)} ${r.year ? `<span class="muted">${r.year}</span>` : ""}</h2>
        <div class="meta">${esc([(r.genres || []).slice(0, 3).join(", "), ppl].filter(Boolean).join(" · "))}</div>
        ${r.view_count ? `<div class="meta">${r.leaf_count ? `${r.view_count} of ${r.leaf_count} episodes watched` : `Watched ${r.view_count > 1 ? r.view_count + " times" : "once"}`}${r.last_viewed_at ? `, last ${new Date(r.last_viewed_at * 1000).toLocaleDateString(undefined, { month: "short", year: "numeric" })}` : ""}</div>` : ""}
        <p class="overview">${esc(r.overview || "")}</p>
        <div class="rate-stars">${starsWidget(0, 'id="rateStars"')}</div>
        <div class="rate-actions">
          ${rateMode === "library" ? `<button class="btn" data-ra="skip">Skip <kbd>S</kbd></button>`
            : `<button class="btn" data-ra="unseen">Haven't seen it <kbd>N</kbd></button><button class="btn" data-ra="seen" title="Seen it, no rating">${ICON.eye}Seen it <kbd>K</kbd></button><button class="btn" data-ra="hide">${ICON.hide}Not interested <kbd>X</kbd></button>`}
        </div>
        ${rateRemaining > 1 ? `<div class="rate-next"><span class="muted">Up next</span>${nextPosters}${rateMode === "library" ? `<span class="muted">${(rateRemaining - 1).toLocaleString()} more</span>` : ""}</div>` : ""}
      </div>
    </div>`);
  starHover($("rateStars"));
}
let rateBusy = false;
async function rateAct(act, value) {   // value: 1-10
  const r = rateQ[0]; if (!r || rateBusy) return;   // one at a time: a double press must not skip the next title unrated
  rateBusy = true;
  try { await rateStep(r, act, value); } finally { rateBusy = false; }
}
async function rateStep(r, act, value) {
  try {
    if (act === "rate") await api("/api/rate", { method: "POST", body: JSON.stringify({ key: r.key, rating: value, title: r.title }) });
    else if (act === "seen") await api("/api/rate", { method: "POST", body: JSON.stringify({ key: r.key, rating: null, title: r.title }) });
    else if (act === "hide") await api("/api/feedback", { method: "POST", body: JSON.stringify({ tmdb_id: r.tmdb_id, media: r.media, action: "dismissed", title: r.title }) });
    else { const s = rateSkips(); s.push(r.key); store.set(rateSkipKey(), JSON.stringify(s.slice(-1000))); }
  } catch (e) { toast(e.message); return; }
  if (act === "rate" || act === "seen") rateDone++;
  rateQ.shift(); rateRemaining = Math.max(0, rateRemaining - 1);
  if (rateQ.length < 3 && rateRemaining > rateQ.length) {
    try {
      const d = await api(`/api/rate/queue?media=${media}&mode=${rateMode}&skip=${[...rateSkips().slice(-500), ...rateQ.map(x => x.key)].join(",")}`);
      const have = new Set(rateQ.map(x => x.key)); rateQ.push(...d.items.filter(x => !have.has(x.key)));
    } catch (e) { toast(e.message); }
  }
  renderRate();
}
$("rateBody").addEventListener("click", e => {
  const sg = e.target.closest("#rateSeg [data-mode]"); if (sg) { rateMode = sg.dataset.mode; loadRate(); return; }
  const s = e.target.closest("#rateStars [data-star]"); if (s) { rateAct("rate", starValue(s, e)); return; }
  const a = e.target.closest("[data-ra]");
  if (a) rateAct(a.dataset.ra === "skip" || a.dataset.ra === "unseen" ? "skip" : a.dataset.ra);
});
document.addEventListener("keydown", e => {
  if (view !== "rate" || e.target.matches("input,select,textarea") || $("drawer").classList.contains("on")) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;   // Ctrl+S, Cmd+N and friends are the browser's, not ours
  const digit = /^Digit([1-5])$/.exec(e.code);   // e.code, because Shift+4 types "$"
  if (digit) rateAct("rate", +digit[1] * 2 - (e.shiftKey ? 1 : 0));
  else if (e.key === "s" || e.key === "S" || e.key === "n" || e.key === "N") rateAct("skip");
  else if ((e.key === "x" || e.key === "X") && rateMode === "picks") rateAct("hide");
  else if ((e.key === "k" || e.key === "K") && rateMode === "picks") rateAct("seen");
});

// ---- person
let personData = null, personOpts = { hideOwned: true, hideSeen: true, type: "all", sort: "popular" };
let personSeq = 0;
async function loadPerson(id) {
  $("personBody").innerHTML = `<div class="shimmer" style="height:200px;border-radius:12px;margin-bottom:20px"></div><div class="grid">${skeleton(14)}</div>`;
  const my = ++personSeq; let p;
  try { p = await api("/api/person/" + id); } catch (e) { if (my === personSeq) $("personBody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (my !== personSeq) return;   // another person was opened before this one came back
  personData = p;
  renderPerson();
}
function renderPerson() {
  const p = personData, o = personOpts;
  let works = p.works.filter(w => w.poster_path);
  const counts = { owned: works.filter(w => w.owned).length, all: works.length };
  if (o.hideOwned) works = works.filter(w => !w.owned);
  if (o.hideSeen) works = works.filter(w => !w.watched && !w.hidden);
  if (o.type !== "all") works = works.filter(w => w.media === o.type);
  works.sort({ popular: (a, b) => (b.popularity || 0) - (a.popularity || 0), newest: (a, b) => (b.year || 0) - (a.year || 0),
    match: (a, b) => (b.score ?? -1) - (a.score ?? -1), rating: (a, b) => (b.tmdb_rating || 0) - (a.tmdb_rating || 0) }[o.sort]);
  const age = p.birthday ? Math.floor(((p.deathday ? new Date(p.deathday) : new Date()) - new Date(p.birthday)) / 3.15576e10) : null;
  $("personBody").innerHTML = `
    <div class="person-head">
      <div class="person-ph" ${p.profile ? `style="background-image:url(${IMG}h632${p.profile})"` : ""}></div>
      <div>
        <h2>${esc(p.name)}</h2>
        <div class="meta">${[p.known_for, p.birthday && `Born ${new Date(p.birthday).getFullYear()}${p.place_of_birth ? " in " + p.place_of_birth : ""}`,
          p.deathday ? `Died ${new Date(p.deathday).getFullYear()} (${age})` : age ? `${age} years old` : ""].filter(Boolean).map(esc).join(" · ")}</div>
        <p class="bio">${esc((p.biography || "").slice(0, 700))}${(p.biography || "").length > 700 ? "…" : ""}</p>
        <div class="d-links">
          <a href="https://www.themoviedb.org/person/${p.id}" target="_blank" rel="noopener" title="TMDB"><img src="${fav("themoviedb.org")}" alt="TMDB"></a>
          ${p.imdb_id ? `<a href="https://www.imdb.com/name/${p.imdb_id}/" target="_blank" rel="noopener" title="IMDb"><img src="${fav("imdb.com")}" alt="IMDb"></a>` : ""}
        </div>
        <p class="muted">${counts.owned} of ${counts.all} titles are in your library.</p>
      </div>
    </div>
    <div class="person-tools">
      <label class="inline"><input type="checkbox" data-po="hideOwned" ${o.hideOwned ? "checked" : ""}> Hide what I own</label>
      <label class="inline"><input type="checkbox" data-po="hideSeen" ${o.hideSeen ? "checked" : ""}> Hide seen &amp; hidden</label>
      <span class="seg">${[["all", "All"], ["movie", "Movies"], ["tv", "TV"]].map(([k, l]) => `<button data-pt="${k}" class="${o.type === k ? "on" : ""}">${l}</button>`).join("")}</span>
      <select class="sort" id="pSort">${[["popular", "Most popular"], ["match", "Best match for you"], ["newest", "Newest"], ["rating", "Highest rated"]].map(([k, l]) =>
        `<option value="${k}" ${o.sort === k ? "selected" : ""}>Sort: ${l}</option>`).join("")}</select>
    </div>
    <p class="count">${works.length} title${works.length === 1 ? "" : "s"}</p>
    <div class="grid" data-list>${works.map(w => card(w, { role: w.roles.slice(0, 2).join(", ") || (w.media === "tv" ? "TV" : ""), noHide: w.owned,
      badge: w.owned ? `<span class="match own">${ICON.check} Owned</span>` : w.score != null ? `<span class="match">${Math.round(w.score * 100)}%</span>` : !w.released ? `<span class="match taste">Upcoming</span>` : "",
      dim: w.watched })).join("") || `<div class="empty">Nothing left with these settings.</div>`}</div>`;
}
$("personBody").addEventListener("change", e => {
  if (e.target.dataset.po) { personOpts[e.target.dataset.po] = e.target.checked; renderPerson(); }
  if (e.target.id === "pSort") { personOpts.sort = e.target.value; renderPerson(); }
});
$("personBody").addEventListener("click", e => { const b = e.target.closest("[data-pt]"); if (b) { personOpts.type = b.dataset.pt; renderPerson(); } });

// ---- watchlist, search, more like this, gaps, coming soon
const WL = new Set();
function rowOpts(row, r) {
  if (row.id === "gaps") return { role: r.gap };
  if (row.id === "upcoming") return { role: upLabel(r), badge: dateBadge(r) };
  return { why: row.id === "top" || row.id.startsWith("m") || ["gems", "new", "classics", "mine", "watchlist"].includes(row.id) };
}
const fmtDate = d => { try { return new Date(d + "T12:00").toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }); } catch { return d; } };
// Coming soon: the date shows once, as the badge on the poster; the caption says who or what it's for
const upLabel = r => (r.people || [])[0] || (r.matches || []).slice(0, 2).join(", ");
const dateBadge = r => r.date ? `<span class="match taste">${esc(fmtDate(r.date))}</span>` : "";
async function toggleWatch(key) {
  const r = REG.get(key) || current; if (!r) return;
  const on = !(WL.has(key) || r.watchlist);
  try { await api("/api/watchlist", { method: "POST", body: JSON.stringify({ key, title: r.title, on }) }); } catch (e) { toast(e.message); return; }
  r.watchlist = on; on ? WL.add(key) : WL.delete(key);
  document.querySelectorAll(`.quick.wl[data-wl="${key}"]`).forEach(b => b.classList.toggle("on", on));
  const d = $("dWl"); if (d && current && current.key === key) { d.classList.toggle("on", on); d.innerHTML = ICON.heart + (on ? "On watchlist" : "Watchlist"); }
  if (!on && view === "watchlist") document.querySelectorAll(`#listBody .card[data-key="${key}"]`).forEach(x => { x.classList.add("leaving"); setTimeout(() => x.remove(), 350); });
  toast(on ? `Saved ${r.title} to your watchlist` : `Removed ${r.title} from your watchlist`, on ? null : { label: "Undo", fn: () => toggleWatch(key) });
}
async function setNoseed(key, on) {
  const r = current || REG.get(key);
  try { await api("/api/noseed", { method: "POST", body: JSON.stringify({ key, title: r ? r.title : "", on }) }); } catch (e) { toast(e.message); return; }
  if (r) r.noseed = on;
  toast(on ? "Got it. Your picks will update in a minute without this." : "It'll shape your picks again.");
}
let listSeq = 0;   // newest request for the shared list page; older ones are dropped when they come back
function listShell(title, sub, inner, tools = "") {
  $("listBody").innerHTML = `<div class="list-head"><div><h2>${title}</h2>${sub ? `<p class="muted">${sub}</p>` : ""}</div>${tools}</div>${inner}`;
}
const gridOf = (arr, opt = () => ({})) => `<div class="grid" data-list>${arr.map(r => card(r, opt(r))).join("")}</div>`;
async function loadWatchlist() {
  listShell("Watchlist", "", `<div class="grid">${skeleton(10)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/watchlist?media=" + media); } catch (e) { if (my === listSeq) listShell("Watchlist", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  const want = d.items.filter(r => !r.owned), got = d.items.filter(r => r.owned);
  want.forEach(r => WL.add(r.key));
  listShell("Watchlist", want.length ? `${want.length} saved for later. Saving something also nudges your picks toward it a little.` : "",
    want.length ? gridOf(want) : `<div class="empty">Nothing saved yet. Tap ${ICON.heart.replace("<svg", '<svg class="inl"')} on any title to add it to your watchlist.</div>`);
  if (got.length) $("listBody").insertAdjacentHTML("beforeend", `<h3 class="list-sub">Now in your library</h3>${gridOf(got, () => ({ noHide: true, badge: `<span class="match own">${ICON.check} In Plex</span>` }))}
    <p class="muted"><button class="link" id="wlClear">Clear these from your watchlist</button></p>`);
  const c = $("wlClear"); if (c) c.onclick = async () => {
    try { for (const r of got) await api("/api/watchlist", { method: "POST", body: JSON.stringify({ key: r.key, on: false }) }); } catch (e) { toast(e.message); }
    loadWatchlist();
  };
}
$("gsearch").onsubmit = e => { e.preventDefault(); const q = $("gq").value.trim(); if (q) location.hash = "#/search/" + encodeURIComponent(q); };
async function runSearch(q) {
  $("gq").value = q;
  if (!q) { listShell("Search", "Find any movie, show or person on TMDB.", ""); $("gq").focus(); return; }
  listShell(`Results for “${esc(q)}”`, "", `<div class="grid">${skeleton(10)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/search?q=" + encodeURIComponent(q)); } catch (e) { if (my === listSeq) listShell("Search", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  const people = d.people.length ? `<div class="people-row">${d.people.map(p => `<a class="person-chip" href="#/person/${p.id}">
      <span class="ph" style="background-image:url(${IMG}w185${p.profile})"></span><span><b>${esc(p.name)}</b><small>${esc(p.known_for || "")}</small></span></a>`).join("")}</div>` : "";
  const items = d.items.filter(r => r.poster_path);
  listShell(`Results for “${esc(q)}”`, `${items.length} title${items.length === 1 ? "" : "s"}${d.people.length ? ` · ${d.people.length} people` : ""}`,
    people + (items.length ? gridOf(items, r => ({ role: r.media === "tv" ? "TV show" : "Movie", noHide: r.owned,
      badge: r.owned ? `<span class="match own">${ICON.check} Owned</span>` : "" })) : `<div class="empty">No titles match.</div>`));
}
async function loadLike(m, id) {
  listShell("More like this", "", `<div class="grid">${skeleton(14)}</div>`);
  const my = ++listSeq; let d; try { d = await api(`/api/like/${m}/${id}`); } catch (e) { if (my === listSeq) listShell("More like this", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  const hideOwned = store.get("likeHideOwned") !== "0";
  const items = d.items.filter(r => !hideOwned || !r.owned);
  listShell(`More like <a href="#" class="likehead" data-open="${m === "tv" ? -id : id}">${esc(d.title || "")}</a>${d.year ? ` <span class="muted">(${d.year})</span>` : ""}`,
    "Ranked by how close they are to it, then by how well they fit your taste.",
    items.length ? gridOf(items, r => ({ noHide: r.owned, dim: r.seen, badge: r.owned ? `<span class="match own">${ICON.check} Owned</span>` : r.score != null ? `<span class="match">${Math.round(r.score * 100)}%</span>` : "" }))
      : `<div class="empty">Nothing left to show.</div>`,
    `<label class="inline"><input type="checkbox" id="likeOwned" ${hideOwned ? "checked" : ""}> Hide what I own</label>`);
  $("likeOwned").onchange = e => { store.set("likeHideOwned", e.target.checked ? "1" : "0"); loadLike(m, id); };
  if (d.backdrop_path) $("listBody").querySelector(".list-head").style.backgroundImage = `linear-gradient(90deg,var(--bg) 35%,rgba(0,0,0,.55)),url(${IMG}w1280${d.backdrop_path})`;
}
$("listBody").addEventListener("click", e => { const a = e.target.closest("[data-open]"); if (a) { e.preventDefault(); navList = []; openTitle(+a.dataset.open); } });
let gapKind = "all";
async function loadGaps() {
  listShell("Finish what you started", "", `<div class="grid">${skeleton(10)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/gaps?kind=" + gapKind); } catch (e) { if (my === listSeq) listShell("Finish what you started", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  const tools = `<span class="seg">${[["all", "All"], ["collection", "Collections"], ["director", "Directors"]].map(([k, l]) => `<button data-gk="${k}" class="${gapKind === k ? "on" : ""}">${l}</button>`).join("")}</span>`;
  if (!d.groups.length) { listShell("Finish what you started", "", `<div class="empty">${d.at ? "No gaps found. Your collections are complete." : "This fills in after your next full sync."}</div>`, tools); return; }
  listShell("Finish what you started", "Films missing from collections and directors you already own. Movies only.",
    d.groups.map(g => `<div class="gap">
      <div class="gap-head">${g.type === "director" ? `<a href="#/person/${g.id}" class="gap-ph" ${g.profile ? `style="background-image:url(${IMG}w185${g.profile})"` : ""}></a>` : g.poster_path ? `<span class="gap-ph" style="background-image:url(${IMG}w185${g.poster_path})"></span>` : ""}
        <div><h3>${g.type === "director" ? `<a href="#/person/${g.id}">${esc(g.name)}</a>` : esc(g.name)}</h3>
        <div class="muted">${g.type === "director" ? "Director · " : ""}You have ${g.owned} of ${g.total}</div>
        <div class="meter"><i style="width:${Math.round(g.owned / g.total * 100)}%"></i></div></div></div>
      <div class="hscroll" data-list>${g.items.map(r => card(r, { why: false, badge: r.upcoming ? `<span class="match taste">${esc(fmtDate(r.date))}</span>` : "" })).join("")}</div></div>`).join(""), tools);
}
async function loadUpcoming() {
  listShell("Coming soon", "", `<div class="grid">${skeleton(10)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/upcoming?media=" + media); } catch (e) { if (my === listSeq) listShell("Coming soon", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  const soonest = d.items.slice().sort((a, b) => (a.date || "").localeCompare(b.date || ""));
  listShell("Coming soon for you", "Unreleased movies and shows that fit your taste, soonest first. Some come from directors, writers and actors you like; the rest match what you watch.",
    soonest.length ? gridOf(soonest, r => ({ role: upLabel(r), badge: dateBadge(r) }))
      : `<div class="empty">${d.at ? "Nothing announced yet." : "This fills in after your next full sync."}</div>`);
}
$("listBody").addEventListener("click", e => { const b = e.target.closest("[data-gk]"); if (b) { gapKind = b.dataset.gk; loadGaps(); } });

// ---- settings layout helpers (same in Filmhound and Recordhound)
const openCards = new Set();   // which collapsible cards are open, so a re-render keeps them open
const pill = (on, onText = "Set up", offText = "Not set up") => `<span class="pill ${on ? "on" : ""}">${on ? "✓ " + onText : offText}</span>`;
const setSection = (id, title, body, sub = "") =>
  `<section class="setsec" id="set-${id}"><h2 class="sh">${title}</h2>${sub ? `<p class="secsub">${sub}</p>` : ""}${body}</section>`;
// A card that can be collapsed: shows its title, status and a one-line summary when closed.
const foldCard = (id, head, status, sub, body) => `<details class="scard fold" id="svc-${id}" data-fold="${id}" ${openCards.has(id) ? "open" : ""}>
    <summary><h3>${head}</h3>${status}<span class="sumsub">${sub}</span><span class="chev">›</span></summary>
    <p class="sub">${sub}</p>${body}</details>`;
const advanced = (id, body) => `<details class="adv" data-fold="${id}" ${openCards.has(id) ? "open" : ""}><summary>Advanced<span class="chev">›</span></summary>${body}</details>`;
const openFor = id => { openCards.add(id); const d = document.querySelector(`#settingsBody [data-fold="${id}"]`); if (d) d.open = true; };
let setSpy = null;
// the full header, even while it's tucked away on a phone (scrolling back up shows it again)
const headerOffset = () => Math.max(document.querySelector("header")?.offsetHeight || 60, PHONE.matches ? hdrFull : 0) + 16;
function setLayout(sections) {   // sections: [[id, menu label, html], …]
  const nav = `<nav class="setnav" id="setnav">${sections.map(([id, label]) => `<button type="button" data-jump="${id}">${label}</button>`).join("")}
    <p class="setnav-note">Changes to fields are saved with the bar at the bottom. Buttons act right away.</p></nav>`;
  return `<div class="setwrap">${nav}<div class="setmain">${sections.map(s => s[2]).join("")}</div></div>`;
}
function watchSections() {   // highlight the menu item for the section in view
  const nav = $("setnav"); if (!nav) return;
  const mark = () => {
    if (!nav.isConnected || view !== "settings") return;
    const secs = [...document.querySelectorAll("#settingsBody .setsec")];
    let cur = secs[0];
    for (const s of secs) if (s.getBoundingClientRect().top <= headerOffset() + 40) cur = s;
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) cur = secs[secs.length - 1];   // at the bottom
    nav.querySelectorAll("[data-jump]").forEach(b => b.classList.toggle("on", cur && b.dataset.jump === cur.id.slice(4)));
  };
  if (!setSpy) { setSpy = () => requestAnimationFrame(() => setSpy.mark && setSpy.mark()); window.addEventListener("scroll", setSpy, { passive: true }); }
  setSpy.mark = mark;
  mark();
}
$("settingsBody").addEventListener("toggle", e => {
  const id = e.target.dataset && e.target.dataset.fold; if (!id) return;
  e.target.open ? openCards.add(id) : openCards.delete(id);
}, true);
$("settingsBody").addEventListener("click", e => {
  const j = e.target.closest("[data-jump]"); if (!j) return;
  const el = document.getElementById("set-" + j.dataset.jump); if (!el) return;
  window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - headerOffset(), behavior: "smooth" });
});

// ---- settings
let S = null, draft = {}, dirty = false, plexLibs = null;
const SVC = {
  plex: { title: "Plex", sub: "Where your library and watch history come from.", url: "PLEX_URL", key: "PLEX_TOKEN", keyLabel: "Token",
    urlHelp: "Usually <code>http://YOUR-SERVER-IP:32400</code>",
    keyHelp: "Plex Web → any title → ⋯ → Get Info → View XML. The token is the <code>X-Plex-Token=</code> value in the URL." },
  tmdb: { title: "TMDB", sub: "Recommendations, posters, cast and crew. Free key at themoviedb.org → Settings → API.", key: "TMDB_API_KEY", keyLabel: "API key or read token" },
  seerr: { optional: true, title: "Seerr", sub: "Optional. Enables Request in Seerr and respects your Seerr blocklist.", url: "SEERR_URL", key: "SEERR_API_KEY", keyLabel: "API key",
    urlHelp: "e.g. <code>http://192.168.1.10:5055</code>", keyHelp: "Seerr → Settings → General → API Key" },
  radarr: { optional: true, title: "Radarr", sub: "Optional. Add movies directly with a quality profile.", url: "RADARR_URL", key: "RADARR_API_KEY", keyLabel: "API key",
    urlHelp: "e.g. <code>http://192.168.1.10:7878</code>", keyHelp: "Radarr → Settings → General → API Key" },
  sonarr: { optional: true, title: "Sonarr", sub: "Optional. Add shows directly with a quality profile.", url: "SONARR_URL", key: "SONARR_API_KEY", keyLabel: "API key",
    urlHelp: "e.g. <code>http://192.168.1.10:8989</code>", keyHelp: "Sonarr → Settings → General → API Key" },
};
const SVC_SITE = { plex: "plex.tv", tmdb: "themoviedb.org", seerr: "overseerr.dev", radarr: "radarr.video", sonarr: "sonarr.tv" };   // for their icons
const val = k => k in draft ? draft[k] : S.values[k];
function srcTag(k) {
  const src = S.sources[k];
  return src === "env" && !(k in draft) ? `<span class="src" title="Set in docker-compose. Saving here overrides it.">from compose</span>` : "";
}
async function loadSettings() {
  try { S = await api("/api/settings"); } catch (e) { $("settingsBody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  draft = {}; setDirty(false);
  renderSettings();
  if (S.values.PLEX_URL && S.values.PLEX_TOKEN && !plexLibs) testSvc("plex", true);
}
function setDirty(d) { dirty = d; $("savebar").classList.toggle("on", d); }
function confirmLeave() { return window.confirm("Discard unsaved settings changes?") && (setDirty(false), draft = {}, true); }
// Saved keys never reach the browser: the server sends eight dots plus the key's last four characters. That
// placeholder is shown as plain text, so you can tell which key is saved. The eye button only reveals what you type.
const isMasked = v => typeof v === "string" && v.startsWith("••••");
const isSecret = (k, v) => /TOKEN|KEY|CLIENT_ID/.test(k) || isMasked(v);
const EYE = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
function secretInput(attr, k, v, ph) {
  const m = isMasked(v);
  return `<span class="secret"><input type="${m ? "text" : "password"}" ${attr}="${k}" data-secret value="${esc(v)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">` +
    `<button type="button" class="eye" data-eye aria-label="Show what you typed" title="Show what you typed" aria-pressed="false" ${m || !v ? "hidden" : ""}>${EYE}</button></span>` +
    `<div class="help saved-note" ${m ? "" : "hidden"}>Saved. For safety the full key is never shown here; paste a new one to replace it.</div>`;
}
document.addEventListener("focusin", e => {   // typing into a saved key replaces it
  if (e.target.matches && e.target.matches("[data-secret]") && isMasked(e.target.value)) e.target.select();
});
document.addEventListener("input", e => {
  const el = e.target;
  if (!el.matches || !el.matches("[data-secret]")) return;
  const eye = el.nextElementSibling, note = el.parentElement.nextElementSibling, m = isMasked(el.value);
  if (!m && eye.getAttribute("aria-pressed") !== "true") el.type = "password";   // what you type stays hidden until you ask
  if (m) el.type = "text";
  eye.hidden = m || !el.value;
  if (note && note.classList.contains("saved-note")) note.hidden = !m;
});
document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("[data-eye]");
  if (!b) return;
  const el = b.previousElementSibling, show = el.type === "password";
  el.type = show ? "text" : "password";
  b.setAttribute("aria-pressed", String(show));
  b.title = show ? "Hide" : "Show what you typed";
  b.setAttribute("aria-label", b.title);
  el.focus();
});
function input(k, type = "text", ph = "") {
  const v = val(k);
  if (isSecret(k, v)) return secretInput("data-k", k, v, ph);
  return `<input type="${type}" data-k="${k}" value="${esc(v)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">`;
}
// What "Don't learn from" leaves out right now, in one line (same as Recordhound)
function nsSummary() {
  const read = val("PLEX_LIBRARIES") || [];
  const own = (val("NOSEED_LIBRARIES") || []).filter(l => !read.length || read.some(x => lc(x) === lc(l)));
  const name = l => ((plexLibs || []).find(x => lc(x.title) === lc(l)) || { title: l }).title;   // as Plex spells it
  const out = [...own.map(l => `the ${name(l)} library (own only)`), ...(val("NOSEED_GENRES") || [])];
  return out.length ? `<b>Left out:</b> ${out.map(esc).join(", ")}` : "Nothing is left out yet.";
}
// "Movies" or "TV" after a library's name, unless the name already says it ("Movies", "TV Shows")
const libKind = x => { const k = x.type === "show" ? "TV" : "Movies"; return (x.title || "").toLowerCase().includes(k.toLowerCase().replace(/s$/, "")) ? "" : ` <small>${k}</small>`; };
// Each Plex library is Use (read, shapes your picks), Own only (read and counts as owned, but doesn't shape your
// picks) or Ignore (not read). Stored the way it always was, so older setups carry over: PLEX_LIBRARIES lists the
// libraries that are read (empty = every library) and NOSEED_LIBRARIES the own-only ones (same as Recordhound).
const lc = s => String(s || "").toLowerCase();
const LIB_MODES = [["use", "Use"], ["own", "Own only"], ["ignore", "Ignore"]];
const LIB_HELP = "<b>Use</b> shapes your picks. <b>Own only</b> counts as yours, so nothing in it is suggested to you, but it doesn't shape your picks: good for a kids' library or someone else's shows. <b>Ignore</b> isn't read at all.";
function libMode(title, read, own) {
  if (read.length && !read.some(x => lc(x) === lc(title))) return "ignore";
  return own.some(x => lc(x) === lc(title)) ? "own" : "use";
}
// The two stored lists after one library changes mode; null when that would leave nothing to read.
function libModeLists(libs, title, mode, read, own) {
  const modes = Object.fromEntries(libs.map(l => [l.title, l.title === title ? mode : libMode(l.title, read, own)]));
  const keep = libs.filter(l => modes[l.title] !== "ignore").map(l => l.title);
  if (!keep.length) return null;
  const known = new Set(libs.map(l => lc(l.title)));
  return { PLEX_LIBRARIES: keep.length === libs.length ? [] : keep,
    NOSEED_LIBRARIES: [...own.filter(x => !known.has(lc(x))), ...libs.filter(l => modes[l.title] === "own").map(l => l.title)] };
}
function libRows(libs, read, own, attr) {
  return `<div class="librows">${libs.map(l => { const m = libMode(l.title, read, own); return `<div class="librow"><span class="libname">${esc(l.title)}${libKind(l)}</span>
    <div class="seg" role="group" aria-label="${esc(l.title)}">${LIB_MODES.map(([k, label]) =>
      `<button type="button" ${attr}="${esc(l.title)}" data-mode="${k}" class="${m === k ? "on" : ""}" aria-pressed="${m === k}">${label}</button>`).join("")}</div></div>`; }).join("")}</div>
    <div class="help">${LIB_HELP}</div>`;
}
function svcCard(id) {
  const c = SVC[id], set = !!((!c.url || val(c.url)) && val(c.key));
  const body = `${c.url ? `<div class="field"><label>URL ${srcTag(c.url)}</label>${input(c.url, "text", "http://")}${c.urlHelp ? `<div class="help">${c.urlHelp}</div>` : ""}</div>` : ""}
    <div class="field"><label>${c.keyLabel} ${srcTag(c.key)}</label>${input(c.key)}${c.keyHelp ? `<div class="help">${c.keyHelp}</div>` : ""}</div>
    <div class="testrow"><button class="btn" data-test="${id}">Test connection</button><span class="tres" id="tres-${id}"></span></div>
    ${id === "plex" ? `<div id="plexLibs"></div>` : ""}`;
  // Every connection folds the same way. A card opens by itself when it needs attention:
  // a required one that isn't set up, or any whose last test failed.
  if (!c.optional && !set) openCards.add(id);
  const head = `<img class="svc" src="${fav(SVC_SITE[id])}" alt=""> ${c.title}`;
  return foldCard(id, head, c.optional ? pill(set) : pill(set, "Set up", "Needed"), c.sub, body);
}
const RANGE_FMT = {};   // how each slider shows its value, so moving it shows the same thing
const allOr = v => +v ? v : "All";   // 0 = keep everything
function rangeField(k, label, min, max, step, help, fmt = v => v) {
  RANGE_FMT[k] = fmt;
  return `<div class="field"><label>${label} ${srcTag(k)}</label>
    <div class="rangeval"><input type="range" data-k="${k}" min="${min}" max="${max}" step="${step}" value="${val(k)}"><output>${fmt(val(k))}</output></div>
    ${help ? `<div class="help">${help}</div>` : ""}</div>`;
}
function renderSettings() {
  const st = S.stats, when = t => t ? new Date(t * 1000).toLocaleString() : "never";
  const hours = [`<option value="-1">Off</option>`].concat([...Array(24).keys()].map(h =>
    `<option value="${h}">${new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" })}</option>`)).join("");
  const lbSet = !!val("LETTERBOXD_USERNAME"), lbExport = lastStatus.letterboxd_export;
  const traktSet = !!(val("TRAKT_USERNAME") && val("TRAKT_CLIENT_ID"));
  const sections = [
    ["connections", "Connections", setSection("connections", "Connections",
      `${svcCard("plex")}${svcCard("tmdb")}${svcCard("seerr")}${svcCard("radarr")}${svcCard("sonarr")}`)],
    ["history", "Watch history", setSection("history", "Watch history", `
      ${foldCard("letterboxd", `<img class="svc" src="${fav("letterboxd.com")}" alt=""> Letterboxd`,
        lbSet && !lbExport ? `<span class="pill warn">Export not uploaded</span>` : pill(lbSet, lbExport ? "Full history" : "Set up"),
        "Your diary feed on every sync, plus your full history from an export.", `
      <div class="field"><label>Username ${srcTag("LETTERBOXD_USERNAME")}</label>${input("LETTERBOXD_USERNAME", "text", "your Letterboxd username")}
        <div class="help">Every sync reads your public diary feed. The feed only holds your most recent entries (about the last 50 films you logged), so it keeps Filmhound current but doesn't bring in your history. For that, upload your export below once. Letterboxd has no public API, so Filmhound can read but not write.</div></div>
      <div class="field"><label>Full history</label>
        <div class="filerow"><label class="btn">Upload export (.zip)<input type="file" id="lbFile" accept=".zip,.csv" hidden></label>
          <button class="btn" data-import="letterboxd-rss">Read diary now</button></div>
        <div class="help">Get the export at letterboxd.com → Settings → Data → Export your data, and upload the zip as it is. It brings in every film you've logged and rated, not just recent ones. Upload a new one if you rate older films without logging them. <b>Read diary now</b> only reads your latest diary entries.</div>
        ${lbExport ? `<div class="help">Full history imported${lastStatus.letterboxd_export_at ? " " + new Date(lastStatus.letterboxd_export_at * 1000).toLocaleDateString() : ""}.</div>`
          : lbSet ? `<div class="help warn">Your export hasn't been uploaded yet, so only your most recent diary entries are in Filmhound. Older ratings are missing, and those films can show up on the Rate page.</div>` : ""}</div>
      <div class="import-status"></div>`)}
      ${foldCard("trakt", `<img class="svc" src="${fav("trakt.tv")}" alt=""> Trakt`, pill(traktSet), "Movies and shows you've watched and rated on Trakt, read on every sync.", `
      <div class="field"><label>Username ${srcTag("TRAKT_USERNAME")}</label>${input("TRAKT_USERNAME", "text", "your Trakt username")}</div>
      <div class="field"><label>Client ID ${srcTag("TRAKT_CLIENT_ID")}</label>${input("TRAKT_CLIENT_ID")}
        <div class="help">Create a free app at trakt.tv/oauth/applications (any name, redirect <code>urn:ietf:wg:oauth:2.0:oob</code>) and paste its Client ID. Your profile needs to be public.</div></div>
      <div class="field"><label></label><div><button class="btn" data-import="trakt">Sync Trakt now</button></div></div>`)}`,
      "Movies you've watched outside Plex train your taste and stop showing up as picks.")],
    ["taste", "Your taste", setSection("taste", "Your taste", `<div class="scard">
      ${rangeField("OWNED_WEIGHT", "Titles you own, unwatched", 0, 1, 0.05,
        "How much the things you've collected but not watched yet shape your taste. Keep it high if your library is curated; lower it if it's a bit of everything.", v => Math.round(v * 100) + "%")}
      <h4>Don't learn from</h4>
      <p class="sub">Choose anything that shouldn't shape your picks. It stays in your library and still counts as owned.</p>
      <p class="ns-sum" id="nsSum">${nsSummary()}</p>
      <div class="field top"><label>Genres to leave out</label><div><div class="chips pick" id="nsgQuick">${nsgQuick()}</div>
        <div id="nsgMore"></div>
        <div class="help">Titles in any of these genres, in every library.</div></div></div>
      <div class="field"><label>Titles left out</label><div id="noseedTitles"><span class="muted">None yet. Open any title you own and tick <b>Don't learn from this</b>.</span></div></div>
    </div>`, "Ratings count most, then what you've watched, then what you own. These settings adjust the rest.")],
    ["picks", "Picks", setSection("picks", "Picks", `<div class="scard">
      ${rangeField("MIN_VOTES", "Minimum TMDB votes: movies", 0, 2000, 25, "Skip obscure titles with fewer votes than this. Older decades need far fewer, so classics aren't cut.")}
      ${rangeField("MIN_VOTES_TV", "Minimum TMDB votes: TV", 0, 1000, 10)}
      ${advanced("picks-adv", `
        <div class="opt"><label class="inline"><input type="checkbox" data-k="IMDB_ENABLED" ${val("IMDB_ENABLED") ? "checked" : ""}> Use IMDb ratings ${srcTag("IMDB_ENABLED")}</label>
          <p class="help">Downloads IMDb's official ratings file weekly (~8 MB) for better quality scores.</p></div>
        <h4>Movies</h4>
        ${rangeField("MAX_RECS", "Picks to keep", 0, 3000, 100, "<b>All</b> keeps every pick that scores. Lower it to keep only the best ones.", allOr)}
        ${rangeField("CANDIDATE_POOL", "Candidates to score", 200, 5000, 100, "More = broader picks, slower first sync.")}
        <h4>TV shows</h4>
        ${rangeField("MAX_RECS_TV", "Picks to keep", 0, 2000, 50, "", allOr)}
        ${rangeField("CANDIDATE_POOL_TV", "Candidates to score", 100, 3000, 100)}`)}
    </div>
    <div class="scard" id="provCard">
      <div class="cardhead"><h3>Streaming services</h3>${pill((val("MY_PROVIDERS") || []).length, `${(val("MY_PROVIDERS") || []).length} chosen`, "None chosen")}</div>
      <p class="sub">Pick the services you pay for. Their logos show on posters, and the <b>On my services</b> filter shows only what you can watch right now.</p>
      <div class="field"><label>Country ${srcTag("WATCH_REGION")}</label><select data-k="WATCH_REGION" id="regionSel"><option>${esc(val("WATCH_REGION"))}</option></select></div>
      <div class="provgrid" id="provGrid"><div class="shimmer" style="height:90px"></div></div>
      <div class="help" style="margin-top:8px">Availability comes from JustWatch through TMDB. Changing country refreshes it on the next sync.</div>
    </div>`)],
    ["plex", "Plex", setSection("plex", "Plex", `<div class="scard">
      <div class="field"><label>Collection name ${srcTag("PLEX_COLLECTION")}</label>${input("PLEX_COLLECTION")}
        <div class="help">Used by <b>Make Plex collection</b> on the Your Library page. Filmhound replaces this collection each time.</div></div>
      <h4>Copy Letterboxd &amp; Trakt ratings to Plex</h4>
      <div class="field"><label>Automatically ${srcTag("RATINGS_TO_PLEX")}</label>
        <select data-k="RATINGS_TO_PLEX">
          <option value="off" ${val("RATINGS_TO_PLEX") === "off" ? "selected" : ""}>Don't change Plex</option>
          <option value="fill" ${val("RATINGS_TO_PLEX") === "fill" ? "selected" : ""}>Fill in titles I haven't rated in Plex</option>
          <option value="overwrite" ${val("RATINGS_TO_PLEX") === "overwrite" ? "selected" : ""}>Make Plex match (replace different ratings)</option>
        </select>
        <div class="help">Runs on its own after every import and sync. Titles are matched by their TMDB ID, so the same film in both places lines up exactly.
          With <b>Make Plex match</b>, a rating you change in Plex (or here) for a film you also rated on Letterboxd or Trakt goes back to their rating on the next sync, so change it there instead.
          Ratings are never sent to Letterboxd or Trakt.</div></div>
      <div class="field top"><label>Once, right now</label><div><div class="filerow">
        <button class="btn" data-import="plex-fill">Fill in unrated now</button>
        <button class="btn warn" data-import="plex-overwrite" data-confirm>Make Plex match now</button></div>
        <div class="help">One-off copies, whatever the setting above says. Handy for a single catch-up while keeping the automatic copy off.</div></div></div>
      <div class="import-status"></div>
    </div>`, "Your Plex server connection and libraries are under Connections.")],
    ["sync", "Sync &amp; data", setSection("sync", "Sync &amp; data", `<div class="scard">
      <div class="field"><label>Nightly sync ${srcTag("SYNC_HOUR")}</label><select data-k="SYNC_HOUR">${hours}</select>
        <div class="help">Re-reads Plex, Seerr, Radarr and Sonarr and refreshes your picks once a day.</div></div>
      <div class="stats">
        <div class="stat"><b>${st.movies.toLocaleString()}</b><span>movies in Plex</span></div>
        <div class="stat"><b>${st.shows.toLocaleString()}</b><span>shows in Plex</span></div>
        <div class="stat"><b>${st.cached.toLocaleString()}</b><span>titles cached from TMDB</span></div>
        <div class="stat"><b>${st.imdb ? (st.imdb / 1e6).toFixed(1) + "M" : "—"}</b><span>IMDb ratings</span></div>
        <div class="stat"><b>${st.feedback}</b><span>titles with feedback</span></div>
        <div class="stat"><b>${(st.db_bytes / 1e6).toFixed(0)} MB</b><span>database</span></div>
      </div>
      <p class="sub">Last full sync: ${when(st.last_sync)} · IMDb ratings updated: ${when(st.imdb_at)}</p>
      <div class="danger">
        <button class="btn" id="setSync">Sync now</button>
        <button class="btn" data-maint="refresh-imdb">Refresh IMDb ratings on next sync</button>
        <button class="btn warn" data-maint="clear-cache">Clear TMDB cache</button>
        <span class="resetfb"><button class="btn warn" data-maint="clear-feedback" data-ask>Reset all feedback</button><span class="muted">${st.feedback.toLocaleString()} title${st.feedback === 1 ? "" : "s"}</span></span>
      </div>
    </div>`)],
    ["signin", "Sign-in", setSection("signin", "Sign-in", `<div class="scard">
      ${lastStatus.auth ? `<p class="sub">Sign-in is on for <b>${esc(lastStatus.user)}</b>.</p>
        <div class="filerow"><button class="btn" id="authChange">Change password</button><button class="btn warn" id="authOff">Turn off sign-in</button><button class="btn" id="logout">Sign out</button></div>`
        : `<p class="sub">Off. Anyone who can reach this page can use it, including Settings. Turn it on if Filmhound is reachable beyond your own devices.</p>`}
      <div id="authForm" class="authform" ${lastStatus.auth ? "hidden" : ""}>
        <div class="field"><label>Username</label><input type="text" id="authUser" value="${esc(lastStatus.user || "")}" autocomplete="username"></div>
        <div class="field"><label>Password</label><input type="password" id="authPass" autocomplete="new-password" placeholder="at least 8 characters"></div>
        <div class="field"><label></label><div><button class="btn primary" id="authSave">${lastStatus.auth ? "Save password" : "Turn on sign-in"}</button></div></div>
      </div>
    </div>`)],
  ];
  $("settingsBody").innerHTML = `
    ${S.configured ? "" : `<div class="setup-banner"><h3>Welcome to Filmhound</h3>Connect Plex and add a TMDB key to get started. Seerr, Radarr and Sonarr are optional. Test each one, then save. Prefer a step-by-step? <a class="link accent" href="#/setup">Use the setup walkthrough</a>.</div>`}
    ${Object.values(S.sources).includes("env") ? `<div class="setup-banner env"><b>Some settings come from your docker-compose file.</b>
      Move them into Filmhound so you can manage everything here and delete them from the compose file.
      <div style="margin-top:10px"><button class="btn primary" id="adoptEnv">Move them into Filmhound</button></div></div>` : ""}
    ${setLayout(sections)}
    <p class="muted" style="text-align:center;font-size:12px;margin-top:24px">Filmhound ${esc(lastStatus.version || "")} · Movie data from TMDB · Streaming data from JustWatch · Ratings from IMDb</p>`;
  watchSections();
  focusPendingCard();
  $("settingsBody").querySelector('[data-k="SYNC_HOUR"]').value = val("SYNC_HOUR");
  loadProviderPicker();
  loadNoseed();
  renderImportStatus(lastStatus.import);
  if (plexLibs) renderLibs();
}
const NOSEED_GENRE_LIST = ["Animation", "Family", "Kids", "Documentary", "Music", "Horror", "Romance", "Reality", "Talk", "News", "Soap", "TV Movie"];
// Quick picks, plus any chosen genre that isn't one of them, so the folded view never hides a choice
let nsgAll = null, nsgOpen = false;
function nsgChip(g, count) {
  return `<button type="button" data-nsg="${esc(g)}" class="${(val("NOSEED_GENRES") || []).includes(g) ? "on" : ""}">${esc(g)}${count != null ? ` <small>${count}</small>` : ""}</button>`;
}
function nsgQuick() {
  const extra = (val("NOSEED_GENRES") || []).filter(g => !NOSEED_GENRE_LIST.includes(g));
  return [...NOSEED_GENRE_LIST, ...extra].map(g => nsgChip(g)).join("");
}
function renderNsgMore() {
  const el = $("nsgMore"); if (!el || !nsgAll) return;
  if (!nsgAll.length) { el.innerHTML = `<p class="muted nsg-none">Sync once to list your genres.</p>`; return; }
  el.innerHTML = nsgOpen
    ? `<div class="chips pick nsg-all">${nsgAll.map(g => nsgChip(g.name, g.count)).join("")}</div><button type="button" class="link accent nsg-tog" data-nsg-more="0">Fewer genres</button>`
    : `<button type="button" class="link accent nsg-tog" data-nsg-more="1">More genres…</button>`;
}
async function loadNoseed() {
  try {
    const [ns, gs] = await Promise.all([api("/api/noseed"), api("/api/noseed/genres")]);
    nsgAll = gs.genres; renderNsgMore();
    if (ns.items.length) $("noseedTitles").innerHTML = `<div class="chips">${ns.items.map(x =>
      `<span class="rm">${esc(x.title || "Untitled")}<button type="button" data-unseed="${x.key}" title="Learn from it again">×</button></span>`).join("")}</div>`;
  } catch {}
}
function renderLibs() {
  if (!$("plexLibs") || !plexLibs) return;
  if ($("nsSum")) $("nsSum").innerHTML = nsSummary();
  $("plexLibs").innerHTML = plexLibs.length ? `<div class="field top"><label>Libraries</label><div>${libRows(plexLibs, val("PLEX_LIBRARIES") || [], val("NOSEED_LIBRARIES") || [], "data-lib")}</div></div>` : "";
}
$("settingsBody").addEventListener("input", e => {
  const el = e.target, k = el.dataset.k;
  if (k) {
    draft[k] = el.type === "checkbox" ? el.checked : el.type === "range" ? +el.value : el.value;
    if (el.type === "range") el.nextElementSibling.textContent = (RANGE_FMT[k] || (v => v))(el.value);
    const svc = Object.keys(SVC).find(id => SVC[id].url === k || SVC[id].key === k);
    if (svc) { $("tres-" + svc).textContent = ""; }
    setDirty(true);
  }
});
$("settingsBody").addEventListener("click", async e => {
  const t = e.target.closest("[data-test]");
  if (t) { testSvc(t.dataset.test); return; }
  const lm = e.target.closest("[data-lib][data-mode]");
  if (lm) {
    const lists = libModeLists(plexLibs, lm.dataset.lib, lm.dataset.mode, val("PLEX_LIBRARIES") || [], val("NOSEED_LIBRARIES") || []);
    if (!lists) { toast("Filmhound needs at least one library to read."); return; }
    Object.assign(draft, lists); setDirty(true); renderLibs(); $("nsSum").innerHTML = nsSummary(); return;
  }
  const more = e.target.closest("[data-nsg-more]");
  if (more) { nsgOpen = more.dataset.nsgMore === "1"; renderNsgMore(); if (!nsgOpen) $("nsgQuick").innerHTML = nsgQuick(); return; }
  const ng = e.target.closest("[data-nsg]");
  if (ng) {
    const v = ng.dataset.nsg, cur = new Set(val("NOSEED_GENRES") || []); cur.has(v) ? cur.delete(v) : cur.add(v);
    draft.NOSEED_GENRES = [...cur]; setDirty(true); $("nsSum").innerHTML = nsSummary();
    document.querySelectorAll("#settingsBody [data-nsg]").forEach(b => { if (b.dataset.nsg === v) b.classList.toggle("on", cur.has(v)); });
    return;
  }
  const un = e.target.closest("[data-unseed]");
  if (un) {
    try { await api("/api/noseed", { method: "POST", body: JSON.stringify({ key: +un.dataset.unseed, on: false }) }); loadNoseed(); toast("It'll shape your picks again."); } catch (err) { toast(err.message); }
    return;
  }
  if (e.target.id === "setSync") { try { await api("/api/sync", { method: "POST" }); wasRunning = true; poll(); toast("Sync started"); } catch (err) { toast(err.message); } return; }
  const m = e.target.closest("[data-maint]");
  if (m) {
    if (m.hasAttribute("data-ask")) { if (!window.confirm(RESET_FEEDBACK)) return; }
    else if (m.classList.contains("warn") && !m.classList.contains("armed")) {
      m.classList.add("armed"); m.dataset.label = m.textContent; m.textContent = "Click again to confirm";
      setTimeout(() => { m.classList.remove("armed"); if (m.dataset.label) m.textContent = m.dataset.label; }, 4000); return;
    }
    try { await api("/api/maintenance/" + m.dataset.maint, { method: "POST" }); toast("Done"); loadSettings(); } catch (err) { toast(err.message); }
  }
});
// What /api/maintenance/clear-feedback deletes: every row of the feedback table (all of History) and the
// Letterboxd export marker. Ratings stored in Plex, the watchlist and "Don't learn from" live elsewhere.
const RESET_FEEDBACK = `Reset all feedback?

This clears everything on the History page:
• titles you marked Not interested or Seen it
• star ratings you gave titles you don't own in Plex
• everything imported from Letterboxd and Trakt
• the Requested and Added lists

Ratings saved to Plex, your watchlist and Don't learn from stay as they are. Trakt and your recent Letterboxd diary come back at the next sync; upload your Letterboxd export again for the rest.`;
async function testSvc(id, quiet) {
  const out = $("tres-" + id); if (!out) return;
  out.className = "tres"; out.textContent = "Testing…";
  const body = {}; Object.keys(S.values).forEach(k => body[k] = val(k));
  try {
    const r = await api("/api/settings/test/" + id, { method: "POST", body: JSON.stringify(body) });
    out.className = "tres " + (r.ok ? "ok" : "bad"); if (!r.ok) openFor(id); out.textContent = (r.ok ? "✓ " : "✕ ") + r.message;
    if (id === "plex" && r.libraries) { plexLibs = r.libraries; renderLibs(); }
  } catch (err) { openFor(id); out.className = "tres bad"; out.textContent = "✕ " + err.message; if (quiet) out.textContent = ""; }
}
$("discard").onclick = () => { draft = {}; setDirty(false); renderSettings(); };
$("saveBtn").onclick = async () => {
  try {
    const wasConfigured = S.configured;
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify(draft) });
    S = r; draft = {}; setDirty(false); renderSettings();
    lastStatus.seerr = !!(S.values.SEERR_URL && S.values.SEERR_API_KEY);
    lastStatus.radarr = !!(S.values.RADARR_URL && S.values.RADARR_API_KEY);
    lastStatus.sonarr = !!(S.values.SONARR_URL && S.values.SONARR_API_KEY);
    Object.keys(arrOpts).forEach(k => delete arrOpts[k]);
    lastStatus.my_provider_ids = S.values.MY_PROVIDERS; lastStatus.my_providers = S.values.MY_PROVIDERS.length;
    lastStatus.seerr_url = S.values.SEERR_URL; lastStatus.radarr_url = S.values.RADARR_URL; lastStatus.sonarr_url = S.values.SONARR_URL;
    const needSync = r.changed.some(k => /PLEX|TMDB|SEERR|RADARR|SONARR|VOTES|POOL|MAX_RECS|OWNED|NOSEED|IMDB|REGION|LETTERBOXD|TRAKT/.test(k));
    if (!wasConfigured && r.configured) {
      await api("/api/sync", { method: "POST" }); wasRunning = true; poll();
      toast("Saved · building your recommendations now");
      go("home");
    } else if (needSync && r.configured) {
      toast("Saved", { label: "Sync now to apply", fn: async () => { await api("/api/sync", { method: "POST" }); userSync = wasRunning = true; poll(); } });
    } else toast(r.changed.length ? "Saved" : "Nothing changed");
  } catch (err) { toast(err.message); }
};
window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

// streaming service picker
let provList = [], regionList = [];
async function loadProviderPicker() {
  const region = val("WATCH_REGION");
  try {
    if (!regionList.length) regionList = await api("/api/regions");
    if (regionList.length) $("regionSel").innerHTML = regionList.map(r => `<option value="${r.code}" ${r.code === region ? "selected" : ""}>${esc(r.name)}</option>`).join("");
    provList = await api("/api/providers?region=" + encodeURIComponent(region));
  } catch { $("provGrid").innerHTML = `<p class="muted">Add a TMDB key first.</p>`; return; }
  const mine = new Set(val("MY_PROVIDERS"));
  const top = provList.slice(0, 60);
  $("provGrid").innerHTML = top.length ? top.map(p => `<button class="provpick ${mine.has(p.id) ? "on" : ""}" data-prov="${p.id}" title="${esc(p.name)}">
      <img src="${IMG}w92${p.logo}" alt=""><span>${esc(p.name)}</span></button>`).join("") : `<p class="muted">No services listed for this country.</p>`;
}
$("settingsBody").addEventListener("click", e => {
  const b = e.target.closest("[data-prov]"); if (!b) return;
  const cur = new Set(val("MY_PROVIDERS")); const id = b.dataset.prov;
  cur.has(id) ? cur.delete(id) : cur.add(id);
  draft.MY_PROVIDERS = [...cur]; b.classList.toggle("on"); setDirty(true);
});
$("settingsBody").addEventListener("change", e => { if (e.target.id === "regionSel") { draft.WATCH_REGION = e.target.value; draft.MY_PROVIDERS = []; setDirty(true); loadProviderPicker(); } });

// imports
function renderImportStatus(st) {
  if (!st) return;
  document.querySelectorAll("#settingsBody .import-status").forEach(el => {
  if (st.running) el.innerHTML = `<span class="tres">${esc({ plex: "Copying ratings to Plex", letterboxd: "Importing from Letterboxd", trakt: "Importing from Trakt" }[st.source] || "Importing")}… ${st.total ? `${st.done.toLocaleString()} / ${st.total.toLocaleString()}` : ""}</span>
    ${st.total ? `<div class="bar-track" style="margin:8px 0 0"><div class="bar-fill" style="width:${Math.round(100 * st.done / st.total)}%"></div></div>` : ""}`;
  else if (st.error) el.innerHTML = `<span class="tres bad">✕ ${esc(st.error)}</span>`;
  else if (st.message) el.innerHTML = `<span class="tres ok">✓ ${esc(st.message)}</span>`;
  else el.innerHTML = "";
  });
}
let importPoll = null;
function watchImport() {
  clearInterval(importPoll);
  let fails = 0;
  importPoll = setInterval(async () => {
    let st; try { st = await api("/api/import"); fails = 0; } catch (e) { if (++fails >= 5) clearInterval(importPoll); return; }
    lastStatus.import = st; renderImportStatus(st);
    if (!st.running) { clearInterval(importPoll); if (st.message) toast(st.message); }
  }, 1000);
}
$("settingsBody").addEventListener("click", async e => {
  const b = e.target.closest("[data-import]"); if (!b) return;
  if (dirty) { toast("Save your changes first"); return; }
  if (b.hasAttribute("data-confirm") && !b.classList.contains("armed")) {
    b.classList.add("armed"); b.dataset.label = b.textContent; b.textContent = "Click again: this replaces ratings in Plex";
    setTimeout(() => { b.classList.remove("armed"); b.textContent = b.dataset.label; }, 4000); return;
  }
  try { await api("/api/import/" + b.dataset.import, { method: "POST" }); watchImport(); } catch (err) { toast(err.message); }
});
$("settingsBody").addEventListener("change", async e => {
  if (e.target.id !== "lbFile" || !e.target.files[0]) return;
  const f = e.target.files[0];
  try {
    const r = await fetch("/api/import/letterboxd?filename=" + encodeURIComponent(f.name), { method: "POST", body: f, headers: { "Content-Type": "application/octet-stream" } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    watchImport();
  } catch (err) { toast(err.message); }
  e.target.value = "";
});

// sign-in
$("settingsBody").addEventListener("click", async e => {
  const id = e.target.id;
  if (id === "authChange") { $("authForm").hidden = false; $("authPass").focus(); }
  if (id === "authSave") {
    try { await api("/api/auth", { method: "POST", body: JSON.stringify({ username: $("authUser").value, password: $("authPass").value }) });
      lastStatus.auth = true; lastStatus.user = $("authUser").value.trim(); toast("Sign-in is on"); renderSettings(); }
    catch (err) { toast(err.message); }
  }
  if (id === "authOff") {
    if (!e.target.classList.contains("armed")) { e.target.classList.add("armed"); e.target.textContent = "Click again to confirm"; return; }
    try { await api("/api/auth", { method: "POST", body: JSON.stringify({ enabled: false }) }); lastStatus.auth = false; toast("Sign-in is off"); renderSettings(); } catch (err) { toast(err.message); }
  }
  if (id === "logout") { try { await api("/api/logout", { method: "POST" }); } catch (err) { toast(err.message); return; } location.href = "/login"; }
  if (id === "adoptEnv") {
    try { S = await api("/api/settings/adopt-env", { method: "POST" }); renderSettings(); toast("Done. You can remove those lines from docker-compose.yml now"); }
    catch (err) { toast(err.message); }
  }
});


// ---- navigation
const VIEWS = ["home", "setup", "picks", "library", "watchlist", "rate", "history", "settings", "person", "search", "like", "gaps", "upcoming"];
const LIST_VIEWS = ["watchlist", "search", "like", "gaps", "upcoming"];
// The Up next tab holds the pages of things you don't have yet, with a switch row between them (like Recordhound's Missing).
// #/next opens the first one.
const UP_NEXT = [["gaps", "Collections"], ["upcoming", "Coming soon"]];
const upNextSwitch = on => `<div class="seg" role="tablist" aria-label="Up next">${UP_NEXT.map(([v, l]) =>
  `<button role="tab" data-go="${v}" aria-selected="${v === on}" class="${v === on ? "on" : ""}">${l}</button>`).join("")}</div>`;
$("subnav").addEventListener("click", e => { const b = e.target.closest("[data-go]"); if (b && !b.classList.contains("on")) go(b.dataset.go); });
function showView(v, keepFilters) {
  view = v;
  document.querySelectorAll(".view").forEach(x => x.classList.remove("on"));
  const sec = LIST_VIEWS.includes(v) ? "v-list" : { home: "v-home", picks: "v-grid", library: "v-grid", rate: "v-rate", history: "v-history", settings: "v-settings", setup: "v-setup", person: "v-person" }[v];
  $(sec).classList.add("on");
  const upNext = UP_NEXT.some(([x]) => x === v);   // the switch stays put; only the page under it changes
  document.querySelectorAll("#nav button, #gear").forEach(b => b.classList.toggle("on", b.dataset.view === v || (upNext && b.dataset.view === "next")));
  $("subnav").hidden = !upNext; if (upNext) { $("subnav").innerHTML = upNextSwitch(v); requestAnimationFrame(filterFade); }
  const gridView = v === "picks" || v === "library";
  $("filters").style.display = $("activeRow").style.display = gridView ? "" : "none";
  if (gridView) requestAnimationFrame(filterFade);
  $("mediaSeg").style.visibility = ["setup", "history", "settings", "person", "search", "like"].includes(v) ? "hidden" : "";
  if (v !== "settings") $("savebar").classList.remove("on");
  if (v !== "search") $("gq").value = "";   // leaving search clears the box
  closePop(); closeDrawer(); window.scrollTo(0, 0); tuckHeader(false);
  if (gridView) {
    if (!keepFilters) loadF();
    syncControls();
    $("libHead").innerHTML = "";
    $("grid").innerHTML = skeleton(); $("count").textContent = "";
    loadFacets().catch(e => toast(e.message))
      .then(() => { if (view === v) return load(true); })   // not if you've already left for another page
      .then(() => { if (view === v && v === "library") $("libHead").innerHTML = libHead(); });
  }
  if (v === "home") loadHome();
  if (v === "watchlist") loadWatchlist();
  if (v === "gaps") loadGaps();
  if (v === "upcoming") loadUpcoming();
  if (v === "rate") loadRate();
  if (v === "history") loadHidden();
  if (v === "settings") loadSettings();
  if (v === "setup") loadSetup();
}
let skipRoute = false;
function route() {
  if (skipRoute) { skipRoute = false; return; }   // drill() already showed the view with its preset filters
  const [, v, arg] = (location.hash || "#/home").split("/");
  if (dirty && v !== "settings" && !confirmLeave()) { history.replaceState(null, "", "#/settings"); return; }
  if (v === "person" && arg) { showView("person"); loadPerson(+arg); return; }
  if (v === "search") { showView("search"); runSearch(safeDecode(arg || "")); return; }
  if (v === "like" && arg) { showView("like"); loadLike(arg, +(location.hash.split("/")[3])); return; }
  if (v === "person" || v === "like") { go("home"); return; }   // an address with the id missing
  if (v === "next") { history.replaceState(null, "", "#/" + UP_NEXT[0][0]); route(); return; }
  if (VIEWS.includes(v) && !(v === view && document.querySelector(".view.on") && v !== "picks" && v !== "library")) showView(v);
  else if (!VIEWS.includes(v)) showView("home");
}
const safeDecode = s => { try { return decodeURIComponent(s); } catch { return s; } };   // a hand-typed address like #/search/100%
function go(v) { if (location.hash === "#/" + v) showView(v); else location.hash = "#/" + v; }
window.addEventListener("hashchange", route);
document.querySelectorAll("#nav button, #gear").forEach(b => b.onclick = () => go(b.dataset.view));
function setMedia(m, reload = true) {
  media = m; store.set("media", m);
  document.querySelectorAll("#mediaSeg button").forEach(b => b.classList.toggle("on", b.dataset.m === m));
  if (reload) showView(["person", "search", "like"].includes(view) ? "home" : view);
}
$("mediaSeg").onclick = e => { const b = e.target.closest("[data-m]"); if (b && b.dataset.m !== media) setMedia(b.dataset.m); };
function refreshView() { if (view === "home") loadHome(); else if (view === "picks" || view === "library") load(true); else if (view === "watchlist") loadWatchlist(); }


const APP_SETUP = {
  name: "Filmhound",
  intro: "Movie and TV recommendations built from your own Plex library and watch history, with a reason for every pick.",
  requiredText: "connect Plex and add a free TMDB key",
  required: ["plex", "tmdb"],
  why: {
    plex: "Filmhound reads your movie and TV libraries, what you've watched and your star ratings. Nothing in Plex changes unless you ask.",
    tmdb: "TMDB supplies recommendations, posters, cast and crew. The key is free: create an account at <a href=\"https://www.themoviedb.org/settings/api\" target=\"_blank\" rel=\"noopener\">themoviedb.org</a>, then Settings → API → create a key (any app name).",
  },
  extras: [
    { title: "Bring in your Letterboxd history", why: "Everything you've watched and rated there trains your picks and stops it showing up as a suggestion. Upload your export once.", card: "letterboxd", done: () => !!lastStatus.letterboxd_export },
    { title: "Or connect Trakt", why: "Movies and shows you've watched and rated on Trakt.", card: "trakt", done: () => !!lastStatus.trakt },
    { title: "Pick your streaming services", why: "See which picks you can watch right now, and filter to just those.", card: "provCard", done: () => (lastStatus.my_providers || 0) > 0 },
    { title: "Connect Seerr, Radarr or Sonarr", why: "Request or add anything you like in one click.", card: "seerr", done: () => !!(lastStatus.seerr || lastStatus.radarr || lastStatus.sonarr) },
  ],
  syncText: "Filmhound is reading your library and looking up every title on TMDB. A few thousand titles take a few minutes.",
  waitTips: [
    { title: "Rate what you've watched", why: "Ratings are the strongest signal there is. The Rate page goes one title at a time.", label: "Open Rate", view: "rate" },
    { title: "Upload your Letterboxd export", why: "If you use Letterboxd, this brings in everything you've rated there.", label: "Set up", card: "letterboxd", done: () => !!lastStatus.letterboxd_export },
  ],
  readyText: "Every pick shows why it was picked. Rate a few titles and your picks get sharper.",
  doneLabel: "See your picks",
  checklist: () => [
    { title: "Connect Plex and TMDB", done: !!lastStatus.configured, card: "plex" },
    { title: "Finish the first sync", done: !!lastStatus.last_sync },
    { title: "Bring in your Letterboxd or Trakt history", done: !!(lastStatus.letterboxd_export || lastStatus.trakt), card: "letterboxd" },
    { title: "Pick your streaming services", done: (lastStatus.my_providers || 0) > 0, card: "provCard" },
    { title: "Connect Seerr, Radarr or Sonarr", done: !!(lastStatus.seerr || lastStatus.radarr || lastStatus.sonarr), card: "seerr" },
  ],
};
// ------------------------------------------------------------------ first-run setup walkthrough
// Shown instead of Settings on a fresh install. Uses the same settings and connection tests as
// Settings; only the presentation differs. APP_SETUP (above) says what's required and what to suggest.
let wz = { step: 0, vals: {}, ok: {}, msg: {}, libs: null, busy: false }, wzTimer = null, pendingCard = null;
const wzSteps = () => ["welcome", ...APP_SETUP.required, "extras", "sync"];
const wzVal = k => wz.vals[k] ?? "";
async function loadSetup() {
  store.set("onboarding", "1");
  try { S = await api("/api/settings"); } catch {}
  if (lastStatus.configured && wz.step < wzSteps().indexOf("extras")) wz.step = wzSteps().indexOf("extras");
  if (S && !wz.vals.PLEX_URL) wz.vals.PLEX_URL = S.values.PLEX_URL || "";
  renderSetup();
}
function openSetting(card) {   // jump to one card in Settings, opened
  pendingCard = card; openCards.add(card);
  go("settings");
}
function focusPendingCard() {
  if (!pendingCard) return;
  const el = $("svc-" + pendingCard) || $(pendingCard); pendingCard = null;
  if (!el) return;
  if (el.tagName === "DETAILS") el.open = true;
  setTimeout(() => window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - headerOffset(), behavior: "smooth" }), 50);
}
function wzField(k, label, help, ph = "") {
  const v = wzVal(k);
  return `<div class="field"><label>${label}</label>${isSecret(k, v) ? secretInput("data-wz-k", k, v, ph) : `<input type="text" data-wz-k="${k}" value="${esc(v)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">`}
    ${help ? `<div class="help">${help}</div>` : ""}</div>`;
}
function renderSetup() {
  clearInterval(wzTimer);
  const steps = wzSteps(), id = steps[wz.step], n = steps.length;
  const dots = `<div class="wz-dots">${steps.map((s, i) => `<span class="${i < wz.step ? "done" : i === wz.step ? "on" : ""}"></span>`).join("")}</div>`;
  let body = "";
  if (id === "welcome") {
    body = `<h1>Welcome to ${APP_SETUP.name}</h1><p class="lead">${APP_SETUP.intro}</p>
      <p class="muted">Setup takes a couple of minutes: ${APP_SETUP.requiredText}, then a few optional extras that make your picks better.</p>
      <div class="wz-actions"><span></span><button class="btn primary" data-wz="next">Get started</button></div>
      <p class="muted wz-skip">Rather do it yourself? <a href="#/settings">Go to Settings</a></p>`;
  } else if (APP_SETUP.required.includes(id)) {
    const c = SVC[id], tested = wz.ok[id];
    body = `<p class="wz-step">Step ${wz.step} of ${n - 1}</p><h2>Connect ${c.title}</h2><p class="lead">${APP_SETUP.why[id]}</p>
      <div class="scard">
        ${c.url ? wzField(c.url, "URL", c.urlHelp, "http://") : ""}
        ${wzField(c.key, c.keyLabel, c.keyHelp)}
        <div class="testrow"><button class="btn" data-wz="test">Test connection</button>
          <span class="tres ${tested ? "ok" : wz.msg[id] ? "bad" : ""}">${wz.msg[id] ? (tested ? "✓ " : "✕ ") + esc(wz.msg[id]) : ""}</span></div>
        ${id === "plex" && wz.libs && wz.libs.length ? `<div class="field top wz-libs"><label>Libraries</label><div>${libRows(wz.libs, wz.vals.PLEX_LIBRARIES || [], wz.vals.NOSEED_LIBRARIES || [], "data-wz-lib")}
          <div class="help">You can change this later in Settings.</div></div></div>` : ""}
      </div>
      <div class="wz-actions"><button class="btn" data-wz="back">Back</button>
        <button class="btn primary" data-wz="next" ${tested && !wz.busy ? "" : "disabled"}>${wz.busy ? "Saving…" : "Continue"}</button></div>
      ${tested ? "" : `<p class="muted wz-skip">Test the connection to continue.</p>`}`;
  } else if (id === "extras") {
    const items = APP_SETUP.extras.filter(x => !x.hide || !x.hide());
    body = `<p class="wz-step">Step ${wz.step} of ${n - 1}</p><h2>Make your picks better</h2>
      <p class="lead">All optional, and all of it can wait. ${lastStatus.running ? "Your first sync is already running in the background." : ""}</p>
      <div class="wz-list">${items.map(x => `<div class="wz-item"><div><b>${x.title}</b>${x.done() ? pill(true, "Done") : ""}<p>${x.why}</p></div>
        <button class="btn" data-wz-card="${x.card}">${x.done() ? "Change" : "Set up"}</button></div>`).join("")}</div>
      <p class="muted">Each <b>Set up</b> opens that part of Settings. You can also do these any time later from Settings.</p>
      <div class="wz-actions"><span></span><button class="btn primary" data-wz="next">Continue</button></div>`;
  } else if (id === "sync") {
    const s = lastStatus, ready = s.last_sync && !s.running;
    body = `<h2>${ready ? "Your picks are ready" : s.error && !s.running ? "The first sync hit a problem" : "Building your picks"}</h2>
      ${ready ? `<p class="lead">${APP_SETUP.readyText}</p><div class="wz-actions"><span></span><button class="btn primary" data-wz="done">${APP_SETUP.doneLabel}</button></div>`
        : s.error && !s.running ? `<p class="lead tres bad">${esc(s.error)}</p><p class="muted">Fix the setting it mentions, then start the sync again.</p>
          <div class="wz-actions"><a class="btn" href="#/settings">Open Settings</a><button class="btn primary" data-wz="resync">Try again</button></div>`
        : `<p class="lead">${APP_SETUP.syncText}</p>
          <div class="wz-progress"><div class="bar-track"><div class="bar-fill wz-indef"></div></div><p class="muted" id="wzStep">${esc(s.step && s.step !== "idle" ? s.step + (s.progress ? " · " + s.progress : "") + "…" : "Starting…")}</p></div>
          <h4 class="wz-h4">While you wait</h4>
          <div class="wz-list">${APP_SETUP.waitTips.map(t => { const done = !!(t.done && t.done()); return `<div class="wz-item"><div><b>${t.title}</b>${done ? pill(true, "Done") : ""}<p>${t.why}</p></div>
            <button class="btn" ${t.card ? `data-wz-card="${t.card}"` : `data-wz-go="${t.view}"`}>${done && t.card ? "Change" : t.label}</button></div>`; }).join("")}</div>
          <p class="muted">You can leave this page; the sync keeps going.</p>`}`;
    if (!ready) wzTimer = setInterval(async () => {
      if (view !== "setup") return clearInterval(wzTimer);
      try { lastStatus = await api("/api/status"); } catch { return; }
      if ((lastStatus.last_sync && !lastStatus.running) || (lastStatus.error && !lastStatus.running)) renderSetup();
      else if ($("wzStep")) $("wzStep").textContent = lastStatus.step && lastStatus.step !== "idle" ? `${lastStatus.step}${lastStatus.progress ? " · " + lastStatus.progress : ""}…` : "Starting…";
    }, 2000);
  }
  $("setupBody").innerHTML = `<div class="wz">${dots}${body}</div>`;
}
async function wzTest() {
  const id = wzSteps()[wz.step], c = SVC[id];
  const vals = { ...(S ? S.values : {}), [c.key]: wzVal(c.key) };
  if (c.url) vals[c.url] = wzVal(c.url);
  wz.msg[id] = "Testing…"; wz.ok[id] = false; renderSetup();
  try {
    const r = await api("/api/settings/test/" + id, { method: "POST", body: JSON.stringify(vals) });
    wz.ok[id] = !!r.ok; wz.msg[id] = r.message;
    if (id === "plex" && r.libraries) wz.libs = r.libraries;
  } catch (e) { wz.ok[id] = false; wz.msg[id] = e.message; }
  renderSetup();
}
async function wzNext() {
  const steps = wzSteps(), id = steps[wz.step];
  if (APP_SETUP.required.includes(id) && id === APP_SETUP.required[APP_SETUP.required.length - 1]) {
    // last required step: save everything required, then start the first sync so it runs during the extras
    const body = {};
    APP_SETUP.required.forEach(r => { const c = SVC[r]; if (c.url) body[c.url] = wzVal(c.url); body[c.key] = wzVal(c.key); });
    if (wz.vals.PLEX_LIBRARIES) body.PLEX_LIBRARIES = wz.vals.PLEX_LIBRARIES;
    if (wz.vals.NOSEED_LIBRARIES) body.NOSEED_LIBRARIES = wz.vals.NOSEED_LIBRARIES;
    wz.busy = true; renderSetup();
    try {
      S = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      lastStatus.configured = S.configured;
      if (S.configured && !lastStatus.running) { await api("/api/sync", { method: "POST" }); lastStatus.running = true; wasRunning = true; poll(); }
    } catch (e) { wz.busy = false; wz.msg[id] = e.message; wz.ok[id] = false; renderSetup(); return; }
    wz.busy = false;
  }
  wz.step = Math.min(steps.length - 1, wz.step + 1); renderSetup(); window.scrollTo(0, 0);
}
$("setupBody").addEventListener("input", e => {
  const k = e.target.dataset.wzK;
  if (k) { wz.vals[k] = e.target.value; const id = wzSteps()[wz.step]; if (wz.ok[id]) { wz.ok[id] = false; wz.msg[id] = ""; renderSetup(); const el = document.querySelector(`[data-wz-k="${k}"]`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } } }
});
$("setupBody").addEventListener("click", async e => {
  const a = e.target.closest("[data-wz]"), card = e.target.closest("[data-wz-card]"), gv = e.target.closest("[data-wz-go]");
  const lm = e.target.closest("[data-wz-lib][data-mode]");
  if (lm) {
    const lists = libModeLists(wz.libs, lm.dataset.wzLib, lm.dataset.mode, wz.vals.PLEX_LIBRARIES || [], wz.vals.NOSEED_LIBRARIES || []);
    if (!lists) { toast("Filmhound needs at least one library to read."); return; }
    Object.assign(wz.vals, lists); renderSetup(); return;
  }
  if (card) { openSetting(card.dataset.wzCard); return; }
  if (gv) { go(gv.dataset.wzGo); return; }
  if (!a) return;
  const act = a.dataset.wz;
  if (act === "next") wzNext();
  else if (act === "back") { wz.step = Math.max(0, wz.step - 1); renderSetup(); }
  else if (act === "test") wzTest();
  else if (act === "done") { store.set("onboarding", "1"); go("home"); }
  else if (act === "resync") { try { await api("/api/sync", { method: "POST" }); lastStatus.running = true; lastStatus.error = null; wasRunning = true; poll(); renderSetup(); } catch (err) { toast(err.message); } }
});

// ---- Getting started checklist on For You (only for installs set up with the walkthrough)
function checklistHTML() {
  if (store.get("onboarding") !== "1" || store.get("checklistDone") === "1") return "";
  const items = APP_SETUP.checklist();
  if (items.every(x => x.done)) { store.set("checklistDone", "1"); return ""; }
  const left = items.filter(x => !x.done).length;
  return `<div class="gs-card"><div class="gs-head"><h3>Getting started</h3><span class="muted">${left} left</span>
      <button class="link" data-gs="dismiss">Hide</button></div>
    <div class="gs-items">${items.map(x => `<div class="gs-item ${x.done ? "done" : ""}"><span class="gs-check">${x.done ? "✓" : ""}</span>
      <span>${x.title}</span>${x.done ? "" : x.card ? `<button class="link accent" data-gs-card="${x.card}">Set up</button>` : x.view ? `<button class="link accent" data-gs-go="${x.view}">Open</button>` : ""}</div>`).join("")}</div></div>`;
}
const _loadHomeBase = loadHome;
loadHome = async (...args) => {
  await _loadHomeBase(...args);
  const html = checklistHTML();
  if (html && $("homeRows") && !$("homeRows").querySelector(".gs-card")) $("homeRows").insertAdjacentHTML("afterbegin", html);
};
$("homeRows").addEventListener("click", e => {
  const d = e.target.closest("[data-gs]"), c = e.target.closest("[data-gs-card]"), g = e.target.closest("[data-gs-go]");
  if (d) { store.set("checklistDone", "1"); e.target.closest(".gs-card")?.remove(); toast("Hidden. Everything's still in Settings."); }
  else if (c) openSetting(c.dataset.gsCard);
  else if (g) go(g.dataset.gsGo);
});

// ---- status / sync
let wasRunning = false, userSync = false;
async function poll() {
  try {
    const s = await api("/api/status"); lastStatus = s;
    if (!items.length && (view === "picks" || view === "library") && !loading) $("grid").innerHTML = emptyState();
    let txt, title = "";
    const ago = t => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
    if (s.running) txt = `${s.step}${s.progress ? " · " + s.progress : ""}…`;
    else if (s.error) txt = `Sync failed`, title = s.error;
    else txt = s.last_sync ? `Synced ${ago(s.last_sync)}` : "Not synced yet",
      title = `${s.movie_count.toLocaleString()} movies · ${s.show_count.toLocaleString()} shows in Plex`;
    $("status").textContent = txt; $("status").title = title;
    $("status").classList.toggle("bad", !!(s.error && !s.running)); $("status").classList.toggle("busy", !!s.running);
    $("sync").disabled = s.running;
    $("sync").classList.toggle("spin", !!s.running);
    if (wasRunning && !s.running) {
      if (s.error) toast("Sync failed: " + s.error);
      // a sync you started reloads the page; a background re-score doesn't yank your scroll position
      if (userSync || !items.length) refreshView();
      else if (["home", "picks", "library"].includes(view)) toast("Your picks were updated", { label: "Refresh", fn: refreshView });
      userSync = false;
    }
    wasRunning = s.running;
  } catch (e) { if (/sign in/i.test(e.message)) location.href = "/login"; }
  clearTimeout(poll._t); poll._t = setTimeout(poll, wasRunning ? 2000 : 15000);
}

$("sync").onclick = async () => { try { await api("/api/sync", { method: "POST" }); userSync = wasRunning = true; poll(); } catch (e) { toast(e.message); } };


(async () => {
  try { lastStatus = await api("/api/status"); } catch {}
  media = store.get("media") === "tv" ? "tv" : "movie";
  document.querySelectorAll("#mediaSeg button").forEach(b => b.classList.toggle("on", b.dataset.m === media));
  if (lastStatus.configured === false) location.hash = "#/setup";
  route();
  poll();
  if (lastStatus.import?.running) watchImport();
})();
