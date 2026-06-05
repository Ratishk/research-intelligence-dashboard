"use strict";

// ─── Utilities ───────────────────────────────────────────────────────────────
const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
};

const toast = msg => {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2800);
};

const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c])
);

const fmtAgo = iso => {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60)    return `${Math.floor(d)}s ago`;
  if (d < 3600)  return `${Math.floor(d/60)}m ago`;
  if (d < 86400) return `${Math.floor(d/3600)}h ago`;
  return `${Math.floor(d/86400)}d ago`;
};

const fmtDate = iso => iso ? new Date(iso).toLocaleDateString(undefined,{month:"short",day:"numeric"}) : "";

const TYPE_ICONS = {
  youtube:"▶", twitter:"𝕏", rss:"●", forum:"◆", sec:"§",
  reddit:"⊕", hackernews:"▲", arxiv:"∑", article:"●", video:"▶",
  tweet:"𝕏", filing:"§", thread:"◆",
};

const typeIcon = t => TYPE_ICONS[t] || "·";

const emptyState = (icon, title, sub) =>
  `<div class="empty"><div class="empty-icon">${icon}</div>
   <div class="empty-title">${esc(title)}</div>
   <div class="empty-sub">${esc(sub)}</div></div>`;

// ─── Ingest status ────────────────────────────────────────────────────────────
let _statusPoll = null;

async function refreshIngestStatus() {
  try {
    const s = await api("/api/ingest/status");
    const el = document.getElementById("ingest-status");
    const btn = document.getElementById("ingest-btn");
    const cls = s.running ? "running" : s.last_run ? "done" : "";
    const label = s.running
      ? `Ingesting… ${s.today_items ? `(${s.today_items} today)` : ""}`
      : `${s.total_items.toLocaleString()} items · ${s.yesterday_items} yesterday`;
    el.innerHTML = `<span class="ingest-dot ${cls}"></span>${label}`;
    if (!s.running && _statusPoll) {
      clearInterval(_statusPoll);
      _statusPoll = null;
      btn.disabled = false;
      btn.textContent = "Run ingestion";
      const total = Object.values(s.last_counts || {}).reduce((a,b) => a+b, 0);
      if (total) toast(`Ingested ${total} new items`);
      renderTab(document.querySelector("#tabs button.active")?.dataset.tab);
    }
  } catch (_) {}
}

// ─── BRIEF ───────────────────────────────────────────────────────────────────
let _briefIndustry = "";

async function populateBriefIndustries() {
  const sel = document.getElementById("brief-industry");
  if (sel.dataset.loaded) return;
  try {
    const inds = await api("/api/industries");
    inds.filter(i => i.signal_count > 0)
        .sort((a,b) => b.signal_count - a.signal_count)
        .forEach(i => {
          const o = document.createElement("option");
          o.value = i.name;
          o.textContent = `${i.name} (${i.signal_count})`;
          sel.appendChild(o);
        });
    sel.dataset.loaded = "1";
  } catch (_) {}
}

async function renderBrief() {
  await populateBriefIndustries();
  const q = _briefIndustry ? `&industry=${encodeURIComponent(_briefIndustry)}` : "";
  const data = await api(`/api/brief?hours=48${q}`);

  // Stats row — bullish/bearish are clickable to jump to Signals tab
  const { signal_count, by_direction, today_items, total_items } = data;
  document.getElementById("brief-stats").innerHTML = [
    { label:"Items in DB",    value:total_items.toLocaleString(), cls:"accent",  dir:"" },
    { label:"Signals (48h)",  value:signal_count,                 cls:"",        dir:"" },
    { label:"Bullish signals",value:by_direction.bullish || 0,    cls:"bull",    dir:"bullish" },
    { label:"Bearish signals",value:by_direction.bearish || 0,    cls:"bear",    dir:"bearish" },
  ].map(s => `<div class="stat-card${s.dir ? " clickable" : ""}" ${s.dir ? `data-jump="${s.dir}"` : ""}>
    <div class="stat-value ${s.cls}">${s.value}</div>
    <div class="stat-label">${s.label}</div>
  </div>`).join("");

  document.querySelectorAll(".stat-card[data-jump]").forEach(card =>
    card.addEventListener("click", () => {
      const dir = card.dataset.jump;
      // Set direction filter and switch to Signals tab
      _sigDirection = dir;
      document.querySelectorAll(".dir-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.dir === dir);
      });
      switchTab("signals");
    })
  );

  document.getElementById("brief-hours").textContent = "Last 48 hours";

  // Top actionable signals
  const sigEl = document.getElementById("brief-signals");
  if (!data.top_actionable.length) {
    sigEl.innerHTML = emptyState("📡","No signals yet",
      "Run ingestion, then wait for hourly classification — or trigger it manually.");
  } else {
    sigEl.innerHTML = data.top_actionable.map(s => briefSigCard(s)).join("");
    sigEl.querySelectorAll("[data-research]").forEach(btn =>
      btn.addEventListener("click", () => triggerResearch(btn))
    );
  }

  // Sector pulse
  const secEl = document.getElementById("brief-sectors");
  if (!data.sector_pulse.length) {
    secEl.innerHTML = `<div style="color:var(--text-3);font-size:12px;padding:8px 0">No sector data yet</div>`;
  } else {
    const maxTotal = Math.max(...data.sector_pulse.map(s => s.total), 1);
    secEl.innerHTML = data.sector_pulse.map(s => {
      const bullPct = Math.round((s.bullish / s.total) * 100);
      const bearPct = Math.round((s.bearish / s.total) * 100);
      return `<div class="sector-row">
        <span class="sector-name" title="${esc(s.sector)}">${esc(s.sector)}</span>
        <div class="sector-bar-wrap">
          <div class="sector-bar-bull" style="height:${bullPct}%"></div>
          <div class="sector-bar-bear" style="height:${bearPct}%"></div>
        </div>
        <span class="sector-counts"><span style="color:var(--bull)">▲${s.bullish}</span> <span style="color:var(--bear)">▼${s.bearish}</span></span>
      </div>`;
    }).join("");
  }

  // Hot tickers
  const tickEl = document.getElementById("brief-tickers");
  if (!data.hot_tickers.length) {
    tickEl.innerHTML = `<div style="color:var(--text-3);font-size:12px;padding:8px 0">No ticker data yet</div>`;
  } else {
    tickEl.innerHTML = data.hot_tickers.map(t => {
      const total = t.total || 1;
      const bullW = Math.round((t.bullish / total) * 100);
      const bearW = Math.round((t.bearish / total) * 100);
      const cls = t.bullish > t.bearish ? "bull" : t.bearish > t.bullish ? "bear" : "";
      return `<div class="hot-ticker-row">
        <span class="ht-sym" style="color:${cls === "bull" ? "var(--bull)" : cls === "bear" ? "var(--bear)" : "var(--text)"}">${esc(t.ticker)}</span>
        <div class="ht-bar">
          <div class="ht-bull" style="width:${bullW}%"></div>
          <div class="ht-bear" style="width:${bearW}%"></div>
        </div>
        <span class="ht-counts">${t.bullish}↑ ${t.bearish}↓</span>
      </div>`;
    }).join("");
  }

  // Under the radar
  const radarWrap = document.getElementById("brief-radar-wrap");
  const radarEl   = document.getElementById("brief-radar");
  if (data.under_the_radar && data.under_the_radar.length) {
    radarWrap.style.display = "block";
    radarEl.innerHTML = data.under_the_radar.map(radarCard).join("");
    radarEl.querySelectorAll("[data-research]").forEach(btn =>
      btn.addEventListener("click", () => triggerResearch(btn))
    );
  } else {
    radarWrap.style.display = "none";
  }

  // AI synthesis placeholder
  const aiEl = document.getElementById("ai-synthesis");
  if (!aiEl.dataset.loaded) {
    aiEl.innerHTML = `<span style="color:var(--text-3);font-size:13px">Click "Generate brief" for an AI synthesis of today's signals.</span>`;
  }
}

