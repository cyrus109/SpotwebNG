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
