"use strict";

const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
};

const toast = (msg) => {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );

const tierBadge = (tier) =>
  `<span class="badge tier${tier === 1 ? 1 : 2}">T${tier}</span>`;

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");

// ------------------------------------------------------------------ item cards
function itemCard(it) {
  return `<div class="card">
    <a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>
    <div class="meta">
      <span class="badge type">${esc(it.source_type)}</span>
      ${tierBadge(it.tier)}
      <span>${esc(it.source)}</span>
      ${it.author ? `<span>${esc(it.author)}</span>` : ""}
      <span>${fmtTime(it.published_at || it.ingested_at)}</span>
    </div>
  </div>`;
}

async function renderFeed(tab) {
  const view = document.getElementById(`view-${tab}`);
  const items = await api(`/api/feed?tab=${tab}&limit=60`);
  view.innerHTML = items.length
    ? items.map(itemCard).join("")
    : `<div class="empty">No items yet. Click “Run ingestion”.</div>`;
}

// -------------------------------------------------------------------- signals
function signalCard(s) {
  const tickers = (s.entities.tickers || []).join(", ");
  const techs = (s.entities.technologies || []).join(", ");
  return `<div class="card">
    <div class="meta" style="margin-top:0">
      <span class="badge type">${esc(s.signal_type)}</span>
      <span class="badge ${s.direction === "bullish" ? "bull" : s.direction === "bearish" ? "bear" : ""}">${esc(s.direction)}</span>
      ${s.speculative ? '<span class="badge spec">⚠ speculative</span>' : ""}
      ${s.industry ? `<span>${esc(s.industry)}</span>` : ""}
    </div>
    <div style="margin-top:8px">${esc(s.summary)}</div>
    <div class="confbar"><div style="width:${Math.round(s.confidence * 100)}%"></div></div>
    <div class="meta">
      ${tickers ? `<span>📈 ${esc(tickers)}</span>` : ""}
      ${techs ? `<span>🔬 ${esc(techs)}</span>` : ""}
      <a href="${esc(s.url)}" target="_blank" rel="noopener">source</a>
      <button data-research="${s.id}">Get research brief</button>
    </div>
    ${s.research_brief ? `<div class="snippet">${esc(s.research_brief)}</div>` : `<div class="snippet" id="brief-${s.id}"></div>`}
  </div>`;
}

async function renderSignals() {
  const view = document.getElementById("view-signals");
  const signals = await api("/api/signals?limit=100");
  view.innerHTML = signals.length
    ? signals.map(signalCard).join("")
    : `<div class="empty">No signals yet. Ingest, then classification runs hourly.</div>`;
  view.querySelectorAll("[data-research]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Researching…";
      try {
        const r = await api(`/api/signals/${btn.dataset.research}/research`, { method: "POST" });
        const target = document.getElementById(`brief-${btn.dataset.research}`);
        if (target) target.textContent = r.research_brief || "(no brief returned)";
      } catch (e) {
        toast("Research failed (check PERPLEXITY_API_KEY)");
      }
      btn.textContent = "Get research brief";
      btn.disabled = false;
    })
  );
}

// ----------------------------------------------------------------- watchlists
function sparkline(values) {
  const max = Math.max(1, ...values);
  return `<span class="spark">${values
    .map((v) => `<span style="height:${Math.round((v / max) * 18)}px"></span>`)
    .join("")}</span>`;
}

function tickerCard(t) {
  const cls = t.signal_score > 0 ? "bull" : t.signal_score < 0 ? "bear" : "";
  return `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <strong>${esc(t.symbol)}</strong>
      <span class="badge ${cls}">${t.signal_score > 0 ? "+" : ""}${t.signal_score}</span>
    </div>
    <div class="meta">${esc(t.name || "")}</div>
    <div class="meta">
      ${t.price != null ? `<span>$${t.price}</span>` : "<span>—</span>"}
      ${t.week52_low != null ? `<span>52w ${t.week52_low}–${t.week52_high}</span>` : ""}
    </div>
    <div class="meta">
      <span class="badge bull">▲ ${t.bullish}</span>
      <span class="badge bear">▼ ${t.bearish}</span>
      ${sparkline(t.sparkline || [])}
    </div>
  </div>`;
}