function briefSigCard(s) {
  const tickers = (s.entities.tickers || []).slice(0, 5);
  const techs   = (s.entities.technologies || []).slice(0, 3);
  return `<div class="sig-card ${esc(s.direction)}" style="margin-bottom:10px">
    <div class="sig-top">
      <span class="sig-dir ${esc(s.direction)}">${s.direction}</span>
      <span class="sig-summary">${esc(s.summary)}</span>
      <span class="sig-conf">${Math.round(s.confidence*100)}%</span>
    </div>
    <div class="confbar"><div class="confbar-fill" style="width:${Math.round(s.confidence*100)}%"></div></div>
    <div class="sig-meta">
      <span class="sig-type-tag">${esc(s.signal_type.replace(/_/g," "))}</span>
      ${tickers.map(t => `<span class="sig-ticker">${esc(t)}</span>`).join("")}
      ${techs.map(t => `<span class="sig-tech">🔬 ${esc(t)}</span>`).join("")}
      ${s.speculative ? `<span class="sig-spec">speculative</span>` : ""}
      ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener" class="sig-source-link">source ↗</a>` : ""}
      <span class="sig-time">${fmtAgo(s.created_at)}</span>
    </div>
    <div id="brief-${s.id}"></div>
    <div class="sig-meta" style="margin-top:6px">
      <button class="btn-sm" data-research="${s.id}">Get research brief</button>
    </div>
  </div>`;
}

function radarCard(s) {
  const score = s.alpha_score ?? 0;
  const cls   = score >= 9 ? "high" : score >= 6 ? "mid" : "low";
  const tickers = (s.entities.tickers || []).slice(0, 4);
  return `<div class="radar-card">
    <div class="sig-top">
      <span class="sig-dir ${esc(s.direction)}">${s.direction}</span>
      <span class="sig-summary">${esc(s.summary)}</span>
      <span style="display:flex;gap:6px;align-items:center;flex-shrink:0">
        <span class="alpha-badge ${cls}">${score}/12 · ${esc(s.alpha_label || "")}</span>
        <span class="sig-conf">${Math.round(s.confidence*100)}%</span>
      </span>
    </div>
    <div class="confbar"><div class="confbar-fill" style="width:${Math.round(s.confidence*100)}%"></div></div>
    <div class="sig-meta">
      <span class="sig-type-tag">${esc(s.signal_type.replace(/_/g," "))}</span>
      ${tickers.map(t => `<span class="sig-ticker">${esc(t)}</span>`).join("")}
      ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener" class="sig-source-link">source ↗</a>` : ""}
      <span class="sig-time">${fmtAgo(s.created_at)}</span>
    </div>
    <div id="brief-${s.id}"></div>
    <div class="sig-meta" style="margin-top:6px">
      <button class="btn-sm" data-research="${s.id}">Get research brief</button>
    </div>
  </div>`;
}

async function triggerResearch(btn) {
  const id = btn.dataset.research;
  btn.disabled = true; btn.textContent = "Researching…";
  try {
    const r = await api(`/api/signals/${id}/research`, { method:"POST" });
    const el = document.getElementById(`brief-${id}`) || document.getElementById(`rb-${id}`);
    if (el) el.innerHTML = `<div class="research-brief">${esc(r.research_brief || "No brief returned")}</div>`;
  } catch { toast("Research brief failed — check PERPLEXITY_API_KEY"); }
  btn.textContent = "Get research brief"; btn.disabled = false;
}

// AI synthesis
document.getElementById("synthesize-btn")?.addEventListener("click", async btn => {
  const b = document.getElementById("synthesize-btn");
  b.disabled = true; b.textContent = "Generating…";
  try {
    const r = await api("/api/brief/synthesize", { method:"POST" });
    const el = document.getElementById("ai-synthesis");
    el.dataset.loaded = "1";
    const text = r.synthesis || "";
    el.innerHTML = text.split("\n").filter(Boolean).map(line =>
      `<span class="bullet">${esc(line)}</span>`
    ).join("");
  } catch { toast("AI synthesis failed"); }
  b.textContent = "Generate brief"; b.disabled = false;
});

// ─── FEED ────────────────────────────────────────────────────────────────────
let _feedTab = "feed";

async function renderFeed() {
  const el = document.getElementById("feed-items");
  el.innerHTML = `<div class="empty"><div class="empty-icon">⌛</div><div class="empty-title">Loading…</div></div>`;
  const tab = _feedTab === "feed" ? "" : _feedTab;
  const path = tab ? `/api/feed?tab=${tab}&limit=80` : "/api/feed?limit=80";
  const items = await api(path);
  if (!items.length) {
    el.innerHTML = emptyState("📭","No items yet","Run ingestion to start pulling articles, videos, and posts.");
    return;
  }
  el.innerHTML = items.map(feedCard).join("");
}

function feedCard(it) {
  const icon = typeIcon(it.source_type || it.media_type);
  const t1 = it.tier === 1;
  return `<div class="feed-card">
    <div class="feed-type-icon">${icon}</div>
    <div class="feed-card-body">
      <a href="${esc(it.url)}" class="feed-title" target="_blank" rel="noopener">${esc(it.title)}</a>
      <div class="feed-meta">
        <span class="tag ${t1 ? "t1" : "t2"}">${t1 ? "Tier 1" : "Tier 2"}</span>
        <span class="tag">${esc(it.source)}</span>
        ${it.author ? `<span style="color:var(--text-3);font-size:11px">${esc(it.author)}</span>` : ""}
        <span style="color:var(--text-3);font-size:11px">${fmtDate(it.published_at || it.ingested_at)}</span>
        ${it.relevance != null ? `<span style="color:var(--text-3);font-size:11px">${Math.round(it.relevance*100)}% relevance</span>` : ""}
      </div>
    </div>
  </div>`;
}

document.querySelectorAll(".filter-btn[data-feed]").forEach(btn =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn[data-feed]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _feedTab = btn.dataset.feed;
    renderFeed();
  })
);

// ─── SIGNALS ─────────────────────────────────────────────────────────────────
let _sigDirection = "";
let _sigSort = "newest";

function alphaBadge(s) {
  if (s.alpha_score == null) return "";
  const score = s.alpha_score;
  const cls = score >= 9 ? "high" : score >= 6 ? "mid" : "low";
  return `<span class="alpha-badge ${cls}">${score}/12 · ${esc(s.alpha_label || "")}</span>`;
}

async function renderSignals() {
  const type   = document.getElementById("sig-type")?.value;
  const ticker = document.getElementById("sig-ticker")?.value?.trim();
  const params = new URLSearchParams({ limit: 150, sort: _sigSort });
  if (_sigDirection) params.set("direction", _sigDirection);
  if (type)          params.set("type", type);
  if (ticker)        params.set("ticker", ticker);
  const signals = await api(`/api/signals?${params}`);
  const el = document.getElementById("signal-list");
  if (!signals.length) {
    el.innerHTML = emptyState("📊","No signals match","Try removing filters, or run ingestion and wait for hourly classification.");
    return;
  }
  el.innerHTML = signals.map(sigCard).join("");
  el.querySelectorAll("[data-research]").forEach(btn =>
    btn.addEventListener("click", () => triggerResearch(btn))
  );
}

function sigCard(s) {
  const tickers = (s.entities.tickers || []).slice(0, 6);
  const techs   = (s.entities.technologies || []).slice(0, 4);
  return `<div class="sig-card ${esc(s.direction)}">
    <div class="sig-top">
      <span class="sig-dir ${esc(s.direction)}">${s.direction}</span>
      <span class="sig-summary">${esc(s.summary)}</span>
      <span style="display:flex;gap:6px;align-items:center;flex-shrink:0">
        ${alphaBadge(s)}
        <span class="sig-conf">${Math.round(s.confidence*100)}%</span>
      </span>
    </div>
    <div class="confbar"><div class="confbar-fill" style="width:${Math.round(s.confidence*100)}%"></div></div>
    <div class="sig-meta">
      <span class="sig-type-tag">${esc(s.signal_type.replace(/_/g," "))}</span>
      ${s.industry ? `<span class="tag">${esc(s.industry)}</span>` : ""}
      ${tickers.map(t => `<span class="sig-ticker">${esc(t)}</span>`).join("")}
      ${techs.map(t => `<span class="sig-tech">🔬 ${esc(t)}</span>`).join("")}
      ${s.speculative ? `<span class="sig-spec">speculative</span>` : ""}
      ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener" class="sig-source-link">source ↗</a>` : ""}
      <span class="sig-time">${fmtAgo(s.created_at)}</span>
    </div>
    ${s.research_brief ? `<div class="research-brief">${esc(s.research_brief)}</div>` : `<div id="rb-${s.id}"></div>`}
    <div class="sig-meta" style="margin-top:6px">
      <button class="btn-sm" data-research="${s.id}">Get research brief</button>
    </div>
  </div>`;
}

// Direction pill buttons
document.querySelectorAll(".dir-btn").forEach(btn =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".dir-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _sigDirection = btn.dataset.dir;
    renderSignals();
  })
);

// Sort dropdown
document.getElementById("sig-sort")?.addEventListener("change", e => {
  _sigSort = e.target.value;
  renderSignals();
});

document.getElementById("sig-type")?.addEventListener("change", renderSignals);
let _tickerDebounce;
document.getElementById("sig-ticker")?.addEventListener("input", () => {
  clearTimeout(_tickerDebounce);
  _tickerDebounce = setTimeout(renderSignals, 350);
});

// ─── WATCHLISTS ───────────────────────────────────────────────────────────────
async function renderWatchlists() {
  const lists = await api("/api/watchlists");
  const names = Object.keys(lists);
  const el = document.getElementById("watchlist-content");
  if (!names.length) {
    el.innerHTML = emptyState("📋","No watchlists","Add tickers via the API or seed_sources.py.");
    return;
  }
  el.innerHTML = names.map(name =>
    `<div class="wl-section">
      <div class="wl-title">${esc(name)}</div>
      <div class="wl-grid">${lists[name].map(wlCard).join("")}</div>
    </div>`
  ).join("");
}

function wlCard(t) {
  const sc  = t.signal_score;
  const cls = sc > 0 ? "bull" : sc < 0 ? "bear" : "flat";
  const max = Math.max(1, ...(t.sparkline || []));
  const spark = (t.sparkline || []).map(v =>
    `<span style="height:${Math.max(2, Math.round((v/max)*18))}px"></span>`
  ).join("");

  // Analyst badge
  const analystHtml = t.analyst_rating
    ? `<span class="analyst-badge ${esc(t.analyst_rating)}">${esc(t.analyst_rating.replace(/_/g," "))}</span>
       ${t.analyst_target ? `<span class="wl-target">↯ $${t.analyst_target}</span>` : ""}
       ${t.analyst_count  ? `<span style="color:var(--text-3)">${t.analyst_count} analysts</span>` : ""}`
    : "";

  // Short interest
  const siHtml = t.short_interest_pct != null
    ? `<span class="wl-short ${t.short_interest_pct > 0.1 ? "hi" : "lo"}">
         SI: ${(t.short_interest_pct * 100).toFixed(1)}%
         ${t.short_ratio ? `(${t.short_ratio.toFixed(1)}d)` : ""}
       </span>`
    : "";

  // Blended consensus
  const consHtml = t.consensus_score != null
    ? `<span class="analyst-badge ${t.consensus_score > 0.15 ? "buy" : t.consensus_score < -0.15 ? "sell" : "hold"}">
         consensus: ${esc(t.consensus_label || "")}
       </span>`
    : "";

  // StockTwits bar
  const stTotal = (t.st_bull || 0) + (t.st_bear || 0);
  const stHtml  = stTotal > 0
    ? `<div class="wl-st">
         <span>ST</span>
         <div class="wl-st-bar">
           <div class="wl-st-bull" style="width:${Math.round((t.st_bull/stTotal)*100)}%"></div>
           <div class="wl-st-bear" style="flex:1"></div>
         </div>
         <span style="color:var(--bull)">${t.st_bull}🐂</span>
         <span style="color:var(--bear)">${t.st_bear}🐻</span>
       </div>`
    : "";

  return `<div class="wl-card">
    <div class="wl-sym">${esc(t.symbol)}</div>
    <div class="wl-name">${esc(t.name || "—")}</div>
    ${t.price != null
      ? `<div class="wl-price">$${t.price.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</div>`
      : `<div class="wl-price na">No price data</div>`}
    ${t.week52_low != null ? `<div class="wl-52w">52W: $${t.week52_low} – $${t.week52_high}</div>` : ""}
    <div class="wl-score-row">
      <span class="score-badge ${cls}">${sc > 0 ? "+" : ""}${sc !== 0 ? sc : "—"}</span>
      <span class="wl-bull-bear">${t.bullish}↑ ${t.bearish}↓</span>
    </div>
    <span class="spark">${spark}</span>
    ${analystHtml || siHtml || consHtml ? `<div class="wl-fundamentals">${consHtml}${analystHtml}${siHtml}</div>` : ""}
    ${stHtml}
  </div>`;
}

// ─── THEME SHIFTS ─────────────────────────────────────────────────────────────
let _shiftPeriod = "month";

async function renderThemeShifts() {
  const [data, alerts] = await Promise.all([
    api(`/api/theme-shifts?period=${_shiftPeriod}`),
    api("/api/shift-alerts?limit=20"),
  ]);

  document.getElementById("shift-meta").textContent =
    `${data.total_signals} signals · first vs second half of period`;

  const risingEl = document.getElementById("shift-rising");
  const fallingEl = document.getElementById("shift-falling");

  risingEl.innerHTML = data.rising.length
    ? data.rising.map(t => themeRow(t, true)).join("")
    : `<div style="color:var(--text-3);font-size:12px;padding:8px 0">Not enough data yet</div>`;

  fallingEl.innerHTML = data.falling.length
    ? data.falling.map(t => themeRow(t, false)).join("")
    : `<div style="color:var(--text-3);font-size:12px;padding:8px 0">Not enough data yet</div>`;

  const alertEl = document.getElementById("shift-alerts");
  alertEl.innerHTML = alerts.length
    ? alerts.map(a => `<div class="alert-row">
        <span class="alert-sym">${esc(a.symbol)}</span>
        <span class="alert-dir ${a.direction === "bullish" ? "bull" : "bear"}">${a.direction}</span>
        <span class="alert-scores">${a.from_score > 0 ? "+" : ""}${a.from_score} → ${a.to_score > 0 ? "+" : ""}${a.to_score} (${a.recent_total} signals)</span>
        <span class="alert-time">${fmtAgo(a.created_at)}</span>
      </div>`).join("")
    : `<div style="color:var(--text-3);font-size:12px;padding:8px 0">No polarity flips detected yet</div>`;

  window.renderThemeCharts(data);
}

function themeRow(t, isRising) {
  const pct = Math.min(100, Math.abs(t.change) / 20 * 100);
  const color = isRising ? "var(--bull)" : "var(--bear)";
  return `<div class="theme-row">
    <span class="theme-name" title="${esc(t.theme)}">${esc(t.theme)}</span>
    <div class="theme-bar"><div class="theme-bar-fill" style="width:${pct}%;background:${color}"></div></div>
    <span class="theme-change ${isRising ? "up" : "down"}">${isRising ? "+" : ""}${t.change}</span>
    <span class="theme-total">${t.total}</span>
  </div>`;
}

document.querySelectorAll(".period-btn").forEach(btn =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _shiftPeriod = btn.dataset.period;
    renderThemeShifts();
  })
);

// ─── INDUSTRIES ───────────────────────────────────────────────────────────────
async function renderIndustries() {
  const inds = await api("/api/industries");
  const el = document.getElementById("industries-grid");
  el.innerHTML = inds.map(i => {
    const pct = Math.round((i.source_count / Math.max(1, i.target)) * 100);
    return `<div class="ind-card">
      <div class="ind-name">${esc(i.name)}</div>
      <div class="ind-group">${esc(i.group)}</div>
      <div class="ind-progress"><div class="ind-progress-fill" style="width:${Math.min(100,pct)}%"></div></div>
      <div class="ind-stats">
        <span>${i.source_count}/${i.target} sources</span>
        <span>${i.signal_count} signals</span>
        <span>avg ${i.avg_score}</span>
      </div>
      <button class="btn-sm" data-discover="${i.id}">Discover more</button>
    </div>`;
  }).join("");
  el.querySelectorAll("[data-discover]").forEach(btn =>
    btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "Discovering…";
      try {
        const r = await api(`/api/industries/${btn.dataset.discover}/discover`, { method:"POST" });
        toast(`Added ${r.added} source(s) to ${r.industry}`);
        renderIndustries();
      } catch { toast("Discovery failed — check PERPLEXITY_API_KEY / XAI_API_KEY"); btn.disabled = false; btn.textContent = "Discover more"; }
    })
  );
}

// ─── SOURCES ─────────────────────────────────────────────────────────────────
async function renderSources() {
  const sources = await api("/api/sources");
  const rows = sources.map(s =>
    `<tr>
      <td>${esc(s.name)}</td>
      <td><span class="tag">${esc(s.type)}</span></td>
      <td style="color:var(--text-3)">${esc((s.industries||[]).join(", "))}</td>
      <td><span class="tag ${s.tier===1?"t1":"t2"}">${s.vetting_score} / T${s.tier}</span></td>
      <td>${s.signal_yield}</td>
      <td><span class="tag">${esc(s.status)}</span></td>
      <td style="color:var(--text-3)">${esc(s.discovered_by)}</td>
    </tr>`
  ).join("");
  document.getElementById("source-table").innerHTML =
    `<table><thead><tr><th>Name</th><th>Type</th><th>Industries</th>
      <th>Score</th><th>Yield</th><th>Status</th><th>Found by</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  document.getElementById("src-add")?.addEventListener("click", async () => {
    const name = document.getElementById("src-name")?.value?.trim();
    const type = document.getElementById("src-type")?.value;
    if (!name) return toast("Name required");
    await api("/api/sources", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        name, type,
        url:  document.getElementById("src-url")?.value,
        tags: document.getElementById("src-tags")?.value,
      }),
    });
    toast("Source added"); renderSources();
  });
}

