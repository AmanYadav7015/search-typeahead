/* ============================================================
   Typeahead Console — frontend logic (wired to the real backend)
   Aman Yadav · 24bcs10183
   ============================================================ */

const $ = (id) => document.getElementById(id);
const NODE_COLORS = {            // assigned in node order A,B,C,D
  "cache-node-A": "#34d39a",
  "cache-node-B": "#7c93ff",
  "cache-node-C": "#f5b454",
  "cache-node-D": "#ef6f9b",
};
const DEBOUNCE_MS = 180;

let mode = "recency";
let activeIdx = -1;
let suggestions = [];
let committed = "";
let trendingSet = new Set();      // queries currently trending -> "hot" badge
let inflight = null;
let debounceTimer = null;
let ringLayout = null;            // {nodes, vnodes, vnodes_per_node}

const input = $("search");
const dropdown = $("dropdown");
const ddRows = $("dropdownRows");

/* ───────── helpers ───────── */
function fmt(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "K";
  return String(n);
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function setLoading(on) { $("searchBox").classList.toggle("loading", on); }
function showError(msg) {
  $("errMsg").textContent = msg;
  $("errBanner").classList.remove("hidden");
}
function hideError() { $("errBanner").classList.add("hidden"); }
$("errDismiss").addEventListener("click", hideError);

/* ───────── search input ───────── */
input.addEventListener("input", () => {
  $("clearBtn").classList.toggle("hidden", !input.value);
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(fetchSuggest, DEBOUNCE_MS);
});
input.addEventListener("focus", () => { if (suggestions.length) dropdown.classList.remove("hidden"); });
input.addEventListener("blur", () => setTimeout(() => dropdown.classList.add("hidden"), 170));
input.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); if (suggestions.length) { activeIdx = (activeIdx + 1) % suggestions.length; paintActive(); } }
  else if (e.key === "ArrowUp") { e.preventDefault(); if (suggestions.length) { activeIdx = (activeIdx - 1 + suggestions.length) % suggestions.length; paintActive(); } }
  else if (e.key === "Enter") { const q = activeIdx >= 0 && suggestions[activeIdx] ? suggestions[activeIdx].query : input.value; if (q.trim()) submit(q.trim()); }
  else if (e.key === "Escape") { dropdown.classList.add("hidden"); activeIdx = -1; }
});

$("goBtn").addEventListener("click", () => { if (input.value.trim()) submit(input.value.trim()); });
$("clearBtn").addEventListener("click", () => {
  input.value = ""; suggestions = []; committed = ""; activeIdx = -1;
  dropdown.classList.add("hidden"); $("clearBtn").classList.add("hidden");
  $("inspector").className = "inspector empty";
  $("inspector").textContent = "start typing to route a prefix key…";
  input.focus();
});

$("segPop").addEventListener("click", () => setMode("popularity"));
$("segRec").addEventListener("click", () => setMode("recency"));
function setMode(m) {
  mode = m;
  $("segPop").classList.toggle("on", m === "popularity");
  $("segRec").classList.toggle("on", m === "recency");
  if (committed) fetchSuggest();
}

/* ───────── /suggest ───────── */
async function fetchSuggest() {
  const q = input.value.trim();
  if (!q) {
    suggestions = []; committed = ""; dropdown.classList.add("hidden");
    return;
  }
  if (inflight) inflight.abort();
  const ctrl = new AbortController(); inflight = ctrl;
  setLoading(true);
  try {
    const res = await fetch(`/suggest?q=${encodeURIComponent(q)}&mode=${mode}`,
      { signal: ctrl.signal });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const data = await res.json();
    hideError();
    committed = data.prefix;
    suggestions = data.suggestions;
    renderDropdown(data);
    renderInspector(data);
    routeRing(data.prefix);
    refreshMetrics();
  } catch (e) {
    if (e.name === "AbortError") return;          // superseded by a newer keystroke
    console.error(e);
    showError(`Couldn't fetch suggestions — ${e.message}. Is the backend running?`);
  } finally {
    if (inflight === ctrl) setLoading(false);      // only clear if this was the latest
  }
}

