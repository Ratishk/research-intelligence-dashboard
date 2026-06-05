"use strict";

const COLORS = ["#3b82f6","#22c55e","#ef4444","#f59e0b","#a855f7","#06b6d4","#f97316","#94a3b8"];

function gridScales(xStacked, yStacked) {
  const grid  = { color:"#252f42" };
  const ticks = { color:"#677892" };
  return {
    x: { grid, ticks, stacked: !!xStacked },
    y: { grid, ticks, beginAtZero:true, stacked: !!yStacked },
  };
}

const chartDefaults = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: {
    legend: { labels:{ color:"#a8b8cc", boxWidth:12, padding:16 } },
    title:  { color:"#e2eaf4", font:{ size:13, weight:"600" } },
  },
};

// ─── Trends tab ───────────────────────────────────────────────────────────────
let _trendCharts = {};
function _mkChart(id, cfg) {
  if (_trendCharts[id]) _trendCharts[id].destroy();
  const el = document.getElementById(id);
  if (!el) return;
  _trendCharts[id] = new Chart(el, cfg);
}
const _title = t => ({ ...chartDefaults.plugins.title, display:true, text:t });

window.renderTrends = async function () {
  const d = await (await fetch("/api/trends")).json();
  const dt = d.direction_total || {bullish:0,bearish:0,neutral:0};

  // 1) Signal volume over time — a clear area chart (replaces the sparse weekly bars).
  const vs = d.volume_series || [];
  // cumulative line for a sense of accumulation
  let run = 0; const cum = vs.map(p => (run += p.count));
  _mkChart("chart-volume", {
    type: "line",
    data: { labels: vs.map(p => p.date.slice(5)),
      datasets: [
        { label:"Signals/day", data: vs.map(p=>p.count), borderColor:"#5B8DEF",
          backgroundColor:"rgba(91,141,239,.15)", fill:true, tension:.35, pointRadius:2, yAxisID:"y" },
        { label:"Cumulative", data: cum, borderColor:"#94a3b8", backgroundColor:"transparent",
          borderDash:[4,4], tension:.35, pointRadius:0, yAxisID:"y1" },
      ]},
    options: { ...chartDefaults, plugins:{...chartDefaults.plugins, title:_title("Signal volume over time")},
      scales: { x:{grid:{color:"#252f42"},ticks:{color:"#677892",maxTicksLimit:8}},
        y:{position:"left",grid:{color:"#252f42"},ticks:{color:"#677892"},beginAtZero:true},
        y1:{position:"right",grid:{display:false},ticks:{color:"#5e6b7e"},beginAtZero:true} } },
  });

  // 2) Direction split — a doughnut (instantly readable: how bullish are we overall).
  _mkChart("chart-direction", {
    type: "doughnut",
    data: { labels:["Bullish","Bearish","Neutral"],
      datasets:[{ data:[dt.bullish,dt.bearish,dt.neutral],
        backgroundColor:["#22c55e","#ef4444","#3d4f66"], borderWidth:0 }]},
    options: { ...chartDefaults, cutout:"62%",
      plugins:{...chartDefaults.plugins, title:_title(`Signal direction (${d.total} total)`),
        legend:{position:"right", labels:{color:"#a8b8cc",boxWidth:12,padding:12}}} },
  });

  // 3) Top tickers by signal activity — horizontal stacked bar (bull vs bear).
  const tk = d.top_tickers || [];
  _mkChart("chart-tickers", {
    type: "bar",
    data: { labels: tk.map(t=>t.ticker),
      datasets:[
        { label:"Bullish", data: tk.map(t=>t.bullish), backgroundColor:"#22c55e" },
        { label:"Bearish", data: tk.map(t=>t.bearish), backgroundColor:"#ef4444" },
      ]},
    options: { ...chartDefaults, indexAxis:"y",
      plugins:{...chartDefaults.plugins, title:_title("Most-active tickers")},
      scales: gridScales(true, true) },
  });

  // 4) Signal type mix — doughnut.
  const types = Object.entries(d.by_type||{}).sort((a,b)=>b[1]-a[1]);
  _mkChart("chart-types", {
    type: "doughnut",
    data: { labels: types.map(([k])=>k.replace(/_/g," ")),
      datasets:[{ data: types.map(([,v])=>v), backgroundColor: COLORS, borderWidth:0 }]},
    options: { ...chartDefaults, cutout:"62%",
      plugins:{...chartDefaults.plugins, title:_title("Signal types"),
        legend:{position:"right", labels:{color:"#a8b8cc",boxWidth:12,padding:10,font:{size:11}}}} },
  });

  // 5) Topic mentions over time — line (kept, full width).
  const weeks = [...new Set(Object.values(d.topics||{}).flatMap(m=>Object.keys(m)))].sort();
  const topics = Object.entries(d.topics||{})
    .map(([n,m]) => [n, Object.values(m).reduce((a,b)=>a+b,0), m])
    .sort((a,b)=>b[1]-a[1]).slice(0,6);
  _mkChart("chart-topics", {
    type: "line",
    data: { labels: weeks,
      datasets: topics.map(([name,,wm],i) => ({ label:name, data: weeks.map(w=>wm[w]||0),
        borderColor: COLORS[i], backgroundColor:"transparent", tension:.35, pointRadius:3 })) },
    options: { ...chartDefaults, plugins:{...chartDefaults.plugins, title:_title("Topic mentions over time")},
      scales: gridScales() },
  });
};