// Brief industry selector
document.getElementById("brief-industry")?.addEventListener("change", e => {
  _briefIndustry = e.target.value;
  renderBrief();
});

// ─── HOME ─────────────────────────────────────────────────────────────────────
function miniSig(s, withAlpha) {
  const tickers = (s.entities?.tickers || []).slice(0, 3);
  return `<div class="mini-sig">
    <div class="mini-dir ${esc(s.direction)}"></div>
    <div class="mini-body">
      <div class="mini-summary">${esc(s.summary)}</div>
      <div class="mini-meta">
        ${withAlpha && s.alpha_score != null ? `<span class="alpha-badge ${s.alpha_score>=9?"high":s.alpha_score>=6?"mid":"low"}">${s.alpha_score}/12 · ${esc(s.alpha_label||"")}</span>` : ""}
        <span class="sig-type-tag">${esc((s.signal_type||"").replace(/_/g," "))}</span>
        ${tickers.map(t => `<span class="sig-ticker">${esc(t)}</span>`).join("")}
        <span class="sig-time">${fmtAgo(s.created_at)}</span>
      </div>
    </div>
  </div>`;
}

async function renderHome() {
  const d = await api("/api/home");

  // Stats
  const st = d.stats;
  document.getElementById("home-stats").innerHTML = [
    { label:"Items tracked",  value:st.total_items.toLocaleString(), cls:"accent" },
    { label:"Signals (48h)",  value:st.signals_48h,                  cls:"" },
    { label:"Bullish",        value:st.bullish,                      cls:"bull" },
    { label:"Bearish",        value:st.bearish,                      cls:"bear" },
  ].map(s => `<div class="stat-card">
    <div class="stat-value ${s.cls}">${s.value}</div>
    <div class="stat-label">${s.label}</div>
  </div>`).join("");

  // Non-consensus (the headline non-consensus view)
  const nc = document.getElementById("home-noncon");
  nc.innerHTML = d.non_consensus.length
    ? d.non_consensus.map(s => miniSig(s, true)).join("")
    : `<div class="mini-sub" style="padding:8px 0">No high-alpha signals in the last 48h yet.</div>`;

  // Top signals
  const ts = document.getElementById("home-signals");
  ts.innerHTML = d.top_signals.length
    ? d.top_signals.map(s => miniSig(s, false)).join("")
    : `<div class="mini-sub" style="padding:8px 0">No directional signals yet.</div>`;

  // Industry pulse (clickable → per-industry brief)
  const ind = document.getElementById("home-industries");
  ind.innerHTML = d.industry_pulse.length
    ? d.industry_pulse.map(p => {
        const total = p.total || 1;
        const bw = Math.round((p.bullish/total)*100);
        const sCls = p.sentiment > 0.1 ? "pos" : p.sentiment < -0.1 ? "neg" : "flat";
        return `<div class="ind-pulse-row" data-industry="${esc(p.name)}">
          <span class="ipr-name">${esc(p.name)}</span>
          <div class="ipr-bar"><div class="ipr-bar-bull" style="width:${bw}%"></div><div class="ipr-bar-bear" style="flex:1"></div></div>
          <span class="ipr-sentiment ${sCls}">${p.sentiment>0?"+":""}${p.sentiment}</span>
          <span class="ipr-count">${p.total} sig</span>
        </div>`;
      }).join("")
    : `<div class="mini-sub" style="padding:8px 0">No industry signals yet.</div>`;
  ind.querySelectorAll("[data-industry]").forEach(row =>
    row.addEventListener("click", () => {
      _briefIndustry = row.dataset.industry;
      const sel = document.getElementById("brief-industry");
      if (sel) sel.value = _briefIndustry;
      switchTab("brief");
    })
  );

  // Theme shifts
  const th = document.getElementById("home-themes");
  const rising = (d.rising_themes||[]).slice(0,4);
  const falling = (d.falling_themes||[]).slice(0,4);
  th.innerHTML =
    (rising.length || falling.length)
    ? rising.map(t => `<div class="mini-row">
         <span class="mini-row-name">${esc(t.theme)}</span>
         <span class="mini-chip up">▲ +${t.change}</span></div>`).join("")
      + falling.map(t => `<div class="mini-row">
         <span class="mini-row-name">${esc(t.theme)}</span>
         <span class="mini-chip down">▼ ${t.change}</span></div>`).join("")
    : `<div class="mini-sub" style="padding:8px 0">Not enough history for theme shifts.</div>`;

  // Watchlist movers
  const mv = document.getElementById("home-movers");
  mv.innerHTML = d.watchlist_movers.length
    ? d.watchlist_movers.map(m => {
        const cls = m.score > 0 ? "up" : m.score < 0 ? "down" : "";
        return `<div class="mini-row">
          <span class="mini-row-name" style="font-weight:600">${esc(m.ticker)}</span>
          <span class="mini-sub">${m.bullish}↑ ${m.bearish}↓</span>
          <span class="mini-chip ${cls}">${m.score>0?"+":""}${m.score}</span>
        </div>`;
      }).join("")
    : `<div class="mini-sub" style="padding:8px 0">No ticker activity yet.</div>`;

  // Smart money trades (politician + insider, prioritize divergent)
  const sm = document.getElementById("home-smartmoney");
  const smTrades = [
    ...(d.smart_money_divergent || []).map(f => ({
      summary: `${f.ticker}: ${f.divergence}`, direction: f.smart_direction > 0 ? "bullish" : "bearish",
    })),
    ...(d.politician_trades || []).slice(0, 3).map(t => ({ summary: t.summary, direction: t.direction })),
    ...(d.insider_trades || []).slice(0, 3).map(t => ({ summary: t.summary, direction: t.direction })),
  ].slice(0, 6);
  sm.innerHTML = smTrades.length
    ? smTrades.map(t => `<div class="sm-mini">
        <div class="sm-mini-dir ${esc(t.direction)}"></div>
        <span class="sm-mini-text">${esc(t.summary)}</span>
      </div>`).join("")
    : `<div class="mini-sub" style="padding:8px 0">No smart-money trades yet.</div>`;

  // Prediction markets
  const pm = document.getElementById("home-predictions");
  pm.innerHTML = (d.predictions || []).length
    ? d.predictions.map(p => {
        const prob = p.probability;
        const cls = prob == null ? "" : prob >= 70 ? "high" : prob >= 40 ? "mid" : "low";
        const q = p.title.includes("—") ? p.title.split("—").slice(1).join("—").trim() : p.title;
        return `<div class="mini-row">
          <span class="mini-chip ${cls === "high" ? "up" : cls === "low" ? "down" : ""}" style="min-width:38px">${prob != null ? prob + "%" : "—"}</span>
          <span class="mini-row-name" title="${esc(q)}">${esc(q)}</span>
        </div>`;
      }).join("")
    : `<div class="mini-sub" style="padding:8px 0">No prediction markets yet.</div>`;

  // Card "View all →" links
  document.querySelectorAll(".card-link[data-goto]").forEach(link =>
    link.addEventListener("click", () => {
      if (link.dataset.sort) { _sigSort = link.dataset.sort;
        const ss = document.getElementById("sig-sort"); if (ss) ss.value = link.dataset.sort; }
      switchTab(link.dataset.goto);
    })
  );
}