function renderDropdown(data) {
  activeIdx = -1;
  const p = data.prefix;
  if (!suggestions.length) { dropdown.classList.add("hidden"); return; }
  ddRows.innerHTML = suggestions.map((s, i) => {
    const lq = s.query.toLowerCase();
    const pre = lq.startsWith(p) ? esc(s.query.slice(0, p.length)) : "";
    const post = lq.startsWith(p) ? esc(s.query.slice(p.length)) : esc(s.query);
    const hot = trendingSet.has(s.query)
      ? `<span class="dd-hot">▲ TRENDING</span>` : "";
    return `<div class="dd-row" data-i="${i}" data-q="${esc(s.query)}">
      <div class="dd-left">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6.5"></circle><path d="M21 21l-4.6-4.6"></path></svg>
        <span class="dd-q"><span class="pre">${pre}</span><span class="post">${post}</span></span>
        ${hot}
      </div>
      <span class="dd-count">${fmt(s.count)}</span>
    </div>`;
  }).join("");
  [...ddRows.children].forEach((el) => {
    const i = +el.dataset.i;
    el.addEventListener("mousedown", () => submit(el.dataset.q));
    el.addEventListener("mouseenter", () => { activeIdx = i; paintActive(); });
  });
  const tag = $("ddTag");
  tag.className = "tag " + (data.source === "hit" ? "hit" : "miss");
  tag.textContent = `${data.source === "hit" ? "CACHE HIT" : "CACHE MISS"} · ${data.latency_ms}ms`;
  dropdown.classList.remove("hidden");
}
function paintActive() {
  [...ddRows.children].forEach((el, i) => el.classList.toggle("active", i === activeIdx));
}

function renderInspector(d) {
  const insp = $("inspector");
  insp.className = "inspector";
  const col = d.cache_node ? (NODE_COLORS[d.cache_node] || "#e9ecf2") : "#e9ecf2";
  const hit = d.source === "hit";
  insp.innerHTML = `
    <div class="insp-row"><span class="insp-k">prefix</span><span class="insp-v">"${esc(d.prefix)}"</span></div>
    <div class="insp-row"><span class="insp-k">mode</span><span class="insp-v">${d.mode}</span></div>
    <div class="insp-row"><span class="insp-k">cache</span><span class="tag ${hit ? "hit" : "miss"}">${hit ? "CACHE HIT" : "CACHE MISS"}</span></div>
    <div class="insp-row"><span class="insp-k">latency</span><span class="accent">${d.latency_ms}ms</span></div>
    <div class="insp-row"><span class="insp-k">routed to</span><span style="color:${col};font-weight:600">${d.cache_node || "—"}</span></div>
    <div class="insp-row"><span class="insp-k">results</span><span class="insp-v">${d.suggestions.length}</span></div>`;
}

