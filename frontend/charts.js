"use strict";

// Trend charts (Chart.js). Exposed as window.renderTrends for app.js dispatch.
let directionsChart = null;
let topicsChart = null;

const CHART_COLORS = ["#4f9dff", "#3fb950", "#f85149", "#d29922", "#a371f7", "#39c5cf"];

window.renderTrends = async function renderTrends() {
  const res = await fetch("/api/trends");
  const data = await res.json();

  const weeks = Object.keys(data.directions).sort();
  const dirData = (dir) => weeks.map((w) => data.directions[w]?.[dir] || 0);

  if (directionsChart) directionsChart.destroy();
  directionsChart = new Chart(document.getElementById("chart-directions"), {
    type: "bar",
    data: {
      labels: weeks,
      datasets: [
        { label: "bullish", data: dirData("bullish"), backgroundColor: "#3fb950" },
        { label: "bearish", data: dirData("bearish"), backgroundColor: "#f85149" },
        { label: "neutral", data: dirData("neutral"), backgroundColor: "#8b97a7" },
      ],
    },
    options: {
      plugins: { title: { display: true, text: "Signal direction by week", color: "#e6edf3" }, legend: { labels: { color: "#e6edf3" } } },
      scales: gridScales(),
      responsive: true,
    },
  });

  // Top topics by total mentions.
  const topics = Object.entries(data.topics)
    .map(([name, weekMap]) => [name, Object.values(weekMap).reduce((a, b) => a + b, 0), weekMap])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);

  if (topicsChart) topicsChart.destroy();
  topicsChart = new Chart(document.getElementById("chart-topics"), {
    type: "line",
    data: {
      labels: weeks,
      datasets: topics.map(([name, , weekMap], i) => ({
        label: name,
        data: weeks.map((w) => weekMap[w] || 0),
        borderColor: CHART_COLORS[i % CHART_COLORS.length],
        backgroundColor: "transparent",
        tension: 0.3,
      })),
    },
    options: {
      plugins: { title: { display: true, text: "Topic mentions by week", color: "#e6edf3" }, legend: { labels: { color: "#e6edf3" } } },
      scales: gridScales(),
      responsive: true,
    },
  });

  if (!weeks.length) {
    document.getElementById("view-trends").insertAdjacentHTML(
      "afterbegin",
      '<div class="empty">No signal history yet — charts populate as signals accrue.</div>'
    );
  }
};

function gridScales() {
  const grid = { color: "#2a3343" };
  const ticks = { color: "#8b97a7" };
  return { x: { grid, ticks }, y: { grid, ticks, beginAtZero: true } };
}