// ─── SMART MONEY ──────────────────────────────────────────────────────────────
function consTag(score, label) {
  if (score == null) return `<span class="flow-cons-label">no consensus data</span>`;
  const cls = score > 0.15 ? "flow-buy" : score < -0.15 ? "flow-sell" : "flow-cons-label";
  return `<span class="${cls}">${label || ""}</span> <span class="flow-cons-label">(${score > 0 ? "+" : ""}${score})</span>`;
}

function flowGrp(label, buy, sell) {
  return `<div class="flow-grp">
    <span class="flow-grp-label">${label}</span>
    <span class="flow-grp-val"><span class="flow-buy">${buy}↑</span> <span class="flow-sell">${sell}↓</span></span>
  </div>`;
}

function flowRow(f) {
  const diverge = f.divergence;
  return `<div class="flow-row ${diverge ? "diverge" : ""}">
    <span class="flow-sym">${esc(f.ticker)}</span>
    <div class="flow-bars">
      ${flowGrp("Insider", f.insider_buy, f.insider_sell)}
      ${flowGrp("Congress", f.congress_buy, f.congress_sell)}
      ${flowGrp("Institutional", f.inst_buy || 0, f.inst_sell || 0)}
    </div>
    <span class="flow-consensus">consensus:<br>${consTag(f.consensus_score, f.consensus_label)}</span>
    ${diverge ? `<span class="flow-diverge-tag">⚡ ${esc(diverge)}</span>` : `<span></span>`}
  </div>`;
}