async function renderWatchlists() {
  const view = document.getElementById("view-watchlists");
  const lists = await api("/api/watchlists");
  const names = Object.keys(lists);
  view.innerHTML = names.length
    ? names
        .map(
          (name) =>
            `<div class="section-title">${esc(name)}</div>
             <div class="grid">${lists[name].map(tickerCard).join("")}</div>`
        )
        .join("")
    : `<div class="empty">No watchlists.</div>`;
}

// ------------------------------------------------------------------ industries
async function renderIndustries() {
  const view = document.getElementById("view-industries");
  const inds = await api("/api/industries");
  view.innerHTML =
    `<div class="grid">` +
    inds
      .map(
        (i) => `<div class="card">
        <strong>${esc(i.name)}</strong>
        <div class="meta">${esc(i.group)}</div>
        <div class="meta"><span>${i.source_count}/${i.target} sources</span>
          <span>avg score ${i.avg_score}</span>
          <span>${i.signal_count} signals</span></div>
        <button data-discover="${i.id}">Discover more</button>
      </div>`
      )
      .join("") +
    `</div>`;
  view.querySelectorAll("[data-discover]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Discovering…";
      try {
        const r = await api(`/api/industries/${btn.dataset.discover}/discover`, { method: "POST" });
        toast(`Added ${r.added} source(s) to ${r.industry}`);
        renderIndustries();
      } catch (e) {
        toast("Discovery failed (check PERPLEXITY_API_KEY / XAI_API_KEY)");
        btn.disabled = false;
        btn.textContent = "Discover more";
      }
    })
  );
}

// --------------------------------------------------------------------- sources
async function renderSources() {
  const view = document.getElementById("view-sources");
  const sources = await api("/api/sources");
  const rows = sources
    .map(
      (s) => `<tr>
      <td>${esc(s.name)}</td>
      <td><span class="badge type">${esc(s.type)}</span></td>
      <td>${esc(s.industries.join(", "))}</td>
      <td>${s.vetting_score} ${tierBadge(s.tier)}</td>
      <td>${s.signal_yield}</td>
      <td>${esc(s.status)}</td>
      <td>${esc(s.discovered_by)}</td>
    </tr>`
    )
    .join("");
  view.innerHTML = `
    <div class="inline-form">
      <input id="src-name" placeholder="Name" />
      <select id="src-type">
        ${["rss", "twitter", "youtube", "forum", "sec", "reddit", "hackernews", "arxiv"]
          .map((t) => `<option>${t}</option>`)
          .join("")}
      </select>
      <input id="src-url" placeholder="URL" />
      <input id="src-tags" placeholder="tags" />
      <button id="src-add">Add source</button>
    </div>
    <table>
      <thead><tr><th>Name</th><th>Type</th><th>Industries</th><th>Score</th>
        <th>Yield</th><th>Status</th><th>Found by</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  document.getElementById("src-add").addEventListener("click", async () => {
    const payload = {
      name: document.getElementById("src-name").value,
      type: document.getElementById("src-type").value,
      url: document.getElementById("src-url").value,
      tags: document.getElementById("src-tags").value,
    };
    if (!payload.name) return toast("Name required");
    await api("/api/sources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast("Source added");
    renderSources();
  });
}

// ------------------------------------------------------------------- dispatch
async function renderTab(tab) {
  try {
    if (["feed", "youtube", "twitter", "blogs"].includes(tab)) await renderFeed(tab);
    else if (tab === "signals") await renderSignals();
    else if (tab === "watchlists") await renderWatchlists();
    else if (tab === "trends") await window.renderTrends();
    else if (tab === "industries") await renderIndustries();
    else if (tab === "sources") await renderSources();
  } catch (e) {
    toast(`Failed to load ${tab}`);
    console.error(e);
  }
}

function switchTab(tab) {
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === `view-${tab}`)
  );
  renderTab(tab);
}

document.querySelectorAll("#tabs button").forEach((btn) =>
  btn.addEventListener("click", () => switchTab(btn.dataset.tab))
);

document.getElementById("ingest-btn").addEventListener("click", async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Ingesting…";
  try {
    const counts = await api("/api/ingest", { method: "POST" });
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    toast(`Ingested ${total} new item(s)`);
    renderTab(document.querySelector("#tabs button.active").dataset.tab);
  } catch (err) {
    toast("Ingestion failed");
  }
  e.target.textContent = "Run ingestion";
  e.target.disabled = false;
});

(async function init() {
  try {
    const cfg = await api("/api/config");
    document.getElementById("tier-badge").textContent = `tier: ${cfg.cost_tier}`;
  } catch (e) {}
  switchTab("feed");
})();