// ─── Theme Shifts charts ──────────────────────────────────────────────────────
let themeTimelineChart = null, themeBarChart = null;

window.renderThemeCharts = function (data) {
  const timeline = data.timeline || {};
  const allWeeks = [...new Set(Object.values(timeline).flatMap(w => Object.keys(w)))].sort();
  const themes   = Object.keys(timeline).slice(0, 8);

  if (themeTimelineChart) themeTimelineChart.destroy();
  const tlCtx = document.getElementById("chart-theme-timeline");
  if (tlCtx && allWeeks.length) {
    themeTimelineChart = new Chart(tlCtx, {
      type: "line",
      data: {
        labels: allWeeks,
        datasets: themes.map((t,i) => ({
          label: t,
          data: allWeeks.map(w => (timeline[t]||{})[w]||0),
          borderColor: COLORS[i], backgroundColor:"transparent",
          tension:.35, pointRadius:3,
        })),
      },
      options: {
        ...chartDefaults,
        plugins: { ...chartDefaults.plugins, title:{ ...chartDefaults.plugins.title, display:true, text:"Top theme mentions over time" } },
        scales: gridScales(),
      },
    });
  } else if (tlCtx) {
    const ctx = tlCtx.getContext("2d");
    ctx.clearRect(0, 0, tlCtx.width, tlCtx.height);
  }

  if (themeBarChart) themeBarChart.destroy();
  const barCtx = document.getElementById("chart-theme-bar");
  const top20  = (data.all||[]).slice(0,20);
  if (barCtx && top20.length) {
    themeBarChart = new Chart(barCtx, {
      type: "bar",
      data: {
        labels: top20.map(t=>t.theme),
        datasets: [
          { label:"Bullish", data:top20.map(t=>t.bullish), backgroundColor:"#22c55e" },
          { label:"Bearish", data:top20.map(t=>t.bearish), backgroundColor:"#ef4444" },
          { label:"Neutral", data:top20.map(t=>t.neutral), backgroundColor:"#3d4f66" },
        ],
      },
      options: {
        ...chartDefaults,
        indexAxis: "y",
        plugins: { ...chartDefaults.plugins, title:{ ...chartDefaults.plugins.title, display:true, text:"Top themes — sentiment breakdown" } },
        scales: gridScales(true, false),
      },
    });
  }
};