function tradeRow(t) {
  return `<div class="trade-row">
    <div class="trade-dir ${esc(t.direction)}"></div>
    <div class="trade-text">${t.url && t.url.startsWith("http") ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.summary)}</a>` : esc(t.summary)}</div>
    <span class="trade-time">${fmtAgo(t.created_at)}</span>
  </div>`;
}

function srcBadge(src) {
  const cls = src === "Kalshi" ? "accent" : "";
  return `<span class="src-badge ${cls}">${esc(src)}</span>`;
}

function predRow(p) {
  const prob = p.probability;
  const cls = prob == null ? "" : prob >= 70 ? "high" : prob >= 40 ? "mid" : "low";
  const q = p.question || (p.title.includes("—") ? p.title.split("—").slice(1).join("—").trim() : p.title);
  return `<div class="pred-row">
    <span class="pred-prob ${cls}">${prob != null ? prob + "%" : "—"}</span>
    <span class="pred-q"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(q)}</a></span>
    ${p.source ? srcBadge(p.source) : ""}
  </div>`;
}

function crossvalRow(c) {
  const tag = c.agreement === "validated" ? "cv-ok" : c.agreement === "diverging" ? "cv-warn" : "cv-bad";
  const label = c.agreement === "validated" ? "✓ validated" : c.agreement === "diverging" ? "diverging" : "⚠ conflicting";
  const members = c.members.map(m =>
    `<span class="cv-member"><b>${m.probability}%</b> ${esc(m.source)}</span>`
  ).join('<span class="cv-vs">vs</span>');
  return `<div class="cv-row">
    <span class="cv-tag ${tag}">${label}</span>
    <span class="cv-q">${esc(c.question)}</span>
    <span class="cv-members">${members}</span>
    <span class="cv-spread">Δ${c.spread}%</span>
  </div>`;
}

// Generic sub-tab switching (data-subview points at the panel id to show).
document.querySelectorAll(".subtab").forEach(btn =>
  btn.addEventListener("click", () => {
    const group = btn.dataset.sub;
    document.querySelectorAll(`.subtab[data-sub="${group}"]`).forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    const target = btn.dataset.subview;
    document.querySelectorAll(`#view-smart-money .subview`).forEach(v =>
      v.classList.toggle("active", v.id === target));
    if (target === "sm-funds-view") loadFunds();
  })
);

let _fundsLoaded = false;
async function loadFunds() {
  if (_fundsLoaded) return;
  const el = document.getElementById("sm-funds");
  el.innerHTML = `<div class="empty"><div class="empty-icon">⏳</div><div class="empty-title">Loading 13F filings…</div><div class="empty-sub">Pulling each fund's latest SEC 13F holdings (~15s).</div></div>`;
  try {
    const funds = await api("/api/funds");
    el.innerHTML = funds.length ? funds.map(fundCard).join("")
      : emptyState("◆", "13F data unavailable", "EDGAR may be throttling this host; try again shortly.");
    el.querySelectorAll("[data-cik]").forEach(card =>
      card.addEventListener("click", () => toggleFundDetail(card)));
    _fundsLoaded = true;
  } catch (e) {
    el.innerHTML = emptyState("⚠️", "Failed to load funds", "EDGAR throttled — retry shortly.");
  }
}

function fundCard(f) {
  const holds = (f.top_holdings || []).map(h =>
    `<span class="fund-holding"><b>${esc(h.issuer || h)}</b>${h.value_usd ? " $" + (h.value_usd/1e9).toFixed(1) + "B" : ""}</span>`).join("");
  const c = f.recent_changes || {};
  const chg = [];
  if (c.new)    chg.push(`<span class="new">+${c.new} new</span>`);
  if (c.added)  chg.push(`<span class="added">▲${c.added} added</span>`);
  if (c.trimmed)chg.push(`<span class="trim">▼${c.trimmed} trimmed</span>`);
  if (c.exited) chg.push(`<span class="exit">✕${c.exited} exited</span>`);
  return `<div class="fund-card clickable" data-cik="${f.cik}">
    <div class="fund-head">
      <span class="fund-name">${esc(f.name)}</span>
      ${f.filing_date ? `<span class="fund-date">13F ${esc(f.filing_date)} · click for full holdings</span>` : ""}
    </div>
    <div class="fund-holdings">${holds || '<span class="mini-sub">holdings unavailable</span>'}</div>
    ${chg.length ? `<div class="fund-chg">${chg.join(" · ")}</div>` : ""}
    <div class="fund-detail" style="display:none"></div>
  </div>`;
}

function chgList(label, items, cls) {
  if (!items || !items.length) return "";
  const rows = items.slice(0, 12).map(i =>
    `<div class="fund-row"><span class="fund-row-name">${esc(i.issuer || i)}</span>
     ${i.value_usd ? `<span class="fund-row-val">$${(i.value_usd/1e9).toFixed(2)}B</span>` : ""}</div>`).join("");
  return `<div class="fund-detail-title"><span class="fund-tag ${cls}">${label}</span> ${items.length}</div>${rows}`;
}

async function toggleFundDetail(card) {
  const box = card.querySelector(".fund-detail");
  if (box.dataset.open === "1") { box.style.display = "none"; box.dataset.open = "0"; return; }
  box.style.display = "block"; box.dataset.open = "1";
  if (box.dataset.loaded) return;
  box.innerHTML = `<div class="mini-sub" style="padding:6px 0">Loading full 13F…</div>`;
  try {
    const d = await api(`/api/funds/${card.dataset.cik}`);
    const h = (d.holdings?.holdings || []).map(x =>
      `<div class="fund-row"><span class="fund-row-name">${esc(x.issuer)}</span>
       <span class="fund-row-val">$${(x.value_usd/1e9).toFixed(2)}B · ${(x.shares/1e6).toFixed(1)}M sh</span></div>`).join("");
    const ch = d.changes || {};
    box.innerHTML =
      `<div class="fund-detail-title">Top holdings (current portfolio)</div>${h || '<div class="mini-sub">unavailable</div>'}` +
      chgList("NEW", ch.new, "new") +
      chgList("EXITED", ch.exited, "exit");
    box.dataset.loaded = "1";
  } catch (_) {
    box.innerHTML = `<div class="mini-sub" style="padding:6px 0">Couldn't load detail (EDGAR throttled).</div>`;
  }
}

async function renderSmartMoney() {
  const sm = await api("/api/smart-money?days=14");

  // Divergence (the headline)
  const dw = document.getElementById("sm-divergent-wrap");
  if (sm.divergent.length) {
    dw.style.display = "block";
    document.getElementById("sm-divergent").innerHTML = sm.divergent.map(flowRow).join("");
  } else {
    dw.style.display = "none";
  }

  // Flow
  document.getElementById("sm-flow").innerHTML = sm.flow.length
    ? sm.flow.map(flowRow).join("")
    : emptyState("💰","No smart-money trades yet","Run ingestion — Form 4 + congressional feeds populate this.");

  // Politicians / insiders
  document.getElementById("sm-politicians").innerHTML = sm.politician_trades.length
    ? sm.politician_trades.map(tradeRow).join("")
    : `<div class="mini-sub" style="padding:8px 0">No congressional trades yet (Quiver feed may be throttled).</div>`;
  document.getElementById("sm-insiders").innerHTML = sm.insider_trades.length
    ? sm.insider_trades.map(tradeRow).join("")
    : `<div class="mini-sub" style="padding:8px 0">No insider filings yet.</div>`;
  const instEl = document.getElementById("sm-institutional");
  if (instEl) instEl.innerHTML = (sm.institutional_trades || []).length
    ? sm.institutional_trades.map(tradeRow).join("")
    : `<div class="mini-sub" style="padding:8px 0">No 13D/13G filings yet.</div>`;
}