/* ───────── /search ───────── */
async function submit(query) {
  query = (query || "").trim();
  if (!query) return;
  input.value = query;
  $("clearBtn").classList.remove("hidden");
  dropdown.classList.add("hidden");
  activeIdx = -1;
  try {
    const res = await fetch("/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const data = await res.json();
    hideError();
    $("searchResult").classList.remove("hidden");
    $("resQuery").textContent = `"${data.query}"`;
    $("resNote").textContent = "— recorded, buffered for batch write";
    $("resJson").textContent = JSON.stringify({ message: data.message }, null, 2);

    await refreshTrending();
    if (committed) fetchSuggest();
    setTimeout(refreshMetrics, 250);
  } catch (e) {
    console.error(e);
    showError(`Search failed — ${e.message}. Is the backend running?`);
  }
}

/* ───────── /trending ───────── */
async function refreshTrending() {
  const r = await fetch("/trending").then((r) => r.json());
  const list = r.trending || [];
  trendingSet = new Set(list.map((t) => t.query));
  const box = $("trending");
  if (!list.length) {
    box.innerHTML = `<div class="tr-empty">No recent activity yet — search something to make it trend.</div>`;
    return;
  }
  const max = Math.max(...list.map((t) => t.score), 0.0001);
  box.innerHTML = list.map((t, i) => {
    const pct = Math.max(5, Math.round((t.score / max) * 100));
    const hot = i < 3;
    const barCol = hot ? "var(--accent)" : "rgba(255,255,255,0.22)";
    const scoreCol = hot ? "var(--accent)" : "var(--faint)";
    return `<div class="tr-row" style="animation-delay:${i * 35}ms">
      <span class="tr-rank">${String(i + 1).padStart(2, "0")}</span>
      <div class="tr-mid">
        <div class="tr-q">${esc(t.query)}</div>
        <div class="tr-track"><div class="tr-bar" style="width:${pct}%;background:${barCol}"></div></div>
      </div>
      <span class="tr-score" style="color:${scoreCol}">${t.score.toFixed(1)}</span>
    </div>`;
  }).join("");
}

/* ───────── /stats ───────── */
async function refreshMetrics() {
  const s = await fetch("/stats").then((r) => r.json());
  $("storeSize").textContent = fmt(s.db.rows);

  $("m-p95").textContent = `${s.latency_ms.p95}ms`;
  $("m-avg").textContent = `p50 ${s.latency_ms.p50}ms`;
  $("m-hit").textContent = `${(s.cache.overall_hit_rate * 100).toFixed(0)}%`;
  $("m-hitsub").textContent = `${fmt(s.cache.overall_hits)} hits / ${fmt(s.cache.overall_misses)} miss`;
  $("m-reads").textContent = fmt(s.db.total_db_reads);
  $("m-writes").textContent = fmt(s.db.total_rows_written);
  $("m-writesub").textContent = `${fmt(s.db.total_search_events)} submits batched`;
  const saved = Math.max(0, s.db.total_search_events - s.db.total_rows_written);
  $("m-saved").textContent = fmt(saved);

  const max = s.batch.flush_batch_size || 200;
  const cnt = s.batch.buffered || 0;
  $("batchFlush").textContent = `flush @ ${max} or ${s.batch.flush_interval_s}s`;
  $("bufBar").style.width = Math.min(100, Math.round((cnt / max) * 100)) + "%";
  $("bufCount").textContent = `${cnt} / ${max} buffered`;
  $("bufExtra").textContent = `${fmt(s.batch.total_flushes)} flushes · aggregated`;
}

/* ───────── consistent-hash ring SVG ───────── */
const SVGNS = "http://www.w3.org/2000/svg";
function el(tag, attrs, text) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (text != null) n.textContent = text;
  return n;
}
async function initRing() {
  ringLayout = await fetch("/ring").then((r) => r.json());
  renderLegend(null);
  drawRing(null, null);
}
function drawRing(keyPos, ownerNode) {
  const holder = $("ringSvg");
  holder.innerHTML = "";
  if (!ringLayout) return;
  const S = 300, c = S / 2, rad = 112;
  const svg = el("svg", { viewBox: `0 0 ${S} ${S}` });
  svg.appendChild(el("circle", { cx: c, cy: c, r: rad, fill: "none", stroke: "rgba(255,255,255,0.10)", "stroke-width": 1 }));
  // virtual nodes around the ring
  for (const v of ringLayout.vnodes) {
    const a = v.pos * Math.PI * 2 - Math.PI / 2;
    svg.appendChild(el("circle", {
      cx: c + Math.cos(a) * rad, cy: c + Math.sin(a) * rad, r: 2.3,
      fill: NODE_COLORS[v.node] || "#888", opacity: 0.5,
    }));
  }
  if (keyPos != null && ownerNode) {
    const col = NODE_COLORS[ownerNode] || "#34d39a";
    const a = keyPos * Math.PI * 2 - Math.PI / 2;
    const x = c + Math.cos(a) * rad, y = c + Math.sin(a) * rad;
    svg.appendChild(el("line", { x1: c, y1: c, x2: x, y2: y, stroke: col, "stroke-width": 1.5, opacity: 0.85 }));
    svg.appendChild(el("circle", { cx: x, cy: y, r: 7.5, fill: "none", stroke: col, "stroke-width": 2 }));
    svg.appendChild(el("circle", { cx: x, cy: y, r: 4, fill: col }));
    svg.appendChild(el("text", { x: c, y: c - 4, "text-anchor": "middle", fill: "#e9ecf2", "font-size": 14, "font-family": "IBM Plex Mono, monospace", "font-weight": 600 }, ownerNode));
    svg.appendChild(el("text", { x: c, y: c + 14, "text-anchor": "middle", fill: "#9097a3", "font-size": 10.5, "font-family": "IBM Plex Mono, monospace" }, `owns "${committed}"`));
  } else {
    svg.appendChild(el("text", { x: c, y: c + 4, "text-anchor": "middle", fill: "#5d646f", "font-size": 11.5, "font-family": "IBM Plex Mono, monospace" }, "type to route a key"));
  }
  holder.appendChild(svg);
}
function renderLegend(ownerNode) {
  if (!ringLayout) return;
  $("nodeLegend").innerHTML = ringLayout.nodes.map((id) => {
    const col = NODE_COLORS[id] || "#888";
    const owns = id === ownerNode;
    const ownBadge = owns
      ? `<span class="lg-own" style="color:${col};border:1px solid ${col}66">OWNER</span>` : "";
    return `<div class="lg-row">
      <div class="lg-left"><span class="lg-dot" style="background:${col};box-shadow:0 0 8px ${col}88"></span><span class="lg-id">${id}</span></div>
      <div class="lg-right"><span class="lg-vn">${ringLayout.vnodes_per_node} vnodes</span>${ownBadge}</div>
    </div>`;
  }).join("");
}
async function routeRing(prefix) {
  if (!prefix) return;
  const d = await fetch(`/cache/debug?prefix=${encodeURIComponent(prefix)}`).then((r) => r.json());
  drawRing(d.key_pos, d.owner_node);
  renderLegend(d.owner_node);
}

/* ───────── init ───────── */
Promise.all([initRing(), refreshTrending(), refreshMetrics()])
  .then(hideError)
  .catch((e) => {
    console.error(e);
    showError("Can't reach the backend. Start it with: uvicorn main:app --port 8765");
  });
setInterval(() => refreshMetrics().catch(() => {}), 4000);
