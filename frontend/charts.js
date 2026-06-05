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
let dirChart = null, topicChart = null;

window.renderTrends = async function () {
  const data  = await (await fetch("/api/trends")).json();
  const weeks = Object.keys(data.directions).sort();

  if (dirChart) dirChart.destroy();
  dirChart = new Chart(document.getElementById("chart-directions"), {
    type: "bar",
    data: {
      labels: weeks,
      datasets: [
        { label:"Bullish", data: weeks.map(w => data.directions[w]?.bullish||0), backgroundColor:"#22c55e" },
        { label:"Bearish", data: weeks.map(w => data.directions[w]?.bearish||0), backgroundColor:"#ef4444" },
        { label:"Neutral", data: weeks.map(w => data.directions[w]?.neutral||0), backgroundColor:"#3d4f66" },
      ],
    },
    options: {
      ...chartDefaults,
      plugins: { ...chartDefaults.plugins, title:{ ...chartDefaults.plugins.title, display:true, text:"Signal direction by week" } },
      scales: gridScales(false, true),
    },
  });

  const topics = Object.entries(data.topics)
    .map(([n,m]) => [n, Object.values(m).reduce((a,b)=>a+b,0), m])
    .sort((a,b)=>b[1]-a[1]).slice(0,6);

  if (topicChart) topicChart.destroy();
  topicChart = new Chart(document.getElementById("chart-topics"), {
    type: "line",
    data: {
      labels: weeks,
      datasets: topics.map(([name,,wm],i) => ({
        label: name,
        data: weeks.map(w => wm[w]||0),
        borderColor: COLORS[i], backgroundColor:"transparent",
        tension:.35, pointRadius:3,
      })),
    },
    options: {
      ...chartDefaults,
      plugins: { ...chartDefaults.plugins, title:{ ...chartDefaults.plugins.title, display:true, text:"Topic mentions by week" } },
      scales: gridScales(),
    },
  });

  if (!weeks.length) {
    document.getElementById("view-trends").insertAdjacentHTML("afterbegin",
      '<div class="empty"><div class="empty-icon">📈</div><div class="empty-title">No signal history yet</div><div class="empty-sub">Charts populate as signals accrue over time.</div></div>');
  }
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