// ─── FORECASTS ────────────────────────────────────────────────────────────────
let _fcSource = "";
let _fcData = null;

async function renderForecasts() {
  if (!_fcData) _fcData = await api("/api/predictions?limit=80");
  const preds = (_fcData.predictions || []).filter(p => !_fcSource || p.source === _fcSource);
  const crossval = _fcData.cross_validated || [];

  const cvWrap = document.getElementById("fc-crossval-wrap");
  if (crossval.length) {
    cvWrap.style.display = "block";
    document.getElementById("fc-crossval").innerHTML = crossval.map(crossvalRow).join("");
  } else {
    cvWrap.style.display = "none";
  }

  document.getElementById("fc-predictions").innerHTML = preds.length
    ? preds.map(predRow).join("")
    : emptyState("🔮","No forecasts yet","Run ingestion — Polymarket + Kalshi populate this.");
}

document.querySelectorAll(".fc-btn").forEach(btn =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".fc-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _fcSource = btn.dataset.fc;
    renderForecasts();
  })
);

// ─── CONVICTION ───────────────────────────────────────────────────────────────
function packetChips(p) {
  const items = [
    ["non-consensus signals", (p.non_consensus_signals || []).length],
    ["smart-money divergences", (p.smart_money_divergent || []).length],
    ["unusual volume", (p.unusual_volume || []).length],
    ["insider trades", (p.insider_trades || []).length],
    ["politician trades", (p.politician_trades || []).length],
    ["institutional stakes", (p.institutional_stakes || []).length],
    ["prediction conflicts", (p.prediction_conflicts || []).length],
  ];
  const regime = p.macro_regime && p.macro_regime !== "unknown"
    ? `<span class="packet-chip">macro: <b>${esc(p.macro_regime)}</b></span>` : "";
  return regime + items.map(([k, v]) => `<span class="packet-chip"><b>${v}</b> ${k}</span>`).join("");
}

function convCard(c) {
  const dir = (c.direction || "watch").toLowerCase();
  const tickers = (c.tickers || []).map(t => `<span class="conv-ticker">${esc(t)}</span>`).join("");
  const evidence = (c.evidence || []).map(e => `<li>${esc(e)}</li>`).join("");
  return `<div class="conv-card ${dir}">
    <div class="conv-card-top">
      <span class="conv-rank">${c.rank ?? "•"}</span>
      <span class="conv-dir ${dir}">${dir}</span>
      <span class="conv-conviction">${esc(c.conviction || "")} conviction</span>
      <span class="conv-card-title">${esc(c.title || "")}</span>
      <span class="conv-tickers">${tickers}</span>
    </div>
    <div class="conv-thesis">${esc(c.thesis || "")}</div>
    ${evidence ? `<div class="conv-section-label">What the data shows</div><ul class="conv-evidence">${evidence}</ul>` : ""}
    ${c.non_consensus ? `<div class="conv-noncon"><b>Non-consensus:</b> ${esc(c.non_consensus)}</div>` : ""}
    ${c.risk ? `<div class="conv-risk"><b>Key risk:</b> ${esc(c.risk)}</div>` : ""}
    ${c.corroboration ? `<div class="conv-corrob"><b>Corroborated by:</b> ${esc(c.corroboration)}</div>` : ""}
  </div>`;
}

function renderFindingsCards(d) {
  document.getElementById("conv-packet").innerHTML = packetChips(d.packet || {});
  const dateEl = document.getElementById("conv-date");
  if (dateEl) dateEl.textContent = d.date ? `· ${d.date}${d.stale ? " (last run — regenerate for today)" : ""}` : "";
  const cards = document.getElementById("conv-cards");
  const f = d.findings || [];
  cards.innerHTML = f.length
    ? f.map(convCard).join("")
    : emptyState("★", "No findings yet today", "Click Regenerate to run the DeepResearch analysis (~20s, two-pass).");
}

async function renderConviction() {
  // Show today's stored findings immediately (no LLM call).
  try {
    const d = await api("/api/findings/today");
    renderFindingsCards(d);
  } catch (_) {
    document.getElementById("conv-cards").innerHTML =
      emptyState("★", "Click Regenerate", "Runs the two-pass DeepResearch analysis on all current data.");
  }
  // History strip
  try {
    const hist = await api("/api/findings/history?limit=10");
    const el = document.getElementById("conv-history");
    el.innerHTML = hist.length
      ? hist.map((h, i) => `<span class="hist-chip ${i === 0 ? "active" : ""}" title="${esc(h.top || "")}">${esc(h.date)} · ${h.count}</span>`).join("")
      : "";
  } catch (_) {}
}

document.getElementById("conv-generate")?.addEventListener("click", async () => {
  const btn = document.getElementById("conv-generate");
  const cards = document.getElementById("conv-cards");
  btn.disabled = true; btn.textContent = "Researching…";
  cards.innerHTML = `<div class="empty"><div class="empty-icon">⏳</div><div class="empty-title">Two-pass DeepResearch…</div><div class="empty-sub">Pass 1: extract candidates from all data. Pass 2: verify & rank. ~20s.</div></div>`;
  try {
    const d = await api("/api/findings/generate", { method: "POST" });
    renderFindingsCards(d);
    renderConviction();  // refresh history
  } catch (e) {
    cards.innerHTML = emptyState("⚠️", "Generation failed", "Check ANTHROPIC_API_KEY and try again.");
  }
  btn.textContent = "Regenerate"; btn.disabled = false;
});

// ─── GUIDE ────────────────────────────────────────────────────────────────────
function renderGuide() {
  const pages = [
    ["Intelligence", [
      ["★", "Daily Findings", "The flagship. A two-pass DeepResearch analysis (extract → verify) of <b>every</b> data source, distilled into the day's 1–5 highest-conviction, non-consensus trade ideas — each with thesis, the exact evidence, why it's off-consensus, the key risk, and which independent sources corroborate. Auto-runs each morning; hit <b>Regenerate</b> for a fresh pass. Past days are in the history strip."],
      ["◰", "Home", "One-glance overview: top signals, non-consensus signals, smart-money trades, prediction markets, industry pulse, theme shifts, and watchlist movers — each linking to its full tab."],
      ["▤", "Brief", "A structured read of the last 48h of signals: top actionable signals, sector pulse, hot tickers, and an AI synthesis. Scope it to a single industry with the dropdown."],
    ]],
    ["Smart Money", [
      ["◆", "Smart Money", "Per-ticker flow combining <b>insider (SEC Form 4)</b>, <b>congressional</b>, and <b>institutional (13D/13G)</b> trades, cross-checked against market consensus. <b>Divergence</b> — smart money moving opposite the crowd — is the strongest non-consensus marker."],
      ["◎", "Forecasts", "Crowd-forecast probabilities from <b>Polymarket + Kalshi</b>. When both venues price the same event we cross-check them: tight agreement = validated, wide spread = the markets disagree (a signal)."],
    ]],
    ["Markets", [
      ["◷", "Macro & Pulse", "<b>Macro regime</b> (risk-on/off) read from the yield curve, CPI, unemployment, VIX (FRED). <b>Watchlist pulse</b> = Wikipedia attention + GDELT news tone per ticker — spikes often precede moves."],
      ["↯", "Signals", "Every classified signal. Filter by direction (All/Bullish/Bearish/Neutral) and sort by Newest, Confidence, <b>Alpha</b> (0–12 non-consensus score), or <b>Contrarian</b>. Insider, dilution, and catalyst signals all land here."],
      ["≈", "Theme Shifts", "Rising vs falling themes over 1W–1Y, plus ticker polarity flips — what the research is collectively moving toward or away from."],
      ["📈", "Trends", "Signal direction by week + top topic mentions over time."],
    ]],
    ["Data", [
      ["≡", "Feed", "The raw ingested stream — articles, videos, tweets, filings — filterable by type."],
      ["★", "Watchlists", "Your ticker lists with price, 52-week range, signal score, analyst rating, short interest, StockTwits sentiment, and blended consensus."],
      ["◫", "Industries", "Per-industry source counts, signal counts, and a Discover-more button to grow coverage."],
      ["⌗", "Sources", "The full source registry — every feed, its tier, vetting score, and measured signal yield. Add your own."],
    ]],
  ];
  const body = document.getElementById("guide-body");
  body.innerHTML = `<div class="guide-wrap">
    <div class="guide-h1">How to use Research Intel</div>
    <div class="guide-intro">An always-on engine that aggregates <b>16 free data streams</b> — news, YouTube, X/Twitter,
      SEC insider &amp; institutional filings, dilution filings, congressional trades, Polymarket + Kalshi forecasts,
      patents, clinical trials, FRED macro, GDELT tone, Wikipedia attention, volume — classifies them into investment
      signals, and surfaces the <b>non-consensus</b> ones. Press <span class="guide-kbd">⌘K</span> anywhere to jump to a
      tab or ticker. Start on <b>Daily Findings</b>; everything else is the supporting evidence.</div>
    ${pages.map(([group, items]) => `
      <div class="guide-section-title">${group}</div>
      ${items.map(([ic, name, desc]) => `<div class="guide-card">
        <div class="guide-card-head"><span class="guide-icon">${ic}</span><span class="guide-card-name">${name}</span></div>
        <div class="guide-card-body">${desc}</div>
      </div>`).join("")}
    `).join("")}
    <div class="guide-section-title">How a signal becomes a finding</div>
    <div class="guide-card"><div class="guide-card-body">
      <b>1. Ingest</b> — 16 sources pulled hourly. <b>2. Classify</b> — a cheap model filters relevance, then Sonnet
      extracts a structured signal (type, direction, confidence, tickers). <b>3. Score</b> — each signal gets a 0–12
      <b>alpha</b> score (timeliness, source originality, exclusivity, contrarian-vs-consensus). <b>4. DeepResearch</b> —
      twice a pass (extract → adversarially verify) turns the aggregate into the daily top-5. Architecture adapted from
      <b>QuantMind</b>'s knowledge-extraction → retrieval pipeline, run on Claude.
    </div></div>
  </div>`;
}

