// Keyboard shortcuts and service-worker registration.
//   /  focus search      g  grid/table      j / k  next / previous spot      ?  help
(function () {
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  const typing = (el) =>
    el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
  const items = () => Array.from(document.querySelectorAll("#grid-view:not(.hidden) .poster-card, #table-view:not(.hidden) tbody tr"));

  function move(step) {
    const list = items();
    if (!list.length) return;
    let i = list.indexOf(document.activeElement);
    i = i < 0 ? (step > 0 ? 0 : list.length - 1) : Math.min(Math.max(i + step, 0), list.length - 1);
    list[i].tabIndex = 0;
    list[i].focus();
    list[i].scrollIntoView({ block: "nearest" });
  }

  function help() {
    let box = document.getElementById("shortcutHelp");
    if (box) { box.remove(); return; }
    box = document.createElement("div");
    box.id = "shortcutHelp";
    box.className = "card";
    box.setAttribute("role", "dialog");
    box.style.cssText = "position:fixed;right:1rem;bottom:1rem;z-index:100;max-width:18rem";
    box.innerHTML = "<h2>Shortcuts</h2><p><kbd>/</kbd> search · <kbd>g</kbd> grid / table<br><kbd>j</kbd> / <kbd>k</kbd> next / previous spot<br><kbd>Enter</kbd> open · <kbd>?</kbd> this help · <kbd>Esc</kbd> close</p>";
    document.body.appendChild(box);
  }

  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape") {
      const box = document.getElementById("shortcutHelp");
      if (box) box.remove();
      if (typing(document.activeElement)) document.activeElement.blur();
      return;
    }
    if (typing(document.activeElement)) return;
    if (e.key === "/") {
      const search = document.querySelector(".search-input");
      if (search) { e.preventDefault(); search.focus(); search.select(); }
    } else if (e.key === "g" && window.toggleViewMode) {
      window.toggleViewMode();
    } else if (e.key === "j") {
      move(1);
    } else if (e.key === "k") {
      move(-1);
    } else if (e.key === "?") {
      help();
    } else if (e.key === "Enter" && document.activeElement && document.activeElement.tagName === "TR") {
      document.activeElement.click();
    }
  });
})();
