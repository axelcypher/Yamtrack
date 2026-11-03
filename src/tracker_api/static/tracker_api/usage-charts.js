// Charts for the yamtrack-plus usage statistics page.
document.addEventListener("DOMContentLoaded", () => {
  const source = document.getElementById("usage-data");
  if (!source || typeof Chart === "undefined") return;
  const data = JSON.parse(source.textContent);

  Chart.defaults.color = "#9ca3af";
  Chart.defaults.borderColor = "rgba(255, 255, 255, 0.06)";

  const weekLabels = data.weeks.map((week) =>
    new Date(`${week}T00:00:00`).toLocaleDateString(undefined, { day: "2-digit", month: "2-digit" }),
  );

  function empty(canvas, text) {
    const note = document.createElement("p");
    note.className = "text-gray-400 text-sm";
    note.textContent = text;
    canvas.replaceWith(note);
  }

  function stacked(id, datasets) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    if (!datasets.length) return empty(canvas, "No data in this period.");
    new Chart(canvas, {
      type: "bar",
      data: { labels: weekLabels, datasets },
      options: {
        maintainAspectRatio: false,
        scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } },
        plugins: { legend: { position: "bottom" } },
      },
    });
  }

  function horizontal(id, rows, valueKey, color) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    if (!rows.length) return empty(canvas, "No data yet.");
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: rows.map((row) => row.label),
        datasets: [{ data: rows.map((row) => row[valueKey]), backgroundColor: color }],
      },
      options: {
        indexAxis: "y",
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true } },
      },
    });
  }

  stacked("usage-units", data.units);
  stacked("usage-hours", data.hours);

  const split = document.getElementById("usage-split");
  if (split) {
    if (!data.split.data.length) empty(split, "No data in this period.");
    else
      new Chart(split, {
        type: "doughnut",
        data: {
          labels: data.split.labels,
          datasets: [{ data: data.split.data, backgroundColor: data.split.colors, borderWidth: 0 }],
        },
        options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom" } } },
      });
  }

  const platforms = data.platforms.map((row) => ({
    ...row,
    value: row.units || Math.round(row.minutes / 60),
  }));
  horizontal("usage-platforms", platforms, "value", "#6366f1");
  horizontal("usage-genres", data.genres, "units", "#a855f7");
});