// ─── Composite gauges (Trends tab) ────────────────────────────────────────────
function gaugeCard(label, c, sub) {
  const score = c.score ?? 0;
  const pct = Math.max(0, Math.min(100, (score + 100) / 2));
  const cls = score > 5 ? "pos" : score < -5 ? "neg" : "mid";
  return `<div class="gauge-card">
    <div class="gauge-top">
      <span class="gauge-label">${esc(label)}</span>
      <span class="gauge-val ${cls}">${score > 0 ? "+" : ""}${score}</span>
    </div>
    <div class="gauge-sub">${esc(c.label || "")}${sub ? " · " + sub : ""}</div>
    <div class="gauge-track">
      <div class="gauge-zero"></div>
      <div class="gauge-mark" style="left:calc(${pct}% - 1.5px)"></div>
    </div>
  </div>`;
}

async function renderComposites() {
  const el = document.getElementById("composite-gauges");
  if (!el) return;
  try {
    const d = await api("/api/composites");
    const mc = d.market_consensus, sm = d.signal_momentum;
    el.innerHTML =
      gaugeCard("Macro Outlook", d.macro_outlook, "yield curve · VIX · credit · jobs") +
      gaugeCard("Market Consensus", mc, mc.n ? `${mc.bull}▲ ${mc.bear}▼ of ${mc.n}` : "awaiting price data") +
      gaugeCard("Signal Momentum", sm, sm.n ? `${sm.bull}▲ ${sm.bear}▼ · ${sm.n} signals/7d` : "");
  } catch (_) {
    el.innerHTML = `<div class="mini-sub">Composites unavailable.</div>`;
  }
}

