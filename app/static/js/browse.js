// View-mode toggle (grid <-> table).
// State precedence: saved localStorage choice > server-rendered initial state.
// The toggle derives the *current* mode from the DOM so it works regardless of
// how the page was initially rendered (e.g. via ?view=table).
(function () {
  const KEY = "spotwebnext.viewMode";

  function els() {
    return {
      grid: document.getElementById("grid-view"),
      table: document.getElementById("table-view"),
      iconGrid: document.getElementById("icon-grid"),
      iconTable: document.getElementById("icon-table"),
    };
  }

  function apply(mode) {
    const { grid, table, iconGrid, iconTable } = els();
    if (!grid || !table) return;
    const isTable = mode === "table";
    grid.classList.toggle("hidden", isTable);
    table.classList.toggle("hidden", !isTable);
    // Show the icon for the mode you can switch TO.
    iconGrid.classList.toggle("hidden", !isTable);
    iconTable.classList.toggle("hidden", isTable);
  }

  function currentMode() {
    const { table } = els();
    return table && !table.classList.contains("hidden") ? "table" : "grid";
  }

  window.toggleViewMode = function () {
    const next = currentMode() === "grid" ? "table" : "grid";
    localStorage.setItem(KEY, next);
    apply(next);
  };

  document.addEventListener("DOMContentLoaded", function () {
    // Only override the server-rendered state if the user has a saved preference.
    const saved = localStorage.getItem(KEY);
    if (saved) apply(saved);
  });
})();

// Manual spot retrieval from the toolbar.
window.refreshSpots = async function (full) {
  if (full && !confirm("Fetch the provider's entire spot history in the background? This can take hours and grow the database a lot.")) return;
  const btn = document.getElementById(full ? "btn-full" : "btn-refresh");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⟳ Fetching…";
  try {
    const resp = await fetch("/retrieve", {
      method: "POST",
      body: full ? new URLSearchParams({ full: "1" }) : undefined,
    });
    const r = await resp.json();
    if (r.ok && r.started) {
      btn.textContent = "✓ Started — see the log";
    } else if (r.ok) {
      btn.textContent = "✓ +" + r.added + " new";
      setTimeout(() => location.reload(), 900);
    } else {
      btn.textContent = original;
      btn.disabled = false;
      alert(r.message);
    }
  } catch (err) {
    btn.textContent = original;
    btn.disabled = false;
    alert("Refresh failed: " + err);
  }
};