// ─── INVESTOR LENS ────────────────────────────────────────────────────────────
async function runInvestorLens(ticker) {
  const cards = document.getElementById("lens-cards");
  const cons = document.getElementById("lens-consensus");
  cons.innerHTML = "";
  cards.innerHTML = `<div class="empty"><div class="empty-icon">⏳</div><div class="empty-title">Convening the panel…</div><div class="empty-sub">Six legendary investors reading ${esc(ticker)}'s dossier.</div></div>`;
  try {
    const d = await api("/api/investor-lens", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    const dos = d.dossier || {};
    document.getElementById("lens-meta").textContent =
      `${dos.signal_count || 0} signals · ${dos.consensus || "no consensus"} · ${dos.rvol ? dos.rvol + "x vol" : ""}`;
    cons.innerHTML = d.consensus
      ? `<div class="lens-consensus"><b>Panel read:</b> ${esc(d.consensus)}</div>` : "";
    const personas = d.personas || [];
    cards.innerHTML = personas.length
      ? `<div class="lens-grid">${personas.map(p => {
          const v = (p.verdict || "neutral").toLowerCase();
          return `<div class="lens-card">
            <div class="lens-head">
              <span class="lens-name">${esc(p.name)}</span>
              <span class="lens-verdict ${v}">${v}</span>
            </div>
            <div class="lens-rationale">${esc(p.rationale || "")}</div>
          </div>`;
        }).join("")}</div>`
      : emptyState("◉", "Not enough data on " + esc(ticker), "Try a watchlist ticker with recent signal activity.");
  } catch (e) {
    cards.innerHTML = emptyState("⚠️", "Panel failed", "Check the ticker and ANTHROPIC_API_KEY.");
  }
}
document.getElementById("lens-go")?.addEventListener("click", () => {
  const t = document.getElementById("lens-ticker").value.trim().toUpperCase();
  if (t) runInvestorLens(t);
});
document.getElementById("lens-ticker")?.addEventListener("keydown", e => {
  if (e.key === "Enter") { const t = e.target.value.trim().toUpperCase(); if (t) runInvestorLens(t); }
});

// ─── MACRO & PULSE ────────────────────────────────────────────────────────────
async function renderMacro() {
  // Macro renders immediately; pulse (slow, rate-limited) fills in after.
  document.getElementById("macro-pulse").innerHTML =
    `<div class="empty"><div class="empty-icon">⏳</div><div class="empty-title">Loading watchlist pulse…</div><div class="empty-sub">Fetching Wikipedia attention + GDELT tone (rate-limited, ~20s).</div></div>`;

  const macro = await api("/api/macro");
  const regime = (macro.regime || "unknown");
  document.getElementById("macro-regime").innerHTML = `
    <span class="regime-badge ${regime}">${regime.replace("-", " ")}</span>
    <span class="regime-text">Market regime read from the yield curve, VIX, and rates.
      ${regime === "risk-off" ? "Defensive backdrop — favor quality, hedge beta." :
        regime === "risk-on" ? "Constructive backdrop — risk appetite supported." :
        "Mixed signals — no strong macro tilt."}</span>`;

  const ind = document.getElementById("macro-indicators");
  ind.innerHTML = (macro.indicators || []).length
    ? macro.indicators.map(i => `<div class="macro-card">
        <div class="macro-label">${esc(i.label)}</div>
        <div class="macro-value">${esc(String(i.value))}</div>
        <div class="macro-interp">${esc(i.interpretation || (i.change != null ? `Δ ${i.change}` : ""))}</div>
      </div>`).join("")
    : `<div class="mini-sub" style="padding:8px 0">FRED indicators unavailable (rate-limited from this host).</div>`;

  // Unusual volume + short pressure (independent, fast — DB/FINRA).
  api("/api/unusual-volume").then(uv => {
    const el = document.getElementById("macro-uvol");
    if (!el) return;
    el.innerHTML = (uv || []).length
      ? uv.map(u => `<div class="pulse-row" style="grid-template-columns:60px 1fr auto auto">
          <span class="pulse-sym">${esc(u.symbol)}</span>
          <span class="pulse-name">${esc(u.consensus_label || "")}</span>
          <span class="pulse-metric"><span class="lbl">rvol</span><b class="up">${u.rvol}x</b></span>
          <span class="pulse-metric"><span class="lbl">today</span><b class="${u.change_pct>=0?"up":"down"}">${u.change_pct>0?"+":""}${u.change_pct}%</b></span>
        </div>`).join("")
      : `<div class="mini-sub" style="padding:8px 0">No unusual volume right now.</div>`;
  }).catch(() => {});
  api("/api/short-volume").then(sv => {
    const el = document.getElementById("macro-short");
    if (!el) return;
    const rows = (sv.tickers || []);
    el.innerHTML = rows.length
      ? `<div class="mini-sub" style="margin-bottom:6px">FINRA ${esc(sv.date||"")}</div>` + rows.slice(0,10).map(t => `<div class="pulse-row" style="grid-template-columns:60px 1fr auto">
          <span class="pulse-sym">${esc(t.symbol)}</span>
          <span class="pulse-name"></span>
          <span class="pulse-metric"><span class="lbl">short vol</span><b class="${t.short_pct>=50?"down":""}">${t.short_pct}%</b></span>
        </div>`).join("")
      : `<div class="mini-sub" style="padding:8px 0">FINRA short data unavailable.</div>`;
  }).catch(() => {});
  api("/api/ftd").then(ftd => {
    const el = document.getElementById("macro-ftd");
    if (!el) return;
    const rows = (ftd.tickers || []);
    el.innerHTML = rows.length
      ? `<div class="mini-sub" style="margin-bottom:6px">SEC ${esc(ftd.date||"")}</div>` + rows.slice(0,10).map(t => `<div class="pulse-row" style="grid-template-columns:60px 1fr auto">
          <span class="pulse-sym">${esc(t.symbol)}</span>
          <span class="pulse-name"></span>
          <span class="pulse-metric"><span class="lbl">fails</span><b>${(t.fails/1e3).toFixed(0)}K</b></span>
        </div>`).join("")
      : `<div class="mini-sub" style="padding:8px 0">No FTD data.</div>`;
  }).catch(() => {});

  // Pulse loads independently (don't block the macro panel on it).
  const metric = (val, kind) => {
    if (val == null) return `<span class="pulse-metric"><span class="lbl">${kind}</span>—</span>`;
    const hot = (kind === "attention" || kind === "news vol") && val >= 1.5;
    const cls = kind === "tone" ? (val > 0 ? "up" : val < 0 ? "down" : "") : (hot ? "up" : "");
    const suffix = (kind === "attention" || kind === "news vol") ? "x" : "";
    return `<span class="pulse-metric"><span class="lbl">${kind}</span><b class="${cls}">${val}${suffix}</b></span>`;
  };
  api("/api/pulse").then(pulse => {
    const pe = document.getElementById("macro-pulse");
    if (!pe) return;
    pe.innerHTML = (pulse || []).length
      ? pulse.map(p => `<div class="pulse-row">
          <span class="pulse-sym">${esc(p.symbol)}</span>
          <span class="pulse-name">${esc(p.name)}</span>
          ${metric(p.attention_ratio, "attention")}
          ${metric(p.tone, "tone")}
          ${metric(p.news_vol_spike, "news vol")}
        </div>`).join("")
      : emptyState("📡", "Pulse rate-limited", "Wikipedia + GDELT throttled this host; populates from a residential IP.");
  }).catch(() => {
    const pe = document.getElementById("macro-pulse");
    if (pe) pe.innerHTML = emptyState("📡", "Pulse unavailable", "Wikipedia/GDELT throttled from this host.");
  });
}

// ─── Command palette (⌘K) ─────────────────────────────────────────────────────
const CMDK_TABS = [
  ["conviction","★ Daily Findings"],["home","Home"],["brief","Brief"],["guide","Guide"],
  ["smart-money","Smart Money"],["forecasts","Forecasts"],["investor-lens","Investor Lens"],
  ["macro","Macro & Pulse"],["signals","Signals"],["theme-shifts","Theme Shifts"],["trends","Trends"],
  ["feed","Feed"],["watchlists","Watchlists"],["industries","Industries"],["sources","Sources"],
];
let _cmdkItems = [], _cmdkActive = 0;

function openCmdk() {
  document.getElementById("cmdk").hidden = false;
  const inp = document.getElementById("cmdk-input");
  inp.value = ""; inp.focus();
  renderCmdk("");
}
function closeCmdk() { document.getElementById("cmdk").hidden = true; }

function renderCmdk(q) {
  q = q.trim().toLowerCase();
  const tabs = CMDK_TABS
    .filter(([id, label]) => !q || label.toLowerCase().includes(q) || id.includes(q))
    .map(([id, label]) => ({ type: "tab", id, label, icon: "→" }));
  // Ticker jump: if query looks like a ticker, offer Signals filtered by it
  const tickerItems = q.length >= 1 && /^[a-z]{1,5}$/.test(q)
    ? [{ type: "ticker", id: q.toUpperCase(), label: `Signals for ${q.toUpperCase()}`, icon: "$" }]
    : [];
  _cmdkItems = [...tabs, ...tickerItems];
  _cmdkActive = 0;
  document.getElementById("cmdk-results").innerHTML = _cmdkItems.map((it, i) =>
    `<div class="cmdk-item ${i === 0 ? "active" : ""}" data-i="${i}">
      <span class="ic">${it.icon}</span>${esc(it.label)}
      <span class="k">${it.type === "tab" ? "tab" : "ticker"}</span>
    </div>`).join("") || `<div class="cmdk-item">No matches</div>`;
  document.querySelectorAll(".cmdk-item[data-i]").forEach(el =>
    el.addEventListener("click", () => execCmdk(+el.dataset.i)));
}

function execCmdk(i) {
  const it = _cmdkItems[i];
  if (!it) return;
  closeCmdk();
  if (it.type === "tab") switchTab(it.id);
  else if (it.type === "ticker") {
    _sigSort = "alpha";
    switchTab("signals");
    const inp = document.getElementById("sig-ticker");
    if (inp) { inp.value = it.id; }
    setTimeout(renderSignals, 50);
  }
}

document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdk(); return; }
  const overlay = document.getElementById("cmdk");
  if (overlay.hidden) return;
  if (e.key === "Escape") closeCmdk();
  else if (e.key === "ArrowDown") { e.preventDefault(); _cmdkActive = Math.min(_cmdkActive+1, _cmdkItems.length-1); updateCmdkActive(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); _cmdkActive = Math.max(_cmdkActive-1, 0); updateCmdkActive(); }
  else if (e.key === "Enter") execCmdk(_cmdkActive);
});
function updateCmdkActive() {
  document.querySelectorAll(".cmdk-item[data-i]").forEach((el, i) =>
    el.classList.toggle("active", i === _cmdkActive));
}
document.getElementById("cmdk-input")?.addEventListener("input", e => renderCmdk(e.target.value));
document.getElementById("cmdk")?.addEventListener("click", e => { if (e.target.id === "cmdk") closeCmdk(); });

// ─── Tab router ───────────────────────────────────────────────────────────────
async function renderTab(tab) {
  if (!tab) return;
  try {
    if (tab === "conviction")   await renderConviction();
    else if (tab === "guide")   renderGuide();
    else if (tab === "home")    await renderHome();
    else if (tab === "macro")   await renderMacro();
    else if (tab === "brief")   await renderBrief();
    else if (tab === "smart-money") await renderSmartMoney();
    else if (tab === "investor-lens") { /* input-driven; nothing to fetch on open */ }
    else if (tab === "forecasts") { _fcData = null; await renderForecasts(); }
    else if (tab === "feed")    await renderFeed();
    else if (tab === "signals") await renderSignals();
    else if (tab === "watchlists") await renderWatchlists();
    else if (tab === "theme-shifts") await renderThemeShifts();
    else if (tab === "trends")  { await renderComposites(); await window.renderTrends(); }
    else if (tab === "industries") await renderIndustries();
    else if (tab === "sources") await renderSources();
  } catch (e) {
    toast(`Failed to load ${tab}`);
    console.error(e);
  }
}

function switchTab(tab) {
  document.querySelectorAll("#tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  document.querySelectorAll(".view").forEach(v =>
    v.classList.toggle("active", v.id === `view-${tab}`)
  );
  renderTab(tab);
}

document.querySelectorAll("#tabs button").forEach(btn =>
  btn.addEventListener("click", () => switchTab(btn.dataset.tab))
);

// ─── Ingest button ────────────────────────────────────────────────────────────
document.getElementById("ingest-btn")?.addEventListener("click", async e => {
  e.target.disabled = true;
  e.target.textContent = "Starting…";
  try {
    const res = await api("/api/ingest", { method:"POST" });
    if (res.status === "already_running") {
      toast("Ingestion already running");
      e.target.disabled = false;
      e.target.textContent = "Run ingestion";
      return;
    }
    _statusPoll = setInterval(refreshIngestStatus, 3000);
  } catch {
    toast("Failed to start ingestion");
    e.target.disabled = false;
    e.target.textContent = "Run ingestion";
  }
});

// ─── Init ─────────────────────────────────────────────────────────────────────
(async () => {
  try {
    const cfg = await api("/api/config");
    const el = document.getElementById("tier-badge");
    if (el) el.textContent = cfg.cost_tier;
  } catch (_) {}
  await refreshIngestStatus();
  switchTab("conviction");
})();
